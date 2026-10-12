#!/usr/bin/env bash
# vws reconfigure: validate before writing, summarise the change, unchanged twice, --dry-run diff only.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"
REAL_HELM=$(command -v helm)

# expect_template CR_JSON_FILTER: the fake helm answers "template" with the real chart rendered
# from what reconfigure will render, so check_render.py sees real manifests.
expect_template() {
  jq "$1" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/want.json"
  python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/render.py" "${WORK}/want.json" "${WORK}/want-values.yaml"
  "$REAL_HELM" template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/want-values.yaml" >"${VWS_FAKE_DIR}/helm/template.json"
}
cr_writes() { grep -c $'^kubectl\tapply' "$VWS_FAKE_LOG" || true; }

installer_scenario set
expect_template '.spec.version = "v2"'
run_vws_real reconfigure --set spec.version=v2;   expect_rc "--set exits 0" 0
expect_text "the change is summarised" "$OUT" 'spec.version: (unset) -> "v2"'
expect_text "the CR is saved server-side" "$(calls)" "apply --server-side --field-manager=vws"
expect_text "apply runs after the save" "$(calls)" "upgrade --install vws"
expect_text "apply results are folded in" "$OUT" "OK    helm"

installer_scenario same
expect_template '.'
run_vws_real reconfigure --set profile=single-node; expect_rc "same value exits 0" 0
expect_text "same value is unchanged" "$OUT" "unchanged"
assert_no_changes "same value changes nothing"
cp "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" "${WORK}/same.json"
run_vws_real reconfigure --from "${WORK}/same.json"; expect_rc "same --from exits 0" 0
expect_text "same --from is unchanged" "$OUT" "unchanged"
assert_no_changes "same --from changes nothing"

installer_scenario bad
expect_template '.'
run_vws_real reconfigure --set nosuch=1;          expect_rc "an unknown key exits 2" 2
assert_no_changes "an unknown key writes nothing"
run_vws_real reconfigure --set profile=3;         expect_rc "a wrong type exits 2" 2
assert_no_changes "a wrong type writes nothing"
run_vws_real reconfigure --set storage.driver=existing; expect_rc "a missing required field exits 2" 2
assert_no_changes "a missing required field writes nothing"
run_vws_real reconfigure --set novalue;           expect_rc "--set without = exits 2" 2
run_vws_real reconfigure --set a=1 --from x;      expect_rc "--from with --set exits 2" 2
run_vws_real reconfigure;                         expect_rc "no input exits 2" 2
echo 'not: [valid' >"${WORK}/broken.yaml"
run_vws_real reconfigure --from "${WORK}/broken.yaml"; expect_rc "broken --from exits 2" 2

installer_scenario schema
# No template fixture: the fake helm fails, as helm does when the values break the schema.
run_vws_real reconfigure --set version=v3;        expect_rc "a schema failure from helm exits 2" 2
expect_text "the schema failure is named" "$ERR" "values.schema.json"
assert_no_changes "a schema failure writes nothing"

installer_scenario invariant
expect_template '.spec.version = "v4"'
python3 - "${VWS_FAKE_DIR}/helm/template.json" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") != "PodDisruptionBudget"]
open(sys.argv[1], "w").write(yaml.safe_dump_all(docs))
PY
run_vws_real reconfigure --set version=v4;        expect_rc "check_render.py rejection exits 2" 2
expect_text "the rejection is named" "$ERR" "check_render.py"
assert_no_changes "a check_render.py rejection writes nothing"

installer_scenario dry
expect_template '.spec.version = "v2"'
run_vws_real --dry-run reconfigure --set version=v2; expect_rc "--dry-run exits 0" 0
expect_text "--dry-run prints the diff" "$OUT" 'spec.version: (unset) -> "v2"'
assert_no_changes "--dry-run changes nothing"
if [[ $(cr_writes) == 0 && $ERR != *DRYRUN* ]]; then ok "--dry-run plans no write at all"; else bad "dry-run" "$ERR"; fi

installer_scenario secret-ref
expect_template '.'
# render.py takes no Secret name from the CR yet (vws-deploy#53): a reference is refused before any write.
run_vws_real reconfigure --set secrets.db.secretName=vws-db-fake; expect_rc "a secret reference is refused (2)" 2
assert_no_changes "a refused secret reference writes nothing"
# A secret-shaped field never shows a value, even if it reached the diff.
printf '%s\n' '{"spec": {"profile": "small", "flags": {"db-password": "FAKE-OLD"}}}' >"${WORK}/a.json"
printf 'spec:\n  profile: small\n  flags:\n    db-password: FAKE-NEW\n' >"${WORK}/b.yaml"
d=$(python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/reconfigure.py" diff "${WORK}/a.json" "${WORK}/b.yaml")
if [[ $d == *"(hidden)"* && $d != *FAKE-* ]]; then ok "a secret-shaped field is hidden"; else bad "hidden" "$d"; fi

installer_scenario json
expect_template '.spec.version = "v2"'
run_vws_real -o json reconfigure --set version=v2; expect_rc "json exits 0" 0
if jq -e '.command == "reconfigure" and any(.checks[]; .name == "change") and any(.checks[]; .name == "helm")' <<<"$OUT" >/dev/null; then ok "json holds the change and apply"; else bad "json" "$OUT"; fi

installer_scenario helm-fails
expect_template '.spec.version = "v2"'
rm "${VWS_FAKE_DIR}/helm/upgrade.json"
run_vws_real reconfigure --set version=v2;        expect_rc "a failing apply exits 1" 1
expect_text "the failing apply is reported" "$OUT" "FAIL  helm"

installer_scenario no-cr
rm "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real reconfigure --set version=v2;        expect_rc "no CR exits 2" 2

finish_tests
