#!/usr/bin/env bash
# Checks of vws status and vws doctor (vws-deploy#56, #43 section 4). Source after common.sh and checks.sh.
# Read only: every call is a vws_kubectl/vws_helm read verb.
#
# Credentials: after vws installer finish the operating role (vws-operator) cannot read Secrets.
# Helm keeps its releases in Secrets, so helm status/get fail with those credentials. The checks
# therefore read the VWSCluster status and the workloads first; the release is extra detail that
# needs the user's own kubeconfig, and its absence is a WARN with that next step, never a FAIL.
# Every FAIL message ends with "next: <one action>".

VWS_PART_OF_SELECTOR=app.kubernetes.io/part-of=vws
vws_status_cr=''
vws_status_values=''
vws_status_tmp=''

vws_status_cleanup() { [[ -z $vws_status_tmp ]] || rm -rf "$vws_status_tmp"; }

# vws_status_load: read the CR and render it once. Sets vws_status_cr (json) and vws_status_values (file).
vws_status_load() {
  VWS_NAMESPACE=${VWS_NAMESPACE:-vws}
  vws_status_tmp=$(mktemp -d)
  if ! vws_status_cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null); then
    vws_status_cr=''
    return 0
  fi
  jq '{apiVersion, kind, metadata: {name: .metadata.name}, spec}' <<<"$vws_status_cr" >"${vws_status_tmp}/cr.yaml"
  if python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "${vws_status_tmp}/cr.yaml" "${vws_status_tmp}/values.yaml" 2>"${vws_status_tmp}/render.err"; then
    vws_status_values="${vws_status_tmp}/values.yaml"
  fi
}

# The CR exists, its generation is applied.
vws_check_cluster_cr() {
  local generation observed profile
  if [[ -z $vws_status_cr ]]; then
    vws_emit FAIL cluster-cr "no VWSCluster vws in ${VWS_NAMESPACE} (or no right to read it); next: vws init"
    return 0
  fi
  profile=$(jq -r '.spec.profile // "unknown"' <<<"$vws_status_cr")
  generation=$(jq -r '.metadata.generation // 0' <<<"$vws_status_cr")
  observed=$(jq -r '.status.observedGeneration // "none"' <<<"$vws_status_cr")
  if [[ $observed == "$generation" ]]; then
    vws_emit OK cluster-cr "profile ${profile}, generation ${generation} applied"
  else
    vws_emit WARN cluster-cr "profile ${profile}, generation ${generation} not applied (applied ${observed}); run vws apply"
  fi
}

# The values the CR renders to now are the values the last apply wrote (status.appliedValuesHash).
vws_check_applied_hash() {
  local applied hash
  [[ -n $vws_status_cr ]] || return 0
  if [[ -z $vws_status_values ]]; then
    vws_emit FAIL applied-hash "the VWSCluster does not render: $(head -1 "${vws_status_tmp}/render.err"); next: fix the CR (vws init)"
    return 0
  fi
  hash=$(vws_applied_hash "$vws_status_values")
  applied=$(jq -r '.status.appliedValuesHash // ""' <<<"$vws_status_cr")
  if [[ -z $applied ]]; then
    vws_emit WARN applied-hash "never applied (no status.appliedValuesHash); run vws apply"
  elif [[ $applied == "$hash" ]]; then
    vws_emit OK applied-hash "values ${hash:0:12} applied"
  else
    vws_emit WARN applied-hash "drift: the CR renders ${hash:0:12}, the last apply wrote ${applied:0:12}; run vws apply"
  fi
}

# vws_status_release_unreadable ERR: helm failed because the credentials cannot read Secrets.
vws_status_release_unreadable() {
  grep -qiE 'forbidden|cannot (list|get) resource "secrets"' <<<"$1"
}

