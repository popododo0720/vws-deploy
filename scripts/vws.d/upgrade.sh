#!/usr/bin/env bash
# summary: change spec.version of the VWSCluster to a new image tag and apply it
# Sourced by scripts/vws. Usage: vws upgrade --to TAG
# Same tag as the CR: unchanged, nothing written. Otherwise the CR is saved with the new tag and
# vws apply runs (helm --atomic --wait --history-max 10). When apply fails, helm has rolled the
# release back and the CR spec is put back to the previous value, so CR and release agree. The
# platform bundle is not upgraded here (#43 section 5, out of scope).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl helm)
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"
# shellcheck source=scripts/vws.d/lib/applyrun.sh
source "${VWS_ROOT}/scripts/vws.d/lib/applyrun.sh"

vws_upgrade_tmp=''

# vws_upgrade_save_spec CR_JSON SPEC_JSON: server-side apply the CR with that spec.
vws_upgrade_save_spec() {
  jq --argjson s "$2" '{apiVersion, kind, metadata: {name: .metadata.name, namespace: .metadata.namespace}, spec: $s}' <<<"$1" |
    vws_kubectl apply --server-side --field-manager=vws -f - >/dev/null
}

vws_cmd_main() {
  local to='' cr old new rc=0
  while (($#)); do
    case $1 in
      --to) (($# >= 2)) || vws_usage "--to needs a tag"; to=$2; shift 2 ;;
      --to=*) to=${1#*=}; shift ;;
      *) vws_usage "usage: vws upgrade --to TAG" ;;
    esac
  done
  [[ -n $to ]] || vws_usage "usage: vws upgrade --to TAG"
  [[ $to =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || vws_usage "${to} is not an image tag"
  vws_installer_namespace
  vws_installer_guard_ttl
  cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) || vws_usage "no VWSCluster vws in ${VWS_NAMESPACE}; run vws init first"
  old=$(jq -c '.spec' <<<"$cr")
  if [[ $(jq -r '.spec.version // ""' <<<"$cr") == "$to" ]]; then
    vws_emit OK upgrade "unchanged: spec.version is already ${to}"
    return 0
  fi
  new=$(jq -c --arg v "$to" '.spec.version = $v' <<<"$cr")
  vws_upgrade_tmp=$(mktemp -d)
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "${vws_upgrade_tmp:-}"; rm -f "$vws_results"' EXIT
  jq '{apiVersion, kind, metadata: {name: .metadata.name}, spec}' <<<"$new" >"${vws_upgrade_tmp}/cr.yaml"
  python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "${vws_upgrade_tmp}/cr.yaml" /dev/null ||
    vws_usage "spec.version ${to} makes the VWSCluster invalid; nothing was written"

  vws_emit WARN backup "take a backup first: vws backup (vws-deploy#61) is not in this build; see docs/etcd-runbook.md for the etcd snapshot"
  vws_emit WARN downtime "zero downtime is not claimed until vws-deploy#39 is closed; stateful services (StatefulSet, one replica) have a short outage"
  vws_emit OK upgrade "spec.version: $(jq -r '.spec.version // "(chart appVersion)"' <<<"$cr") -> ${to}"
  if [[ -n $VWS_DRY_RUN ]]; then
    vws_emit OK upgrade "dry-run: the CR is not saved and apply does not run"
    return 0
  fi
  vws_upgrade_save_spec "$cr" "$(jq -c '.spec' <<<"$new")" || vws_die_env "cannot save VWSCluster vws"
  vws_run_apply || rc=$?
  # shellcheck disable=SC2154 # vws_fail_count is set by lib/common.sh
  if ((rc != 0 || vws_fail_count > 0)); then
    vws_upgrade_save_spec "$cr" "$old" || vws_die_env "apply failed and the CR could not be put back; set spec.version back by hand"
    vws_emit FAIL upgrade "apply failed (the release was rolled back by --atomic); the CR spec is back to the previous value; next: vws status, then fix and retry"
  fi
}
