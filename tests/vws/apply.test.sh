#!/usr/bin/env bash
# vws apply: render, bundle in file order, helm, status; a second apply is unchanged.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"

installer_scenario first
run_vws_real apply --unattended;                 expect_rc "the first apply exits 0" 0
assert_changes "the first apply changes the cluster"
expect_text "helm runs atomic and wait" "$(calls)" "upgrade --install vws"
expect_text "the status is written" "$(calls)" "patch vwsclusters vws --subresource=status"
order=$(grep -o 'bundle  [0-9a-z.-]*' <<<"$OUT" | awk '{print $2}' | paste -sd' ')
want=$(cd "$VWS_BUNDLE_DIR" && printf '%s\n' *.yaml | sort | paste -sd' ')
if [[ $order == "$want" ]]; then ok "the bundle goes in file number order"; else bad "bundle order" "${order} vs ${want}"; fi
applies=$(grep -c $'^kubectl\tapply' "$VWS_FAKE_LOG")
if [[ $applies -eq 3 ]]; then ok "one apply per bundle file"; else bad "one apply per bundle file" "$applies"; fi
first_helm=$(grep -n $'^helm\tupgrade' "$VWS_FAKE_LOG" | cut -d: -f1)
last_apply=$(grep -n $'^kubectl\tapply' "$VWS_FAKE_LOG" | tail -1 | cut -d: -f1)
if ((first_helm > last_apply)); then ok "helm runs after the bundle"; else bad "helm after bundle" "$(calls)"; fi
hash=$(grep -o '"appliedValuesHash":"[0-9a-f]*"' "$VWS_FAKE_LOG" | cut -d'"' -f4)

# The cluster now reports what the first apply wrote: the second apply is unchanged.
jq --arg h "$hash" '.status = {observedGeneration: 2, appliedValuesHash: $h}' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply;                              expect_rc "the second apply exits 0" 0
expect_text "the second apply is unchanged" "$OUT" "unchanged"
assert_no_changes "the second apply changes nothing"

jq '.metadata.generation = 3' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real --dry-run apply;                    expect_rc "a new generation with --dry-run exits 0" 0
assert_no_changes "dry-run changes nothing"
expect_text "dry-run prints the helm plan" "$ERR" "DRYRUN helm upgrade --install vws"

installer_scenario no-cr
rm "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply;                              expect_rc "no CR exits 2" 2

installer_scenario bad-cr
jq '.spec.network.driver = "calico"' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply;                              expect_rc "an invalid CR exits 2" 2
assert_no_changes "an invalid CR changes nothing"

installer_scenario helm-fails
rm "${VWS_FAKE_DIR}/helm/upgrade.json"
run_vws_real apply;                              expect_rc "a failed helm upgrade exits 1" 1
if grep -q $'^kubectl\tpatch' "$VWS_FAKE_LOG"; then bad "no status after a failed helm" "$(calls)"; else ok "no status after a failed helm"; fi

installer_scenario fresh-binding
temp_binding 60 14400
run_vws_real apply;                              expect_rc "a binding within its TTL does not stop apply" 0
installer_scenario expired
temp_binding 20000 14400
run_vws_real apply;                              expect_rc "past the TTL apply revokes and exits 1" 1
if grep -q $'^helm\tupgrade' "$VWS_FAKE_LOG"; then bad "no install past the TTL" "$(calls)"; else ok "no install past the TTL"; fi

finish_tests