# Release revision and status, and whether the release values match the CR. Needs Secret reads.
vws_check_release_detail() {
  local mode=$1 json err status revision
  err="${vws_status_tmp}/helm.err"
  if ! json=$(vws_helm status vws -o json 2>"$err"); then
    if vws_status_release_unreadable "$(cat "$err")"; then
      vws_emit WARN release "not readable with these credentials: helm keeps the release in Secrets and the operating role cannot read Secrets; revision and release drift skipped (the workload checks below do not need them). For them run vws status with your own kubeconfig"
    elif grep -qi 'not found' "$err"; then
      vws_emit "$(vws_missing WARN "$mode")" release "release vws is not installed; next: vws apply"
    else
      vws_emit WARN release "helm status failed: $(head -1 "$err")"
    fi
    return 0
  fi
  status=$(jq -r '.info.status // "unknown"' <<<"$json")
  revision=$(jq -r '.version // "?"' <<<"$json")
  case $status in
    deployed) vws_emit OK release "revision ${revision} deployed" ;;
    pending-* | failed) vws_emit FAIL release "revision ${revision} is ${status}; next: helm history vws, then vws apply" ;;
    *) vws_emit WARN release "revision ${revision} is ${status}" ;;
  esac
  [[ -n $vws_status_values ]] || return 0
  if ! vws_helm get values vws -o json >"${vws_status_tmp}/release.json" 2>"$err"; then
    vws_emit WARN release-drift "helm get values failed: $(head -1 "$err")"
    return 0
  fi
  if python3 - "$vws_status_values" "${vws_status_tmp}/release.json" <<'PY'; then
import json, sys, yaml
sys.exit(0 if (yaml.safe_load(open(sys.argv[1])) or {}) == (json.load(open(sys.argv[2])) or {}) else 1)
PY
    vws_emit OK release-drift "release values match the CR"
  else
    vws_emit WARN release-drift "the release values differ from what the CR renders (helm run by hand?); run vws apply"
  fi
}

# Bundle operators report the version they run; compare with scripts/versions.env.
vws_check_bundle_versions() {
  local resource key field want have json
  for resource in kubevirt:KUBEVIRT_VERSION:observedKubeVirtVersion cdi:CDI_VERSION:observedVersion; do
    IFS=: read -r resource key field <<<"$resource"
    want=$(vws_versions_env "$key")
    if ! json=$(vws_kubectl get "$resource" -A -o json 2>/dev/null); then
      vws_emit WARN "bundle-${resource}" "cannot read ${resource} with these credentials; version not compared"
      continue
    fi
    have=$(jq -r --arg f "$field" '[.items[] | .status[$f] // "unknown"] | join(",")' <<<"$json")
    if [[ -z $have ]]; then
      vws_emit WARN "bundle-${resource}" "no ${resource} CR; want ${want}"
    elif [[ $have == "$want" ]]; then
      vws_emit OK "bundle-${resource}" "$have"
    else
      vws_emit WARN "bundle-${resource}" "drift: running ${have}, scripts/versions.env ${key}=${want}"
    fi
  done
}

