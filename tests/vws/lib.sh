#!/usr/bin/env bash
# Helpers for tests/vws/*.test.sh. Source it, do not run it.
# A test runs scripts/vws with the fake kubectl and helm first on PATH and reads the call log.

VWS_TEST_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
VWS_BIN="${VWS_TEST_ROOT}/scripts/vws"
FIXTURES="${VWS_TEST_ROOT}/tests/fixtures"
CASES=0
FAILED=0
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export VWS_FAKE_DIR="$FIXTURES"
export VWS_FAKE_LOG="${WORK}/calls.log"

reset_log() { : >"$VWS_FAKE_LOG"; : >"${VWS_FAKE_LOG}.stdin"; }

# run_vws ARGS...: runs scripts/vws with the fixture commands. Sets OUT, ERR and RC.
run_vws() {
  reset_log
  set +e
  # shellcheck disable=SC2034
  OUT=$(PATH="${FIXTURES}/bin:${PATH}" VWS_COMMAND_DIR="${VWS_COMMAND_DIR:-${FIXTURES}/commands}" bash "$VWS_BIN" "$@" 2>"${WORK}/err")
  RC=$?
  set -e
  ERR=$(cat "${WORK}/err")
}

# run_vws_real ARGS...: the real scripts/vws.d, still with the fakes first on PATH.
run_vws_real() {
  VWS_COMMAND_DIR="${VWS_TEST_ROOT}/scripts/vws.d" run_vws "$@"
}

ok() { CASES=$((CASES + 1)); echo "ok   $1"; }
bad() { CASES=$((CASES + 1)); FAILED=$((FAILED + 1)); echo "FAIL $1: $2" >&2; }

# expect_rc NAME WANT
expect_rc() {
  if [[ $RC -eq $2 ]]; then ok "$1"; else bad "$1" "exit ${RC}, want $2 (stderr: ${ERR})"; fi
}

# expect_text NAME HAYSTACK NEEDLE
expect_text() {
  if [[ $2 == *"$3"* ]]; then ok "$1"; else bad "$1" "missing '$3' in: $2"; fi
}

# The verbs that can change a cluster. Kept apart from scripts/vws.d/lib/common.sh on purpose:
# the harness must not trust the code it checks.
MUTATING_VERBS=" apply create delete patch replace label annotate scale rollout set edit drain cordon uncordon taint run expose install upgrade uninstall rollback "

# changing_calls: the logged calls whose verb can change the cluster.
changing_calls() {
  local tool verb rest
  while IFS=$'\t' read -r tool verb rest; do
    [[ -n $tool ]] || continue
    if [[ $MUTATING_VERBS == *" ${verb} "* ]]; then printf '%s %s %s\n' "$tool" "$verb" "$rest"; fi
  done <"$VWS_FAKE_LOG"
}

# assert_no_changes NAME: the log holds no changing call.
assert_no_changes() {
  local found
  found=$(changing_calls)
  if [[ -z $found ]]; then ok "$1"; else bad "$1" "changing call: ${found}"; fi
}

# assert_changes NAME: the log holds at least one changing call (the control for assert_no_changes).
assert_changes() {
  if [[ -n $(changing_calls) ]]; then ok "$1"; else bad "$1" "expected a changing call, found none"; fi
}

finish_tests() {
  echo "${CASES} cases, ${FAILED} failed"
  [[ $FAILED -eq 0 ]]
}
