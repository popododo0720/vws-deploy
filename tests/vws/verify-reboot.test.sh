#!/usr/bin/env bash
# vws verify-reboot: snapshot holds counts and names only; check passes gates in order, the blocked
# gate is the first line (1 when it cannot pass, 3 on timeout), reboot not observed is a WARN.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export VWS_VERIFY_INTERVAL=1

scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/verify/ok" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
}
edit_json() { # FILE FILTER
  local f="${VWS_FAKE_DIR}/kubectl/$1"
  jq "$2" "$f" >"${f}.new" && mv "${f}.new" "$f"
}
first_line() { head -1 <<<"$OUT"; }
rebooted() { edit_json get_nodes.json '.items[].status.nodeInfo.bootID |= sub("-1$"; "-2")'; }

scenario snapshot
run_vws_real verify-reboot --snapshot "${WORK}/snap.json"; expect_rc "snapshot exits 0" 0
assert_no_changes "snapshot changes nothing"
snap=$(cat "${WORK}/snap.json")
if ! grep -qi 'uid' <<<"$snap"; then ok "no UID in the snapshot"; else bad "uid" "$snap"; fi
if [[ $snap != *FAKE-CONFIGMAP-DATA* ]]; then ok "no ConfigMap data in the snapshot"; else bad "data" "$snap"; fi
if jq -e '.configMapKeys == [{name: "vws-compute-store", keys: 2}] and .podCounts.vws == 2 and (.alwaysVMs | length) == 1 and (.nodes | length) == 2' <<<"$snap" >/dev/null; then
  ok "the snapshot has counts and names"; else bad "snapshot shape" "$snap"; fi
mode=$(stat -c %a "${WORK}/snap.json")
if [[ $mode == 600 ]]; then ok "the snapshot is 0600"; else bad "mode" "$mode"; fi
expect_text "no RBD attachment is OK" "$OUT" "OK    krbd  no RBD volume attached"

# #116: a node with RBD volumes attached gets a drain WARN before the reboot.
scenario krbd
jq -n '{items: [
  {spec: {nodeName: "node-a", attacher: "rbd.csi.ceph.com"}, status: {attached: true}},
  {spec: {nodeName: "node-a", attacher: "rbd.csi.ceph.com"}, status: {attached: true}},
  {spec: {nodeName: "node-b", attacher: "other.csi"}, status: {attached: true}},
  {spec: {nodeName: "node-b", attacher: "rbd.csi.ceph.com"}, status: {attached: false}}]}' >"${VWS_FAKE_DIR}/kubectl/get_volumeattachments.json"
run_vws_real verify-reboot --snapshot "${WORK}/snap2.json"; expect_rc "snapshot with RBD attachments exits 0" 0
expect_text "the node with RBD volumes is warned" "$OUT" "WARN  krbd  node-a: 2 RBD volumes attached; drain it before rebooting"
if ! grep -q "node-b" <<<"$OUT"; then ok "other attachers and detached volumes are not warned"; else bad "node-b warned" "$OUT"; fi
assert_no_changes "the krbd check changes nothing"
run_vws_real verify-reboot --snapshot "${WORK}/snap.json"; expect_rc "an existing snapshot is not overwritten (2)" 2

# A real cluster's pods -A JSON is larger than the argument limit; the snapshot must still work.
scenario big-pods
jq '.items as $p | .items = [range(3000) as $i | $p[0] | .metadata.name = "pod-\($i)" | .metadata.annotations.pad = ("x" * 800)]' \
  "${VWS_FAKE_DIR}/kubectl/get_pods.json" >"${WORK}/pods.big" && mv "${WORK}/pods.big" "${VWS_FAKE_DIR}/kubectl/get_pods.json"
size=$(stat -c %s "${VWS_FAKE_DIR}/kubectl/get_pods.json")
if ((size > 2 * 1024 * 1024)); then ok "the pods fixture is over 2MB"; else bad "pods fixture size" "$size"; fi
run_vws_real verify-reboot --snapshot "${WORK}/snap-big.json"; expect_rc "a snapshot with a >2MB pods list exits 0" 0
if jq -e '[.podCounts[]] | add == 3000' "${WORK}/snap-big.json" >/dev/null 2>&1; then ok "every pod is counted"; else bad "big pod count" "$(cat "${WORK}/snap-big.json" 2>&1)"; fi
expect_text "the krbd check still runs" "$OUT" "OK    krbd  no RBD volume attached"

