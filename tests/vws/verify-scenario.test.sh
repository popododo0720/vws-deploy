#!/usr/bin/env bash
# vws verify list and vws verify scenario: JSON step evidence, exit codes, injected and real failures with a
# clean teardown, SIGTERM cleanup, the vws-test fingerprint gate, and a result the feature map accepts.
# The scenario is ec2-volume-reboot; aws is the fake in tests/fixtures/bin/aws, a small EC2 kept on disk.
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
  {"feature": "EC2 RunInstances / TerminateInstances", "repo": "vws-console-api", "issue": null, "scenario": "ec2-volume-reboot"}
]}
JSON
jq --arg k "$(test_signer)" '.vwsTest.signers = [$k]' "$map" >"${map}.new" && mv "${map}.new" "$map"
export VWS_RESULT_SIGNING_KEY="${WORK}/signer-a"
export VWS_VERIFY_EC2_ENDPOINT=http://ec2.vws.example.invalid:8080 VWS_VERIFY_ACCESS_KEY_ID=AKIDVERIFY
export VWS_VERIFY_SECRET_ACCESS_KEY=sekret-value-must-not-leak VWS_LEAK_CANARY=1
export VWS_VERIFY_IMAGE_ID=ami-00000001 VWS_VERIFY_INSTANCE_TYPE=vws.small VWS_VERIFY_POLL=0 VWS_VERIFY_REBOOT_SETTLE=0
ec2="${VWS_FAKE_LOG}.ec2"
# left: instances not terminated or shutting down, and volumes, by the fake's files
left() {
  [[ ! -d $ec2 ]] || find "$ec2" -name '*.json' -exec jq -r 'select(.State != {Name: "terminated"} and .State != {Name: "shutting-down"}) | .InstanceId // .VolumeId' {} + |
    paste -sd ' '
}
scenario='ec2-volume-reboot'
all_pass="run-instance=pass instance-running=pass create-volume=pass volume-available=pass attach-volume=pass volume-in-use=pass reboot-instance=pass reboot-settle=pass running-after-reboot=pass attached-after-reboot=pass detach-volume=pass volume-detached=pass delete-volume=pass terminate-instance=pass instance-terminated=pass cleanup:terminate-instance=pass cleanup:delete-volume=pass"
steps() { jq -r '"\(.step)=\(.result)"' <<<"$OUT" | paste -sd ' '; }
no_secret() {
  if [[ $OUT$ERR$(cat "$VWS_FAKE_LOG") != *sekret-value* ]]; then ok "$1 never prints the secret key"
  else bad "$1 never prints the secret key" "$OUT$ERR"; fi
}

run_vws_real verify list
expect_rc "list exits 0" 0
expect_text "list names ec2-volume-reboot with its summary" "$OUT" "OK    ec2-volume-reboot  EC2 instance run, EBS volume"
run_vws_real -o json verify list
if jq -e '.checks[] | select(.name == "ec2-volume-reboot")' <<<"$OUT" >/dev/null; then ok "list -o json"; else bad "list -o json" "$OUT"; fi
if jq -e '[.checks[].name] | index("s3-basic") == null' <<<"$OUT" >/dev/null; then ok "list has no s3-basic"; else bad "no s3-basic" "$OUT"; fi

