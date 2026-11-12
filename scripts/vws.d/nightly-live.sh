#!/usr/bin/env bash
# summary: gate the Proxmox test cluster, sweep leftovers and check VM slots
# shellcheck disable=SC2034
VWS_NEED=(python3 flock)

vws_live_call() {
  python3 "${VWS_ROOT}/scripts/vws.d/lib/live.py" "$@" &
  live_pid=$!
  local status=0
  wait "$live_pid" || status=$?
  live_pid=''
  return "$status"
}

vws_live_signal() {
  trap '' INT TERM
  if [[ -n $live_pid ]]; then
    kill -TERM "$live_pid" 2>/dev/null || true
    wait "$live_pid" 2>/dev/null || true
  fi
  exit "$1"
}

vws_live_end() {
  local status=$?
  trap - EXIT
  trap '' INT TERM
  if ((live_ready)); then
    if ! vws_live_call sweep "${live_args[@]}" --phase l0-end; then
      ((status == 130 || status == 143)) || status=1
    fi
    vws_live_call results "${live_args[@]}" --exit "$status" || status=1
  fi
  rm -f -- "${results}/.gate.json"
  exit "$status"
}

vws_cmd_main() {
  local region='' results='' snippets=local ca='' fail_at='' status=0
  local live_pid='' live_ready=0 lock_fd
  local -a live_args
  while (($#)); do
    case $1 in
      --region | --results | --snippets-storage | --pve-ca-file | --fail-at)
        (($# >= 2)) || vws_usage "nightly-live: option needs a value"
        case $1 in
          --region) region=$2 ;; --results) results=$2 ;;
          --snippets-storage) snippets=$2 ;; --pve-ca-file) ca=$2 ;;
          --fail-at) fail_at=$2 ;;
        esac
        shift 2 ;;
      *) vws_usage "nightly-live: invalid arguments" ;;
    esac
  done
  [[ -n $region ]] || vws_usage "nightly-live requires --region"
  [[ -z $fail_at || $fail_at == slots ]] || vws_usage "nightly-live: --fail-at accepts slots"
  [[ -z $VWS_DRY_RUN ]] || vws_die_env "nightly-live requires a gated run"
  results=${results:-"${XDG_STATE_HOME:-${HOME}/.local/state}/vws-nightly-live/$(date -u +%Y%m%dT%H%M%SZ)"}
  umask 077
  mkdir -p -- "$results" || vws_die_env "cannot create live results"
  exec {lock_fd}>"$(dirname "$results")/nightly-live.lock" || vws_die_env "cannot open live lock"
  flock -n "$lock_fd" || vws_die_env "another nightly-live run holds the lock"
  rm -f -- "$results"/{.gate,l0-start,l0-end,slots,results}.json
  live_args=(--region "$region" --results "$results" --snippets-storage "$snippets")
  [[ -z $ca ]] || live_args+=(--pve-ca-file "$ca")
  [[ -z $fail_at ]] || live_args+=(--fail-at "$fail_at")
  trap vws_live_end EXIT
  trap 'vws_live_signal 130' INT
  trap 'vws_live_signal 143' TERM
  vws_live_call gate "${live_args[@]}" || exit 3
  live_ready=1
  vws_live_call sweep "${live_args[@]}" --phase l0-start || status=1
  if ((status == 0)); then vws_live_call slots "${live_args[@]}" --need 0 || status=1; fi
  exit "$status"
}