# Every service of charts/vws/values.yaml has a workload with all replicas ready.
vws_check_services() {
  local json names name ready desired kind
  json=$(vws_kubectl get deployments,statefulsets -l "$VWS_PART_OF_SELECTOR" -o json 2>/dev/null) ||
    vws_die_env "cannot list deployments and statefulsets in ${VWS_NAMESPACE}"
  printf '%s\n' "$json" >"${vws_status_tmp}/workloads.json"
  names=$(python3 -c 'import sys,yaml; print("\n".join(sorted(yaml.safe_load(open(sys.argv[1]))["services"])))' "${VWS_ROOT}/charts/vws/values.yaml")
  while IFS= read -r name; do
    read -r kind ready desired < <(jq -r --arg n "$name" '[.items[] | select(.metadata.name == $n)] | first
      | if . == null then "none 0 0" else "\(.kind) \(.status.readyReplicas // 0) \(.spec.replicas // 1)" end' <<<"$json")
    if [[ $kind == none ]]; then
      vws_emit FAIL "service-${name}" "no workload; next: vws apply"
    elif ((ready >= desired)); then
      vws_emit OK "service-${name}" "${kind} ${ready}/${desired} ready"
    elif ((ready == 0)); then
      vws_emit FAIL "service-${name}" "${kind} 0/${desired} ready; next: vws doctor (restarts, PVCs), or kubectl -n ${VWS_NAMESPACE} describe ${kind,,} ${name}"
    else
      vws_emit WARN "service-${name}" "${kind} ${ready}/${desired} ready"
    fi
  done <<<"$names"
}

# A PDB with fewer healthy pods than it wants blocks node drains.
vws_check_pdbs() {
  local json short total
  json=$(vws_kubectl get pdb -l "$VWS_PART_OF_SELECTOR" -o json 2>/dev/null) || { vws_emit WARN pdb "cannot list PodDisruptionBudgets"; return 0; }
  total=$(jq '.items | length' <<<"$json")
  short=$(jq -r '[.items[] | select((.status.currentHealthy // 0) < (.status.desiredHealthy // 0))
    | "\(.metadata.name) \(.status.currentHealthy // 0)/\(.status.desiredHealthy // 0)"] | join(", ")' <<<"$json")
  if [[ -n $short ]]; then
    vws_emit WARN pdb "below desired healthy (drains will block): ${short}"
  else
    vws_emit OK pdb "${total} PDBs healthy"
  fi
}

# --- doctor only ---

vws_check_restarts() {
  local json bad
  json=$(vws_kubectl get pods -l "$VWS_PART_OF_SELECTOR" -o json 2>/dev/null) || vws_die_env "cannot list pods in ${VWS_NAMESPACE}"
  bad=$(jq -r '[.items[] | . as $p | .status.containerStatuses[]?
    | select(.state.waiting.reason == "CrashLoopBackOff" or (.restartCount // 0) >= 5)
    | "\($p.metadata.name) (\(.restartCount // 0) restarts\(if .state.waiting.reason then ", " + .state.waiting.reason else "" end))"] | unique | join(", ")' <<<"$json")
  if [[ -n $bad ]]; then
    vws_emit FAIL restarts "${bad}; next: kubectl -n ${VWS_NAMESPACE} logs <pod> --previous"
  else
    vws_emit OK restarts "no restart loops"
  fi
}

vws_check_pending_pvcs() {
  local json bad
  json=$(vws_kubectl get pvc -A -o json 2>/dev/null) || { vws_emit WARN pending-pvc "cannot list PVCs in all namespaces with these credentials"; return 0; }
  bad=$(jq -r '[.items[] | select(.status.phase == "Pending") | "\(.metadata.namespace)/\(.metadata.name)"] | join(", ")' <<<"$json")
  if [[ -n $bad ]]; then
    vws_emit FAIL pending-pvc "Pending: ${bad}; next: kubectl describe pvc (StorageClass, Ceph health)"
  else
    vws_emit OK pending-pvc "no Pending PVC"
  fi
}

# The VWSCluster CRD itself is Established (the bundle CRDs are vws_check_crds).
vws_check_vws_crd() {
  local name json
  name=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["metadata"]["name"])' "${VWS_ROOT}/charts/vws/crds/vwsclusters.yaml")
  json=$(vws_kubectl get crd -o json 2>/dev/null) || vws_die_env "cannot list CRDs"
  json=$(jq --arg n "$name" '[.items[] | select(.metadata.name == $n)] | first // empty' <<<"$json")
  if [[ -z $json ]]; then
    vws_emit FAIL vws-crd "CRD ${name} is missing; next: install the bootstrap (build/bootstrap.yaml, vws-deploy#54) with cluster-admin credentials"
  elif jq -e 'any(.status.conditions[]?; .type == "Established" and .status == "True")' <<<"$json" >/dev/null; then
    vws_emit OK vws-crd "${name} Established"
  else
    vws_emit FAIL vws-crd "${name} not Established; next: kubectl describe crd ${name}"
  fi
}

# The reads status and doctor need. Missing ones explain WARNs above.
vws_check_read_rights() {
  local pair answer missing=()
  for pair in "get vwsclusters" "list deployments" "list statefulsets" "list pods" "list poddisruptionbudgets" "get secrets"; do
    # shellcheck disable=SC2086 # pair is two words on purpose
    answer=$(vws_kubectl auth can-i $pair 2>/dev/null || true)
    [[ $answer == yes ]] || missing+=("$pair")
  done
  if ((${#missing[@]} == 0)); then
    vws_emit OK read-rights "all status reads allowed, release readable"
  elif [[ ${missing[*]} == "get secrets" ]]; then
    vws_emit OK read-rights "operating role: workloads readable; helm release not readable (no get secrets), expected after vws installer finish"
  else
    vws_emit WARN read-rights "cannot: ${missing[*]}"
  fi
}

# Ceph health when the CR uses rook-ceph.
vws_check_ceph() {
  local driver json health
  driver=$(jq -r '.spec.storage.driver // "rook-ceph"' <<<"${vws_status_cr:-{\}}")
  if [[ $driver != rook-ceph ]]; then
    vws_emit OK ceph "storage driver ${driver}; not checked"
    return 0
  fi
  if ! json=$(vws_kubectl get cephcluster -A -o json 2>/dev/null); then
    vws_emit WARN ceph "cannot read CephCluster with these credentials"
    return 0
  fi
  health=$(jq -r '[.items[] | .status.ceph.health // "unknown"] | join(",")' <<<"$json")
  case $health in
    HEALTH_OK) vws_emit OK ceph "$health" ;;
    HEALTH_WARN) vws_emit WARN ceph "$health" ;;
    "") vws_emit FAIL ceph "no CephCluster; next: check the rook-ceph bundle (kubectl -n rook-ceph get pods)" ;;
    *) vws_emit FAIL ceph "${health}; next: kubectl -n rook-ceph get cephcluster -o yaml (status.ceph.details)" ;;
  esac
}

# The running args carry the pendingFlags exactly when the CR renders global.pendingFlags true (#38).
vws_check_pending_flags() {
  local out
  [[ -n $vws_status_values && -f ${vws_status_tmp}/workloads.json ]] || return 0
  out=$(python3 - "${VWS_ROOT}/charts/vws/values.yaml" "$vws_status_values" "${vws_status_tmp}/workloads.json" <<'PY'
import json, sys, yaml
base = yaml.safe_load(open(sys.argv[1]))
over = yaml.safe_load(open(sys.argv[2])) or {}
want = bool((over.get("global") or {}).get("pendingFlags", base["global"].get("pendingFlags", False)))
items = {i["metadata"]["name"]: i for i in json.load(open(sys.argv[3])).get("items", [])}
bad = []
for name, spec in sorted(base["services"].items()):
    keys = list((spec.get("pendingFlags") or {}).keys()) + list((((over.get("services") or {}).get(name) or {}).get("pendingFlags") or {}).keys())
    if not keys or name not in items:
        continue
    args = [a for c in items[name]["spec"]["template"]["spec"]["containers"] for a in (c.get("args") or [])]
    have = [k for k in sorted(set(keys)) if any(a.startswith(f"-{k}=") for a in args)]
    if want and len(have) < len(set(keys)):
        bad.append(f"{name} lacks {','.join(sorted(set(keys) - set(have)))}")
    if not want and have:
        bad.append(f"{name} runs with {','.join(have)}")
print(("true" if want else "false") + "\t" + "; ".join(bad))
PY
  )
  if [[ -z ${out#*$'\t'} ]]; then
    vws_emit OK pending-flags "running args match global.pendingFlags=${out%%$'\t'*}"
  else
    vws_emit WARN pending-flags "global.pendingFlags=${out%%$'\t'*} but ${out#*$'\t'}; run vws apply"
  fi
}
