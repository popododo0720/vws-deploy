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
  {"feature": "Console login", "repo": "vws-console", "scenario": null, "na": "browser UI"}
]}
JSON
p1="${WORK}/fm/phase1-features.json"
echo '{"phase1": ["EC2 key pair import", "Console login"], "later": [{"feature": "STS", "reason": "test"}]}' >"$p1"
jq --arg k "$(test_signer a)" '.vwsTest.signers = [$k]' "$map" >"${map}.new" && mv "${map}.new" "$map"
result() { # FILE SCENARIO RESULT CLUSTER TIME [CLUSTER_UID]
  jq -n --arg s "$2" --arg r "$3" --arg c "$4" --arg t "$5" --arg u "${6-uid-vws-test}" \
    '{scenario: $s, result: $r, cluster: $c, finishedAt: $t, clusterUID: $u, evidence: "https://example.invalid/\($s)"}' >"${WORK}/fm/$1"
  sign_result "${WORK}/fm/$1"
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
expect_text "md marks unverified with the na reason" "$md" "| Console login | vws-console | - | N/A: browser UI | unverified | - |"

# The fixture names scenarios that have no scenario file.
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a scenario with no scenario file fails the check" 1
expect_text "the missing scenario is named" "$OUT" "scenario ec2-keypair in ${map} does not exist"
expect_text "every missing scenario is named" "$OUT" "scenario sts-assume-role in ${map} does not exist"
expect_text "the md itself is current" "$OUT" "${WORK}/fm/feature-map.md matches"
cp "$map" "${map}.scenarios"
jq '.features[0].scenario = "../s3-basic" | .features[1].scenario = null | .features[1].na = "test"' "${map}.scenarios" >"$map"
python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" render "$map"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a path as a scenario name fails the check" 1
expect_text "the path is named" "$OUT" "scenario ../s3-basic in ${map} does not exist"
# From here to the restore, the map names only s3-basic, which exists.
jq '.features[0].scenario = "s3-basic" | .features[1].scenario = null | .features[1].na = "test"' "${map}.scenarios" >"$map"
python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" render "$map"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a fresh md passes the check" 0
echo "edited" >>"${WORK}/fm/feature-map.md"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a hand-edited md fails the check" 1
python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" render "$map"

cp "$p1" "${p1}.orig"
jq '.phase1 += ["IAM groups and roles"]' "${p1}.orig" >"$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a Phase 1 feature missing from the map fails the check" 1
expect_text "the missing Phase 1 feature is named" "$OUT" "FAIL  phase1  IAM groups and roles: Phase 1 feature has no row in"
jq '.later = []' "${p1}.orig" >"$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a map row in neither list fails the check" 1
expect_text "the unclassified row is named" "$OUT" "FAIL  phase1  STS: map row is neither phase1 nor later"
jq '.later += [{"feature": "Console login", "reason": "dup"}]' "${p1}.orig" >"$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a name in both lists is an unreadable list" 3
jq '.later[0].reason = ""' "${p1}.orig" >"$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a later feature without a reason is an unreadable list" 3
jq '.later = {}' "${p1}.orig" >"$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a later that is not a list is an unreadable list" 3
echo '{"phase1": [' >"$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a truncated Phase 1 list is unreadable" 3
rm "$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "a missing Phase 1 list is unreadable" 3
mv "${p1}.orig" "$p1"
run_vws_real verify --check-feature-map --map "$map"; expect_rc "the restored list passes again" 0
cp "$map" "${map}.na"
na_case() { # JQ RC NAME [TEXT]
  jq "$1" "${map}.na" >"$map"
  python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" render "$map"
  run_vws_real verify --check-feature-map --map "$map"; expect_rc "$3" "$2"
  [[ -z ${4-} ]] || expect_text "$3 (named)" "$OUT" "$4"
}
na_case 'del(.features[2].na)' 1 "a row with no scenario and no na fails the check" "Console login: no scenario and no na reason"
na_case '.features[0].na = "x"' 1 "a row with a scenario and an na fails the check" "has both a scenario and an na reason"
na_case '.features[2].na = ""' 1 "an empty na fails the check" "Console login: na must be a one-line reason"
na_case '.features[2].na = "a | b"' 1 "an na with a table pipe fails the check" "Console login: na must be a one-line reason"
na_case '.features[2].na = "a\nb"' 1 "a multi-line na fails the check" "Console login: na must be a one-line reason"
na_case '.features[2].na = "a\u2028b"' 1 "an na with a Unicode line separator fails the check" "Console login: na must be a one-line reason"
na_case '.features[2].na = 7' 1 "an na that is not a string fails the check" "Console login: na must be a one-line reason"
na_case '.features[2].na = "   "' 1 "a blank na fails the check" "Console login: na must be a one-line reason"
mv "${map}.na" "$map"
mv "${map}.scenarios" "$map"
python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" render "$map"
run_vws_real verify --check-feature-map; expect_rc "the repo feature map has every Phase 1 feature and a fresh md" 0

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

# Signature (vws-deploy#288): only a result signed by a key in vwsTest.signers is recorded.
refused() { # NAME FILE NEEDLE [MAP]
  local m_before
  m_before=$(cat "${4:-$map}")
  run_vws_real verify --update-feature-map --map "${4:-$map}" "$2"
  expect_rc "$1 is refused" 1
  expect_text "$1 names the reason" "$OUT" "$3"
  if [[ $(cat "${4:-$map}") == "$m_before" ]]; then ok "$1 leaves the map unchanged"; else bad "$1 map unchanged" "$(cat "${4:-$map}")"; fi
}
result signed.json ec2-keypair pass vws-test 2026-10-14T08:00:00Z
jq '.evidence = "https://example.invalid/forged"' "${WORK}/fm/signed.json" >"${WORK}/fm/tampered.json"
refused "a tampered field" "${WORK}/fm/tampered.json" "signature does not verify against vwsTest.signers"
jq '.finishedAt = "2026-10-14T09:00:00Z"' "${WORK}/fm/signed.json" >"${WORK}/fm/tampered-time.json"
refused "a tampered finishedAt" "${WORK}/fm/tampered-time.json" "signature does not verify"
jq 'del(.signature)' "${WORK}/fm/signed.json" >"${WORK}/fm/unsigned.json"
refused "a missing signature" "${WORK}/fm/unsigned.json" "unsigned; refused"
jq '.signature = "not an sshsig"' "${WORK}/fm/signed.json" >"${WORK}/fm/garbage-sig.json"
refused "a garbage signature" "${WORK}/fm/garbage-sig.json" "signature does not verify"
jq 'del(.signature)' "${WORK}/fm/signed.json" >"${WORK}/fm/other-signer.json"; sign_result "${WORK}/fm/other-signer.json" b
refused "the wrong signer" "${WORK}/fm/other-signer.json" "signature does not verify against vwsTest.signers"
jq '.vwsTest.signers = []' "$map" >"${WORK}/fm/nosigners.json"
refused "an empty signers list" "${WORK}/fm/signed.json" "no trusted signer (vwsTest.signers)" "${WORK}/fm/nosigners.json"
# Rotation: with both keys listed either verifies; once the old key is removed its results are refused.
jq --arg b "$(test_signer b)" '.vwsTest.signers += [$b]' "$map" >"${WORK}/fm/rotate.json"
cp "${WORK}/fm/rotate.json" "${WORK}/fm/rotate-orig.json"
run_vws_real verify --update-feature-map --map "${WORK}/fm/rotate.json" "${WORK}/fm/signed.json" "${WORK}/fm/other-signer.json"
expect_rc "during rotation both keys verify" 0
jq --arg b "$(test_signer b)" '.vwsTest.signers = [$b]' "${WORK}/fm/rotate-orig.json" >"${WORK}/fm/rotated.json"
refused "a result by a removed key" "${WORK}/fm/signed.json" "signature does not verify" "${WORK}/fm/rotated.json"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/signed.json"; expect_rc "a valid signature is accepted" 0
if jq -e '.features[0].lastPass == "2026-10-14T08:00:00Z"' "$map" >/dev/null; then ok "a signed pass is recorded"; else bad "signed pass" "$(cat "$map")"; fi
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/signed.json"; expect_rc "recording the same signed result again is a no-op" 0
expect_text "the re-run says so" "$OUT" "already has a pass at or after 2026-10-14T08:00:00Z"
if python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/feature_map.py" sign "${WORK}/fm/unsigned.json" "${WORK}/no-such-key" 2>"${WORK}/sign.err"; then
  bad "a missing key cannot sign" "$(cat "${WORK}/fm/unsigned.json")"; else ok "a missing key cannot sign"; fi
expect_text "the sign error names the file" "$(cat "${WORK}/sign.err")" "FAIL: cannot sign ${WORK}/fm/unsigned.json"

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
jq -n --arg t "$recent" '{scenario: "ec2-keypair", result: "pass", cluster: "vws-test", finishedAt: $t, clusterUID: "uid-vws-test", signature: 5}' >"${WORK}/fm/numsig.json"
invalid "a non-string signature" "$map" "${WORK}/fm/numsig.json" "signature must be a string"
jq '.vwsTest.signers = "vws-test ssh-ed25519 AAAA"' "$map" >"${WORK}/fm/strsigners.json"
invalid "a signers string, not a list" "${WORK}/fm/strsigners.json" "${WORK}/fm/recent.json" "vwsTest.signers must be a list"
jq '.vwsTest.signers = ["vws-test ssh-ed25519 AAAA\nvws-test ssh-ed25519 BBBB"]' "$map" >"${WORK}/fm/nlsigners.json"
invalid "a multi-line signers entry" "${WORK}/fm/nlsigners.json" "${WORK}/fm/recent.json" "one-line allowed_signers strings"
skew=$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%SZ)
result skew.json sts-assume-role fail vws-test "$skew"
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/skew.json"; expect_rc "a finishedAt within the 5 minute skew is accepted" 0

# The repository's own map renders to the committed md.
run_vws_real verify --check-feature-map; expect_rc "docs/feature-map.md is current" 0

# Every scenario a map names is one `vws verify list` knows (vws-deploy#262), so a typo cannot stay "never passed".
unknown_scenarios() { # MAP
  run_vws_real verify list
  jq -r '.features[].scenario // empty' "$1" | sort -u | comm -23 - <(awk '$1 == "OK" {print $2}' <<<"$OUT" | sort -u)
}
missing=$(unknown_scenarios "${VWS_TEST_ROOT}/docs/feature-map.json")
if [[ -z $missing ]] && jq -e 'any(.features[]; .scenario == "s3-basic")' "${VWS_TEST_ROOT}/docs/feature-map.json" >/dev/null; then
  ok "docs/feature-map.json names only known scenarios, s3-basic among them"; else bad "known scenarios" "unknown: ${missing:-none}"; fi
jq '.features[0].scenario = "s3-basik"' "${VWS_TEST_ROOT}/docs/feature-map.json" >"${WORK}/fm/typo.json"
expect_text "a typo'd scenario name is caught" "$(unknown_scenarios "${WORK}/fm/typo.json")" "s3-basik"

finish_tests
