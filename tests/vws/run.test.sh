#!/usr/bin/env bash
# tests/vws/run.sh: the total covers every suite, so an injected FAIL in any suite is counted and fails the run.
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
