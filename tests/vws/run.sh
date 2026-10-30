#!/usr/bin/env bash
# Runs every tests/vws/*.test.sh (or VWS_TEST_DIR/*.test.sh). No cluster, no network: scripts/vws runs against
# fake kubectl and helm.
# Each suite prints its own "N cases, M failed" line. This runner adds them up over every suite and prints one
# total, so a FAIL in an early suite is not hidden behind the last suite's "0 failed". A suite's failures are
# the larger of its summary count and its FAIL lines; a suite that exits without a summary counts as one, and
# one that exits 0 without a summary is named. No suites at all is a failure, so a wrong VWS_TEST_DIR cannot
# pass. Any failure makes the exit code 1.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
dir=${VWS_TEST_DIR:-tests/vws}
log=$(mktemp)
trap 'rm -f "$log"' EXIT
total_cases=0 total_failed=0 suites=0 bad_suites=() missing_summary=()
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
  else
    missing_summary+=("$test")
    failed=1
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
if ((suites == 0)); then
  echo "no suites found in ${dir}" >&2
  total_failed=1
fi
for s in "${missing_summary[@]}"; do echo "no summary: ${s}"; done
for s in "${bad_suites[@]}"; do echo "failed suite: ${s}"; done
echo "total: ${total_cases} cases, ${total_failed} failed in ${suites} suites"
((total_failed == 0))
