#!/usr/bin/env bash
# vws restore: checksums first, plan without --yes, CR + apply + ConfigMaps with --yes, unchanged twice.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"
# shellcheck source=tests/vws/backup_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/backup_lib.sh"
REAL_HELM=$(command -v helm)

backup_scenario src
run_vws_real backup --out "${WORK}/b";                  expect_rc "the backup to restore exits 0" 0
template() {
  python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/render.py" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" "${WORK}/want-values.yaml"
  "$REAL_HELM" template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/want-values.yaml" >"${VWS_FAKE_DIR}/helm/template.json"
}

# An empty cluster: no CR, no ConfigMaps (plan only; the fake cluster does not keep writes).
backup_scenario empty
template
mv "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" "${WORK}/cr-kept.json"
echo '{"items":[]}' >"${VWS_FAKE_DIR}/kubectl/get_configmaps.json"
run_vws_real restore "${WORK}/b";                       expect_rc "without --yes exits 2" 2
expect_text "the plan creates the CR" "$OUT$ERR" "VWSCluster vws: create"
expect_text "the plan names the ConfigMaps" "$OUT$ERR" "ConfigMaps to write: vm-meta-fake"
expect_text "the plan points at snapshots" "$OUT$ERR" "state-snap-fake"
assert_no_changes "without --yes nothing changes"
run_vws_real --dry-run restore "${WORK}/b";             expect_rc "--dry-run exits 0" 0
assert_no_changes "--dry-run changes nothing"
# The CR now differs from the backup: overwrite.
jq '.spec.version = "old-fake"' "${WORK}/cr-kept.json" >"${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real --yes restore "${WORK}/b";                 expect_rc "--yes exits 0" 0
expect_text "the CRD is applied" "$(cat "${VWS_FAKE_LOG}.stdin")" "CustomResourceDefinition"
expect_text "the CR is saved" "$OUT" "VWSCluster vws saved from the backup"
expect_text "apply runs" "$(calls)" "upgrade --install vws"
expect_text "the ConfigMap is written" "$(cat "${VWS_FAKE_LOG}.stdin")" "vm-meta-fake"
expect_text "doctor is folded in" "$OUT" "after-"

# The same backup again on a cluster that already matches: unchanged.
backup_scenario same
template
run_vws_real --yes restore "${WORK}/b";                 expect_rc "a second restore exits 0" 0
expect_text "a second restore is unchanged" "$OUT" "unchanged: the cluster already matches the backup"
if grep -q vm-meta-fake "${VWS_FAKE_LOG}.stdin"; then bad "a second restore writes no ConfigMap" "written"; else ok "a second restore writes no ConfigMap"; fi

# Tampered: exit 1 before any write.
cp -r "${WORK}/b" "${WORK}/t"
echo '{"items":[]}' >"${WORK}/t/configmaps.json"
run_vws_real --yes restore "${WORK}/t";                 expect_rc "a checksum mismatch exits 1" 1
expect_text "the mismatch names the file" "$OUT" "configmaps.json"
assert_no_changes "a checksum mismatch writes nothing"

run_vws_real restore "${WORK}/nosuch";                  expect_rc "not a backup exits 2" 2
run_vws_real restore;                                   expect_rc "no directory exits 2" 2

finish_tests