# A list that cannot be read makes the snapshot fail (3) without a file, so nobody reboots unchecked.
for resource in pods volumeattachments; do
  scenario "unreadable-${resource}"
  rm "${VWS_FAKE_DIR}/kubectl/get_${resource}.json"
  run_vws_real verify-reboot --snapshot "${WORK}/snap-${resource}.json"; expect_rc "unreadable ${resource} exits 3" 3
  expect_text "unreadable ${resource} says why" "$ERR" "snapshot failed: cannot list ${resource}"
  expect_text "unreadable ${resource} says do not reboot" "$ERR" "do not reboot"
  if [[ ! -e ${WORK}/snap-${resource}.json ]]; then ok "unreadable ${resource} leaves no snapshot"; else bad "snapshot left" "${resource}"; fi
done

scenario recovered
rebooted
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "a full recovery exits 0" 0
assert_no_changes "check changes nothing"
order=$(grep -o 'gate-[a-z]*' <<<"$OUT" | paste -sd' ')
if [[ $order == "gate-api gate-nodes gate-cni gate-csi gate-bundle gate-services" ]]; then ok "gates in boot order"; else bad "gate order" "$order"; fi
expect_text "gates have a time" "$OUT" "passed in"
expect_text "reboot observed" "$OUT" "OK    reboot"

scenario not-rebooted
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "no reboot observed exits 0 (WARN)" 0
expect_text "reboot not observed is a WARN" "$OUT" "WARN  reboot  reboot not observed"

scenario node-gone
rebooted
edit_json get_nodes.json 'del(.items[1])'
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "a node gone exits 1" 1
expect_text "the blocked gate is the first line" "$(first_line)" "FAIL  gate-nodes  blocked at gate nodes: node gone: node-b"

scenario ceph-err
rebooted
edit_json get_cephcluster.json '.items[0].status.ceph.health = "HEALTH_ERR"'
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "Ceph HEALTH_ERR exits 1" 1
expect_text "csi is the first line" "$(first_line)" "FAIL  gate-csi"
expect_text "earlier gates are still listed" "$OUT" "OK    gate-cni"

scenario cni-timeout
rebooted
edit_json get_pods.json '(.items[] | select(.metadata.name == "kube-ovn-cni-x1") | .status.containerStatuses[0].ready) = false'
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s; expect_rc "a gate still waiting at the timeout exits 3" 3
expect_text "the timed-out gate is the first line" "$(first_line)" "FAIL  gate-cni  blocked at gate cni after 2s (timeout)"
run_vws_real -o json verify-reboot --check "${WORK}/snap.json" --timeout 1s; expect_rc "json timeout exits 3" 3
if jq -e '.status == "timeout" and .checks[0].name == "gate-cni"' <<<"$OUT" >/dev/null; then ok "json timeout"; else bad "json timeout" "$OUT"; fi

scenario service-short
rebooted
edit_json get_deployments,statefulsets.json '(.items[] | select(.metadata.name == "console") | .status.readyReplicas) = 1'
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s; expect_rc "a service not ready at the timeout exits 3" 3
expect_text "services is the first line" "$(first_line)" "FAIL  gate-services"

scenario compare
rebooted
edit_json get_pvc.json '.items[1].status.phase = "Pending"'
edit_json get_configmaps.json '.items[0].data = {"vm-1": "x"}'
edit_json get_virtualmachines.json '.items[0].status.printableStatus = "Stopped"'
edit_json get_pods.json '(.items[] | select(.metadata.name == "compute-0") | .status.containerStatuses[0].restartCount) = 4'
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "failed comparisons exit 1" 1
expect_text "PVC not Bound" "$OUT" "FAIL  pvcs  not Bound or gone: data-network-0"
expect_text "fewer ConfigMap entries" "$OUT" "FAIL  configmaps  fewer entries: vws-compute-store 1/2"
expect_text "VM not Running" "$OUT" "FAIL  vms  not Running: vws-vms/vm-a Stopped"
expect_text "restarts over the threshold" "$OUT" "FAIL  restarts  over 3 restarts: compute-0 4"

scenario bad
run_vws_real verify-reboot;                      expect_rc "no mode exits 2" 2
run_vws_real verify-reboot --snapshot a --check b; expect_rc "both modes exit 2" 2
run_vws_real verify-reboot --check "${WORK}/missing.json"; expect_rc "a missing snapshot exits 2" 2
echo '{}' >"${WORK}/not.json"
run_vws_real verify-reboot --check "${WORK}/not.json"; expect_rc "a non-snapshot exits 2" 2
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout soon; expect_rc "a bad timeout exits 2" 2
rm "${VWS_FAKE_DIR}/kubectl/version_.json"
run_vws_real verify-reboot --snapshot "${WORK}/s2.json"; expect_rc "snapshot without API exits 3" 3

finish_tests
