#!/usr/bin/env bash
# vws_run_apply: run vws apply --unattended as a child and fold its result lines into this
# command's results (text and -o json alike). Exit 2 or 3 of the child is passed on; a FAIL line
# makes this command exit 1 through vws_finish. Source after common.sh.
vws_run_apply() {
  local json rc=0 level name message
  json=$("${VWS_ROOT}/scripts/vws" -o json apply --unattended) || rc=$?
  if ((rc == 2 || rc == 3)); then
    [[ -z $json ]] || jq -r '.checks[] | "\(.level) \(.name) \(.message)"' <<<"$json" >&2
    ((rc == 2)) && vws_usage "vws apply rejected the saved CR"
    vws_die_env "vws apply could not evaluate the cluster"
  fi
  while IFS=$'\t' read -r level name message; do
    [[ -n $level ]] && vws_emit "$level" "$name" "$message"
  done < <(jq -r '.checks[] | [.level, .name, .message] | @tsv' <<<"$json")
  return "$rc"
}
