#!/usr/bin/env bash
# vws nightly: runs each feature-map scenario once, in name order, one at a time; records passes in
# the map; a fail, a timeout or a missing result is FAIL; overlapping runs are refused.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fm="${WORK}/fm"; mkdir -p "$fm"
map="${fm}/feature-map.json"
write_map() {
  cat >"$map" <<'JSON'
{"vwsTest": {"kubeSystemUID": "uid-vws-test"}, "features": [
  {"feature": "EC2 run", "repo": "vws-console-api", "issue": null, "scenario": "ec2-run"},
  {"feature": "EC2 key pair", "repo": "vws-console-api", "issue": null, "scenario": "ec2-keypair"},
  {"feature": "STS", "repo": "vws-iam", "issue": null, "scenario": "sts-assume-role", "lastPass": "2026-10-14T00:00:00Z", "evidence": "old"},
  {"feature": "Console login", "repo": "vws-console", "scenario": null}
]}
JSON
}
# The fake runner: NAME --result FILE. FAKE_MODE_<name> picks pass, fail, hang, none or crash.
runner="${WORK}/runner"
cat >"$runner" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
name=$1; [[ $2 == --result ]]; out=$3
echo "$name" >>"${FAKE_RUNS}"
if [[ -e ${FAKE_RUNS}.busy ]]; then echo overlap >>"${FAKE_RUNS}"; fi
touch "${FAKE_RUNS}.busy"; trap 'rm -f "${FAKE_RUNS}.busy"' EXIT
var="FAKE_MODE_${name//-/_}"; mode=${!var:-pass}
case $mode in
  hang) sleep 30 ;;
  none) exit 0 ;;
  crash) echo "boom" >&2; exit 4 ;;
esac
jq -n --arg s "$name" --arg r "$mode" '{scenario: $s, result: $r, cluster: "vws-test",
  finishedAt: "2026-10-30T13:00:00Z", clusterUID: "uid-vws-test", evidence: "https://example.invalid/\($s)"}' >"$out"
[[ $mode == pass ]]
SH
chmod +x "$runner"
export VWS_NIGHTLY_RUNNER="$runner" FAKE_RUNS="${WORK}/runs"

write_map; : >"$FAKE_RUNS"
run_vws_real nightly --map "$map" --results "${WORK}/r1"
expect_rc "all scenarios pass: exit 0" 0
assert_no_changes "the command itself makes no cluster call"
if [[ $(cat "$FAKE_RUNS") == $'ec2-keypair\nec2-run\nsts-assume-role' ]]; then ok "each scenario runs once, in name order, never overlapping"
else bad "run order" "$(cat "$FAKE_RUNS")"; fi
if jq -e '[.features[0,1,2].lastPass] == ["2026-10-30T13:00:00Z","2026-10-30T13:00:00Z","2026-10-30T13:00:00Z"]' "$map" >/dev/null; then
  ok "passes are recorded in the map"; else bad "passes recorded" "$(cat "$map")"; fi
expect_text "md is regenerated" "$(cat "${fm}/feature-map.md")" "2026-10-30 22:00 KST"
expect_text "each scenario has a log" "$OUT" "log ${WORK}/r1/ec2-run.log"

write_map; : >"$FAKE_RUNS"
FAKE_MODE_sts_assume_role=fail run_vws_real nightly --map "$map" --results "${WORK}/r2"
expect_rc "a failed scenario: exit 1" 1
expect_text "the failed scenario is named" "$OUT" "FAIL  scenario/sts-assume-role  result fail"
if jq -e '.features[2].lastPass == "2026-10-14T00:00:00Z" and .features[0].lastPass == "2026-10-30T13:00:00Z"' "$map" >/dev/null; then
  ok "a fail keeps the old last pass and the other pass is still recorded"; else bad "fail kept" "$(cat "$map")"; fi

write_map
FAKE_MODE_ec2_run=hang run_vws_real nightly --map "$map" --results "${WORK}/r3" --timeout 1
expect_rc "a hung scenario: exit 1" 1
expect_text "the timeout is reported" "$OUT" "FAIL  scenario/ec2-run  timed out after 1s"
expect_text "the next scenario still runs" "$OUT" "OK    scenario/sts-assume-role"

write_map
FAKE_MODE_ec2_run=none FAKE_MODE_sts_assume_role=crash run_vws_real nightly --map "$map" --results "${WORK}/r4"
expect_rc "no result written: exit 1" 1
expect_text "a missing result is FAIL" "$OUT" "FAIL  scenario/ec2-run  no valid result written (exit 0)"
expect_text "a crash is FAIL with its exit code" "$OUT" "FAIL  scenario/sts-assume-role  no valid result written (exit 4)"
expect_text "the crash output is kept in the log" "$(cat "${WORK}/r4/sts-assume-role.log")" "boom"

jq '.features |= map(.scenario = null)' "$map" >"${fm}/none.json"
run_vws_real nightly --map "${fm}/none.json" --results "${WORK}/r5"
expect_rc "no scenario at all: exit 0" 0
expect_text "no scenario is a WARN" "$OUT" "WARN  nightly  no feature in"

jq '.features[0].scenario = "../evil"' "$map" >"${fm}/evil.json"; : >"$FAKE_RUNS"
run_vws_real nightly --map "${fm}/evil.json" --results "${WORK}/r6"
expect_rc "an invalid scenario name: exit 1" 1
if ! grep -q evil "$FAKE_RUNS"; then ok "an invalid scenario name is never run"; else bad "invalid name run" "$(cat "$FAKE_RUNS")"; fi

write_map
mkdir -p "${WORK}/locked/run"
exec {held}>"${WORK}/locked/nightly.lock"; flock -n "$held"
run_vws_real nightly --map "$map" --results "${WORK}/locked/run"
expect_rc "a second run while the lock is held: exit 3" 3
expect_text "the held lock is named" "$ERR" "another nightly run holds"
exec {held}>&-

echo 'not json' >"${fm}/bad.json"
run_vws_real nightly --map "${fm}/bad.json" --results "${WORK}/r7"; expect_rc "an unreadable map: exit 3" 3
run_vws_real nightly --map "$map" --timeout 0; expect_rc "a zero timeout is a usage error" 2
run_vws_real nightly --bogus; expect_rc "an unknown argument is a usage error" 2

finish_tests
