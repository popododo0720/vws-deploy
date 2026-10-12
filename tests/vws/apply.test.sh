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
# Bundle files carry their own namespaces; -n vws on their apply makes kubectl refuse them.
if grep $'^kubectl\tapply' "$VWS_FAKE_LOG" | grep -q -e '--namespace' -e $'\t-n '; then bad "bundle apply has no namespace flag" "$(grep $'^kubectl\tapply' "$VWS_FAKE_LOG")"; else ok "bundle apply has no namespace flag"; fi
if grep $'^kubectl\twait' "$VWS_FAKE_LOG" | grep -e ' -A' | grep -q -e '--namespace'; then bad "the -A wait has no namespace flag" "$(grep $'^kubectl\twait' "$VWS_FAKE_LOG")"; else ok "the -A wait has no namespace flag"; fi
if [[ $(grep $'^kubectl\tapply' "$VWS_FAKE_LOG" | grep -c -e '--force-conflicts') -eq 3 ]]; then ok "bundle apply takes fields back from operators"; else bad "bundle apply --force-conflicts" "$(grep $'^kubectl\tapply' "$VWS_FAKE_LOG")"; fi
if grep $'^kubectl\tpatch' "$VWS_FAKE_LOG" | grep -q -e '--namespace=vws'; then ok "the CR status patch keeps the release namespace"; else bad "status patch namespace" "$(grep $'^kubectl\tpatch' "$VWS_FAKE_LOG")"; fi
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

# The applied hash covers the chart: the same values with a chart-only change are not unchanged (#101).
mkdir -p "${WORK}/root/charts" && cp -r "${VWS_TEST_ROOT}/charts/vws" "${WORK}/root/charts/vws"
printf 'a: 1\n' >"${WORK}/values.yaml"
h1=$(VWS_ROOT="${WORK}/root" bash -c 'source "$1"; vws_applied_hash "$2"' _ "${VWS_TEST_ROOT}/scripts/vws.d/lib/hash.sh" "${WORK}/values.yaml")
echo '# chart-only change' >>"${WORK}/root/charts/vws/templates/control-plane.yaml"
h2=$(VWS_ROOT="${WORK}/root" bash -c 'source "$1"; vws_applied_hash "$2"' _ "${VWS_TEST_ROOT}/scripts/vws.d/lib/hash.sh" "${WORK}/values.yaml")
if [[ -n $h1 && $h1 != "$h2" ]]; then ok "a chart-only change changes the applied hash"; else bad "chart hash" "${h1} ${h2}"; fi
h3=$(sha256sum "${WORK}/values.yaml" | cut -d' ' -f1)
if [[ $h1 != "$h3" ]]; then ok "the applied hash is not the values hash alone"; else bad "values-only hash" "$h1"; fi
jq '.status.appliedValuesHash = "values-only-hash-of-an-older-chart"' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply;                              expect_rc "an apply after a chart change exits 0" 0
if [[ $OUT != *unchanged* ]]; then ok "an apply after a chart change is not unchanged"; else bad "chart change applied" "$OUT"; fi

jq '.metadata.generation = 3' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real --dry-run apply;                    expect_rc "a new generation with --dry-run exits 0" 0
assert_no_changes "dry-run changes nothing"
expect_text "dry-run prints the helm plan" "$ERR" "DRYRUN helm upgrade --install vws"

installer_scenario preflight-fail
echo '{"items":[]}' >"${VWS_FAKE_DIR}/kubectl/get_storageclass.json"
run_vws_real apply;                              expect_rc "a failing preflight exits 1" 1
expect_text "a failing preflight says so" "$OUT" "preflight failed"
assert_no_changes "a failing preflight changes nothing"

installer_scenario description-cap
run_vws_real apply;                              expect_rc "a short spec: apply exits 0" 0
expect_text "a short spec is recorded in full" "$(calls)" '--description vws-spec:{'
spec_json=$(jq -cS '.spec' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json")
spec_hash=$(printf '%s' "$spec_json" | sha256sum | cut -d' ' -f1)
export VWS_SPEC_DESCRIPTION_MAX=20
run_vws_real apply;                              expect_rc "a spec over the cap: apply exits 0" 0
expect_text "over the cap only the hash is in the description" "$(calls)" "--description vws-spec-sha256:${spec_hash}"
patch=$(grep $'^kubectl\tpatch' "$VWS_FAKE_LOG" | grep -o '{"status".*}')
if jq -e --arg h "$spec_hash" --argjson s "$spec_json" '.status.specHistory[-1] == {specHash: $h, spec: $s}' <<<"$patch" >/dev/null; then
  ok "over the cap the full spec goes to status.specHistory"; else bad "specHistory" "$patch"; fi
jq '.status.specHistory = [range(10) | {specHash: "old\(.)", spec: {profile: "small"}}]' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply;                              expect_rc "a full specHistory: apply exits 0" 0
patch=$(grep $'^kubectl\tpatch' "$VWS_FAKE_LOG" | grep -o '{"status".*}')
if jq -e --arg h "$spec_hash" '(.status.specHistory | length) == 10 and .status.specHistory[0].specHash == "old1" and .status.specHistory[-1].specHash == $h' <<<"$patch" >/dev/null; then
  ok "specHistory keeps the newest 10"; else bad "specHistory bound" "$patch"; fi
unset VWS_SPEC_DESCRIPTION_MAX

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
