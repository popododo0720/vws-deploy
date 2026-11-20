#!/usr/bin/env bash
# summary: list the commands and the common flags
# Sourced by scripts/vws. Needs no external tool.

vws_cmd_main() {
  local file name summary
  if [[ $VWS_OUTPUT == json ]]; then
    for file in "${VWS_COMMAND_DIR}"/*.sh; do
      [[ -f $file ]] || continue
      name=$(basename "$file" .sh)
      vws_emit OK "$name" "$(sed -n 's/^# summary: //p' "$file" | head -n 1)"
    done
    return 0
  fi
  cat <<'USAGE'
usage: scripts/vws [flags] <command> [args]

flags (before or after the command):
  -o text|json       output format (default text)
  --dry-run          print the request an admin command would send instead of sending it
  -y, --yes          confirm a destructive command
  --strict           exit 1 on WARN as well as FAIL
  -h, --help         this text

exit codes: 0 ok, 1 a check failed, 2 usage error, 3 environment (tool missing, service unreachable)

commands:
USAGE
  for file in "${VWS_COMMAND_DIR}"/*.sh; do
    [[ -f $file ]] || continue
    name=$(basename "$file" .sh)
    summary=$(sed -n 's/^# summary: //p' "$file" | head -n 1)
    printf '  %-12s %s\n' "$name" "$summary"
  done
}
