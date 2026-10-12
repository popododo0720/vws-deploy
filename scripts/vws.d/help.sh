#!/usr/bin/env bash
# summary: list the commands and the common flags
# Sourced by scripts/vws. Needs no cluster tool.

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
  --context NAME     kubeconfig context
  -n, --namespace N  namespace
  -o text|json       output format (default text)
  --dry-run          print changing kubectl and helm calls instead of running them
  -y, --yes          confirm a destructive command
  --strict           exit 1 on WARN as well as FAIL
  -h, --help         this text

exit codes: 0 ok, 1 a check failed, 2 usage error, 3 environment (tool missing, cluster unreachable)

commands:
USAGE
  for file in "${VWS_COMMAND_DIR}"/*.sh; do
    [[ -f $file ]] || continue
    name=$(basename "$file" .sh)
    summary=$(sed -n 's/^# summary: //p' "$file" | head -n 1)
    printf '  %-12s %s\n' "$name" "$summary"
  done
}
