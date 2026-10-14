#!/usr/bin/env bash
# summary: check the cluster before an install (read only, except a lowered spec.network.vmMTU)
# Sourced by scripts/vws. Usage: vws preflight --profile single-node|small|ha [--links FILE]
# --links: talosctl -n <every node> get links -o json, to check the NICs of spec.network.nodes (#128).
# Exit 1 on any FAIL (or WARN with --strict), 3 when the cluster cannot be evaluated.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl helm)
# shellcheck source=scripts/vws.d/lib/checks.sh
source "${VWS_ROOT}/scripts/vws.d/lib/checks.sh"

vws_cmd_main() {
  local profile='' links=''
  while (($#)); do
    case $1 in
      --profile) (($# >= 2)) || vws_usage "--profile needs a value"; profile=$2; shift 2 ;;
      --profile=*) profile=${1#*=}; shift ;;
      --links) (($# >= 2)) || vws_usage "--links needs a file"; links=$2; shift 2 ;;
      --links=*) links=${1#*=}; shift ;;
      *) vws_usage "usage: vws preflight --profile single-node|small|ha [--links FILE]" ;;
    esac
  done
  [[ -n $profile ]] || vws_usage "usage: vws preflight --profile single-node|small|ha"
  [[ -n ${VWS_PROFILE_MIN_NODES[$profile]:-} ]] || vws_usage "unknown profile ${profile}"
  vws_check_tools
  vws_check_api
  vws_check_k8s_minor
  vws_check_install_rights
  vws_check_nodes "$profile"
  vws_check_storage preflight
  vws_check_crds preflight
  vws_check_kernel_indirect preflight
  vws_check_node_network "$links"
  vws_check_release
  vws_check_source
}
