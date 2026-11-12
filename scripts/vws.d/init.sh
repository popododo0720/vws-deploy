#!/usr/bin/env bash
# summary: open the legacy installer TUI or print its connection details
# Sourced by scripts/vws. Usage: vws init [--print-url]
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=()

vws_cmd_main() {
  local bin=${VWS_INSTALLER_BIN:-/opt/vws/bin/vws-installer} action=tui
  if [[ $# -eq 1 && ${1:-} == --print-url ]]; then
    action=print-url
  elif [[ $# -ne 0 || ${VWS_INSTALLER_POD:-} != 1 ]]; then
    vws_usage "usage: vws init [--print-url]; the TUI runs in the installer pod"
  fi
  [[ -x $bin ]] || vws_usage "init needs the installer binary (${bin})"
  exec "$bin" "$action"
}
