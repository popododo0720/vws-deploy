#!/usr/bin/env bash
# Upgrade compat: the VWSCluster record written by 0.1.0 renders and upgrades with the current code
# (#396), and the upgrade adds the chart CRDs that release lacks before it reads the CR (#381).
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"

FIXTURE="${VWS_TEST_ROOT}/testdata/upgrade/0.1.0/vwscluster.json"

# fixture OUT: the record as the cluster returns it. apiVersion is FILLED_BY_TEST in the fixture
# because R18 keeps the API group only in charts/vws/crds/vwsclusters.yaml.
fixture() {
  jq --arg v "${GROUP}/v1alpha1" '.apiVersion = $v' "$FIXTURE" >"$1"
}

# behind_release IN OUT: the record while the release is behind it. Its status.observedGeneration
# is older than metadata.generation, the state vws apply upgrades from.
behind_release() {
  jq '.status.observedGeneration = 1' "$1" >"$2"
}

# rendered_hash VALUES_FILE: the applied hash the current chart gives those values.
rendered_hash() {
  VWS_ROOT="$VWS_TEST_ROOT" bash -c 'source "$1"; vws_applied_hash "$2"' _ \
    "${VWS_TEST_ROOT}/scripts/vws.d/lib/hash.sh" "$1"
}

# status_hash: appliedValuesHash of the last status patch.
status_hash() {
  grep $'^kubectl\tpatch' "$VWS_FAKE_LOG" | grep -o '{"status".*}' | jq -r '.status.appliedValuesHash'
}

# The fields vws apply reads outside the spec keep their names, types and values.
if jq -e '(.metadata.name == "vws") and (.metadata.generation == 2) and
          (.status.observedGeneration == 2) and
          ((.status.appliedValuesHash | type) == "string") and
          (.status.appliedValuesHash | test("^[0-9a-f]{64}$"))' "$FIXTURE" >/dev/null; then
  ok "the fixture keeps the identity and status fields vws apply reads"
else
  bad "fixture shape" "$(jq -c '{metadata, status}' "$FIXTURE")"
fi

# The record decodes: every spec field 0.1.0 wrote still reaches the values.
fixture "${WORK}/vwscluster.json"
run_vws_real render "${WORK}/vwscluster.json" "${WORK}/values.yaml"
expect_rc "the 0.1.0 record renders" 0
expect_text "the 0.1.0 profile is kept" "$(cat "${WORK}/values.yaml")" "profile: single-node"
expect_text "the 0.1.0 image tag is kept" "$(cat "${WORK}/values.yaml")" "tag: sha-0123456"
expect_text "the 0.1.0 storage class is kept" "$(cat "${WORK}/values.yaml")" "storageClass: fast-rbd"
expect_text "the 0.1.0 elastic IP pool is kept" "$(cat "${WORK}/values.yaml")" "pool: 198.18.0.0/24"
expect_text "the 0.1.0 console origin is kept" "$(cat "${WORK}/values.yaml")" "console-origins: https://console.example.invalid"
expect_text "the 0.1.0 VM MTU is kept" "$(cat "${WORK}/values.yaml")" "vm-mtu: 8900"

# Upgrade from an install older than charts/vws/crds/iam.yaml (#369): Helm does not upgrade crds/
# on its own, so the cluster has no iam CRDs until vws apply adds them (#381). The record is the
# same 0.1.0 record; only the cluster's CRD set is older.
installer_scenario upgrade
behind_release "${WORK}/vwscluster.json" "${WORK}/behind.json"
cp "${WORK}/behind.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
jq 'del(.items[] | select(.spec.group | startswith("iam.")))' \
  "${VWS_FAKE_DIR}/kubectl/get_crd.json" >"${WORK}/crds.json"
mv "${WORK}/crds.json" "${VWS_FAKE_DIR}/kubectl/get_crd.json"
installer_template
run_vws_real apply --unattended
expect_rc "the 0.1.0 record upgrades" 0
expect_text "the missing chart CRDs are applied" "$OUT" "OK    crd"
iam_docs=$(grep -c 'group: iam\.' "${VWS_FAKE_LOG}.stdin" || true)
if ((iam_docs >= 11)); then ok "the iam CRDs are applied"; else bad "iam CRDs applied" "$(head -5 "${VWS_FAKE_LOG}.stdin")"; fi
wait_line=$(grep -n $'^kubectl\twait.*condition=Established crd --all' "$VWS_FAKE_LOG" | head -1 | cut -d: -f1 || true)
cr_line=$(grep -n -E $'^kubectl\tget\t.*vwsclusters vws' "$VWS_FAKE_LOG" | head -1 | cut -d: -f1 || true)
if [[ -n $wait_line && -n $cr_line ]] && ((wait_line < cr_line)); then
  ok "the CRD wait comes before the CR is read"
else
  bad "CRD wait before CR read" "$(calls)"
fi
expect_text "the release is upgraded" "$(calls)" "upgrade --install vws"
if [[ $(status_hash) == "$(rendered_hash "${WORK}/values.yaml")" ]]; then
  ok "the status records the hash of the values the 0.1.0 record renders"
else
  bad "status hash" "$(status_hash) vs $(rendered_hash "${WORK}/values.yaml")"
fi
if grep -q $'^kubectl\tdelete' "$VWS_FAKE_LOG"; then bad "no CRD is deleted" "$(calls)"; else ok "no CRD is deleted"; fi

# A renamed field is a record the current code cannot read: apply fails instead of dropping it.
jq 'del(.spec.network.driver) | .spec.network.driverName = "kube-ovn"' "${WORK}/vwscluster.json" >"${WORK}/renamed.json"
installer_scenario renamed
cp "${WORK}/renamed.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply --unattended
expect_rc "a renamed fixture field exits 2" 2
expect_text "the renamed field is named" "$ERR" "spec.network.driverName: unknown field"
assert_no_changes "a renamed fixture field changes nothing"

# A changed encoding (here a string field written as a number) also fails.
jq '.spec.version = 0.1' "${WORK}/vwscluster.json" >"${WORK}/reencoded.json"
installer_scenario reencoded
cp "${WORK}/reencoded.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real apply --unattended
expect_rc "a re-encoded fixture field exits 2" 2
expect_text "the re-encoded field is named" "$ERR" "spec.version: want string"
assert_no_changes "a re-encoded fixture field changes nothing"

finish_tests
