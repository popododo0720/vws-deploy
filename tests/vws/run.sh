#!/usr/bin/env bash
# Runs every tests/vws/*.test.sh (or VWS_TEST_DIR/*.test.sh). No cluster, no network: scripts/vws runs against
# fake kubectl and helm.
# Each suite prints its own "N cases, M failed" line. This runner adds them up over every suite and prints one
# total, so a FAIL in an early suite is not hidden behind the last suite's "0 failed". A suite's failures are
# the larger of its summary count and its FAIL lines; a suite that exits non-zero without counting a failure
# (it died before finish_tests) counts as one. Any failure makes the exit code 1.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
dir=${VWS_TEST_DIR:-tests/vws}
log=$(mktemp)
trap 'rm -rf "$log"' EXIT
total_cases=0 total_failed=0 suites=0 bad_suites=()
for test in "$dir"/*.test.sh; do
  [[ -f $test ]] || continue
  echo "== ${test}"
  rc=0
  bash "$test" >"$log" 2>&1 || rc=$?
  cat "$log"
  summary=$(grep -E '^[0-9]+ cases, [0-9]+ failed$' "$log" | tail -n 1 || true)
  cases=0 failed=0
  if [[ -n $summary ]]; then
    read -r cases _ failed _ <<<"$summary"
  fi
  fails=$(grep -c '^FAIL ' "$log" || true)
  ((fails > failed)) && failed=$fails
  ((rc != 0 && failed == 0)) && failed=1
  if ((failed > 0)); then
    bad_suites+=("${test} (${failed} failed, exit ${rc})")
  fi
  suites=$((suites + 1))
  total_cases=$((total_cases + cases))
  total_failed=$((total_failed + failed))
done
echo "== total"
for s in "${bad_suites[@]}"; do echo "failed suite: ${s}"; done
echo "total: ${total_cases} cases, ${total_failed} failed in ${suites} suites"
((total_failed == 0))
