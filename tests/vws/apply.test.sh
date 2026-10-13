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
# The cluster has the chart's CRD: no CRD apply, one apply per bundle file (#114).
if [[ $applies -eq 3 ]]; then ok "one apply per bundle file, none for a current CRD"; else bad "one apply per bundle file" "$applies"; fi
expect_text "the CRD is read before the CR" "$(calls)" "get crd vwsclusters.${GROUP}"
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

# #171: an apply before #165 left ovn-external-gw-config; it breaks the underlay bridge.
extgw_cm() { # MANAGER: the ConfigMap as kubectl get returns it, written by MANAGER
  jq -n --arg m "$1" '{apiVersion: "v1", kind: "ConfigMap", metadata: {name: "ovn-external-gw-config", namespace: "kube-system",
    managedFields: [{manager: $m, operation: "Apply"}]}, data: {"enable-external-gw": "true", "external-gw-nic": "enp4s0"}}' \
    >"${VWS_FAKE_DIR}/kubectl/get_configmap.json"
}
installer_scenario extgw
run_vws_real apply --unattended;                 expect_rc "apply without the ConfigMap exits 0" 0
if grep $'^kubectl\tdelete' "$VWS_FAKE_LOG" | grep -q 'delete configmap ovn-external-gw-config'; then bad "no ConfigMap, no delete" "$(calls)"; else ok "no ConfigMap, no delete"; fi
expect_text "the ConfigMap is looked up in kube-system" "$(calls)" "--namespace=kube-system get configmap ovn-external-gw-config -o json --ignore-not-found"
extgw_cm vws
run_vws_real apply --unattended --dry-run;       expect_rc "dry-run with the ConfigMap exits 0" 0
if grep -q $'^kubectl\tdelete' "$VWS_FAKE_LOG"; then bad "dry-run deletes nothing" "$(calls)"; else ok "dry-run deletes nothing"; fi
run_vws_real apply --unattended;                 expect_rc "apply with the old ConfigMap exits 0" 0
expect_text "the old ConfigMap is deleted in kube-system" "$(grep $'^kubectl\tdelete' "$VWS_FAKE_LOG")" "--namespace=kube-system delete configmap ovn-external-gw-config --ignore-not-found"
expect_text "the delete is reported" "$OUT" "OK    extgw"
del_line=$(grep -n $'^kubectl\tdelete' "$VWS_FAKE_LOG" | cut -d: -f1)
helm_line=$(grep -n $'^helm\tupgrade' "$VWS_FAKE_LOG" | cut -d: -f1)
if ((del_line < helm_line)); then ok "the ConfigMap goes before the release"; else bad "delete before helm" "$(calls)"; fi
hash=$(grep -o '"appliedValuesHash":"[0-9a-f]*"' "$VWS_FAKE_LOG" | cut -d'"' -f4)
jq --arg h "$hash" '.status = {observedGeneration: 2, appliedValuesHash: $h}' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply --unattended;                 expect_rc "an unchanged apply with the old ConfigMap exits 0" 0
expect_text "an unchanged apply is still unchanged" "$OUT" "unchanged"
if grep $'^kubectl\tdelete' "$VWS_FAKE_LOG" | grep -q 'delete configmap ovn-external-gw-config'; then ok "an unchanged apply still deletes the old ConfigMap"; else bad "unchanged delete" "$(calls)"; fi
extgw_cm someone-else
run_vws_real apply --unattended;                 expect_rc "a ConfigMap vws did not write exits 0" 0
if grep -q $'^kubectl\tdelete' "$VWS_FAKE_LOG"; then bad "a foreign ConfigMap is kept" "$(calls)"; else ok "a foreign ConfigMap is kept"; fi
expect_text "a foreign ConfigMap is a WARN" "$OUT" "WARN  extgw"

# #114: an older CRD (no spec.storage.s3) is applied before the CR is read, so the CR keeps the field.
installer_scenario oldcrd
jq 'del(.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.storage.properties.s3)' \
  "${VWS_FAKE_DIR}/kubectl/get_crd.json" >"${WORK}/oldcrd.json" && mv "${WORK}/oldcrd.json" "${VWS_FAKE_DIR}/kubectl/get_crd.json"
run_vws_real apply --unattended;                 expect_rc "apply over an older CRD exits 0" 0
expect_text "the CRD step is reported" "$OUT" "OK    crd"
crd_line=$(grep -n $'^kubectl\tapply' "$VWS_FAKE_LOG" | head -1 | cut -d: -f1)
cr_line=$(grep -n -E $'^kubectl\tget\t.*vwsclusters vws' "$VWS_FAKE_LOG" | head -1 | cut -d: -f1 || true)
if [[ -n $crd_line && -n $cr_line ]] && ((crd_line < cr_line)); then ok "the CRD is applied before the CR is read"; else bad "CRD apply before CR read" "$(calls)"; fi
# grep -c reads to the end: grep -q would close the pipe early and, under pipefail, fail on sed's SIGPIPE
# once the CRD outgrows the pipe buffer.
if (( $(sed -n '2,/^---/p' "${VWS_FAKE_LOG}.stdin" | grep -c '^kind: CustomResourceDefinition') > 0 )); then ok "the first applied document is the CRD"; else bad "first document" "$(head -5 "${VWS_FAKE_LOG}.stdin")"; fi

# #144: apply -f FILE saves the file as the CR first (vws init --from), then applies; twice is unchanged.
EX="${VWS_TEST_ROOT}/examples/vwscluster"
installer_scenario same
run_vws_real apply --unattended -f "${EX}/single-node.yaml"; expect_rc "apply -f exits 0" 0
expect_text "apply -f runs init --from first" "$OUT" "OK    init"
expect_text "apply -f then applies" "$(calls)" "upgrade --install vws"
hash=$(grep -o '"appliedValuesHash":"[0-9a-f]*"' "$VWS_FAKE_LOG" | cut -d'"' -f4)
jq --arg h "$hash" '.status = {observedGeneration: (.metadata.generation // 0), appliedValuesHash: $h}' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply --unattended -f "${EX}/single-node.yaml"; expect_rc "a second apply -f exits 0" 0
expect_text "the second apply -f leaves the CR unchanged" "$OUT" "unchanged: VWSCluster vws already has this spec"
expect_text "the second apply -f applies nothing" "$OUT" "OK    apply"
assert_no_changes "the second apply -f changes nothing"
sed 's/driver: kube-ovn/driver: calico/' "${EX}/single-node.yaml" >"${WORK}/bad-f.yaml"
run_vws_real apply --unattended -f "${WORK}/bad-f.yaml";   expect_rc "apply -f with an invalid file exits 2" 2
assert_no_changes "an invalid -f file changes nothing"
if grep -q $'^helm\tupgrade' "$VWS_FAKE_LOG"; then bad "an invalid -f file does not reach helm" "$(calls)"; else ok "an invalid -f file does not reach helm"; fi
run_vws_real apply -f;                                      expect_rc "-f without a file exits 2" 2

finish_tests
