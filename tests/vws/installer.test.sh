#!/usr/bin/env bash
# vws installer finish: revoke the temporary binding, verify with auth can-i, idempotent.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"

installer_scenario ok
run_vws_real installer finish;                   expect_rc "finish revokes and verifies" 0
expect_text "finish deletes the binding" "$OUT" "vws-installer-temp: deleted"
expect_text "finish checks * *" "$(calls)" "auth can-i * * --as=system:serviceaccount:vws:vws-installer"
expect_text "finish checks the kept rights" "$OUT" "can get vwsclusters"
assert_changes "finish deletes something"

: >"${VWS_FAKE_DIR}/kubectl/delete_clusterrolebinding.json"
run_vws_real installer finish;                   expect_rc "a second finish exits 0" 0
expect_text "a second finish is unchanged" "$OUT" "vws-installer-temp: unchanged"

VWS_INSTALLER_POD=1 run_vws_real installer finish; expect_rc "in the pod finish asks for itself" 0
if grep -q -- '--as=' "$VWS_FAKE_LOG"; then bad "in the pod no impersonation" "$(calls)"; else ok "in the pod no impersonation"; fi

installer_scenario still-admin
echo yes >"${VWS_FAKE_DIR}/kubectl/auth_can-i_all_all.json"
run_vws_real installer finish;                   expect_rc "cluster-admin still there exits 1" 1
expect_text "the failure names the right" "$OUT" "can still * *"

installer_scenario still-crb
echo yes >"${VWS_FAKE_DIR}/kubectl/auth_can-i_create_clusterrolebindings.json"
run_vws_real installer finish;                   expect_rc "create clusterrolebindings left exits 1" 1

installer_scenario lost-operating
echo no >"${VWS_FAKE_DIR}/kubectl/auth_can-i_get_vwsclusters.json"
run_vws_real installer finish;                   expect_rc "losing the operating Role exits 1" 1

installer_scenario no-answer
rm "${VWS_FAKE_DIR}/kubectl/auth_can-i_all_all.json"
run_vws_real installer finish;                   expect_rc "an unreadable can-i exits 3" 3

installer_scenario dry
run_vws_real --dry-run installer finish;         expect_rc "dry-run exits 0" 0
assert_no_changes "dry-run deletes nothing"
expect_text "dry-run prints the delete" "$ERR" "DRYRUN kubectl delete clusterrolebinding vws-installer-temp"

run_vws_real installer;                          expect_rc "installer without finish exits 2" 2
run_vws_real installer start;                    expect_rc "an unknown installer verb exits 2" 2

finish_tests
