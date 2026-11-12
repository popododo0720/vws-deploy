#!/usr/bin/env bash
# summary: validate the global layer bootstrap file /etc/vws/global.yaml
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3)

vws_cmd_main() {
  python3 "${VWS_ROOT}/scripts/vws.d/lib/globalconfig.py" "$@"
  exit 0
}
