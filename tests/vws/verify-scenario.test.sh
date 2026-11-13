#!/usr/bin/env bash
# vws verify list and vws verify scenario: JSON step evidence, exit codes, injected and real failures with a
# clean teardown, SIGTERM cleanup, the vws-test fingerprint gate, and a result the feature map accepts.
# aws is the fake in tests/fixtures/bin/aws, a small S3 kept on disk.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fake="${WORK}/fake"
mkdir -p "${fake}/aws"
export VWS_FAKE_DIR=$fake
start_identity_stub
map="${WORK}/map.json"
cat >"$map" <<'JSON'
{"vwsTest": {"pveServerId": "TestServer826"}, "features": [
  {"feature": "S3 object put / get (RGW)", "repo": "vws-storage", "issue": "vws-deploy#51", "scenario": "s3-basic"}
]}
JSON
jq --arg k "$(test_signer)" '.vwsTest.signers = [$k]' "$map" >"${map}.new" && mv "${map}.new" "$map"
export VWS_RESULT_SIGNING_KEY="${WORK}/signer-a"
export VWS_VERIFY_ENDPOINT=http://vws.example.invalid:7480 VWS_VERIFY_ACCESS_KEY_ID=AKIDVERIFY
export VWS_VERIFY_SECRET_ACCESS_KEY=sekret-value-must-not-leak VWS_LEAK_CANARY=1
s3="${VWS_FAKE_LOG}.s3"
buckets() { find "$s3" -mindepth 1 2>/dev/null | wc -l; }
steps() { jq -r '"\(.step)=\(.result)"' <<<"$OUT" | paste -sd ' '; }
no_secret() {
  if [[ $OUT$ERR$(cat "$VWS_FAKE_LOG") != *sekret-value* ]]; then ok "$1 never prints the secret key"
  else bad "$1 never prints the secret key" "$OUT$ERR"; fi
}

run_vws_real verify list
expect_rc "list exits 0" 0
expect_text "list names s3-basic with its summary" "$OUT" "OK    s3-basic  S3 bucket create"
run_vws_real -o json verify list
if jq -e '.checks[] | select(.name == "s3-basic")' <<<"$OUT" >/dev/null; then ok "list -o json"; else bad "list -o json" "$OUT"; fi

