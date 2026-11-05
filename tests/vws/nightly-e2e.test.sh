#!/usr/bin/env bash
# vws nightly-e2e: runs scope, apply, scenarios and feature-map-pr in order on vws-test only, writes a
# JSON summary, opens one incident on a failure (reused on a rerun) and never reaches OpenStack.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fake="${WORK}/fake"; mkdir -p "${fake}/kubectl" "${WORK}/bin"
export VWS_FAKE_DIR=$fake
uid=$(jq -r .vwsTest.kubeSystemUID "${VWS_TEST_ROOT}/docs/feature-map.json")
printf '%s' "$uid" >"${fake}/kubectl/get_ns.json"
cr="${WORK}/cr.yaml"; echo 'kind: VWSCluster' >"$cr"

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
    echo "FAIL  ${cmd}  broken with secret ${VWS_VERIFY_SECRET_ACCESS_KEY:-} token=${API_TOKEN:-}"
    printf '%s\n' "${PRIVATE_KEY:-}"
    if [[ $cmd == nightly ]]; then
      echo "scenario output with ${VWS_VERIFY_SECRET_ACCESS_KEY:-}" >"$2/ec2-run.log"
      echo '{"result": "fail"}' >"$2/ec2-run.json"
      echo "fine" >"$2/s3-basic.log"; echo '{"result": "pass"}' >"$2/s3-basic.json"
    fi
    exit 1 ;;
esac
SH
# The fake gh: logs each call; issue list prints FAKE_GH_LIST; issue create saves the body.
cat >"${WORK}/bin/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_GH_LOG}"
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
export OS_CLOUD=other-project OS_AUTH_URL=https://keystone.invalid OS_PASSWORD=hunter22
export VWS_VERIFY_SECRET_ACCESS_KEY=s3cr3t-value API_TOKEN=abc
export PRIVATE_KEY=$'-----BEGIN KEY-----\npemline1secret\npemline2secret\n-----END KEY-----'
url=https://logs.invalid/run
e2e() { : >"$FAKE_VWS_LOG"; : >"$FAKE_GH_LOG"; : >"$FAKE_OS_LOG"; rm -f "$FAKE_GH_BODY"; run_vws_real nightly-e2e --cr "$cr" --log-url "$url" "$@"; }
summary() { jq -r "$2" "${WORK}/$1/e2e/summary.json"; }

r="r/20261105T130000Z"
e2e --results "${WORK}/$r" --timeout 60
expect_rc "every step passes: exit 0" 0
if [[ $(cut -d' ' -f1 "$FAKE_VWS_LOG") == $'apply\nnightly\nnightly-pr' ]]; then ok "apply, nightly and nightly-pr run once, in order"
else bad "step order" "$(cat "$FAKE_VWS_LOG")"; fi
expect_text "apply gets the CR unattended" "$(cat "$FAKE_VWS_LOG")" "apply --unattended -f ${cr}"
expect_text "nightly writes into the run directory" "$(cat "$FAKE_VWS_LOG")" "nightly --results ${WORK}/$r --timeout 60"
expect_text "nightly-pr gets the run and the log URL" "$(cat "$FAKE_VWS_LOG")" "nightly-pr --results ${WORK}/$r --log-url ${url} --repo popododo0720/vws-deploy"
if ! grep -q '^env ' "$FAKE_VWS_LOG"; then ok "no step inherits an OS_* variable"; else bad "OS env leaked" "$(cat "$FAKE_VWS_LOG")"; fi
if [[ ! -s $FAKE_OS_LOG ]]; then ok "no step calls openstack"; else bad "openstack called" "$(cat "$FAKE_OS_LOG")"; fi
assert_no_changes "the scope check only reads the cluster"
expect_text "the scope check reads kube-system" "$(cat "$VWS_FAKE_LOG")" "get ns kube-system"
if [[ $(summary "$r" '[.result, .run, .incident, ([.steps[].result] | join(","))] | join(" ")') == "pass 20261105T130000Z  pass,pass,pass,pass" ]]; then
  ok "summary.json records the run and four passed steps"; else bad "summary" "$(cat "${WORK}/$r/e2e/summary.json")"; fi
if [[ ! -s $FAKE_GH_LOG ]]; then ok "a passing run opens no incident"; else bad "gh on pass" "$(cat "$FAKE_GH_LOG")"; fi

FAKE_CALL_OPENSTACK=1 e2e --results "${WORK}/r/20261105T130100Z"
if [[ -s $FAKE_OS_LOG ]]; then ok "control: the openstack fake catches a step that calls it"; else bad "control" "fake never called"; fi

