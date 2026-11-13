#!/usr/bin/env bash
# vws nightly-e2e: runs scope, scenarios, feature-map-pr and publish in order on vws-test only, writes a
# JSON summary, opens one incident and posts one lead wake on a failure (each reused on a rerun), posts nothing on a
# pass and never reaches OpenStack.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fake="${WORK}/fake"; mkdir -p "${fake}/kubectl" "${WORK}/bin"
export VWS_FAKE_DIR=$fake
uid=$(jq -r .vwsTest.kubeSystemUID "${VWS_TEST_ROOT}/docs/feature-map.json")
printf '%s' "$uid" >"${fake}/kubectl/get_ns.json"

# The fake vws: logs the command and any OS_* variable it got; FAKE_<command> picks pass, fail or hang.
cat >"${WORK}/vws" <<'SH'
#!/usr/bin/env bash
cmd=$1; shift
printf '%s %s\n' "$cmd" "$*" >>"${FAKE_VWS_LOG}"
compgen -e | grep '^OS_' | sed 's/^/env /' >>"${FAKE_VWS_LOG}"
[[ -z ${FAKE_CALL_OPENSTACK:-} ]] || openstack server list
var="FAKE_${cmd//-/_}"
case ${!var:-pass} in
  hang) sleep 30 ;;
  fail)
    echo "FAIL  ${cmd}  broken with secret ${VWS_VERIFY_SECRET_ACCESS_KEY:-} token=${API_TOKEN:-} gh=${GH_TOKEN:-}"
    printf '%s\n' "${PRIVATE_KEY:-}"
    if [[ $cmd == nightly ]]; then
      echo "scenario output with ${VWS_VERIFY_SECRET_ACCESS_KEY:-}" >"$2/ec2-run.log"
      echo '{"result": "fail"}' >"$2/ec2-run.json"
      echo "fine" >"$2/s3-basic.log"; echo '{"result": "pass"}' >"$2/s3-basic.json"
    fi
    exit 1 ;;
esac
SH
# The fake gh: logs each call; issue list prints FAKE_GH_LIST; issue create saves the body. api user prints
# FAKE_GH_ME; api --paginate runs its --jq on FAKE_GH_COMMENTS; an api post saves the comment body.
cat >"${WORK}/bin/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_GH_LOG}"
if [[ $1 == api ]]; then
  [[ $2 != user ]] || { echo "${FAKE_GH_ME:-popododo0720}"; exit 0; }
  if [[ $2 == --paginate ]]; then [[ $4 == --jq ]] && jq -r "$5" <<<"${FAKE_GH_COMMENTS:-[]}"; exit; fi
  [[ -z ${FAKE_GH_POST_FAIL:-} ]] || { echo "HTTP 502" >&2; exit 1; }
  [[ $3 == -F && $4 == body=@* ]] && cp "${4#body=@}" "${FAKE_GH_WAKE}"
  echo "https://github.com/popododo0720/vws-deploy/pull/339#issuecomment-1"; exit 0
