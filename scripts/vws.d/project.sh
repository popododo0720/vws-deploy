#!/usr/bin/env bash
# summary: show, map and unmap project pools and AWS accounts
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3 curl)

vws_cmd_main() {
  python3 "${VWS_ROOT}/scripts/vws.d/lib/admin.py" project "$@"
  exit 0
}
