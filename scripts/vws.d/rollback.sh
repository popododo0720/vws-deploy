#!/usr/bin/env bash
# summary: roll the release back to a revision and put the VWSCluster spec back to that revision's (needs --yes)
# Sourced by scripts/vws. Usage: vws rollback [--to REVISION] [--yes]
# Without --yes it prints the plan and the limits and exits 2. With --yes: helm rollback --wait, then
# the CR spec becomes the spec recorded in that revision (vws apply writes it into the revision
# description as "vws-spec:<json>") and the CR status is written, so CR and release point at the
# same values. Already at that revision with that spec: unchanged.
# Helm keeps its history in Secrets: this needs credentials that can read them (not the operating role).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 sha256sum kubectl helm)
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"

vws_rollback_tmp=''

vws_rollback_limits() {
  vws_emit WARN limits "helm rollback restores chart resources and values only; if the newer version migrated a store format, the older binaries may not read it. Treat it as safe only for the previous revision with an unchanged store format"
  vws_emit WARN limits "CRDs and platform operators are not rolled back; the etcd snapshot is the only way back for them (docs/etcd-runbook.md)"
  vws_emit WARN limits "zero downtime is not claimed until vws-deploy#39 is closed"
}

vws_cmd_main() {
  local to='' history current target spec cr err values hash generation
  while (($#)); do
    case $1 in
      --to) (($# >= 2)) || vws_usage "--to needs a revision"; to=$2; shift 2 ;;
      --to=*) to=${1#*=}; shift ;;
      *) vws_usage "usage: vws rollback [--to REVISION] [--yes]" ;;
    esac
  done
  [[ -z $to || $to =~ ^[1-9][0-9]*$ ]] || vws_usage "--to must be a revision number"
  vws_installer_namespace
  vws_rollback_tmp=$(mktemp -d)
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "${vws_rollback_tmp:-}"; rm -f "$vws_results"' EXIT
  err="${vws_rollback_tmp}/err"
  if ! history=$(vws_helm history vws -o json 2>"$err"); then
    grep -qiE 'forbidden|secrets' "$err" &&
      vws_die_env "cannot read the release history with these credentials (the history lives in Secrets, which the operating role cannot read); run vws rollback with your own kubeconfig"
    vws_die_env "helm history failed: $(head -1 "$err")"
  fi
  current=$(jq -r 'map(.revision) | max // empty' <<<"$history")
  [[ -n $current ]] || vws_usage "release vws has no history"
  if [[ -z $to ]]; then
    to=$(jq -r --argjson c "$current" '[.[] | select(.revision < $c and (.status == "superseded" or .status == "deployed"))] | map(.revision) | max // empty' <<<"$history")
    [[ -n $to ]] || vws_usage "no earlier revision to roll back to"
  fi
  jq -e --argjson t "$to" 'any(.[]; .revision == $t)' <<<"$history" >/dev/null || vws_usage "revision ${to} is not in the history (the last 10 are kept)"
  spec=$(jq -r --argjson t "$to" '.[] | select(.revision == $t) | .description // ""' <<<"$history")
  if [[ $spec != vws-spec:* ]] || ! spec=$(jq -ce '.' <<<"${spec#vws-spec:}" 2>/dev/null); then
    vws_emit FAIL rollback "revision ${to} has no recorded VWSCluster spec (made before vws-deploy#60 or by hand); CR and release cannot be kept in step; next: roll the release back by hand and run vws reconfigure --from the matching CR"
    return 0
  fi
  cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) || vws_usage "no VWSCluster vws in ${VWS_NAMESPACE}"
  if [[ $to == "$current" && $(jq -c '.spec' <<<"$cr") == "$spec" ]]; then
    vws_emit OK rollback "unchanged: release is at revision ${to} and the CR has its spec"
    return 0
  fi

  vws_emit OK plan "helm rollback vws ${to} --wait (current revision ${current})"
  vws_emit OK plan "VWSCluster spec -> $(jq -c '.' <<<"$spec")"
  vws_rollback_limits
  if [[ -z $VWS_YES || -n $VWS_DRY_RUN ]]; then
    [[ -n $VWS_DRY_RUN ]] && { vws_emit OK rollback "dry-run: nothing done"; return 0; }
    vws_confirm "vws rollback"
  fi

  vws_helm rollback vws "$to" --wait --timeout 15m >/dev/null 2>"$err" ||
    { vws_emit FAIL rollback "helm rollback failed: $(head -1 "$err"); the CR is unchanged; next: check the release history"; return 0; }
  jq --argjson s "$spec" '{apiVersion, kind, metadata: {name: .metadata.name, namespace: .metadata.namespace}, spec: $s}' <<<"$cr" |
    vws_kubectl apply --server-side --field-manager=vws -f - >/dev/null ||
    vws_die_env "the release is at revision ${to} but the CR could not be saved; run vws reconfigure with the spec above"
  # Status: the values hash of the restored spec, the generation the save produced.
  jq --argjson s "$spec" '{apiVersion, kind, metadata: {name: .metadata.name}, spec: $s}' <<<"$cr" >"${vws_rollback_tmp}/cr.yaml"
  values="${vws_rollback_tmp}/values.yaml"
  python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "${vws_rollback_tmp}/cr.yaml" "$values" || vws_die_env "the recorded spec does not render"
  hash=$(sha256sum "$values" | cut -d' ' -f1)
  generation=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null | jq -r '.metadata.generation // 0') || generation=0
  vws_kubectl patch vwsclusters vws --subresource=status --type=merge -p "$(jq -nc --argjson g "$generation" --arg h "$hash" \
    '{status: {observedGeneration: $g, appliedValuesHash: $h, conditions: [{type: "Applied", status: "True", reason: "RolledBack", message: "vws rollback"}]}}')" >/dev/null ||
    vws_die_env "cannot write VWSCluster status"
  vws_emit OK rollback "release and CR at revision ${to} (values ${hash:0:12})"
}
