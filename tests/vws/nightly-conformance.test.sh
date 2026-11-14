#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

checkout="${WORK}/conformance"
mkdir -p "${checkout}/scripts" "${WORK}/credentials"
cat >"${checkout}/scripts/pve-test.sh" <<'SH'
#!/bin/sh
set -eu
printf '%s\n' "$VWS_CONF_TARGET $VWS_CONF_ALLOWED_ACCOUNTS $VWS_CONF_REGION $VWS_CONF_EC2_ENDPOINT $VWS_CONF_IAM_ENDPOINT $VWS_CONF_OUT $*" >"$FAKE_CONF_CALL"
test "$VWS_CONF_ACCESS_KEY_ID" = "$FAKE_ACCESS"
test "$VWS_CONF_SECRET_ACCESS_KEY" = "$FAKE_SECRET"
mkdir -p "$VWS_CONF_OUT/run"
if [ -z "${FAKE_NO_REPORT:-}" ]; then
  python3 -c 'import json, os
print(json.dumps({"target":{"name":os.getenv("FAKE_TARGET", "pve-test")},
 "results":[{"id":os.getenv("FAKE_CASE", "EC2-DI-102"),"operation":"DescribeInstances",
 "status":"pass","actual":{"secret":os.environ["VWS_CONF_SECRET_ACCESS_KEY"]}}]}))' >"$VWS_CONF_OUT/run/report.json"
fi
printf '%s %s %s\n' "$VWS_CONF_ACCESS_KEY_ID" "$VWS_CONF_SECRET_ACCESS_KEY" "${VWS_CONF_SESSION_TOKEN:-}"
if [ "${FAKE_SIGNAL:-}" = term ]; then kill -TERM "$PPID"; fi
if [ "${FAKE_SIGNAL:-}" = hang ]; then
  exec python3 -c 'import signal; signal.pause()'
fi
exit "${FAKE_EXIT:-0}"
SH
export VWS_CONF_DIR=$checkout FAKE_CONF_CALL="${WORK}/called"
export VWS_CONF_ALLOWED_HOSTS=box.invalid VWS_CONF_EC2_ENDPOINT=https://box.invalid/ec2
export VWS_CONF_IAM_ENDPOINT=https://box.invalid/iam VWS_CONF_REGION=pve-test-1
export VWS_CONF_ACCOUNT_ID=123456789012 VWS_CONF_TARGET=local-fake
export FAKE_ACCESS=throwaway-key-831 FAKE_SECRET=throwaway-secret-831
export VWS_CONF_ACCESS_KEY_ID=$FAKE_ACCESS VWS_CONF_SECRET_ACCESS_KEY=$FAKE_SECRET
export VWS_CONF_SESSION_TOKEN=throwaway-session-831

conf() {
  rm -f "$FAKE_CONF_CALL"
  run_vws_real nightly-conformance --results "${WORK}/$1" "${@:2}"
}
archive_exists() {
  if tar -tzf "${WORK}/$1/reports.tar.gz" | grep -q 'reports/run/report.json'; then
    ok "$1: report archived"
  else bad "$1: archive" missing; fi
}
conf pass
expect_rc "read-only report passes" 0
expect_text "zero creating cases confirmed" "$OUT" "zero creating cases"
expect_text "runner forces target, account and endpoints without arguments" "$(cat "$FAKE_CONF_CALL")" \
  "pve-test 123456789012 pve-test-1 https://box.invalid/ec2 https://box.invalid/iam ${WORK}/pass/reports "
archive_exists pass
if jq -e '.exit == 0 and .creatingCases == 0 and .readOnlyCases == 1 and .startedAt != null and .finishedAt != null' \
  "${WORK}/pass/summary.json" >/dev/null; then ok "summary records exit, times and case counts"
else bad summary "$(cat "${WORK}/pass/summary.json")"; fi
if ! grep -rF "$FAKE_SECRET" "${WORK}/pass" --exclude='*.gz' && \
  ! grep -rF "$FAKE_ACCESS" "${WORK}/pass" --exclude='*.gz' && \
  ! tar -xOzf "${WORK}/pass/reports.tar.gz" | grep -F "$FAKE_SECRET" && \
  [[ $OUT != *"$FAKE_SECRET"* && $ERR != *"$FAKE_SECRET"* ]] && \
  ! grep -rF "$VWS_CONF_SESSION_TOKEN" "${WORK}/pass" --exclude='*.gz'; then
  ok "secrets stay out of output, logs, reports and archive"
else bad secrets leaked; fi

FAKE_EXIT=7 conf failed
expect_rc "conformance exit 7 preserved" 7
archive_exists failed
expect_text "failed status recorded" "$(jq -r .exit "${WORK}/failed/summary.json")" 7

