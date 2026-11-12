#!/usr/bin/env bash
# Installer state for vws installer (vws-deploy#54). Source after common.sh.
# The temporary cluster-admin binding (bootstrap/bootstrap.yaml) is VWS_INSTALLER_BINDING. Its
# annotation vws-installer-ttl-seconds bounds its life; past that, any installer command revokes it.

VWS_INSTALLER_BINDING=vws-installer-temp
VWS_INSTALLER_SA=vws-installer

vws_installer_namespace() {
  VWS_NAMESPACE=${VWS_NAMESPACE:-vws}
  export VWS_NAMESPACE
}

# vws_installer_revoke: delete the temporary binding. Prints deleted, unchanged or planned (--dry-run).
# Returns 3 when the delete fails; callers turn that into exit 3.
vws_installer_revoke() {
  local out
  out=$(vws_kubectl delete clusterrolebinding "$VWS_INSTALLER_BINDING" --ignore-not-found) || return 3
  if [[ -n $VWS_DRY_RUN ]]; then echo planned; elif [[ -n $out ]]; then echo deleted; else echo unchanged; fi
}

# vws_installer_can_i VERB RESOURCE: yes or no for the installer ServiceAccount. Inside the installer
# pod (VWS_INSTALLER_POD=1) it asks for itself; elsewhere it impersonates the ServiceAccount.
# Returns 3 without an answer (callers exit 3: revocation cannot be verified).
vws_installer_can_i() {
  local as=() answer
  [[ ${VWS_INSTALLER_POD:-} == 1 ]] || as=("--as=system:serviceaccount:${VWS_NAMESPACE}:${VWS_INSTALLER_SA}")
  answer=$(vws_kubectl auth can-i "$1" "$2" "${as[@]}" 2>/dev/null || true)
  answer=${answer%%$'\n'*}
  [[ $answer == yes || $answer == no ]] || return 3
  echo "$answer"
}
