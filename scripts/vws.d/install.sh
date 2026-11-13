#!/usr/bin/env bash
# summary: install the lab systemd packages over ssh with a readiness gate
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3 ssh dpkg-deb)

vws_cmd_main() {
  python3 "${VWS_ROOT}/scripts/vws.d/lib/install.py" "$@"
  exit 0
}