run_vws_real verify scenario "$scenario" --json --map "$map" --result "${WORK}/r1.json"
expect_rc "a passing run exits 0" 0
if jq -se 'length > 0 and all(.[]; (keys == ["cmd","duration_ms","evidence","result","scenario","started_at","step"])
    and .scenario == "ec2-volume-reboot" and (.duration_ms | type) == "number"
    and (.started_at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$")))' <<<"$OUT" >/dev/null; then
  ok "every line is one step object with the issue's fields"; else bad "step objects" "$OUT"; fi
expect_text "steps run in order and cleanup is idempotent" "$(steps)" "$all_pass"
expect_text "cleanup of a deleted resource reports it" "$OUT" '"evidence":"already gone"'
expect_text "the cmd field shows the call" "$OUT" '"cmd":"vws_ec2_run"'
if jq -e '.scenario == "ec2-volume-reboot" and .result == "pass" and .cluster == "vws-test" and .pveServerId == "TestServer826"
    and .evidence == $e' --arg e "${WORK}/r1.steps.jsonl" "${WORK}/r1.json" >/dev/null; then
  ok "the result file names the cluster and the steps file"; else bad "result file" "$(cat "${WORK}/r1.json")"; fi
if [[ $(cat "${WORK}/r1.steps.jsonl") == "$OUT" ]]; then ok "the steps file holds the printed lines"; else bad "steps file" "$(cat "${WORK}/r1.steps.jsonl")"; fi
expect_text "aws goes to the VWS endpoint" "$(cat "$VWS_FAKE_LOG")" $'\thttp://ec2.vws.example.invalid:8080'
if [[ -z $(left) ]]; then ok "a passing run leaves nothing"; else bad "nothing left" "$(left)"; fi
no_secret "a passing run"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/r1.json"
expect_rc "the feature map accepts the result" 0
if jq -e '.features[0].lastPass != null' "$map" >/dev/null; then ok "the pass is recorded"; else bad "pass recorded" "$(cat "$map")"; fi
rm -rf "$ec2"
VWS_RESULT_SIGNING_KEY='' run_vws_real verify scenario "$scenario" --map "$map" --result "${WORK}/unsigned.json"
expect_rc "a run without a signing key still passes" 0
expect_text "it warns that the result is unsigned" "$ERR" "unsigned (no VWS_RESULT_SIGNING_KEY)"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/unsigned.json"
expect_rc "the feature map refuses an unsigned run" 1
rm -rf "$ec2"
VWS_RESULT_SIGNING_KEY="${WORK}/no-such-key" run_vws_real verify scenario "$scenario" --map "$map" --result "${WORK}/nokey.json"
expect_rc "an unreadable signing key is an environment error" 3
if [[ ! -e ${WORK}/nokey.json && ! -e ${WORK}/nokey.json.tmp ]]; then ok "a failed signature leaves no result"; else bad "no result" "$(ls "${WORK}")"; fi
rm -rf "$ec2"

run_vws_real verify scenario "$scenario" --map "$map"
expect_rc "a second run passes too" 0
expect_text "text output names each step" "$OUT" "OK    reboot-instance  "
rm -rf "$ec2"

run_vws_real verify scenario "$scenario" --json --map "$map" --fail-at reboot-instance
expect_rc "an injected failure exits 1" 1
expect_text "the injected step fails, later steps skip and cleanup runs" "$(steps)" \
  "run-instance=pass instance-running=pass create-volume=pass volume-available=pass attach-volume=pass volume-in-use=pass reboot-instance=fail reboot-settle=skip running-after-reboot=skip attached-after-reboot=skip detach-volume=skip volume-detached=skip delete-volume=skip terminate-instance=skip instance-terminated=skip cleanup:terminate-instance=pass cleanup:delete-volume=pass"
expect_text "the injected failure says so" "$OUT" "injected failure (--fail-at reboot-instance); not run"
if [[ -z $(left) ]]; then ok "an injected failure tears down"; else bad "teardown after injection" "$(left)"; fi
rm -rf "$ec2"

echo "An error occurred (InternalError) when calling the DescribeVolumes operation: boom" >"${fake}/aws/ec2_describe-volumes.fail"
run_vws_real verify scenario "$scenario" --json --map "$map" --result "${WORK}/r2.json"
expect_rc "a failing call exits 1" 1
expect_text "the error text is kept" "$OUT" "exit 1: An error occurred (InternalError)"
if jq -e '.result == "fail"' "${WORK}/r2.json" >/dev/null; then ok "the result file says fail"; else bad "fail result" "$(cat "${WORK}/r2.json")"; fi
rm "${fake}/aws/ec2_describe-volumes.fail"
rm -rf "$ec2"

echo "An error occurred (UnauthorizedOperation) when calling the TerminateInstances operation: no" >"${fake}/aws/ec2_terminate-instances.fail"
run_vws_real verify scenario "$scenario" --json --map "$map"
expect_rc "a failed cleanup exits 1" 1
expect_text "the cleanup failure is reported" "$(steps)" "cleanup:terminate-instance=fail"
rm "${fake}/aws/ec2_terminate-instances.fail"
rm -rf "$ec2"

echo 5 >"${fake}/aws/ec2_reboot-instances.sleep"
reset_log
cp "${WORK}/r1.json" "${WORK}/term.json"
(PATH="${FIXTURES}/bin:${PATH}" exec bash "$VWS_BIN" verify scenario "$scenario" --json --map "$map" --result "${WORK}/term.json" \
  >"${WORK}/term.out" 2>&1) &
pid=$!
for _ in $(seq 100); do grep -q reboot-instances "$VWS_FAKE_LOG" 2>/dev/null && break; sleep 0.1; done
kill -TERM "$pid"
RC=0; wait "$pid" || RC=$?
OUT=$(cat "${WORK}/term.out") ERR=''
expect_rc "SIGTERM mid-step exits 143" 143
expect_text "SIGTERM runs the cleanup" "$(jq -r 'select(.step | startswith("cleanup:")) | "\(.step)=\(.result)"' <<<"$OUT" 2>/dev/null | paste -sd ' ')" \
  "cleanup:terminate-instance=pass cleanup:delete-volume=pass"
if [[ -z $(left) ]]; then ok "SIGTERM tears down"; else bad "teardown after SIGTERM" "$(left)"; fi
expect_text "the interrupted step is a fail" "$OUT" '"step":"reboot-instance"'
expect_text "it says why" "$OUT" "interrupted by SIGTERM"
if jq -e '.result == "fail"' "${WORK}/term.json" >/dev/null; then ok "an interrupted run replaces an old pass with a fail"
else bad "interrupted result" "$(cat "${WORK}/term.json")"; fi
rm "${fake}/aws/ec2_reboot-instances.sleep"
rm -rf "$ec2"

# term_when OP ARGS...: run the scenario, send SIGTERM once the fake logs OP, and set OUT, RC and SECS.
term_when() {
  local op=$1 pid t0
  shift
  reset_log
  t0=$(date +%s)
  (PATH="${FIXTURES}/bin:${PATH}" exec bash "$VWS_BIN" verify scenario "$scenario" --json --map "$map" "$@" \
    >"${WORK}/term.out" 2>&1) &
  pid=$!
  for _ in $(seq 100); do grep -q "$op" "$VWS_FAKE_LOG" 2>/dev/null && break; sleep 0.1; done
  kill -TERM "$pid"
  RC=0; wait "$pid" || RC=$?
  SECS=$(($(date +%s) - t0)) OUT=$(cat "${WORK}/term.out") ERR=''
}
echo 30 >"${fake}/aws/ec2_delete-volume.sleep"
VWS_VERIFY_SIGNAL_GRACE=2 term_when ec2_delete-volume --fail-at reboot-instance
expect_rc "a cleanup past the signal grace still exits 143" 143
if ((SECS < 12)); then ok "the cleanup after a signal ends within its grace (${SECS}s)"; else bad "grace bound" "took ${SECS}s"; fi
expect_text "the interrupted cleanup step is recorded" "$OUT" "interrupted by SIGTERM"
rm -f "${fake}/aws/ec2_delete-volume.sleep"; rm -rf "$ec2"

cp "${WORK}/r1.json" "${WORK}/stale.json"
echo '{"serverid":"OtherServer826"}' >"${WORK}/identity.json"
run_vws_real verify scenario "$scenario" --map "$map" --result "${WORK}/stale.json"
if [[ ! -e ${WORK}/stale.json ]]; then ok "a refused run leaves no old result behind"; else bad "stale result" "$(cat "${WORK}/stale.json")"; fi
echo '{"serverid":"TestServer826"}' >"${WORK}/identity.json"

run_vws_real nightly --map "$map" --results "${WORK}/nightly/run1"
expect_rc "nightly runs the real scenario against its own map" 0
if jq -e '.result == "pass" and .pveServerId == "TestServer826"' "${WORK}/nightly/run1/ec2-volume-reboot.json" >/dev/null; then
  ok "nightly records the scenario result"; else bad "nightly result" "$OUT $(cat "${WORK}/nightly/run1/ec2-volume-reboot.log")"; fi
rm -rf "$ec2"

echo '{"serverid":"OtherServer826"}' >"${WORK}/identity.json"
run_vws_real verify scenario "$scenario" --map "$map"
expect_rc "another cluster is refused" 3
expect_text "the refusal names both server IDs" "$ERR" "OtherServer826 is not vws-test (TestServer826); nothing ran"
if ! grep -q '^aws' "$VWS_FAKE_LOG"; then ok "a refused cluster gets no aws call"; else bad "no aws call" "$(cat "$VWS_FAKE_LOG")"; fi
echo '{"serverid":"TestServer826"}' >"${WORK}/identity.json"
jq '.vwsTest.pveServerId = null' "$map" >"${WORK}/nofp.json"
run_vws_real verify scenario "$scenario" --map "${WORK}/nofp.json"; expect_rc "a map without a fingerprint is refused" 3
identity_refusals "$scenario" "$map"

run_vws_real verify scenario nope --map "$map"; expect_rc "an unknown scenario is a usage error" 2
expect_text "the error points at list" "$ERR" "unknown scenario nope; try: scripts/vws verify list"
run_vws_real verify scenario s3-basic --map "$map"; expect_rc "the retired s3-basic scenario is unknown" 2
run_vws_real verify scenario ../x --map "$map"; expect_rc "a path as name is a usage error" 2
run_vws_real verify scenario; expect_rc "no name is a usage error" 2
run_vws_real verify scenario "$scenario" --bogus; expect_rc "an unknown flag is a usage error" 2
run_vws_real verify scenario "$scenario" --map "$map" --fail-at no-such-step
expect_rc "a --fail-at step that never ran fails" 1
expect_text "it names the step" "$OUT" "scenario ec2-volume-reboot has no step no-such-step"
rm -rf "$ec2"
VWS_VERIFY_SECRET_ACCESS_KEY='' run_vws_real verify scenario "$scenario" --map "$map"
expect_rc "a missing credential is an environment error" 3
expect_text "it names the variable, not a value" "$ERR" "needs VWS_VERIFY_SECRET_ACCESS_KEY; nothing ran"
aws_host="ec2.us-east-1.ama""zonaws.com"
VWS_VERIFY_EC2_ENDPOINT="https://${aws_host}.:443/" run_vws_real verify scenario "$scenario" --map "$map"
expect_rc "an AWS-owned endpoint with a trailing dot is refused" 2
expect_text "it says why" "$ERR" "is AWS-owned, not VWS; refused"
VWS_VERIFY_EC2_ENDPOINT='ftp://x' run_vws_real verify scenario "$scenario" --map "$map"
expect_rc "a non-http endpoint is refused" 2
if ! grep -q '^aws' "$VWS_FAKE_LOG"; then ok "refused input gets no aws call"; else bad "no aws call" "$(cat "$VWS_FAKE_LOG")"; fi

finish_tests