run_vws_real verify scenario s3-basic --json --map "$map" --result "${WORK}/r1.json"
expect_rc "a passing run exits 0" 0
if jq -se 'length > 0 and all(.[]; (keys == ["cmd","duration_ms","evidence","result","scenario","started_at","step"])
    and .scenario == "s3-basic" and (.duration_ms | type) == "number"
    and (.started_at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$")))' <<<"$OUT" >/dev/null; then
  ok "every line is one step object with the issue's fields"; else bad "step objects" "$OUT"; fi
expect_text "steps run in order and cleanup is idempotent" "$(steps)" \
  "create-bucket=pass put-object=pass get-object=pass object-matches=pass list-objects=pass delete-object=pass delete-bucket=pass cleanup:delete-object=pass cleanup:delete-bucket=pass"
expect_text "cleanup of a deleted bucket reports it" "$OUT" '"evidence":"already gone"'
expect_text "the cmd field shows the call" "$OUT" '"cmd":"vws_aws s3api create-bucket --bucket vws-verify-'
if jq -e '.scenario == "s3-basic" and .result == "pass" and .cluster == "vws-test" and .pveServerId == "TestServer826"
    and .evidence == $e' --arg e "${WORK}/r1.steps.jsonl" "${WORK}/r1.json" >/dev/null; then
  ok "the result file names the cluster and the steps file"; else bad "result file" "$(cat "${WORK}/r1.json")"; fi
if [[ $(cat "${WORK}/r1.steps.jsonl") == "$OUT" ]]; then ok "the steps file holds the printed lines"; else bad "steps file" "$(cat "${WORK}/r1.steps.jsonl")"; fi
expect_text "aws goes to the VWS endpoint" "$(cat "$VWS_FAKE_LOG")" $'\thttp://vws.example.invalid:7480'
if [[ $(buckets) == 0 ]]; then ok "a passing run leaves nothing"; else bad "nothing left" "$(find "$s3")"; fi
no_secret "a passing run"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/r1.json"
expect_rc "the feature map accepts the result" 0
if jq -e '.features[0].lastPass != null' "$map" >/dev/null; then ok "the pass is recorded"; else bad "pass recorded" "$(cat "$map")"; fi
VWS_RESULT_SIGNING_KEY='' run_vws_real verify scenario s3-basic --map "$map" --result "${WORK}/unsigned.json"
expect_rc "a run without a signing key still passes" 0
expect_text "it warns that the result is unsigned" "$ERR" "unsigned (no VWS_RESULT_SIGNING_KEY)"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/unsigned.json"
expect_rc "the feature map refuses an unsigned run" 1
VWS_RESULT_SIGNING_KEY="${WORK}/no-such-key" run_vws_real verify scenario s3-basic --map "$map" --result "${WORK}/nokey.json"
expect_rc "an unreadable signing key is an environment error" 3
if [[ ! -e ${WORK}/nokey.json && ! -e ${WORK}/nokey.json.tmp ]]; then ok "a failed signature leaves no result"; else bad "no result" "$(ls "${WORK}")"; fi

run_vws_real verify scenario s3-basic --map "$map"
expect_rc "a second run passes too" 0
expect_text "text output names each step" "$OUT" "OK    object-matches  "

run_vws_real verify scenario s3-basic --json --map "$map" --fail-at put-object
expect_rc "an injected failure exits 1" 1
expect_text "the injected step fails, later steps skip and cleanup runs" "$(steps)" \
  "create-bucket=pass put-object=fail get-object=skip object-matches=skip list-objects=skip delete-object=skip delete-bucket=skip cleanup:delete-object=pass cleanup:delete-bucket=pass"
expect_text "the injected failure says so" "$OUT" "injected failure (--fail-at put-object); not run"
if [[ $(buckets) == 0 ]]; then ok "an injected failure tears down"; else bad "teardown after injection" "$(find "$s3")"; fi

echo "An error occurred (InternalError) when calling the GetObject operation: boom" >"${fake}/aws/s3api_get-object.fail"
run_vws_real verify scenario s3-basic --json --map "$map" --result "${WORK}/r2.json"
expect_rc "a failing call exits 1" 1
expect_text "the error is the evidence" "$OUT" '"step":"get-object","cmd"'
expect_text "the error text is kept" "$OUT" "exit 254: An error occurred (InternalError)"
expect_text "cleanup deletes the object and bucket left behind" "$(steps)" "cleanup:delete-object=pass cleanup:delete-bucket=pass"
if [[ $(buckets) == 0 ]]; then ok "a real failure tears down"; else bad "teardown after failure" "$(find "$s3")"; fi
if jq -e '.result == "fail"' "${WORK}/r2.json" >/dev/null; then ok "the result file says fail"; else bad "fail result" "$(cat "${WORK}/r2.json")"; fi
rm "${fake}/aws/s3api_get-object.fail"

echo "An error occurred (AccessDenied) when calling the DeleteBucket operation: no" >"${fake}/aws/s3api_delete-bucket.fail"
run_vws_real verify scenario s3-basic --json --map "$map"
expect_rc "a failed cleanup exits 1" 1
expect_text "the cleanup failure is reported" "$(steps)" "delete-bucket=fail cleanup:delete-object=pass cleanup:delete-bucket=fail"
rm "${fake}/aws/s3api_delete-bucket.fail"
rm -rf "$s3"

echo 5 >"${fake}/aws/s3api_get-object.sleep"
reset_log
cp "${WORK}/r1.json" "${WORK}/term.json"
(PATH="${FIXTURES}/bin:${PATH}" exec bash "$VWS_BIN" verify scenario s3-basic --json --map "$map" --result "${WORK}/term.json" \
  >"${WORK}/term.out" 2>&1) &
pid=$!
for _ in $(seq 50); do grep -q get-object "$VWS_FAKE_LOG" 2>/dev/null && break; sleep 0.1; done
kill -TERM "$pid"
RC=0; wait "$pid" || RC=$?
OUT=$(cat "${WORK}/term.out") ERR=''
expect_rc "SIGTERM mid-step exits 143" 143
expect_text "SIGTERM runs the cleanup" "$(jq -r 'select(.step | startswith("cleanup:")) | "\(.step)=\(.result)"' <<<"$OUT" 2>/dev/null | paste -sd ' ')" \
  "cleanup:delete-object=pass cleanup:delete-bucket=pass"
if [[ $(buckets) == 0 ]]; then ok "SIGTERM tears down"; else bad "teardown after SIGTERM" "$(find "$s3")"; fi
expect_text "the interrupted step is a fail" "$OUT" '"step":"get-object"'
expect_text "it says why" "$OUT" "interrupted by SIGTERM"
if jq -e '.result == "fail"' "${WORK}/term.json" >/dev/null; then ok "an interrupted run replaces an old pass with a fail"
else bad "interrupted result" "$(cat "${WORK}/term.json")"; fi
rm "${fake}/aws/s3api_get-object.sleep"

# term_when OP ARGS...: run the scenario, send SIGTERM once the fake logs OP, and set OUT, RC and SECS.
term_when() {
  local op=$1 pid t0
  shift
  reset_log
  t0=$(date +%s)
  (PATH="${FIXTURES}/bin:${PATH}" exec bash "$VWS_BIN" verify scenario s3-basic --json --map "$map" "$@" \
    >"${WORK}/term.out" 2>&1) &
  pid=$!
  for _ in $(seq 100); do grep -q "$op" "$VWS_FAKE_LOG" 2>/dev/null && break; sleep 0.1; done
  kill -TERM "$pid"
  RC=0; wait "$pid" || RC=$?
  SECS=$(($(date +%s) - t0)) OUT=$(cat "${WORK}/term.out") ERR=''
}
echo 2 >"${fake}/aws/s3api_delete-object.sleep"
term_when s3api_delete-object --fail-at get-object
expect_rc "SIGTERM during a cleanup step exits 143" 143
expect_text "the interrupted cleanup step is recorded" "$OUT" "interrupted by SIGTERM"
expect_text "the interrupted cleanup step runs again, then the rest" "$(steps)" \
  "cleanup:delete-object=fail cleanup:delete-object=pass cleanup:delete-bucket=pass"
if [[ $(buckets) == 0 ]]; then ok "SIGTERM during cleanup still tears down"; else bad "teardown after SIGTERM in cleanup" "$(find "$s3")"; fi
echo 30 >"${fake}/aws/s3api_delete-object.sleep"
VWS_VERIFY_SIGNAL_GRACE=2 term_when s3api_delete-object --fail-at get-object
expect_rc "a cleanup past the signal grace still exits 143" 143
if ((SECS < 10)); then ok "the cleanup after a signal ends within its grace (${SECS}s)"; else bad "grace bound" "took ${SECS}s"; fi
expect_text "what the grace could not clean is named by run ID" "$OUT" "the signal grace is spent; not run; find what is left by vws-verify-"
rm -f "${fake}/aws/s3api_delete-object.sleep"; rm -rf "$s3"

cp "${WORK}/r1.json" "${WORK}/stale.json"
echo '{"serverid":"OtherServer826"}' >"${WORK}/identity.json"
run_vws_real verify scenario s3-basic --map "$map" --result "${WORK}/stale.json"
if [[ ! -e ${WORK}/stale.json ]]; then ok "a refused run leaves no old result behind"; else bad "stale result" "$(cat "${WORK}/stale.json")"; fi
echo '{"serverid":"TestServer826"}' >"${WORK}/identity.json"

run_vws_real nightly --map "$map" --results "${WORK}/nightly/run1"
expect_rc "nightly runs the real scenario against its own map" 0
if jq -e '.result == "pass" and .pveServerId == "TestServer826"' "${WORK}/nightly/run1/s3-basic.json" >/dev/null; then
  ok "nightly records the scenario result"; else bad "nightly result" "$OUT $(cat "${WORK}/nightly/run1/s3-basic.log")"; fi

echo '{"serverid":"OtherServer826"}' >"${WORK}/identity.json"
run_vws_real verify scenario s3-basic --map "$map"
expect_rc "another cluster is refused" 3
expect_text "the refusal names both server IDs" "$ERR" "OtherServer826 is not vws-test (TestServer826); nothing ran"
if ! grep -q '^aws' "$VWS_FAKE_LOG"; then ok "a refused cluster gets no aws call"; else bad "no aws call" "$(cat "$VWS_FAKE_LOG")"; fi
echo '{"serverid":"TestServer826"}' >"${WORK}/identity.json"
jq '.vwsTest.pveServerId = null' "$map" >"${WORK}/nofp.json"
run_vws_real verify scenario s3-basic --map "${WORK}/nofp.json"; expect_rc "a map without a fingerprint is refused" 3
identity_refusals s3-basic "$map"

run_vws_real verify scenario nope --map "$map"; expect_rc "an unknown scenario is a usage error" 2
expect_text "the error points at list" "$ERR" "unknown scenario nope; try: scripts/vws verify list"
run_vws_real verify scenario ../x --map "$map"; expect_rc "a path as name is a usage error" 2
run_vws_real verify scenario; expect_rc "no name is a usage error" 2
run_vws_real verify scenario s3-basic --bogus; expect_rc "an unknown flag is a usage error" 2
run_vws_real verify scenario s3-basic --map "$map" --fail-at no-such-step
expect_rc "a --fail-at step that never ran fails" 1
expect_text "it names the step" "$OUT" "scenario s3-basic has no step no-such-step"
VWS_VERIFY_SECRET_ACCESS_KEY='' run_vws_real verify scenario s3-basic --map "$map"
expect_rc "a missing credential is an environment error" 3
expect_text "it names the variable, not a value" "$ERR" "needs VWS_VERIFY_SECRET_ACCESS_KEY; nothing ran"
aws_host="s3.us-east-1.ama""zonaws.com"
VWS_VERIFY_ENDPOINT="https://${aws_host}" run_vws_real verify scenario s3-basic --map "$map"
expect_rc "an AWS-owned endpoint is refused" 2
expect_text "it says why" "$ERR" "is AWS-owned, not VWS; refused"
VWS_VERIFY_ENDPOINT="https://${aws_host}.:443/" run_vws_real verify scenario s3-basic --map "$map"
expect_rc "an AWS-owned endpoint with a trailing dot is refused" 2
VWS_VERIFY_ENDPOINT='ftp://x' run_vws_real verify scenario s3-basic --map "$map"
expect_rc "a non-http endpoint is refused" 2
if ! grep -q '^aws' "$VWS_FAKE_LOG"; then ok "refused input gets no aws call"; else bad "no aws call" "$(cat "$VWS_FAKE_LOG")"; fi

finish_tests
