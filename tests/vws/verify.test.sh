#!/usr/bin/env bash
# vws verify --update-feature-map: only a vws-test pass moves last pass forward; the md is regenerated;
# features without a scenario are WARN unverified; no cluster call is made.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

map="${WORK}/fm/feature-map.json"
mkdir -p "${WORK}/fm"
cat >"$map" <<'JSON'
{"features": [
  {"feature": "EC2 key pair import", "repo": "vws-console-api", "issue": "vws-console-api#128", "scenario": "ec2-keypair"},
  {"feature": "STS", "repo": "vws-iam", "issue": "vws-iam#5", "scenario": "sts-assume-role", "lastPass": "2026-10-14T00:00:00Z", "evidence": "old"},
  {"feature": "Console login", "repo": "vws-console", "scenario": null}
]}
JSON
result() { # FILE SCENARIO RESULT CLUSTER TIME
  jq -n --arg s "$2" --arg r "$3" --arg c "$4" --arg t "$5" \
    '{scenario: $s, result: $r, cluster: $c, finishedAt: $t, evidence: "https://example.invalid/\($s)"}' >"${WORK}/fm/$1"
}
result pass.json ec2-keypair pass vws-test 2026-10-14T06:30:00Z
result fail.json sts-assume-role fail vws-test 2026-10-14T07:00:00Z
result other.json ec2-keypair pass kind 2026-10-15T00:00:00Z
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
run_vws_real verify --update-feature-map --map "$map" "${WORK}/fm/bad.json"; expect_rc "an unreadable result fails" 1
run_vws_real verify --update-feature-map --map "$map"; expect_rc "no result file is a usage error" 2
run_vws_real verify; expect_rc "no mode is a usage error" 2

# The repository's own map renders to the committed md.
run_vws_real verify --check-feature-map; expect_rc "docs/feature-map.md is current" 0

finish_tests
