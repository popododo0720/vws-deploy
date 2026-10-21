#!/usr/bin/env bash
# Supervisor loop for the factory orchestrator on the shared box (vws-deploy#333).
# One loop per box: it holds loop.lock for its lifetime. Each tick runs orchestrate.py in a fresh
# process, so a crash ends only that tick. Failed ticks back off exponentially (capped); after each
# failed tick orchestrate.py --notify-failure runs, and at ORCHESTRATE_FAILURE_THRESHOLD failures in a
# row it posts one failure comment on the status thread (draft PR) per streak. Start it with
# orchestrate-ensure.sh, not by hand.
#
# Environment (tests override these): ORCHESTRATE_STATE_DIR, ORCHESTRATE_CONFIG_DIR,
# ORCHESTRATE_INTERVAL (default 600 s), ORCHESTRATE_BACKOFF_BASE (default 60 s),
# ORCHESTRATE_BACKOFF_MAX (default 1800 s), ORCHESTRATE_LOOP_ITERATIONS (default 0 = forever),
# ORCHESTRATE_PY (default: orchestrate.py next to this directory).
set -uo pipefail
unset GH_TOKEN GITHUB_TOKEN

here=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state=${ORCHESTRATE_STATE_DIR:-$HOME/.local/state/vws-orchestrate}
config=${ORCHESTRATE_CONFIG_DIR:-$HOME/.config/vws-orchestrate}
interval=${ORCHESTRATE_INTERVAL:-600}
base=${ORCHESTRATE_BACKOFF_BASE:-60}
cap=${ORCHESTRATE_BACKOFF_MAX:-1800}
iterations=${ORCHESTRATE_LOOP_ITERATIONS:-0}
py=${ORCHESTRATE_PY:-$here/../orchestrate.py}
loop_log=$state/log/loop.log

umask 077
mkdir -p "$state/log"
exec 9>"$state/loop.lock"
if ! flock -n 9; then
  echo "orchestrate-loop: another loop holds $state/loop.lock" >&2
  exit 0
fi
echo $$ >"$state/loop.pid"

# Stopping the loop also stops a running tick, so `kill $(cat loop.pid)` halts every write at once.
child=""
stop() {
  if [[ -n $child ]]; then
    kill -TERM -- "-$child" 2>/dev/null # the whole process group: python and any gh it started
    wait "$child" 2>/dev/null
  fi
  exit 143
}
trap stop TERM INT HUP

# run CMD...: run in the background in its own process group (setsid), without the loop.lock fd
# (a leftover child must not hold the lock), and wait so the trap fires at once. Sets rc.
run() {
  setsid "$@" 9>&- &
  child=$!
  wait "$child"
  rc=$?
  child=""
}

rotate_loop_log() {
  local size
  size=$(stat -c %s "$loop_log" 2>/dev/null || echo 0)
  if ((size > 1048576)); then
    mv -f "$loop_log" "$loop_log.1"
  fi
}

failures=0
streak_start=0
n=0
while :; do
  n=$((n + 1))
  rotate_loop_log
  # The interval in the env file wins over the environment, so it can change without a restart.
  if [[ -f $config/env ]]; then
    v=$(sed -n 's/^ORCHESTRATE_INTERVAL=\([0-9][0-9]*\)$/\1/p' "$config/env" | tail -n 1)
    [[ -n $v ]] && interval=$v
  fi
  run python3 "$py" --once >>"$loop_log" 2>&1
  if ((rc == 0)); then
    failures=0
    streak_start=0
    delay=$interval
  else
    failures=$((failures + 1))
    ((streak_start == 0)) && streak_start=$(date +%s)
    echo "$(date -u +%FT%TZ) tick failed rc=$rc failures=$failures" >>"$loop_log"
    last=$(grep -v '^{' "$loop_log" | tail -n 1 | cut -c1-300)
    tick_rc=$rc
    run python3 "$py" --notify-failure --failures "$failures" --exit-code "$tick_rc" --streak-start "$streak_start" \
      --last-error "$last" >>"$loop_log" 2>&1
    delay=$((base * (1 << (failures > 6 ? 6 : failures - 1))))
    ((delay > cap)) && delay=$cap
  fi
  if ((iterations > 0 && n >= iterations)); then
    break
  fi
  run sleep "$delay"
done
