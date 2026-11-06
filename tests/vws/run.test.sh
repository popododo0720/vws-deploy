#!/usr/bin/env bash
# tests/vws/run.sh: the total covers every suite, so an injected FAIL in any suite is counted and fails the run.
# A suite that prints no summary and a run that finds no suites fail too.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

RUNNER="${VWS_TEST_ROOT}/tests/vws/run.sh"
suites="${WORK}/suites"
mkdir -p "$suites"
# suite NAME BODY: a throwaway suite that uses the real lib.sh helpers.
suite() {
  printf '#!/usr/bin/env bash\nset -euo pipefail\nsource %q\n%s\n' "${VWS_TEST_ROOT}/tests/vws/lib.sh" "$2" >"${suites}/$1.test.sh"
}
run_runner() {
  set +e
  OUT=$(VWS_TEST_DIR="$suites" bash "$RUNNER" 2>&1)
  RC=$?
  set -e
  ERR=$OUT
}

suite a-pass 'ok one; ok two; finish_tests'
suite z-pass 'ok three; finish_tests'
run_runner
expect_rc "all suites pass: exit 0" 0
expect_text "the total adds every suite" "$OUT" "total: 3 cases, 0 failed in 2 suites"

# Mutation: one FAIL in the first suite. The last suite still prints "0 failed".
suite a-pass 'ok one; bad injected "mutation"; finish_tests'
run_runner
expect_rc "an injected FAIL in an early suite: exit 1" 1
expect_text "the injected FAIL is in the total" "$OUT" "total: 3 cases, 1 failed in 2 suites"
expect_text "the failing suite is named" "$OUT" "failed suite: ${suites}/a-pass.test.sh (1 failed, exit 1)"
if [[ $(tail -n 1 <<<"$OUT") == "total: 3 cases, 1 failed in 2 suites" ]]; then ok "the last line is the total"; else bad "the last line is the total" "last line: $(tail -n 1 <<<"$OUT")"; fi

# A FAIL line the suite's own counter missed still counts.
suite a-pass 'ok one; echo "FAIL printed by hand: x" >&2; finish_tests'
run_runner
expect_rc "a FAIL line not in the suite's count: exit 1" 1
expect_text "a FAIL line not in the suite's count is in the total" "$OUT" "1 failed in 2 suites"

# A suite that dies before finish_tests counts as failed.
suite a-pass 'ok one; exit 3'
run_runner
expect_rc "a suite that dies before its summary: exit 1" 1
expect_text "a suite that dies before its summary counts" "$OUT" "1 failed in 2 suites"

# A suite that exits 0 without a summary is named and counts as failed.
suite a-pass 'ok one; ok two'
run_runner
expect_rc "a suite that exits 0 without its summary: exit 1" 1
expect_text "a suite that exits 0 without its summary is named" "$OUT" "no summary: ${suites}/a-pass.test.sh"
expect_text "a suite that exits 0 without its summary counts" "$OUT" "total: 1 cases, 1 failed in 2 suites"

# No suites at all is a failure, so a typo in VWS_TEST_DIR cannot pass.
empty="${WORK}/empty"
mkdir -p "$empty"
set +e
OUT=$(VWS_TEST_DIR="$empty" bash "$RUNNER" 2>&1)
RC=$?
set -e
ERR=$OUT
expect_rc "no suites found: exit 1" 1
expect_text "no suites found names the directory" "$OUT" "no suites found in ${empty}"
expect_text "no suites found has no total of zero failures" "$OUT" "total: 0 cases, 1 failed in 0 suites"

