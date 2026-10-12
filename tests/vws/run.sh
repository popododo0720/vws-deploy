#!/usr/bin/env bash
# Runs every tests/vws/*.test.sh. No cluster, no network: scripts/vws runs against fake kubectl and helm.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
status=0
for test in tests/vws/*.test.sh; do
  echo "== ${test}"
  bash "$test" || status=1
done
exit "$status"
