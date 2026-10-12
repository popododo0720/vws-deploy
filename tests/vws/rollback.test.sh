#!/usr/bin/env bash
# vws rollback: plan only without --yes (2), CR and release on the same revision with --yes, limits printed.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"

last_saved_spec() {
  python3 - "${VWS_FAKE_LOG}.stdin" <<'PY'
import sys, yaml, json
specs = [d["spec"] for doc in open(sys.argv[1]).read().split("--- apply\n")
         for d in [yaml.safe_load(doc) if doc.strip() else None] if isinstance(d, dict) and d.get("kind") == "VWSCluster"]
print(json.dumps(specs[-1], sort_keys=True) if specs else "")
PY
}
recorded_spec() { # REVISION
  jq -r --argjson r "$1" '.[] | select(.revision == $r) | .description | ltrimstr("vws-spec:")' "${VWS_FAKE_DIR}/helm/history.json" | jq -S -c .
}
LIMITS=(
  "helm rollback restores chart resources and values only"
  "CRDs and platform operators are not rolled back"
  "zero downtime is not claimed until vws-deploy#39 is closed"
)

installer_scenario plan
run_vws_real rollback;                            expect_rc "without --yes exits 2" 2
assert_no_changes "without --yes changes nothing"
expect_text "the plan names the revision" "$OUT" "helm rollback vws 2 --wait"
for l in "${LIMITS[@]}"; do expect_text "limit printed: ${l:0:30}" "$OUT" "$l"; done

installer_scenario yes
run_vws_real --yes rollback;                      expect_rc "--yes exits 0" 0
expect_text "helm rolls back to 2" "$(calls)" "rollback vws 2 --wait"
want=$(recorded_spec 2)
have=$(last_saved_spec | jq -S -c .)
if [[ $have == "$want" ]]; then ok "the CR spec is revision 2's spec"; else bad "CR spec" "have ${have} want ${want}"; fi
expect_text "the CR status is written" "$(calls)" "patch vwsclusters vws --subresource=status"
for l in "${LIMITS[@]}"; do expect_text "limit printed after --yes: ${l:0:30}" "$OUT" "$l"; done
first_rollback=$(grep -n $'^helm\trollback' "$VWS_FAKE_LOG" | cut -d: -f1)
first_save=$(grep -n $'^kubectl\tapply' "$VWS_FAKE_LOG" | cut -d: -f1)
if ((first_rollback < first_save)); then ok "helm rolls back before the CR changes"; else bad "order" "$(calls)"; fi

installer_scenario unchanged
run_vws_real --yes rollback --to 3;               expect_rc "rollback to the current revision exits 0" 0
expect_text "current revision with its spec is unchanged" "$OUT" "unchanged"
assert_no_changes "unchanged changes nothing"

installer_scenario hashed
# Revision 2 records only a hash; the spec is in the CR's status.specHistory.
spec2=$(recorded_spec 2)
hash2=$(printf '%s' "$spec2" | sha256sum | cut -d' ' -f1)
jq --arg d "vws-spec-sha256:${hash2}" '(.[] | select(.revision == 2) | .description) = $d' "${VWS_FAKE_DIR}/helm/history.json" >"${WORK}/h" && mv "${WORK}/h" "${VWS_FAKE_DIR}/helm/history.json"
jq --arg h "$hash2" --argjson s "$spec2" '.status.specHistory = [{specHash: $h, spec: $s}]' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/c" && mv "${WORK}/c" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real --yes rollback;                      expect_rc "a hashed revision with its spec in status exits 0" 0
have=$(last_saved_spec | jq -S -c .)
if [[ $have == "$spec2" ]]; then ok "a hashed revision restores the spec from status.specHistory"; else bad "hashed spec" "have ${have} want ${spec2}"; fi
jq '.status.specHistory = []' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/c" && mv "${WORK}/c" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real --yes rollback;                      expect_rc "a hashed revision missing from status exits 1" 1
expect_text "a missing hash says why" "$OUT" "status.specHistory no longer has it"
assert_no_changes "a missing hash changes nothing"

installer_scenario no-record
run_vws_real --yes rollback --to 1;               expect_rc "a revision without a recorded spec exits 1" 1
expect_text "no record says why" "$OUT" "no recorded VWSCluster spec"
assert_no_changes "no record changes nothing"

installer_scenario helm-fails
rm "${VWS_FAKE_DIR}/helm/rollback.json"
run_vws_real --yes rollback;                      expect_rc "a failing helm rollback exits 1" 1
if ! grep -q $'^kubectl\tapply' "$VWS_FAKE_LOG"; then ok "a failing helm rollback leaves the CR alone"; else bad "CR touched" "$(calls)"; fi

installer_scenario dry
run_vws_real --dry-run --yes rollback;            expect_rc "--dry-run exits 0" 0
assert_no_changes "--dry-run changes nothing"

installer_scenario bad
run_vws_real --yes rollback --to 9;               expect_rc "an unknown revision exits 2" 2
run_vws_real --yes rollback --to x;               expect_rc "a bad revision exits 2" 2
rm "${VWS_FAKE_DIR}/helm/history.json"
run_vws_real --yes rollback;                      expect_rc "no readable history exits 3" 3

finish_tests