r="r/20261105T130200Z"
FAKE_nightly=fail e2e --results "${WORK}/$r"
expect_rc "a failed scenario: exit 1" 1
expect_text "the failed step is named" "$OUT" "FAIL  e2e/scenarios  vws nightly exited 1; FAIL  nightly  broken"
expect_text "feature-map-pr still runs after failed scenarios" "$(cat "$FAKE_VWS_LOG")" "nightly-pr --results"
expect_text "the incident is reported" "$OUT" "OK    e2e/incident  https://github.com/popododo0720/vws-deploy/issues/9001"
expect_text "the incident has the incident label and names the step" "$(cat "$FAKE_GH_LOG")" \
  "issue create --repo popododo0720/vws-deploy --label incident --title incident: nightly e2e 20261105T130200Z failed --body-file"
body=$(cat "$FAKE_GH_BODY")
expect_text "the body follows the incident form" "$body" "## The check that would have caught this"
expect_text "the body has the failing step log" "$body" "### step scenarios (last 60 lines)"
expect_text "the body has the failed scenario log" "$body" "### scenario ec2-run (last 30 lines)"
if [[ $body != *"### scenario s3-basic"* ]]; then ok "a passed scenario log is left out"; else bad "passed log" "$body"; fi
if [[ $body != *s3cr3t-value* && $body != *pemline* && $body != *token=abc* && $body == *"[redacted]"* ]]; then ok "secret values are redacted in the body"; else bad "redaction" "$body"; fi
if [[ $OUT != *s3cr3t-value* && $OUT != *token=abc* ]] && ! grep -qE 's3cr3t-value|token=abc' "${WORK}/$r/e2e/summary.json"; then ok "secret values stay out of the output and summary.json"
else bad "secret leaked" "$OUT"; fi
if [[ $(summary "$r" '.result + " " + .incident') == "fail https://github.com/popododo0720/vws-deploy/issues/9001" ]]; then
  ok "summary.json records the failure and the incident"; else bad "summary fail" "$(cat "${WORK}/$r/e2e/summary.json")"; fi

FAKE_apply=fail FAKE_GH_LIST='[{"title":"incident: nightly e2e 20261105T130200Z failed","url":"https://github.com/popododo0720/vws-deploy/issues/77"}]' \
  e2e --results "${WORK}/$r"
expect_text "a rerun of the same run reuses (even when another step fails) the open incident" "$OUT" "OK    e2e/incident  https://github.com/popododo0720/vws-deploy/issues/77"
if ! grep -q 'issue create' "$FAKE_GH_LOG"; then ok "a rerun opens no second incident"; else bad "duplicate" "$(cat "$FAKE_GH_LOG")"; fi

FAKE_apply=fail e2e --results "${WORK}/r/20261105T130300Z"
expect_rc "a failed apply: exit 1" 1
if [[ $(cut -d' ' -f1 "$FAKE_VWS_LOG") == apply ]]; then ok "a failed apply skips the scenarios and the PR"; else bad "after apply" "$(cat "$FAKE_VWS_LOG")"; fi
expect_text "the skipped steps are WARN" "$OUT" "WARN  e2e/scenarios  skipped after apply failed"

printf 'uid-elsewhere' >"${fake}/kubectl/get_ns.json"
r="r/20261105T130400Z"
e2e --results "${WORK}/$r"
expect_rc "another cluster: exit 1" 1
expect_text "the scope failure names the cluster" "$OUT" "FAIL  e2e/scope  cluster kube-system UID 'uid-elsewhere' is not vws-test"
if [[ ! -s $FAKE_VWS_LOG ]]; then ok "on another cluster no step runs"; else bad "ran elsewhere" "$(cat "$FAKE_VWS_LOG")"; fi
expect_text "the scope failure opens an incident" "$(cat "$FAKE_GH_LOG")" "issue create"
expect_text "the incident body names the scope step" "$(cat "$FAKE_GH_BODY")" "failed at step \`scope\`"
printf '%s' "$uid" >"${fake}/kubectl/get_ns.json"

FAKE_apply=fail FAKE_GH_CREATE_FAIL=1 e2e --results "${WORK}/r/20261105T130500Z"
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
run_vws_real nightly-e2e --log-url "$url"; expect_rc "no --cr: usage" 2
run_vws_real nightly-e2e --cr "$cr" --log-url http://insecure.invalid; expect_rc "a non-https log URL: usage" 2
run_vws_real --dry-run nightly-e2e --cr "$cr" --log-url "$url"; expect_rc "--dry-run is refused" 2
run_vws_real nightly-e2e --bogus; expect_rc "an unknown argument: usage" 2

finish_tests
