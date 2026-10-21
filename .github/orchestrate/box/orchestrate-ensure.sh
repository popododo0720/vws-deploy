#!/usr/bin/env bash
# Start the orchestrator loop if it is not running (vws-deploy#333). Idempotent and locked: safe to
# run any number of times, by any agent, after a box restart. Prints one status line.
#   ~/.local/opt/vws-orchestrate/current/.github/orchestrate/box/orchestrate-ensure.sh
set -euo pipefail
unset GH_TOKEN GITHUB_TOKEN

here=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state=${ORCHESTRATE_STATE_DIR:-$HOME/.local/state/vws-orchestrate}
umask 077
mkdir -p "$state/log"

exec 8>"$state/ensure.lock"
flock -n 8 || { echo "orchestrate-ensure: another ensure is running"; exit 0; }

# The loop holds loop.lock for its lifetime; if we can take it, no loop is running.
if ! flock -n "$state/loop.lock" true; then
  echo "orchestrate-ensure: running (pid $(cat "$state/loop.pid" 2>/dev/null || echo '?'))"
  exit 0
fi
setsid nohup "$here/orchestrate-loop.sh" >>"$state/log/loop.log" 2>&1 </dev/null 8>&- &
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.2
  if ! flock -n "$state/loop.lock" true; then
    echo "orchestrate-ensure: started (pid $(cat "$state/loop.pid" 2>/dev/null || echo '?'))"
    exit 0
  fi
done
echo "orchestrate-ensure: loop did not start; see $state/log/loop.log" >&2
exit 1
