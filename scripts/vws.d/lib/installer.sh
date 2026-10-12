#!/usr/bin/env bash
# Installer state shared by vws init, vws apply and vws installer (vws-deploy#54). Source after common.sh.
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

# vws_installer_expired: 0 when the binding exists and is past its TTL. A binding we cannot read
# (gone, or no rights left to read it) is not active.
vws_installer_expired() {
  local json
  json=$(vws_kubectl get clusterrolebinding "$VWS_INSTALLER_BINDING" -o json 2>/dev/null) || return 1
  python3 - "$json" <<'PY'
import datetime, json, sys
o = json.loads(sys.argv[1])
created = datetime.datetime.fromisoformat(o["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
ttl = int((o["metadata"].get("annotations") or {}).get("vws-installer-ttl-seconds", "0"))
now = datetime.datetime.now(datetime.timezone.utc)
sys.exit(0 if ttl <= 0 or (now - created).total_seconds() > ttl else 1)
PY
}

# vws_installer_guard_ttl: called first by every installer command. Past the TTL it revokes and stops.
vws_installer_guard_ttl() {
  if vws_installer_expired; then
    vws_installer_revoke >/dev/null || vws_die_env "cannot delete the expired installer binding"
    vws_emit FAIL installer-ttl "the temporary installer binding was past its TTL and is revoked; apply bootstrap.yaml again to install"
    vws_finish
  fi
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

# vws_api_group: the VWSCluster group, read from the CRD (written in one place, AGENTS.md R18).
vws_api_group() {
  sed -n 's/^  group: //p' "${VWS_ROOT}/charts/vws/crds/vwsclusters.yaml"
}
