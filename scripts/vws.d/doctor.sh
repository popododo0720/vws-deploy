#!/usr/bin/env bash
# summary: status plus diagnosis (restarts, Pending PVCs, CRDs, rights, Ceph, pendingFlags), each FAIL with a next step
# Sourced by scripts/vws. Usage: vws doctor. Read only. Exit 1 on a FAIL (or a WARN with --strict), 3
# when the cluster cannot be read.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 sha256sum kubectl helm)
# shellcheck source=scripts/vws.d/status.sh
source "${VWS_ROOT}/scripts/vws.d/status.sh"

vws_cmd_main() {
  (($# == 0)) || vws_usage "usage: vws doctor"
  trap 'vws_status_cleanup; rm -f "$vws_results"' EXIT
  vws_status_all doctor
  vws_check_restarts
  vws_check_pending_pvcs
  vws_check_vws_crd
  vws_check_crds doctor
  vws_check_read_rights
  vws_check_ceph
  vws_check_pending_flags
}