# Suites run at the same time: a waits for the file z writes, so a passes only when both run at once.
handshake="${WORK}/handshake"
suite a-pass "for _ in \$(seq 50); do [[ -f $(printf %q "$handshake") ]] && break; sleep 0.1; done; [[ -f $(printf %q "$handshake") ]] && ok saw-z || bad saw-z 'z did not run alongside a'; finish_tests"
suite z-pass ": >$(printf %q "$handshake"); ok wrote; finish_tests"
OUT=$(VWS_TEST_JOBS=2 VWS_TEST_DIR="$suites" bash "$RUNNER" 2>&1) && RC=0 || RC=$?
ERR=$OUT
expect_rc "two jobs run two suites at once: exit 0" 0
expect_text "two jobs run two suites at once: total" "$OUT" "total: 2 cases, 0 failed in 2 suites"
if [[ $(grep '\.test\.sh$' <<<"$OUT") == "== ${suites}/a-pass.test.sh"$'\n'"== ${suites}/z-pass.test.sh" ]]; then
  ok "output stays in suite name order though z finishes first"
else
  bad "output stays in suite name order though z finishes first" "got: $(grep '\.test\.sh$' <<<"$OUT")"
fi
# Control: one job runs them one after another, so a times out before z starts.
rm -f "$handshake"
OUT=$(VWS_TEST_JOBS=1 VWS_TEST_DIR="$suites" bash "$RUNNER" 2>&1) && RC=0 || RC=$?
ERR=$OUT
expect_rc "control: one job runs suites one at a time: exit 1" 1
expect_text "control: one job runs suites one at a time: a fails" "$OUT" "failed suite: ${suites}/a-pass.test.sh"
rm -f "$handshake"
suite a-pass 'ok one; finish_tests'
suite z-pass 'ok three; finish_tests'

# VWS_TEST_JOBS must be a positive integer; anything else stops before any suite runs.
for bad_jobs in 0 -1 x 2x 01; do
  OUT=$(VWS_TEST_JOBS="$bad_jobs" VWS_TEST_DIR="$suites" bash "$RUNNER" 2>&1) && RC=0 || RC=$?
  ERR=$OUT
  expect_rc "VWS_TEST_JOBS='${bad_jobs}' is refused" 2
  expect_text "VWS_TEST_JOBS='${bad_jobs}' is named" "$OUT" "VWS_TEST_JOBS must be a positive integer"
done

# A killed runner stops the suites it started, and their children.
hung="${WORK}/hung"
mkdir -p "$hung"
for i in 1 2 3; do
  printf '#!/usr/bin/env bash\n: >%q\nsleep 60\n' "${hung}/started-${i}" >"${hung}/s${i}.test.sh"
done
VWS_TEST_JOBS=3 VWS_TEST_DIR="$hung" bash "$RUNNER" >/dev/null 2>&1 &
runner=$!
for _ in $(seq 50); do [[ -f ${hung}/started-3 ]] && break; sleep 0.1; done
kill "$runner"
wait "$runner" && RC=0 || RC=$?
ERR="kill test"
expect_rc "a killed runner exits 143" 143
sleep 0.5
if pgrep -f "${hung}/s" >/dev/null; then bad "a killed runner leaves no suite running" "$(pgrep -af "${hung}/s")"; else ok "a killed runner leaves no suite running"; fi

# Control: the runner from before this fix prints only per-suite lines; its last count line says 0 failed.
old="${WORK}/old"
mkdir -p "${old}/tests/vws"
printf '#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "${BASH_SOURCE[0]}")/../.."\nstatus=0\nfor test in %q/*.test.sh; do\n  echo "== ${test}"\n  bash "$test" || status=1\ndone\nexit "$status"\n' "$suites" >"${old}/tests/vws/run.sh"
suite a-pass 'ok one; bad injected "mutation"; finish_tests'
set +e
OLD=$(bash "${old}/tests/vws/run.sh" 2>/dev/null)
set -e
if [[ $(grep -E 'cases, [0-9]+ failed' <<<"$OLD" | tail -n 1) == "1 cases, 0 failed" ]]; then
  ok "control: the old runner's last summary hides the FAIL"
else
  bad "control: the old runner's last summary hides the FAIL" "got: ${OLD}"
fi
finish_tests
