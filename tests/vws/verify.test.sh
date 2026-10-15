#!/usr/bin/env bash
# vws verify --update-feature-map: only a vws-test pass moves last pass forward; the md is regenerated;
# features without a scenario are WARN unverified; no cluster call is made.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

map="${WORK}/fm/feature-map.json"
mkdir -p "${WORK}/fm"
cat >"$map" <<'JSON'
{"vwsTest": {"kubeSystemUID": "uid-vws-test"}, "features": [
  {"feature": "EC2 key pair import", "repo": "vws-console-api", "issue": "vws-console-api#128", "scenario": "ec2-keypair"},
  {"feature": "STS", "repo": "vws-iam", "issue": "vws-iam#5", "scenario": "sts-assume-role", "lastPass": "2026-10-14T00:00:00Z", "evidence": "old"},
  {"feature": "Console login", "repo": "vws-console", "scenario": null}
]}
JSON
result() { # FILE SCENARIO RESULT CLUSTER TIME [CLUSTER_UID]
  jq -n --arg s "$2" --arg r "$3" --arg c "$4" --arg t "$5" --arg u "${6-uid-vws-test}" \
    '{scenario: $s, result: $r, cluster: $c, finishedAt: $t, clusterUID: $u, evidence: "https://example.invalid/\($s)"}' >"${WORK}/fm/$1"
}
result pass.json ec2-keypair pass vws-test 2026-10-14T06:30:00Z
result fail.json sts-assume-role fail vws-test 2026-10-14T07:00:00Z
result other.json ec2-keypair pass kind 2026-10-14T07:45:00Z
result older.json sts-assume-role pass vws-test 2026-10-13T00:00:00Z

run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/pass.json" "${WORK}/fm/fail.json" "${WORK}/fm/other.json" "${WORK}/fm/older.json"
expect_rc "update exits 0" 0
assert_no_changes "update makes no cluster call"
if jq -e '.features[0].lastPass == "2026-10-14T06:30:00Z" and .features[0].evidence == "https://example.invalid/ec2-keypair"' "$map" >/dev/null; then
  ok "a vws-test pass sets last pass and evidence"; else bad "pass recorded" "$(cat "$map")"; fi
if jq -e '.features[1].lastPass == "2026-10-14T00:00:00Z" and .features[1].evidence == "old"' "$map" >/dev/null; then
  ok "a fail or an older pass keeps the last pass"; else bad "last pass kept" "$(cat "$map")"; fi
expect_text "another cluster is ignored" "$OUT" "result is from 'kind', not vws-test; ignored"
expect_text "unverified feature is flagged" "$OUT" "WARN  unverified  Console login"
md=$(cat "${WORK}/fm/feature-map.md")
expect_text "md shows the pass in KST" "$md" "| EC2 key pair import | vws-console-api | vws-console-api#128 | \`scripts/vws verify scenario ec2-keypair\` | 2026-10-14 15:30 KST |"
expect_text "md marks unverified" "$md" "| Console login | vws-console | - | - | unverified | - |"

run_vws_real verify --check-feature-map --map "$map"; expect_rc "a fresh md passes the check" 0
echo "edited" >>"${WORK}/fm/feature-map.md"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a hand-edited md fails the check" 1

echo '{"scenario": "x"}' >"${WORK}/fm/bad.json"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/bad.json"; expect_rc "a result missing fields is invalid input" 2
echo 'not json' >"${WORK}/fm/garbage.json"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/garbage.json"; expect_rc "an unreadable result fails" 1
run_vws_real verify --update-feature-map --map "$map"; expect_rc "no result file is a usage error" 2
run_vws_real verify; expect_rc "no mode is a usage error" 2

