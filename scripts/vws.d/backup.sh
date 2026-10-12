#!/usr/bin/env bash
# summary: write a backup directory (CR, helm values/history/manifest, VWS ConfigMaps, manifest, SHA256SUMS); no Secrets
# Sourced by scripts/vws. Usage: vws backup [--out DIR] [--etcd-snapshot FILE]
# #43 section 6. Reads the cluster only. The directory is 0700, every file 0600, never overwritten.
# Contents: the VWSCluster (spec, no status), helm get values -a / history / manifest, the release
# namespace ConfigMaps labelled app.kubernetes.io/part-of=vws (VM metadata: customer data), and in
# manifest.json the names of existing VolumeSnapshots, CephCluster health, and the etcd snapshot
# path with its existence and age (24 hours). Not included: Secrets, Ceph/VM disks, etcd itself.
# The CR and helm files must pass the lib/redact.py scan with zero hits, or nothing is written (1);
# when the scanner cannot run, nothing is written (3). Helm keeps its release in Secrets: run this
# with credentials that can read them.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 sha256sum kubectl helm)

VWS_BACKUP_SELECTOR=app.kubernetes.io/part-of=vws
vws_backup_tmp=''

vws_backup_limits() {
  vws_emit WARN limits "copying the backup off this node is up to you; vws cannot see where it goes"
  vws_emit WARN limits "VM disks (Ceph RBD) are not backed up; VolumeSnapshots are inside the same Ceph cluster and are not a backup against node or cluster loss"
}

