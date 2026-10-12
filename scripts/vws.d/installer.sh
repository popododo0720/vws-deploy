#!/usr/bin/env bash
# summary: installer finish: revoke the temporary cluster-admin binding and verify it is gone
# Sourced by scripts/vws. Usage: vws installer finish
# Idempotent: an already deleted binding is unchanged and the check runs again. The command fails
# (exit 1) when the installer ServiceAccount can still act as cluster-admin, and exits 3 when the
# answer cannot be read. Outside the installer pod it checks with --as (needs impersonate rights).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl)
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"

# Rights cluster-admin has and the operating rights do not. Every one must answer no.
VWS_REVOKED_RIGHTS=("* *" "create clusterrolebindings" "create clusterroles" "delete namespaces"
  "create customresourcedefinitions.apiextensions.k8s.io" "get secrets")
# Rights the installer keeps (bootstrap Role vws-operator). Every one must answer yes.
VWS_KEPT_RIGHTS=("get vwsclusters" "patch vwsclusters/status")

vws_cmd_main() {
  local right verb resource result answer
  [[ $# -eq 1 && $1 == finish ]] || vws_usage "usage: vws installer finish"
  vws_installer_namespace
  result=$(vws_installer_revoke) || vws_die_env "cannot delete clusterrolebinding ${VWS_INSTALLER_BINDING}"
  vws_emit OK revoke "clusterrolebinding ${VWS_INSTALLER_BINDING}: ${result}"
  if [[ -n $VWS_DRY_RUN ]]; then
    vws_emit WARN verify "dry-run: the binding was not deleted, so nothing to verify"
    return 0
  fi
  for right in "${VWS_REVOKED_RIGHTS[@]}"; do
    read -r verb resource <<<"$right"
    answer=$(vws_installer_can_i "$verb" "$resource") || vws_die_env "auth can-i ${verb} ${resource} gave no answer; revocation cannot be verified"
    if [[ $answer == yes ]]; then
      vws_emit FAIL revoked "${VWS_INSTALLER_SA} can still ${verb} ${resource}; the temporary rights are not gone"
    else
      vws_emit OK revoked "cannot ${verb} ${resource}"
    fi
  done
  for right in "${VWS_KEPT_RIGHTS[@]}"; do
    read -r verb resource <<<"$right"
    answer=$(vws_installer_can_i "$verb" "$resource") || vws_die_env "auth can-i ${verb} ${resource} gave no answer"
    if [[ $answer == yes ]]; then
      vws_emit OK kept "can ${verb} ${resource}"
    else
      vws_emit FAIL kept "${VWS_INSTALLER_SA} lost ${verb} ${resource}; apply the vws-operator Role from bootstrap.yaml"
    fi
  done
}
