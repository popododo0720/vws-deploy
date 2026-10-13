#!/usr/bin/env bash
# vws init: --from and questions build the same CR; the same CR is unchanged.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"
EX="${VWS_TEST_ROOT}/examples/vwscluster"

installer_scenario fresh
rm "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real init --from "${EX}/small.yaml";     expect_rc "init --from saves" 0
expect_text "init saves" "$OUT" "saved VWSCluster vws"
kinds=$(grep "^kind:" "${VWS_FAKE_LOG}.stdin" | head -2 | paste -sd" ")
if [[ $kinds == "kind: CustomResourceDefinition kind: VWSCluster" ]]; then ok "init applies the CRD before the CR (#112)"; else bad "init CRD before CR" "$kinds"; fi
assert_changes "the first init applies the CR"

installer_scenario same
run_vws_real init --from "${EX}/single-node.yaml"; expect_rc "init with the same spec exits 0" 0
expect_text "the same spec is unchanged" "$OUT" "unchanged"
assert_no_changes "the same spec changes nothing"

# Questions: the answers that give single-node.yaml give the same spec, so unchanged too.
run_vws_real init <<<$'single-node\nrook-ceph\n\n198.18.0.0/24\n'; expect_rc "interactive init exits 0" 0
expect_text "interactive answers equal to --from are unchanged" "$OUT" "unchanged"
assert_no_changes "interactive same spec changes nothing"

installer_scenario other
run_vws_real init <<<$'ha\nexisting\nfast-rbd\n0.1.0\n198.18.1.0/24\n'; expect_rc "interactive init with new answers saves" 0
assert_changes "new answers apply the CR"

run_vws_real init <<<$'single-node\nrook-ceph\n\n\n'; expect_rc "no elastic IP pool answer exits 2" 2
assert_no_changes "a CR without a pool is not saved"
run_vws_real init <<<$'huge\n\n\n';              expect_rc "an invalid answer exits 2" 2
assert_no_changes "an invalid CR is not saved"
sed 's/driver: kube-ovn/driver: calico/' "${EX}/ha.yaml" >"${WORK}/bad.yaml"
run_vws_real init --from "${WORK}/bad.yaml";     expect_rc "an invalid file exits 2" 2
run_vws_real init --from "${WORK}/missing.yaml"; expect_rc "a missing file exits 2" 2

installer_scenario expired
temp_binding 20000 14400
run_vws_real init --from "${EX}/small.yaml";     expect_rc "past the TTL init revokes and exits 1" 1
expect_text "the TTL failure says so" "$OUT" "past its TTL and is revoked"
expect_text "the expired binding is deleted" "$(calls)" "delete clusterrolebinding vws-installer-temp"

finish_tests
