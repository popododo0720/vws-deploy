#!/usr/bin/env bash
# summary: manage AWS accounts through the console admin API
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3 curl)

vws_cmd_main() {
  python3 "${VWS_ROOT}/scripts/vws.d/lib/admin.py" aws-account "$@"
  exit 0
}
