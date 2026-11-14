#!/usr/bin/env bash
# summary: run the external box's read-only pve-test conformance suite and archive its reports
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3)

vws_cmd_main() {
  local conformance=${VWS_CONF_DIR:-} results='' timeout_s=1800
  while (($#)); do
    case $1 in
      --conformance | --results | --timeout)
        (($# >= 2)) || vws_usage "$1 needs a value"
        case $1 in
          --conformance) conformance=$2 ;;
          --results) results=$2 ;;
          --timeout) timeout_s=$2 ;;
        esac
        shift 2 ;;
      *) vws_usage "nightly-conformance: unknown argument" ;;
    esac
  done
  [[ -z $VWS_DRY_RUN ]] || vws_usage "nightly-conformance has no --dry-run"
  [[ $timeout_s =~ ^[1-9][0-9]{0,5}$ ]] || vws_usage "--timeout must be whole seconds from 1 to 999999"
  [[ -n $results ]] || results="${XDG_STATE_HOME:-${HOME}/.local/state}/vws-nightly-conformance/$(date -u +%Y%m%dT%H%M%SZ)"
  exec python3 "${VWS_ROOT}/scripts/vws.d/lib/conformance.py" "$conformance" "$results" "$timeout_s"
}
