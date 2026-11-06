#!/usr/bin/env bash
# vws verify scenario ec2-volume-reboot: the step order, teardown after an injected failure, a real failure while
# the volume is attached, a run-instances reply that was lost, a wait that never ends, SIGTERM mid-step, a missing
# variable and an AWS-owned EC2 endpoint. aws is the fake in tests/fixtures/bin/aws, a small EC2 kept on disk.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fake="${WORK}/fake"
mkdir -p "${fake}/kubectl" "${fake}/aws"
export VWS_FAKE_DIR=$fake
printf 'uid-vws-test' >"${fake}/kubectl/get_ns.json"
map="${WORK}/map.json"
cat >"$map" <<'JSON'
{"vwsTest": {"kubeSystemUID": "uid-vws-test"}, "features": [
  {"feature": "EC2 RunInstances / TerminateInstances", "repo": "vws-console-api", "issue": null, "scenario": "ec2-volume-reboot"}
]}
JSON
unset VWS_VERIFY_ENDPOINT
export VWS_VERIFY_EC2_ENDPOINT=http://ec2.vws.example.invalid:8080 VWS_VERIFY_ACCESS_KEY_ID=AKIDVERIFY
export VWS_VERIFY_SECRET_ACCESS_KEY=sekret-value-must-not-leak VWS_LEAK_CANARY=1
export VWS_VERIFY_IMAGE_ID=ami-00000001 VWS_VERIFY_INSTANCE_TYPE=t3.micro VWS_VERIFY_POLL=0 VWS_VERIFY_REBOOT_SETTLE=0
ec2="${VWS_FAKE_LOG}.ec2"
steps() { jq -r '"\(.step)=\(.result)"' <<<"$OUT" | paste -sd ' '; }
# left: instances not terminated or shutting down, and volumes, by the fake's files
left() {
  [[ ! -d $ec2 ]] || find "$ec2" -name '*.json' -exec jq -r 'select(.State != {Name: "terminated"} and .State != {Name: "shutting-down"}) | .InstanceId // .VolumeId' {} + |
    paste -sd ' '
}
leaves_nothing() { if [[ -z $(left) ]]; then ok "$1 leaves nothing"; else bad "$1 leaves nothing" "$(left)"; fi; }
run() { run_vws_real verify scenario ec2-volume-reboot --json --map "$map" "$@"; }
all_pass="run-instance=pass instance-running=pass create-volume=pass volume-available=pass attach-volume=pass volume-in-use=pass reboot-instance=pass reboot-settle=pass running-after-reboot=pass attached-after-reboot=pass detach-volume=pass volume-detached=pass delete-volume=pass terminate-instance=pass instance-terminated=pass cleanup:terminate-instance=pass cleanup:delete-volume=pass"

run_vws_real verify list
expect_text "list names ec2-volume-reboot with its summary" "$OUT" "ec2-volume-reboot  EC2 instance run, EBS volume"

run --result "${WORK}/r1.json"
expect_rc "a passing run exits 0" 0
expect_text "steps run in order and cleanup finds nothing left" "$(steps)" "$all_pass"
expect_text "cleanup of a finished run says already gone" \
  "$(jq -r 'select(.step | startswith("cleanup:")) | .evidence' <<<"$OUT" | paste -sd ' ')" "already gone already gone"
expect_text "the reboot settle records the states it saw" "$OUT" '"evidence":"i-00000001 states seen: pending"'
expect_text "the reboot keeps the attachment" "$OUT" 'attached to i-00000001'
expect_text "run-instances carries the run ID as client token and tag" "$(cat "$VWS_FAKE_LOG")" '--client-token vws-verify-'
expect_text "the volume is in the instance's zone" "$(cat "$VWS_FAKE_LOG")" '--availability-zone vws-1a'
expect_text "ec2 goes to the EC2 endpoint" "$(cat "$VWS_FAKE_LOG")" $'\thttp://ec2.vws.example.invalid:8080'
if jq -e '.scenario == "ec2-volume-reboot" and .result == "pass"' "${WORK}/r1.json" >/dev/null; then ok "the result says pass"
else bad "pass result" "$(cat "${WORK}/r1.json")"; fi
leaves_nothing "a passing run"
if [[ $OUT$ERR$(cat "$VWS_FAKE_LOG") != *sekret-value* ]]; then ok "the secret key is never printed"; else bad "secret" "$OUT"; fi
rm -rf "$ec2"

run --fail-at reboot-instance
expect_rc "an injected failure exits 1" 1
expect_text "later steps skip, the attached volume is detached and deleted, the instance terminated" "$(steps)" \
  "run-instance=pass instance-running=pass create-volume=pass volume-available=pass attach-volume=pass volume-in-use=pass reboot-instance=fail reboot-settle=skip running-after-reboot=skip attached-after-reboot=skip detach-volume=skip volume-detached=skip delete-volume=skip terminate-instance=skip instance-terminated=skip cleanup:terminate-instance=pass cleanup:delete-volume=pass"
