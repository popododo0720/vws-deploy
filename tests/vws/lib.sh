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

start_identity_stub() {
  mkfifo "${WORK}/identity.ready"
  python3 "${VWS_TEST_ROOT}/tests/vws/identity_stub.py" "$WORK" >"${WORK}/identity.ready" &
  local identity_pid=$!
  # shellcheck disable=SC2064 # keep the server PID fixed for this test process
  trap "kill '$identity_pid'; wait '$identity_pid' 2>/dev/null || true; rm -rf '$WORK'" EXIT
  read -r VWS_API_URL <"${WORK}/identity.ready"
  export VWS_API_URL VWS_LIVE_PVE_URL="$VWS_API_URL" VWS_VERIFY_REGION=pve-test-1
  export VWS_API_TOKEN_FILE="${WORK}/credentials/console" CREDENTIALS_DIRECTORY="${WORK}/credentials"
}

identity_refusals() { # SCENARIO MAP
  local state map=$2
  for state in '{"serverid":"OtherServer826"}' '{}' '{"serverid":"bad-id"}' \
    '{"serverid":"TestServer826","status":403}' '{"serverid":"TestServer826","cluster":"production"}'; do
    echo "$state" >"${WORK}/identity.json"
    echo '{"result":"pass"}' >"${WORK}/refused.json"
    run_vws_real verify scenario "$1" --map "$map" --result "${WORK}/refused.json"
    expect_rc "identity $state is refused" 3
    if [[ ! -s $VWS_FAKE_LOG && ! -e ${WORK}/refused.json && $OUT != *OK* ]]; then
      ok "identity refusal leaves no AWS call or passing result"
    else bad "identity refusal" "$OUT$ERR $(cat "$VWS_FAKE_LOG")"; fi
  done
  echo '{"serverid":"TestServer826"}' >"${WORK}/identity.json"
  jq '.vwsTest.pveServerId = null' "$map" >"${WORK}/missing-id.json"
  run_vws_real verify scenario "$1" --map "${WORK}/missing-id.json"
  expect_rc "a map with no server ID is refused" 3
  [[ ! -s $VWS_FAKE_LOG ]] && ok "no fingerprint makes no AWS call" || bad "no fingerprint" "$(cat "$VWS_FAKE_LOG")"
  jq '.vwsTest |= {kubeSystemUID: .pveServerId}' "$map" >"${WORK}/retired-id.json"
  run_vws_real verify scenario "$1" --map "${WORK}/retired-id.json"
  expect_rc "a Kubernetes-only map is refused" 3
  expect_text "the old map names the retired field" "$ERR" "vwsTest.kubeSystemUID is retired"
  [[ ! -s $VWS_FAKE_LOG ]] && ok "a retired map makes no AWS call" || bad "retired map" "$(cat "$VWS_FAKE_LOG")"
  VWS_VERIFY_REGION='' run_vws_real verify scenario "$1" --map "$map"
  expect_rc "a missing verification region is refused" 3
  [[ ! -s $VWS_FAKE_LOG ]] && ok "no region makes no AWS call" || bad "no region" "$(cat "$VWS_FAKE_LOG")"
  VWS_LIVE_PVE_URL=http://remote.example.invalid run_vws_real verify scenario "$1" --map "$map"
  expect_rc "HTTP beyond loopback is refused" 3
  [[ ! -s $VWS_FAKE_LOG ]] && ok "insecure origin makes no AWS call" || bad "insecure origin" "$(cat "$VWS_FAKE_LOG")"
  VWS_VERIFY_PVE_CA_FILE="${WORK}/absent-ca" run_vws_real verify scenario "$1" --map "$map"
  expect_rc "an unreadable pinned CA is refused" 3
  [[ ! -s $VWS_FAKE_LOG ]] && ok "a CA failure makes no AWS call" || bad "CA failure" "$(cat "$VWS_FAKE_LOG")"
}

# test_signer [NAME]: an ed25519 key ${WORK}/signer-NAME (default a); prints its vwsTest.signers line.
test_signer() {
  local key="${WORK}/signer-${1:-a}"
  [[ -f $key ]] || ssh-keygen -q -t ed25519 -N '' -C "test-${1:-a}" -f "$key" </dev/null
  printf 'vws-test %s' "$(cut -d' ' -f1,2 "${key}.pub")"
}

# sign_result FILE [NAME]: signs a result file in place with test_signer NAME's key.
sign_result() {
  test_signer "${2:-a}" >/dev/null
  python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" sign "$1" "${WORK}/signer-${2:-a}"
}

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
