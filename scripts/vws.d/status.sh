#!/usr/bin/env bash
# summary: show the installed state: CR, applied values, release, bundle versions, services (read only)
# Sourced by scripts/vws. Usage: vws status. Exit 1 on a FAIL (or a WARN with --strict), 3 when the
# cluster cannot be read. Works with the operating role (no Secrets): see lib/status.sh.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 sha256sum kubectl helm)
# shellcheck source=scripts/vws.d/lib/checks.sh
source "${VWS_ROOT}/scripts/vws.d/lib/checks.sh"
# shellcheck source=scripts/vws.d/lib/status.sh
source "${VWS_ROOT}/scripts/vws.d/lib/status.sh"

vws_status_all() {
  local mode=$1
  vws_check_api
  vws_status_load
  vws_check_cluster_cr
  vws_check_applied_hash
  vws_check_release_detail "$mode"
  vws_check_bundle_versions
  vws_check_services
  vws_check_pdbs
}

vws_cmd_main() {
  (($# == 0)) || vws_usage "usage: vws status"
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'vws_status_cleanup; rm -f "$vws_results"' EXIT
  vws_status_all status
}