expect_text "the cleanup force-detaches the volume" "$(cat "$VWS_FAKE_LOG")" 'detach-volume --volume-id vol-00000002 --force'
leaves_nothing "an injected failure"
rm -rf "$ec2"

echo "An error occurred (InternalError) when calling the DescribeVolumes operation: boom" >"${fake}/aws/ec2_describe-volumes.fail"
run --result "${WORK}/r2.json"
expect_rc "a failing call exits 1" 1
expect_text "the error is the step's evidence" "$OUT" "exit 1: An error occurred (InternalError) when calling the DescribeVolumes"
if jq -e '.result == "fail"' "${WORK}/r2.json" >/dev/null; then ok "the result says fail"; else bad "fail result" "$(cat "${WORK}/r2.json")"; fi
expect_text "a cleanup that cannot list volumes fails too" "$(steps)" "cleanup:terminate-instance=pass cleanup:delete-volume=fail"
rm "${fake}/aws/ec2_describe-volumes.fail"
run
expect_rc "a rerun after the failure passes" 0
rm -rf "$ec2"

echo "Could not connect to the endpoint URL" >"${fake}/aws/ec2_run-instances.lost"
run
expect_rc "a lost run-instances reply exits 1" 1
expect_text "the instance it created is found by tag and terminated" "$(steps)" \
  "run-instance=fail instance-running=skip create-volume=skip volume-available=skip attach-volume=skip volume-in-use=skip reboot-instance=skip reboot-settle=skip running-after-reboot=skip attached-after-reboot=skip detach-volume=skip volume-detached=skip delete-volume=skip terminate-instance=skip instance-terminated=skip cleanup:terminate-instance=pass cleanup:delete-volume=pass"
expect_text "the cleanup looked it up by the run ID tag" "$(cat "$VWS_FAKE_LOG")" 'describe-instances --filters Name=tag:Name,Values=vws-verify-'
leaves_nothing "a lost reply"
rm -f "${fake}/aws/ec2_run-instances.lost"; rm -rf "$ec2"

touch "${fake}/aws/ec2.stuck"
VWS_VERIFY_WAIT=1 run
expect_rc "a state that never changes exits 1" 1
expect_text "the wait names the state" "$OUT" "is pending, not running, after waiting 1s"
rm "${fake}/aws/ec2.stuck"
run
expect_rc "a rerun once the state moves passes" 0
rm -rf "$ec2"

echo 5 >"${fake}/aws/ec2_reboot-instances.sleep"
reset_log
(PATH="${FIXTURES}/bin:${PATH}" exec bash "$VWS_BIN" verify scenario ec2-volume-reboot --json --map "$map" \
  >"${WORK}/term.out" 2>&1) &
pid=$!
for _ in $(seq 100); do grep -q reboot-instances "$VWS_FAKE_LOG" 2>/dev/null && break; sleep 0.1; done
kill -TERM "$pid"
RC=0; wait "$pid" || RC=$?
OUT=$(cat "${WORK}/term.out") ERR=''
expect_rc "SIGTERM during the reboot exits 143" 143
expect_text "the interrupted step is a fail" "$OUT" "interrupted by SIGTERM"
expect_text "SIGTERM runs both cleanups" "$(jq -r 'select(.step | startswith("cleanup:")) | "\(.step)=\(.result)"' <<<"$OUT" 2>/dev/null | paste -sd ' ')" \
  "cleanup:terminate-instance=pass cleanup:delete-volume=pass"
expect_text "after a signal the instance is terminated without a wait" "$OUT" '"evidence":"terminating i-00000001"'
leaves_nothing "SIGTERM"
rm "${fake}/aws/ec2_reboot-instances.sleep"; rm -rf "$ec2"

VWS_VERIFY_IMAGE_ID='' run
expect_rc "a missing image is an environment error" 3
expect_text "it names the variable" "$ERR" "needs VWS_VERIFY_IMAGE_ID; nothing ran"
aws_host="ec2.us-east-1.ama""zonaws.com"
VWS_VERIFY_EC2_ENDPOINT="https://${aws_host}" run
expect_rc "an AWS-owned EC2 endpoint is refused" 2
expect_text "it names the variable" "$ERR" "VWS_VERIFY_EC2_ENDPOINT ${aws_host} is AWS-owned"
if ! grep -q '^aws' "$VWS_FAKE_LOG"; then ok "refused input gets no aws call"; else bad "no aws call" "$(cat "$VWS_FAKE_LOG")"; fi

finish_tests
