#!/usr/bin/env bash
# vws upgrade: same tag unchanged; a failing --atomic apply exits 1 and leaves the CR at the old value.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"

# saved_specs: the spec of every CR document written through "kubectl apply -f -", one line each.
saved_specs() {
  python3 - "${VWS_FAKE_LOG}.stdin" <<'PY'
import sys, yaml, json
for doc in open(sys.argv[1]).read().split("--- apply\n"):
    try:
        d = yaml.safe_load(doc)
    except yaml.YAMLError:
        continue
    if isinstance(d, dict) and d.get("kind") == "VWSCluster":
        print(json.dumps(d["spec"], sort_keys=True))
PY
}

installer_scenario up
run_vws_real upgrade --to v2;                     expect_rc "upgrade exits 0" 0
expect_text "the CR gets the new tag" "$(saved_specs)" '"version": "v2"'
expect_text "helm keeps 10 revisions" "$(calls)" "--history-max 10"
expect_text "helm records the spec in the revision" "$(calls)" 'vws-spec:{'
expect_text "helm is atomic" "$(calls)" "--atomic --wait"
expect_text "the backup WARN is printed" "$OUT" "WARN  backup"
expect_text "no zero-downtime claim" "$OUT" "zero downtime is not claimed until vws-deploy#39"

installer_scenario same
edit() { jq "$1" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/c" && mv "${WORK}/c" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"; }
edit '.spec.version = "v2"'
run_vws_real upgrade --to v2;                     expect_rc "same tag exits 0" 0
expect_text "same tag is unchanged" "$OUT" "unchanged"
assert_no_changes "same tag changes nothing"

installer_scenario atomic-fails
rm "${VWS_FAKE_DIR}/helm/upgrade.json"
run_vws_real upgrade --to v2;                     expect_rc "a failing --atomic apply exits 1" 1
expect_text "the failure says the CR is back" "$OUT" "the CR spec is back to the previous value"
last=$(saved_specs | tail -1)
if [[ $last != *'"version"'* && $last == *'"profile": "single-node"'* ]]; then ok "the last saved CR is the previous spec"; else bad "CR kept" "$(saved_specs)"; fi

installer_scenario dry
run_vws_real --dry-run upgrade --to v2;           expect_rc "--dry-run exits 0" 0
assert_no_changes "--dry-run changes nothing"
expect_text "--dry-run names the change" "$OUT" "-> v2"

installer_scenario bad
run_vws_real upgrade;                             expect_rc "no --to exits 2" 2
run_vws_real upgrade --to 'bad tag';              expect_rc "a bad tag exits 2" 2
assert_no_changes "a bad tag changes nothing"
rm "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real upgrade --to v2;                     expect_rc "no CR exits 2" 2

finish_tests
