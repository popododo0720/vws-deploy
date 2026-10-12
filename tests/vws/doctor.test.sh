#!/usr/bin/env bash
# vws doctor: status plus diagnosis; one scenario per finding from tests/fixtures/status/ok.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/status_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/status_lib.sh"

scenario ok
run_vws_real doctor;                          expect_rc "doctor: all healthy exits 0" 0
assert_no_changes "doctor changes nothing"
expect_line "doctor checks restarts" OK restarts
expect_line "doctor checks the VWSCluster CRD" OK vws-crd
expect_line "doctor checks ceph" OK ceph HEALTH_OK
expect_line "doctor checks pendingFlags" OK pending-flags "global.pendingFlags=false"
run_vws_real -o json doctor;                  expect_rc "doctor json exits 0" 0
if jq -e '.command == "doctor" and .status == "ok" and (.checks | length) >= 20' <<<"$OUT" >/dev/null; then ok "doctor json"; else bad "doctor json" "$OUT"; fi

# The operating role after vws installer finish: no Secrets, so helm cannot read the release.

scenario operator
rm "${VWS_FAKE_DIR}"/helm/*.json
echo 'Error: query: failed to query with labels: secrets is forbidden: User "system:serviceaccount:vws:vws-installer" cannot list resource "secrets" in API group "" in the namespace "vws"' >"${VWS_FAKE_DIR}/helm/status.err"
echo no >"${VWS_FAKE_DIR}/kubectl/auth_can-i.json"
run_vws_real doctor;                          expect_rc "operator creds without reads: doctor WARN only exits 0" 0
expect_line "doctor reports missing reads" WARN read-rights "get secrets"

scenario not-installed
rm "${VWS_FAKE_DIR}/helm/status.json"
echo 'Error: release: not found' >"${VWS_FAKE_DIR}/helm/status.err"
run_vws_real doctor;                          expect_rc "no release: doctor exits 1" 1
expect_line "no release: doctor FAIL" FAIL release "next: vws apply"

scenario restart-loop
edit_json kubectl/get_pods.json '.items[0].status.containerStatuses[0] |= (.restartCount = 7 | .state = {waiting: {reason: "CrashLoopBackOff"}})'
run_vws_real doctor;                          expect_rc "restart loop exits 1" 1
expect_line "restart loop FAIL" FAIL restarts "CrashLoopBackOff"
run_vws_real status;                          expect_rc "status ignores restarts (doctor only)" 0

scenario pending-pvc
edit_json kubectl/get_pvc.json '.items[0].status.phase = "Pending"'
run_vws_real doctor;                          expect_rc "Pending PVC exits 1" 1
expect_line "Pending PVC FAIL" FAIL pending-pvc "vws/data-storage-0"

scenario crd-not-ready
edit_json kubectl/get_crd.json '(.items[] | select(.spec.group == "kubevirt.io") | .status.conditions) = []'
run_vws_real doctor;                          expect_rc "CRD not ready exits 1" 1
expect_line "CRD not ready FAIL" FAIL crd-kubevirt.io "not Established"

scenario vws-crd-missing
edit_json kubectl/get_crd.json --arg g "$GROUP" 'del(.items[] | select(.spec.group == $g))'
run_vws_real doctor;                          expect_rc "VWSCluster CRD missing exits 1" 1
expect_line "VWSCluster CRD missing FAIL" FAIL vws-crd "bootstrap"

scenario ceph
edit_json kubectl/get_cephcluster.json '.items[0].status.ceph.health = "HEALTH_ERR"'
run_vws_real doctor;                          expect_rc "HEALTH_ERR exits 1" 1
expect_line "HEALTH_ERR FAIL" FAIL ceph "HEALTH_ERR"

scenario pending-flags
edit_json kubectl/get_deployments,statefulsets.json '(.items[] | select(.metadata.name == "compute") | .spec.template.spec.containers[0].args) += ["-hypervisor=kubevirt"]'
# #121: the compute KubeVirt flags are ordinary flags now, so running with them is not a mismatch.
run_vws_real doctor;                          expect_rc "compute -hypervisor=kubevirt exits 0" 0
expect_line "compute -hypervisor=kubevirt is not a pendingFlags mismatch" OK pending-flags "global.pendingFlags=false"

scenario all-bad
edit_json kubectl/get_pvc.json '.items[0].status.phase = "Pending"'
edit_json kubectl/get_cephcluster.json '.items = []'
edit_json kubectl/get_deployments,statefulsets.json '.items = []'
run_vws_real doctor;                          expect_rc "many FAILs exit 1" 1
fails_have_next "every doctor FAIL has a next step"

finish_tests