aws_domain=ama''zon
for endpoint in "ec2.${aws_domain}aws.com" "iam.${aws_domain}aws.com.cn" ec2.api.aws box.aws.dev \
  "www.${aws_domain}.com" "EC2.${aws_domain^^}AWS.COM."; do
  VWS_CONF_ALLOWED_HOSTS=$endpoint VWS_CONF_EC2_ENDPOINT="https://$endpoint" conf aws
  expect_rc "AWS-owned endpoint refused" 3
  [[ ! -e $FAKE_CONF_CALL ]] && ok "AWS-owned host never runs suite" || bad AWS ran
done
VWS_CONF_IAM_ENDPOINT=https://other.invalid conf outside
expect_rc "host outside allowlist refused" 3
[[ ! -e $FAKE_CONF_CALL ]] && ok "outside host never runs suite" || bad allowlist ran
VWS_CONF_IAM_ENDPOINT=https://iam.amazonaws.com conf aws-iam
expect_rc "AWS IAM refused" 3
VWS_CONF_S3_ENDPOINT=https://s3.amazonaws.com conf aws-s3
expect_rc "optional AWS S3 refused" 3
VWS_CONF_EC2_ENDPOINT="https://box.invalid@${aws_domain}aws.com" conf userinfo
expect_rc "userinfo endpoint refused" 3
VWS_CONF_REGION='' conf missing
expect_rc "missing region refused" 3
VWS_CONF_ALLOWED_HOSTS='' conf no-allowlist
expect_rc "missing host allowlist refused" 3

FAKE_CASE=EC2-RUN-001 conf creating
expect_rc "creating case fails" 1
expect_text "creating case has distinct diagnostic" "$OUT" "creating or unknown case"
archive_exists creating
FAKE_CASE=EBS-VOL-002 conf fixture
expect_rc "Describe case with creating fixtures fails" 1
FAKE_TARGET=vws-test conf target
expect_rc "wrong target report fails" 1
FAKE_EXIT=7 FAKE_CASE=EBS-VOL-002 conf unsafe-failure
expect_rc "report failure does not hide original exit 7" 7
FAKE_NO_REPORT=1 conf missing-report
expect_rc "missing report fails closed" 1
[[ -s ${WORK}/missing-report/reports.tar.gz ]] && ok "empty reports directory still archived" || bad archive missing
FAKE_EXIT=7 FAKE_NO_REPORT=1 conf missing-failed-report
expect_rc "missing report preserves original failure" 7

printf '%s' "$FAKE_ACCESS" >"${WORK}/credentials/conf-access-key-id"
printf '%s' "$FAKE_SECRET" >"${WORK}/credentials/conf-secret-access-key"
VWS_CONF_ACCESS_KEY_ID='' VWS_CONF_SECRET_ACCESS_KEY='' CREDENTIALS_DIRECTORY="${WORK}/credentials" conf credential-run
expect_rc "systemd credential files supported" 0
archive_exists credential-run
if ! grep -rF "$FAKE_SECRET" "${WORK}/credential-run/run.log" "${WORK}/credential-run/reports"; then
  ok "credential file secret redacted"
else bad credentials leaked; fi
FAKE_SECRET=$'secret-831-"quote"\\backslash' VWS_CONF_SECRET_ACCESS_KEY=$'secret-831-"quote"\\backslash' conf escaped
expect_rc "escaped JSON secret supported" 0
if [[ $(jq -r '.results[0].actual.secret' "${WORK}/escaped/reports/run/report.json") == '[redacted]' ]]; then
  ok "JSON-escaped credentials redacted"
else bad "escaped secret" leaked; fi

FAKE_SIGNAL=hang conf timeout --timeout 1
expect_rc "hard timeout returns 124" 124
archive_exists timeout
FAKE_SIGNAL=term conf interrupted
expect_rc "SIGTERM returns 143" 143
archive_exists interrupted
conf pass
expect_rc "rerun cannot overwrite prior reports" 3
[[ ! -e $FAKE_CONF_CALL ]] && ok "rerun refusal runs no suite" || bad rerun ran
archive_exists pass
run_vws_real nightly-conformance --timeout 0; expect_rc "zero timeout is usage" 2
run_vws_real nightly-conformance --bogus; expect_rc "unknown option is usage" 2
run_vws_real --dry-run nightly-conformance; expect_rc "dry-run is refused" 2
VWS_CONF_DIR='' conf no-checkout; expect_rc "missing checkout refused" 3
conf explicit --conformance "$checkout"; expect_rc "explicit checkout supported" 0
finish_tests
