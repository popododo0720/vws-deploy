#!/usr/bin/env bash
# vws status: one scenario per finding, built from tests/fixtures/status/ok by changing one file.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/status_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/status_lib.sh"

scenario ok
run_vws_real status;                          expect_rc "status: all healthy exits 0" 0
assert_no_changes "status changes nothing"
expect_line "status shows the applied generation" OK cluster-cr "generation 2 applied"
expect_line "status shows the release revision" OK release "revision 3 deployed"
if grep -q "bundle-kubevirt" <<<"$OUT" || grep -q $'kubectl\tget\tkubevirt' "$VWS_FAKE_LOG"; then
  bad "status ignores retired bundle pins" "$OUT"
else
  ok "status ignores retired bundle pins"
fi
if [[ -z $(grep -E '^(WARN|FAIL)' <<<"$OUT" || true) ]]; then ok "status ok has no WARN or FAIL"; else bad "status ok clean" "$OUT"; fi

scenario operator
rm "${VWS_FAKE_DIR}"/helm/*.json
echo 'Error: query: failed to query with labels: secrets is forbidden: User "system:serviceaccount:vws:vws-installer" cannot list resource "secrets" in API group "" in the namespace "vws"' >"${VWS_FAKE_DIR}/helm/status.err"
run_vws_real status;                          expect_rc "operator creds: status exits 0" 0
expect_line "operator creds: the release WARN says why" WARN release "cannot read Secrets"
expect_line "operator creds: the release WARN says what to do" WARN release "your own kubeconfig"
if ! grep -q $'^helm\tget' "$VWS_FAKE_LOG"; then ok "operator creds: no helm get after a forbidden status"; else bad "no helm get" "$(cat "$VWS_FAKE_LOG")"; fi
run_vws_real --strict status;                 expect_rc "operator creds: --strict status exits 1 (WARN)" 1

scenario not-installed
rm "${VWS_FAKE_DIR}/helm/status.json"
echo 'Error: release: not found' >"${VWS_FAKE_DIR}/helm/status.err"
run_vws_real status;                          expect_rc "no release: status WARN exits 0" 0
expect_line "no release: status WARN" WARN release "not installed"

scenario no-cr
rm "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real status;                          expect_rc "no CR exits 1" 1
expect_line "no CR names the operator read" FAIL cluster-cr "next: check the installed VWSCluster"

scenario cr-drift
edit_json kubectl/get_vwsclusters.json '.spec.profile = "small" | .metadata.generation = 3'
run_vws_real status;                          expect_rc "CR drift: WARN exits 0" 0
expect_line "CR drift: generation not applied" WARN cluster-cr "not applied"
run_vws_real --strict status;                 expect_rc "CR drift: --strict exits 1" 1

scenario failed-release
edit_json helm/status.json '.info.status = "failed"'
run_vws_real status;                          expect_rc "failed release exits 1" 1
expect_line "failed release says helm history" FAIL release "helm history vws"


scenario pdb
edit_json kubectl/get_pdb.json '.items[0].status.currentHealthy = 0'
run_vws_real status;                          expect_rc "short PDB WARN exits 0" 0
expect_line "short PDB WARN" WARN pdb "drains will block"

scenario api-down
rm "${VWS_FAKE_DIR}/kubectl/version_.json"
run_vws_real status;                          expect_rc "no API exits 3" 3
run_vws_real status extra;                    expect_rc "an argument is a usage error" 2

finish_tests
