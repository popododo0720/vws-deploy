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

# with_doctor_fixtures: add the healthy status/doctor answers (tests/fixtures/status/ok) to the scenario.
with_doctor_fixtures() {
  cp -rn "${FIXTURES}/status/ok/." "${VWS_FAKE_DIR}/"
  local f="${VWS_FAKE_DIR}/kubectl/get_crd.json" env="${VWS_TEST_ROOT}/scripts/versions.env"
  jq --arg g "$GROUP" '(.items[] | select(.spec.group == "FILLED_BY_TEST")) |= (.spec.group = $g | .metadata.name = "vwsclusters." + $g)' "$f" >"${f}.new" && mv "${f}.new" "$f"
  f="${VWS_FAKE_DIR}/kubectl/get_kubevirt.json"
  jq --arg v "$(sed -n 's/^KUBEVIRT_VERSION=//p' "$env")" '.items[0].status.observedKubeVirtVersion = $v' "$f" >"${f}.new" && mv "${f}.new" "$f"
  f="${VWS_FAKE_DIR}/kubectl/get_cdi.json"
  jq --arg v "$(sed -n 's/^CDI_VERSION=//p' "$env")" '.items[0].status.observedVersion = $v' "$f" >"${f}.new" && mv "${f}.new" "$f"
}

installer_scenario up
run_vws_real upgrade --to v2;                     expect_rc "upgrade exits 0" 0
expect_text "the CR gets the new tag" "$(saved_specs)" '"version": "v2"'
expect_text "helm keeps 10 revisions" "$(calls)" "--history-max 10"
expect_text "helm records the spec in the revision" "$(calls)" 'vws-spec:{'
expect_text "helm is atomic" "$(calls)" "--atomic --wait"
expect_text "the backup WARN is printed" "$OUT" "WARN  backup"
expect_text "no zero-downtime claim" "$OUT" "zero downtime is not claimed until vws-deploy#39"

installer_scenario judged
with_doctor_fixtures
run_vws_real upgrade --to v2;                     expect_rc "a healthy cluster after upgrade exits 0" 0
expect_text "doctor runs after the upgrade" "$OUT" "after-restarts"
expect_text "the upgrade is judged" "$OUT" "vws doctor reports no FAIL after the upgrade"
expect_text "doctor only reads" "$(changing_calls | grep -c 'rollback' || true)" "0"

installer_scenario judged-fail
with_doctor_fixtures
f="${VWS_FAKE_DIR}/kubectl/get_pods.json"
jq '.items[0].status.containerStatuses[0] |= (.restartCount = 9 | .state = {waiting: {reason: "CrashLoopBackOff"}})' "$f" >"${f}.new" && mv "${f}.new" "$f"
run_vws_real upgrade --to v2;                     expect_rc "a doctor FAIL after upgrade exits 1" 1
expect_text "the doctor FAIL is shown" "$OUT" "FAIL  after-restarts"
expect_text "the next step names rollback" "$OUT" "vws rollback --yes"
if ! grep -q $'^helm\trollback' "$VWS_FAKE_LOG"; then ok "no automatic rollback"; else bad "auto rollback" "$(calls)"; fi

installer_scenario unjudged
run_vws_real upgrade --to v2;                     expect_rc "doctor that cannot run: upgrade still exits 0" 0
expect_text "doctor that cannot run is a WARN" "$OUT" "WARN  after-upgrade"

installer_scenario same
edit() { jq "$1" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/c" && mv "${WORK}/c" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"; }
edit '.spec.version = "v2"'
run_vws_real upgrade --to v2;                     expect_rc "same tag exits 0" 0
expect_text "same tag is unchanged" "$OUT" "unchanged"
assert_no_changes "same tag changes nothing"

installer_scenario atomic-fails
rm "${VWS_FAKE_DIR}/helm/upgrade.json"
run_vws_real upgrade --to v2;                     expect_rc "a failing --atomic apply exits 1" 1
expect_text "the failure says the CR is back" "$OUT" "the VWSCluster spec is back to the previous value"
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