fi
case "$1 $2" in
  "issue list") [[ -z ${FAKE_GH_LIST_FAIL:-} ]] || exit 1; printf '%s' "${FAKE_GH_LIST:-[]}" ;;
  "issue create")
    [[ -z ${FAKE_GH_CREATE_FAIL:-} ]] || { echo "HTTP 502" >&2; exit 1; }
    while (($#)); do [[ $1 == --body-file ]] && cp "$2" "${FAKE_GH_BODY}"; shift; done
    echo "https://github.com/popododo0720/vws-deploy/issues/9001" ;;
  *) exit 9 ;;
esac
SH
printf '#!/usr/bin/env bash\necho "openstack $*" >>"${FAKE_OS_LOG}"\n' >"${WORK}/bin/openstack"
chmod +x "${WORK}/vws" "${WORK}/bin/gh" "${WORK}/bin/openstack"
export PATH="${WORK}/bin:${PATH}" VWS_NIGHTLY_E2E_VWS="${WORK}/vws" VWS_NIGHTLY_E2E_BACKOFF=0
export FAKE_VWS_LOG="${WORK}/vws.log" FAKE_GH_LOG="${WORK}/gh.log" FAKE_GH_BODY="${WORK}/body.md" FAKE_OS_LOG="${WORK}/os.log"
export FAKE_GH_WAKE="${WORK}/wake.md"
unset ORCHESTRATE_WAKE_THREAD ORCHESTRATE_WAKES ORCHESTRATE_DISPATCH
orch="${WORK}/orchestrate"; mkdir -p "$orch"; export ORCHESTRATE_CONFIG_DIR=$orch
export OS_CLOUD=other-project OS_AUTH_URL=https://keystone.invalid OS_PASSWORD=hunter22
export VWS_VERIFY_SECRET_ACCESS_KEY=s3cr3t-value API_TOKEN=abc GH_TOKEN=abc-longer-token
export PRIVATE_KEY=$'-----BEGIN KEY-----\npemline1secret\npemline2secret\n-----END KEY-----'
url=https://logs.invalid/run
e2e() { : >"$FAKE_VWS_LOG"; : >"$FAKE_GH_LOG"; : >"$FAKE_OS_LOG"; rm -f "$FAKE_GH_BODY" "$FAKE_GH_WAKE"; run_vws_real nightly-e2e --log-url "$url" "$@"; }
summary() { jq -r "$2" "${WORK}/$1/e2e/summary.json"; }

r="r/20261105T130000Z"
e2e --results "${WORK}/$r" --timeout 60 --wake-thread 339
expect_rc "every step passes: exit 0" 0
if [[ $(cut -d' ' -f1 "$FAKE_VWS_LOG") == $'nightly\nnightly-pr\nnightly-publish' ]]; then ok "nightly, nightly-pr and nightly-publish run once, in order"
else bad "step order" "$(cat "$FAKE_VWS_LOG")"; fi
expect_text "nightly writes into the run directory" "$(cat "$FAKE_VWS_LOG")" "nightly --results ${WORK}/$r --timeout 60"
expect_text "nightly-publish gets the run" "$(cat "$FAKE_VWS_LOG")" "nightly-publish --results ${WORK}/$r"
expect_text "nightly-pr gets the run and the log URL" "$(cat "$FAKE_VWS_LOG")" "nightly-pr --results ${WORK}/$r --log-url ${url} --repo popododo0720/vws-deploy"
if ! grep -q '^env ' "$FAKE_VWS_LOG"; then ok "no step inherits an OS_* variable"; else bad "OS env leaked" "$(cat "$FAKE_VWS_LOG")"; fi
if [[ ! -s $FAKE_OS_LOG ]]; then ok "no step calls openstack"; else bad "openstack called" "$(cat "$FAKE_OS_LOG")"; fi
assert_no_changes "the scope check only reads the cluster"
expect_text "the scope check reads kube-system" "$(cat "$VWS_FAKE_LOG")" "get ns kube-system"
if [[ $(summary "$r" '[.result, .run, .incident, ([.steps[].result] | join(","))] | join(" ")') == "pass 20261105T130000Z  pass,pass,pass,pass" ]]; then
  ok "summary.json records the run and five passed steps"; else bad "summary" "$(cat "${WORK}/$r/e2e/summary.json")"; fi
if [[ ! -s $FAKE_GH_LOG ]]; then ok "a passing run with a wake thread opens no incident and posts no wake"; else bad "gh on pass" "$(cat "$FAKE_GH_LOG")"; fi

FAKE_CALL_OPENSTACK=1 e2e --results "${WORK}/r/20261105T130100Z"
if [[ -s $FAKE_OS_LOG ]]; then ok "control: the openstack fake catches a step that calls it"; else bad "control" "fake never called"; fi

r="r/20261105T130200Z"
FAKE_nightly=fail e2e --results "${WORK}/$r"
expect_rc "a failed scenario: exit 1" 1
expect_text "the failed step is named" "$OUT" "FAIL  e2e/scenarios  vws nightly exited 1; FAIL  nightly  broken"
expect_text "feature-map-pr still runs after failed scenarios" "$(cat "$FAKE_VWS_LOG")" "nightly-pr --results"
expect_text "publish still runs after failed scenarios" "$(cat "$FAKE_VWS_LOG")" "nightly-publish --results"
expect_text "the incident is reported" "$OUT" "OK    e2e/incident  https://github.com/popododo0720/vws-deploy/issues/9001"
expect_text "the incident has the incident label and names the step" "$(cat "$FAKE_GH_LOG")" \
  "issue create --repo popododo0720/vws-deploy --label incident --title incident: nightly e2e 20261105T130200Z failed --body-file"
body=$(cat "$FAKE_GH_BODY")
expect_text "the body follows the incident form" "$body" "## The check that would have caught this"
expect_text "the body has the failing step log" "$body" "### step scenarios (last 60 lines)"
expect_text "the body has the failed scenario log" "$body" "### scenario ec2-run (last 30 lines)"
if [[ $body != *"### scenario s3-basic"* ]]; then ok "a passed scenario log is left out"; else bad "passed log" "$body"; fi
if [[ $body != *s3cr3t-value* && $body != *pemline* && $body != *token=abc* && $body != *longer-token* && $body == *"[redacted]"* ]]; then ok "secret values are redacted in the body"; else bad "redaction" "$body"; fi
if [[ $OUT != *s3cr3t-value* && $OUT != *token=abc* ]] && ! grep -qE 's3cr3t-value|token=abc' "${WORK}/$r/e2e/summary.json"; then ok "secret values stay out of the output and summary.json"
else bad "secret leaked" "$OUT"; fi
if [[ $(summary "$r" '.result + " " + .incident') == "fail https://github.com/popododo0720/vws-deploy/issues/9001" ]]; then
  ok "summary.json records the failure and the incident"; else bad "summary fail" "$(cat "${WORK}/$r/e2e/summary.json")"; fi

expect_text "no wake thread: WARN, nobody is woken" "$OUT" "WARN  e2e/wake  no wake thread"
if ! grep -q '^api ' "$FAKE_GH_LOG"; then ok "no wake thread: no comment is posted"; else bad "wake without thread" "$(cat "$FAKE_GH_LOG")"; fi

w="r/20261105T130250Z"
FAKE_nightly=fail e2e --results "${WORK}/$w" --wake-thread 339
expect_rc "a failed scenario with a wake thread: exit 1" 1
expect_text "the wake is reported" "$OUT" "OK    e2e/wake  popododo0720/vws-deploy#339: https://github.com/popododo0720/vws-deploy/pull/339#issuecomment-1"
expect_text "the wake goes to the thread's comments" "$(cat "$FAKE_GH_LOG")" "api repos/popododo0720/vws-deploy/issues/339/comments -F body=@"
wake=$(cat "$FAKE_GH_WAKE")
if [[ $(head -n1 <<<"$wake") == '<!-- vws-orchestrate:lead-wake -->' ]]; then ok "the wake's first line is the lead-wake marker"; else bad "marker" "$wake"; fi
expect_text "the wake names the run and the step" "$wake" "run 20261105T130250Z on vws-test failed at step \`scenarios\`"
expect_text "the wake names the step log" "$wake" "Step log: \`${WORK}/$w/e2e/scenarios.log\`"
expect_text "the wake names the failed scenario and its log" "$wake" "- scenario \`ec2-run\` failed, log \`${WORK}/$w/ec2-run.log\`"
expect_text "the wake links the incident and the run log" "$wake" $'Run log: https://logs.invalid/run\nIncident: https://github.com/popododo0720/vws-deploy/issues/9001'
if [[ $wake != *s3-basic* ]]; then ok "a passed scenario is left out of the wake"; else bad "passed in wake" "$wake"; fi
if [[ $wake != *s3cr3t-value* && $wake != *token=abc* && $wake != *longer-token* ]]; then ok "the wake has no secret values"; else bad "wake secret" "$wake"; fi
key=$(grep '^Idempotency key: ' <<<"$wake")
if [[ $key =~ ^Idempotency\ key:\ \`sha256:[0-9a-f]{64}\`$ ]]; then ok "the wake carries one idempotency key"; else bad "key" "$wake"; fi

own=$(jq -cn --arg b "$wake" '[{user: {login: "popododo0720"}, body: $b}]')
FAKE_nightly=fail FAKE_GH_COMMENTS=$own e2e --results "${WORK}/$w" --wake-thread 339
expect_text "a rerun of the same run finds its own wake" "$OUT" "OK    e2e/wake  popododo0720/vws-deploy#339: already posted"
if ! grep -q -- '-F body=@' "$FAKE_GH_LOG"; then ok "a rerun posts no second wake"; else bad "duplicate wake" "$(cat "$FAKE_GH_LOG")"; fi
copy=$(jq -cn --arg b "$wake" '[{user: {login: "someone-else"}, body: $b}]')
FAKE_nightly=fail FAKE_GH_COMMENTS=$copy e2e --results "${WORK}/$w" --wake-thread 339
expect_text "a copy of the key by another login does not suppress the wake" "$(cat "$FAKE_GH_LOG")" "-F body=@"
FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130260Z" --wake-thread 340
if grep -q -- '-F body=@' "$FAKE_GH_LOG" && [[ $(grep '^Idempotency key: ' "$FAKE_GH_WAKE") != "$key" ]]; then
  ok "another run gets its own key and its own wake"; else bad "new run" "$(cat "$FAKE_GH_LOG")"; fi

FAKE_nightly=fail ORCHESTRATE_WAKES=off e2e --results "${WORK}/r/20261105T130270Z" --wake-thread 339
expect_text "STOP (ORCHESTRATE_WAKES=off): WARN" "$OUT" "WARN  e2e/wake  wakes are off"
if ! grep -q '^api ' "$FAKE_GH_LOG"; then ok "STOP: no wake is posted"; else bad "wake under STOP" "$(cat "$FAKE_GH_LOG")"; fi
FAKE_nightly=fail ORCHESTRATE_DISPATCH=off ORCHESTRATE_WAKE_THREAD=339 e2e --results "${WORK}/r/20261105T130271Z"
if ! grep -q '^api ' "$FAKE_GH_LOG"; then ok "ORCHESTRATE_DISPATCH=off: no wake is posted"; else bad "wake under dispatch off" "$(cat "$FAKE_GH_LOG")"; fi

printf '# box config\nORCHESTRATE_WAKE_THREAD = "339"\nORCHESTRATE_WAKES=off' >"${orch}/env"
FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130272Z"
expect_text "STOP in the orchestrator's env file, on a last line without a newline: WARN" "$OUT" "WARN  e2e/wake  wakes are off (ORCHESTRATE_WAKES is off)"
if ! grep -q '^api ' "$FAKE_GH_LOG"; then ok "STOP in the env file: no wake is posted"; else bad "wake under file STOP" "$(cat "$FAKE_GH_LOG")"; fi
printf 'ORCHESTRATE_WAKE_THREAD="339"\n' >"${orch}/env"
FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130273Z" --repo popododo0720/vws-iam
expect_text "the env file's wake thread is used" "$OUT" "OK    e2e/wake  popododo0720/vws-deploy#339:"
expect_text "the wake goes to vws-deploy whatever --repo is" "$(cat "$FAKE_GH_LOG")" "api repos/popododo0720/vws-deploy/issues/339/comments -F body=@"
ORCHESTRATE_WAKES=off FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130274Z"
if ! grep -q '^api ' "$FAKE_GH_LOG"; then ok "the environment wins over the env file, as in the orchestrator"; else bad "env precedence" "$(cat "$FAKE_GH_LOG")"; fi
touch "${orch}/STOP"
FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130275Z" --wake-thread 339
expect_text "the STOP file: WARN" "$OUT" "WARN  e2e/wake  wakes are off (the STOP file ${orch}/STOP is present)"
if ! grep -q '^api ' "$FAKE_GH_LOG"; then ok "the STOP file: no wake is posted"; else bad "wake under STOP file" "$(cat "$FAKE_GH_LOG")"; fi
rm -f "${orch}/STOP"
printf 'ORCHESTRATE_WAKE_THREAD=abc\n' >"${orch}/env"
FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130276Z"
expect_text "a wake thread in the env file that is not a number: FAIL" "$OUT" "FAIL  e2e/wake  ORCHESTRATE_WAKE_THREAD must be an issue or PR number, not abc"
rm -f "${orch}/env"

url="https://logs.invalid/run?token=${GH_TOKEN}" FAKE_nightly=fail e2e --results "${WORK}/r/20261105T130277Z" --wake-thread 339
expect_text "a secret in the log URL is redacted in the wake" "$(cat "$FAKE_GH_WAKE")" "Run log: https://logs.invalid/run?token=[redacted]"

ORCHESTRATE_WAKE_THREAD=339 FAKE_nightly=fail FAKE_GH_POST_FAIL=1 e2e --results "${WORK}/r/20261105T130280Z"
expect_rc "a wake that cannot be posted: exit 1" 1
expect_text "the unposted wake is FAIL with the body path" "$OUT" "FAIL  e2e/wake  could not post the lead wake on popododo0720/vws-deploy#339; body at ${WORK}/r/20261105T130280Z/e2e/wake.md"
if [[ $(grep -c -- '-F body=@' "$FAKE_GH_LOG") == 3 ]]; then ok "the wake post is tried three times"; else bad "wake retries" "$(cat "$FAKE_GH_LOG")"; fi

FAKE_nightly=fail FAKE_GH_LIST='[{"title":"incident: nightly e2e 20261105T130200Z failed","url":"https://github.com/popododo0720/vws-deploy/issues/77"}]' \
  e2e --results "${WORK}/$r"
expect_text "a rerun of the same run reuses (even when another step fails) the open incident" "$OUT" "OK    e2e/incident  https://github.com/popododo0720/vws-deploy/issues/77"
if ! grep -q 'issue create' "$FAKE_GH_LOG"; then ok "a rerun opens no second incident"; else bad "duplicate" "$(cat "$FAKE_GH_LOG")"; fi

r="r/20261105T130350Z"
FAKE_nightly_publish=fail e2e --results "${WORK}/$r"
expect_rc "a failed publish: exit 1" 1
expect_text "the failed publish is named" "$OUT" "FAIL  e2e/publish  vws nightly-publish exited 1"
expect_text "a failed publish opens an incident on that step" "$(cat "$FAKE_GH_BODY")" "failed at step \`publish\`"

printf 'uid-elsewhere' >"${fake}/kubectl/get_ns.json"
r="r/20261105T130400Z"
e2e --results "${WORK}/$r"
expect_rc "another cluster: exit 1" 1
expect_text "the scope failure names the cluster" "$OUT" "FAIL  e2e/scope  cluster kube-system UID 'uid-elsewhere' is not vws-test"
if [[ ! -s $FAKE_VWS_LOG ]]; then ok "on another cluster no step runs"; else bad "ran elsewhere" "$(cat "$FAKE_VWS_LOG")"; fi
expect_text "the scope failure opens an incident" "$(cat "$FAKE_GH_LOG")" "issue create"
expect_text "the incident body names the scope step" "$(cat "$FAKE_GH_BODY")" "failed at step \`scope\`"
printf '%s' "$uid" >"${fake}/kubectl/get_ns.json"

FAKE_nightly=fail FAKE_GH_CREATE_FAIL=1 e2e --results "${WORK}/r/20261105T130500Z"
expect_rc "an incident that cannot be opened: exit 1" 1
expect_text "the unopened incident is FAIL with the body path" "$OUT" "FAIL  e2e/incident  could not open the incident issue"
if [[ $(grep -c 'issue create' "$FAKE_GH_LOG") == 3 ]]; then ok "issue create is tried three times"; else bad "retries" "$(cat "$FAKE_GH_LOG")"; fi
if [[ -s ${WORK}/r/20261105T130500Z/e2e/incident.md ]]; then ok "the body is kept on the box"; else bad "body kept" "missing"; fi

mkdir -p "${WORK}/locked"
exec {held}>"${WORK}/locked/nightly-e2e.lock"; flock -n "$held"
e2e --results "${WORK}/locked/20261105T130600Z"
expect_rc "a second run while the lock is held: exit 3" 3
exec {held}>&-

if ! grep -rqw openstack "${VWS_TEST_ROOT}/scripts/vws" "${VWS_TEST_ROOT}/scripts/vws.d"; then
  ok "no vws command calls openstack"; else bad "openstack in vws" "$(grep -rlw openstack "${VWS_TEST_ROOT}/scripts/vws.d")"; fi

e2e --results "${WORK}/r/latest"; expect_rc "a results directory not named like a run: usage" 2
e2e --results "${WORK}/r/20261105T130700Z" --timeout 0; expect_rc "a zero timeout: usage" 2
run_vws_real nightly-e2e --log-url http://insecure.invalid; expect_rc "a non-https log URL: usage" 2
run_vws_real --dry-run nightly-e2e --log-url "$url"; expect_rc "--dry-run is refused" 2
run_vws_real nightly-e2e --bogus; expect_rc "an unknown argument: usage" 2
run_vws_real nightly-e2e --log-url "$url" --wake-thread 339abc; expect_rc "a wake thread that is not a number: usage" 2

finish_tests
