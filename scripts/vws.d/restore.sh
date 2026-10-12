#!/usr/bin/env bash
# summary: restore a vws backup directory: CR, apply, ConfigMaps, then doctor (plan only without --yes)
# Sourced by scripts/vws. Usage: vws restore DIR [--yes]
# #43 section 6. SHA256SUMS is checked first; a mismatch exits 1 before anything is written.
# Without --yes: the plan (what is created or overwritten) and the limits, exit 2.
# With --yes: the VWSCluster CRD from this repo, the CR spec from the backup, vws apply (helm values
# come from the CR again and are compared with the backup's), the ConfigMaps server-side, then
# vws doctor. Parts that already match are unchanged; a second restore of the same backup changes
# nothing. State volumes are not restored from VolumeSnapshots: the names are printed with a manual
# procedure (replacing a PVC risks data loss; out of the MVP).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 sha256sum kubectl helm)
# shellcheck source=scripts/vws.d/lib/applyrun.sh
source "${VWS_ROOT}/scripts/vws.d/lib/applyrun.sh"

vws_restore_limits() {
  vws_emit WARN limits "the restore returns to the backup time; Ceph volumes of VMs created after the backup become orphans (docs/etcd-runbook.md)"
  vws_emit WARN limits "VM disks are not in a vws backup; state volumes are restored by hand from their VolumeSnapshots"
}

# vws_restore_changed_configmaps DIR: names of backup ConfigMaps whose data differs from the cluster.
vws_restore_changed_configmaps() {
  local have
  have=$(vws_kubectl get configmaps -l app.kubernetes.io/part-of=vws -o json 2>/dev/null || echo '{"items":[]}')
  jq -r --argjson have "$have" '($have.items | map({key: .metadata.name, value: {data: (.data // {}), binaryData: (.binaryData // {})}}) | from_entries) as $h
    | .items[] | select($h[.metadata.name] != {data: .data, binaryData: .binaryData}) | .metadata.name' "$1/configmaps.json"
}

vws_cmd_main() {
  local dir='' cr current spec crd changed=() name snaps level message json rc=0 cr_changed=0
  while (($#)); do
    case $1 in
      -*) vws_usage "usage: vws restore DIR [--yes]" ;;
      *) [[ -z $dir ]] || vws_usage "usage: vws restore DIR [--yes]"; dir=$1; shift ;;
    esac
  done
  [[ -n $dir ]] || vws_usage "usage: vws restore DIR [--yes]"
  [[ -d $dir && -f $dir/SHA256SUMS && -f $dir/manifest.json ]] || vws_usage "${dir} is not a vws backup (no SHA256SUMS or manifest.json)"
  VWS_NAMESPACE=$(jq -r '.namespace // "vws"' "$dir/manifest.json")
  export VWS_NAMESPACE

  # 1. Integrity before anything else.
  if ! (cd "$dir" && sha256sum --quiet -c SHA256SUMS) >/dev/null 2>&1; then
    vws_emit FAIL checksums "SHA256SUMS does not match: $(cd "$dir" && sha256sum -c SHA256SUMS 2>/dev/null | grep -v ': OK$' | cut -d: -f1 | paste -sd, -); nothing was written"
    return 0
  fi
  vws_emit OK checksums "$(wc -l <"$dir/SHA256SUMS") files match SHA256SUMS"

  vws_kubectl version -o json >/dev/null 2>&1 || vws_die_env "the Kubernetes API does not answer"
  spec=$(jq -cS '.spec' "$dir/vwscluster.json")
  cr=$(cat "$dir/vwscluster.json")
  crd=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["metadata"]["name"])' "${VWS_ROOT}/charts/vws/crds/vwsclusters.yaml")
  current=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null || true)
  [[ -n $current && $(jq -cS '.spec' <<<"$current") == "$spec" ]] || cr_changed=1
  mapfile -t changed < <(vws_restore_changed_configmaps "$dir")
  snaps=$(jq -r '.volumeSnapshots | join(", ")' "$dir/manifest.json")

  # 2. The plan.
  if vws_kubectl get crd "$crd" -o json >/dev/null 2>&1; then vws_emit OK plan "CRD ${crd}: present (server-side apply from charts/vws/crds, unchanged if equal)"
  else vws_emit OK plan "CRD ${crd}: create from charts/vws/crds"; fi
  if ((cr_changed)); then vws_emit OK plan "VWSCluster vws: $([[ -n $current ]] && echo overwrite spec || echo create) -> ${spec}"
  else vws_emit OK plan "VWSCluster vws: unchanged"; fi
  vws_emit OK plan "vws apply (unchanged when the release already has these values)"
  if ((${#changed[@]})); then vws_emit OK plan "ConfigMaps to write: ${changed[*]}"; else vws_emit OK plan "ConfigMaps: unchanged"; fi
  vws_emit OK plan "then vws doctor"
  [[ -z $snaps ]] || vws_emit WARN volumes "restore by hand from VolumeSnapshots: ${snaps} (create a PVC with dataSource from the snapshot, then point the StatefulSet at it; not automated)"
  vws_restore_limits
  if [[ -n $VWS_DRY_RUN ]]; then vws_emit OK restore "dry-run: nothing done"; return 0; fi
  vws_confirm "vws restore"

  # 3. Apply.
  vws_apply_crd
  if ((cr_changed)); then
    vws_save_spec "$cr" "$spec" || vws_die_env "cannot save VWSCluster vws"
    vws_emit OK restore "VWSCluster vws saved from the backup"
  fi
  vws_run_apply || rc=$?
  # shellcheck disable=SC2154 # vws_fail_count is set by lib/common.sh
  if ((rc != 0 || vws_fail_count > 0)); then
    vws_emit FAIL restore "vws apply failed; ConfigMaps were not written; next: vws doctor, fix, and restore again"
    return 0
  fi
  if json=$(vws_helm get values vws -a -o json 2>/dev/null); then
    if [[ $(jq -cS . <<<"$json") == "$(jq -cS . "$dir/helm-values.json")" ]]; then vws_emit OK helm-values "release values match the backup"
    else vws_emit WARN helm-values "release values differ from the backup (chart or render changed since); the CR is the source"; fi
  fi
  for name in "${changed[@]}"; do
    jq --arg n "$name" '.items[] | select(.metadata.name == $n)' "$dir/configmaps.json" |
      vws_kubectl apply --server-side --field-manager=vws -f - >/dev/null || vws_die_env "cannot write ConfigMap ${name}"
  done
  if ((${#changed[@]})); then vws_emit OK configmaps "wrote ${#changed[@]}: ${changed[*]}"; else vws_emit OK configmaps "unchanged"; fi
  ((cr_changed || ${#changed[@]})) || vws_emit OK restore "unchanged: the cluster already matches the backup"

  # 4. doctor, folded in as after-<check>.
  rc=0
  json=$("${VWS_ROOT}/scripts/vws" -o json doctor 2>/dev/null) || rc=$?
  if ((rc == 2 || rc == 3)) || ! jq -e '.checks' <<<"$json" >/dev/null 2>&1; then
    vws_emit WARN after-restore "vws doctor could not judge the result (exit ${rc}); run it by hand"
    return 0
  fi
  while IFS=$'\t' read -r level name message; do
    [[ -n $level ]] && vws_emit "$level" "after-${name}" "$message"
  done < <(jq -r '.checks[] | [.level, .name, .message] | @tsv' <<<"$json")
}