# Provenance: a hand-written result without clusterUID, or with another cluster's UID, is refused.
before=$(cat "$map")
jq -n '{scenario: "ec2-keypair", result: "pass", cluster: "vws-test", finishedAt: "2026-10-14T07:30:00Z", evidence: "forged"}' >"${WORK}/fm/nouid.json"
result baduid.json ec2-keypair pass vws-test 2026-10-14T07:30:00Z uid-elsewhere
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/nouid.json"; expect_rc "a result without clusterUID is refused" 1
expect_text "missing clusterUID is reported" "$OUT" "clusterUID None does not match the vws-test fingerprint; refused"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/baduid.json"; expect_rc "a mismatched clusterUID is refused" 1
expect_text "mismatched clusterUID is reported" "$OUT" "clusterUID 'uid-elsewhere' does not match"
if [[ $(cat "$map") == "$before" ]]; then ok "refused results leave the map unchanged"; else bad "map unchanged" "$(cat "$map")"; fi
jq '.vwsTest.kubeSystemUID = null' "$map" >"${WORK}/fm/nofp.json"
result good.json ec2-keypair pass vws-test 2026-10-14T07:30:00Z
run_vws_real verify --update-feature-map --map "${WORK}/fm/nofp.json" "${WORK}/fm/good.json"; expect_rc "no configured fingerprint refuses every result" 1
expect_text "unconfigured fingerprint is reported" "$OUT" "no vws-test fingerprint"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/good.json"; expect_rc "a matching clusterUID is accepted" 0
if jq -e '.features[0].lastPass == "2026-10-14T07:30:00Z" and .vwsTest.kubeSystemUID == "uid-vws-test"' "$map" >/dev/null; then
  ok "a matching clusterUID records the pass and keeps the fingerprint"; else bad "matching pass" "$(cat "$map")"; fi

# Invalid input: wrong types, a future finishedAt or a non-string fingerprint exit 2 with one
# "FAIL: <reason>" line, no traceback, and the map and md unchanged.
invalid() { # NAME MAP FILE NEEDLE
  local m_before md_before
  m_before=$(cat "$2"); md_before=$(cat "${2%.json}.md" 2>/dev/null || true)
  run_vws_real verify --update-feature-map --map "$2" "$3"
  expect_rc "$1 exits 2" 2
  expect_text "$1 prints the FAIL line" "$OUT" "FAIL: "
  expect_text "$1 names the reason" "$OUT" "$4"
  if [[ $OUT$ERR != *Traceback* ]]; then ok "$1 has no traceback"; else bad "$1 has no traceback" "$OUT$ERR"; fi
  if [[ $(cat "$2") == "$m_before" && $(cat "${2%.json}.md" 2>/dev/null || true) == "$md_before" ]]; then
    ok "$1 leaves the map unchanged"; else bad "$1 map unchanged" "$(cat "$2")"; fi
}
recent=2026-10-14T05:00:00Z
jq -n --arg t "$recent" '{scenario: ["ec2-keypair"], result: "pass", cluster: "vws-test", finishedAt: $t, clusterUID: "uid-vws-test"}' >"${WORK}/fm/sclist.json"
invalid "a list scenario" "$map" "${WORK}/fm/sclist.json" "scenario must be a non-empty string"
jq -n --arg t "$recent" '{scenario: {name: "ec2-keypair"}, result: "pass", cluster: "vws-test", finishedAt: $t, clusterUID: "uid-vws-test"}' >"${WORK}/fm/scobj.json"
invalid "an object scenario" "$map" "${WORK}/fm/scobj.json" "scenario must be a non-empty string"
jq -n '{scenario: "ec2-keypair", result: "pass", cluster: "vws-test", finishedAt: 1760000000, clusterUID: "uid-vws-test"}' >"${WORK}/fm/numtime.json"
invalid "a numeric finishedAt" "$map" "${WORK}/fm/numtime.json" "finishedAt must be a non-empty string"
future=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
result future.json ec2-keypair pass vws-test "$future"
invalid "a future finishedAt" "$map" "${WORK}/fm/future.json" "is in the future"
jq -n --arg t "$recent" '{scenario: "ec2-keypair", result: "pass", cluster: "vws-test", finishedAt: $t, clusterUID: 42}' >"${WORK}/fm/numuid.json"
invalid "a non-string clusterUID" "$map" "${WORK}/fm/numuid.json" "clusterUID must be a string"
jq '.vwsTest.kubeSystemUID = 7' "$map" >"${WORK}/fm/numfp.json"
python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" render "${WORK}/fm/numfp.json" 2>/dev/null || true
result recent.json ec2-keypair pass vws-test "$recent"
invalid "a non-string fingerprint" "${WORK}/fm/numfp.json" "${WORK}/fm/recent.json" "kubeSystemUID must be a string or null"
skew=$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%SZ)
result skew.json sts-assume-role fail vws-test "$skew"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/skew.json"; expect_rc "a finishedAt within the 5 minute skew is accepted" 0

# The repository's own map renders to the committed md.
run_vws_real verify --check-feature-map; expect_rc "docs/feature-map.md is current" 0

finish_tests