vws_cmd_main() {
  local out='' etcd='' release dir err cr json age sums now fresh=false exists=false hits name
  while (($#)); do
    case $1 in
      --out) (($# >= 2)) || vws_usage "--out needs a directory"; out=$2; shift 2 ;;
      --etcd-snapshot) (($# >= 2)) || vws_usage "--etcd-snapshot needs a file"; etcd=$2; shift 2 ;;
      *) vws_usage "usage: vws backup [--out DIR] [--etcd-snapshot FILE]" ;;
    esac
  done
  out=${out:-vws-backup-$(date -u +%Y%m%dT%H%M%SZ)}
  [[ ! -e $out ]] || vws_usage "${out} exists; backup never overwrites"
  release=${VWS_NAMESPACE:-vws}
  umask 077
  vws_backup_tmp=$(mktemp -d)
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "${vws_backup_tmp:-}"; rm -f "$vws_results"' EXIT
  dir="${vws_backup_tmp}/b"
  mkdir -p "$dir"
  err="${vws_backup_tmp}/err"

  vws_kubectl version -o json >/dev/null 2>&1 || vws_die_env "the Kubernetes API does not answer"
  cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) || vws_usage "no VWSCluster vws in ${release}; nothing to back up"
  jq '{apiVersion, kind, metadata: {name: .metadata.name, namespace: .metadata.namespace}, spec}' <<<"$cr" >"$dir/vwscluster.json"

  for name in values history manifest; do
    local args=(get values vws -a -o json) file="helm-values.json"
    case $name in
      history) args=(history vws -o json) file="helm-history.json" ;;
      manifest) args=(get manifest vws) file="helm-manifest.yaml" ;;
    esac
    if ! vws_helm "${args[@]}" >"$dir/$file" 2>"$err"; then
      if grep -qiE 'forbidden|secrets' "$err"; then
        vws_emit FAIL helm "cannot read the release with these credentials (the release is kept in Secrets, which the operating role cannot read); nothing was written; next: run vws backup with your own kubeconfig"
      else
        vws_emit FAIL helm "helm ${args[*]:0:2} failed: $(head -1 "$err"); nothing was written"
      fi
      return 0
    fi
  done

  # Scan before anything is kept: the chart contract is no secret in values (#43 sections 6, 7).
  command -v python3 >/dev/null 2>&1 || vws_die_env "the secret scanner (python3) is not on PATH; nothing was written"
  mkdir -p "${vws_backup_tmp}/scan"
  cp "$dir"/* "${vws_backup_tmp}/scan/"
  python3 "${VWS_ROOT}/scripts/vws.d/lib/redact.py" "${vws_backup_tmp}/scan" "${vws_backup_tmp}/report" ||
    vws_die_env "the secret scanner failed; nothing was written"
  hits=$(awk '$1 == "total" {print $2}' "${vws_backup_tmp}/report")
  if ((hits > 0)); then
    vws_emit FAIL secret-scan "${hits} secret-shaped lines in the CR or helm files ($(awk '$1 != "total" && $2 > 0 {printf "%s %s ", $1, $2}' "${vws_backup_tmp}/report")); nothing was written; next: vws diag shows them masked, remove the secret from values"
    return 0
  fi
  vws_emit OK secret-scan "CR and helm files: no secret-shaped content"

  # ConfigMaps labelled part-of=vws (decision 6). VM metadata: customer data, not scanned.
  json=$(vws_kubectl get configmaps -n "$release" -l "$VWS_BACKUP_SELECTOR" -o json 2>/dev/null) || vws_die_env "cannot list ConfigMaps in ${release}"
  jq '{items: [.items[] | {apiVersion: "v1", kind: "ConfigMap", metadata: {name: .metadata.name, namespace: .metadata.namespace, labels: .metadata.labels}, data: (.data // {}), binaryData: (.binaryData // {})}]}' <<<"$json" >"$dir/configmaps.json"
  if [[ $(jq '.items | length' "$dir/configmaps.json") == 0 ]]; then
    vws_emit WARN configmaps "no ConfigMap in ${release} has ${VWS_BACKUP_SELECTOR}; compute metadata is not in this backup (#43 decision 6)"
  else
    vws_emit OK configmaps "$(jq '.items | length' "$dir/configmaps.json") ConfigMaps"
  fi

  if [[ -n $etcd ]]; then
    if [[ -f $etcd ]]; then
      exists=true
      age=$(($(date +%s) - $(stat -c %Y "$etcd")))
      if ((age <= 86400)); then fresh=true; vws_emit OK etcd-snapshot "${etcd} is $((age / 3600))h old"
      else vws_emit WARN etcd-snapshot "${etcd} is $((age / 3600))h old (over 24h); take a new one (docs/etcd-runbook.md)"; fi
    else
      vws_emit WARN etcd-snapshot "${etcd} does not exist; take one (docs/etcd-runbook.md)"
    fi
  else
    vws_emit WARN etcd-snapshot "no --etcd-snapshot given; vws does not take etcd snapshots (docs/etcd-runbook.md)"
  fi
  vws_backup_limits

  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local snaps ceph
  snaps=$(vws_kubectl get volumesnapshots -n "$release" -o json 2>/dev/null || echo '{"items":[]}')
  ceph=$(vws_kubectl get cephcluster -A -o json 2>/dev/null || echo '{"items":[]}')
  (cd "$dir" && for f in *; do printf '%s\t%s\t%s\n' "$f" "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d' ' -f1)"; done) >"${vws_backup_tmp}/files.tsv"
  jq -n --arg at "$now" --arg ns "$release" --arg etcd "$etcd" --argjson exists "$exists" --argjson fresh "$fresh" \
    --arg helm "$(vws_helm version --short 2>/dev/null || echo unknown)" \
    --argjson cr "$(cat "$dir/vwscluster.json")" --argjson snaps "$snaps" --argjson ceph "$ceph" \
    --rawfile files "${vws_backup_tmp}/files.tsv" --rawfile versions "${VWS_ROOT}/scripts/versions.env" '{
      createdAt: $at, namespace: $ns, profile: $cr.spec.profile, helm: $helm,
      versions: ($versions | split("\n") | map(select(test("^[A-Z0-9_]+_VERSION=")) | split("=") | {key: .[0], value: .[1]}) | from_entries),
      files: ($files | split("\n") | map(select(length > 0) | split("\t") | {name: .[0], size: (.[1] | tonumber), sha256: .[2]})),
      volumeSnapshots: [$snaps.items[] | .metadata.name],
      ceph: [$ceph.items[] | {name: .metadata.name, health: (.status.ceph.health // "unknown")}],
      etcdSnapshot: (if $etcd == "" then null else {path: $etcd, exists: $exists, within24h: $fresh} end),
      notIncluded: ["Secrets", "Ceph RBD / VM disks", "etcd (only the snapshot path is recorded)"],
      limits: ["copying the backup off the node is done by a person", "VM disks are not backed up"]
    }' >"$dir/manifest.json"
  (cd "$dir" && sha256sum -- * >SHA256SUMS)
  chmod 0600 "$dir"/*
  chmod 0700 "$dir"
  mv "$dir" "$out" || vws_die_env "cannot create ${out}"
  sums=$(wc -l <"$out/SHA256SUMS")
  vws_emit OK backup "wrote ${out} (0700, files 0600, ${sums} checksums)"
}
