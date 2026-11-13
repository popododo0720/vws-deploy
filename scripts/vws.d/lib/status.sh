#!/usr/bin/env bash
# Checks of vws status and vws doctor (vws-deploy#56, #43 section 4). Source after common.sh and checks.sh.
# Read only: every call is a vws_kubectl/vws_helm read verb.
#
# Credentials: after vws installer finish the operating role (vws-operator) cannot read Secrets.
# Helm keeps its releases in Secrets, so helm status/get fail with those credentials. The checks
# therefore read the VWSCluster status first; the release is extra detail that
# needs the user's own kubeconfig, and its absence is a WARN with that next step, never a FAIL.
# Every FAIL message ends with "next: <one action>".

VWS_PART_OF_SELECTOR=app.kubernetes.io/part-of=vws
vws_status_cr=''
vws_status_tmp=''

vws_status_cleanup() { [[ -z $vws_status_tmp ]] || rm -rf "$vws_status_tmp"; }

# vws_status_load: read the CR once for generation and storage checks.
vws_status_load() {
  VWS_NAMESPACE=${VWS_NAMESPACE:-vws}
  vws_status_tmp=$(mktemp -d)
  if ! vws_status_cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null); then
    vws_status_cr=''
    return 0
  fi
}

# The CR exists, its generation is applied.
vws_check_cluster_cr() {
  local generation observed profile
  if [[ -z $vws_status_cr ]]; then
    vws_emit FAIL cluster-cr "no VWSCluster vws in ${VWS_NAMESPACE} (or no right to read it); next: check the installed VWSCluster with your operator kubeconfig"
    return 0
  fi
  profile=$(jq -r '.spec.profile // "unknown"' <<<"$vws_status_cr")
  generation=$(jq -r '.metadata.generation // 0' <<<"$vws_status_cr")
  observed=$(jq -r '.status.observedGeneration // "none"' <<<"$vws_status_cr")
  if [[ $observed == "$generation" ]]; then
    vws_emit OK cluster-cr "profile ${profile}, generation ${generation} applied"
  else
    vws_emit WARN cluster-cr "profile ${profile}, generation ${generation} not applied (applied ${observed}); check the installed VWSCluster with your operator kubeconfig"
  fi
}

# vws_status_release_unreadable ERR: helm failed because the credentials cannot read Secrets.
vws_status_release_unreadable() {
  grep -qiE 'forbidden|cannot (list|get) resource "secrets"' <<<"$1"
}

# Release revision and status. Needs Secret reads.
vws_check_release_detail() {
  local mode=$1 json err status revision
  err="${vws_status_tmp}/helm.err"
  if ! json=$(vws_helm status vws -o json 2>"$err"); then
    if vws_status_release_unreadable "$(cat "$err")"; then
      vws_emit WARN release "not readable with these credentials: helm keeps the release in Secrets and the operating role cannot read Secrets; run vws status with your own kubeconfig"
    elif grep -qi 'not found' "$err"; then
      vws_emit "$(vws_missing WARN "$mode")" release "release vws is not installed; next: inspect the release with your operator kubeconfig"
    else
      vws_emit WARN release "helm status failed: $(head -1 "$err")"
    fi
    return 0
  fi
  status=$(jq -r '.info.status // "unknown"' <<<"$json")
  revision=$(jq -r '.version // "?"' <<<"$json")
  case $status in
    deployed) vws_emit OK release "revision ${revision} deployed" ;;
    pending-* | failed) vws_emit FAIL release "revision ${revision} is ${status}; next: helm history vws" ;;
    *) vws_emit WARN release "revision ${revision} is ${status}" ;;
  esac
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
