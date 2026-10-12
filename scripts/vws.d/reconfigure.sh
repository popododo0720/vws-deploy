#!/usr/bin/env bash
# summary: change the VWSCluster (--from FILE or --set path=value) and apply it
# Sourced by scripts/vws. Usage: vws reconfigure (--from FILE | --set path=value [--set ...])
# The CR is the only source: there is no path that edits a values file. The new CR is validated
# with render.py, the chart schema (helm template) and scripts/check_render.py before anything is
# written; invalid input exits 2 and changes nothing. The changed spec fields are printed
# (secret-shaped fields as "(hidden)"). Same spec as in the cluster: unchanged, no call that writes.
# --dry-run prints the change summary only. Otherwise the CR is saved and vws apply runs.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl helm)
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"
# shellcheck source=scripts/vws.d/lib/applyrun.sh
source "${VWS_ROOT}/scripts/vws.d/lib/applyrun.sh"

vws_reconfigure_tmp=''

# vws_reconfigure_validate CR DIR: render, schema, recovery invariants. Exit 2 when any fails.
vws_reconfigure_validate() {
  local cr=$1 dir=$2
  python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "$cr" "${dir}/values.yaml" ||
    vws_usage "the new VWSCluster is invalid (see above); nothing was written"
  vws_helm template vws "${VWS_ROOT}/charts/vws" --namespace "$VWS_NAMESPACE" -f "${dir}/values.yaml" >"${dir}/rendered.yaml" 2>"${dir}/helm.err" ||
    vws_usage "the rendered values fail charts/vws/values.schema.json: $(head -1 "${dir}/helm.err"); nothing was written"
  python3 "${VWS_ROOT}/scripts/check_render.py" "${dir}/rendered.yaml" "${VWS_ROOT}/charts/vws/values.yaml" "$VWS_NAMESPACE" "${dir}/values.yaml" >"${dir}/check.out" 2>&1 ||
    vws_usage "scripts/check_render.py rejects the result: $(head -1 "${dir}/check.out"); nothing was written"
}

vws_cmd_main() {
  local from='' sets=() current changes path old new
  while (($#)); do
    case $1 in
      --from) (($# >= 2)) || vws_usage "--from needs a file"; from=$2; shift 2 ;;
      --from=*) from=${1#*=}; shift ;;
      --set) (($# >= 2)) || vws_usage "--set needs path=value"; sets+=("$2"); shift 2 ;;
      --set=*) sets+=("${1#*=}"); shift ;;
      *) vws_usage "usage: vws reconfigure (--from FILE | --set path=value [--set ...])" ;;
    esac
  done
  [[ -n $from || ${#sets[@]} -gt 0 ]] || vws_usage "usage: vws reconfigure (--from FILE | --set path=value [--set ...])"
  [[ -z $from || ${#sets[@]} -eq 0 ]] || vws_usage "--from and --set do not mix"
  [[ -z $from || -f $from ]] || vws_usage "${from} does not exist"
  vws_installer_namespace
  vws_installer_guard_ttl

  vws_reconfigure_tmp=$(mktemp -d)
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "${vws_reconfigure_tmp:-}"; rm -f "$vws_results"' EXIT
  current=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) || vws_usage "no VWSCluster vws in ${VWS_NAMESPACE}; run vws init first"
  printf '%s\n' "$current" >"${vws_reconfigure_tmp}/current.json"
  if [[ -n $from ]]; then
    python3 "${VWS_ROOT}/scripts/vws.d/lib/reconfigure.py" build "${vws_reconfigure_tmp}/current.json" "${vws_reconfigure_tmp}/cr.yaml" --from "$from" ||
      vws_usage "cannot read ${from}"
  else
    python3 "${VWS_ROOT}/scripts/vws.d/lib/reconfigure.py" build "${vws_reconfigure_tmp}/current.json" "${vws_reconfigure_tmp}/cr.yaml" "${sets[@]}" ||
      vws_usage "bad --set"
  fi
  vws_reconfigure_validate "${vws_reconfigure_tmp}/cr.yaml" "$vws_reconfigure_tmp"

  changes=$(python3 "${VWS_ROOT}/scripts/vws.d/lib/reconfigure.py" diff "${vws_reconfigure_tmp}/current.json" "${vws_reconfigure_tmp}/cr.yaml") ||
    vws_die_env "cannot compare the CRs"
  if [[ -z $changes ]]; then
    vws_emit OK reconfigure "unchanged: VWSCluster vws already has this spec"
    return 0
  fi
  while IFS=$'\t' read -r path old new; do
    vws_emit OK change "${path}: ${old} -> ${new}"
  done <<<"$changes"
  if [[ -n $VWS_DRY_RUN ]]; then
    vws_emit OK reconfigure "dry-run: the change above is not saved and apply does not run"
    return 0
  fi
  vws_kubectl apply --server-side --field-manager=vws -f - <"${vws_reconfigure_tmp}/cr.yaml" >/dev/null ||
    vws_die_env "cannot save VWSCluster vws"
  vws_emit OK reconfigure "saved VWSCluster vws; applying"
  vws_run_apply || true
}
