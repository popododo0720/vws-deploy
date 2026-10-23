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
if jq -e '.configMapKeys == [{name: "vws-compute-store", keys: 2}] and .podCounts.vws == 2 and .restarts == {"compute-0": {c: 1}, "storage-0": {c: 0}} and (.alwaysVMs | length) == 1 and (.nodes | length) == 2' <<<"$snap" >/dev/null; then
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

# #150: the drain gate. A reboot goes ahead only when the node is cordoned and has no RBD attachment.
drain_case() { # NAME UNSCHEDULABLE ATTACHED WANT_RC TEXT
  scenario "drain-$1"
  jq -n --argjson u "$2" '{kind: "Node", metadata: {name: "node-a"}, spec: {unschedulable: $u}}' >"${VWS_FAKE_DIR}/kubectl/get_nodes.json"
  jq -n --argjson a "$3" '{items: [{spec: {nodeName: "node-a", attacher: "rbd.csi.ceph.com", source: {persistentVolumeName: "pv-a"}}, status: {attached: $a}},
    {spec: {nodeName: "node-b", attacher: "rbd.csi.ceph.com"}, status: {attached: true}}]}' >"${VWS_FAKE_DIR}/kubectl/get_volumeattachments.json"
  run_vws_real verify-reboot --drained node-a --timeout 2s; expect_rc "drain $1 exits $4" "$4"
  expect_text "drain $1 says why" "$OUT" "$5"
  assert_no_changes "drain $1 changes nothing"
}
drain_case finished true false 0 "OK    drained  node-a is cordoned and has no RBD volume attached; safe to reboot"
drain_case not-run false false 1 "FAIL  drained  node-a is not cordoned: the drain did not run or failed; do not reboot"
drain_case unfinished true true 1 "FAIL  drained  node-a still has 1 RBD volumes attached after 2s: pv-a; the drain did not finish; do not reboot"

# #193: the CSI detach lags the drain; --drained waits for the last RBD volume to go.
scenario drain-waits
jq -n '{kind: "Node", metadata: {name: "node-a"}, spec: {unschedulable: true}}' >"${VWS_FAKE_DIR}/kubectl/get_nodes.json"
jq -n '{items: [{spec: {nodeName: "node-a", attacher: "rbd.csi.ceph.com", source: {persistentVolumeName: "pv-a"}}, status: {attached: true}},
  {spec: {nodeName: "node-b", attacher: "rbd.csi.ceph.com", source: {persistentVolumeName: "pv-b"}}, status: {attached: true}}]}' >"${VWS_FAKE_DIR}/kubectl/get_volumeattachments.json"
(sleep 2; edit_json get_volumeattachments.json '(.items[] | select(.spec.nodeName == "node-a") | .status.attached) = false') &
detach_pid=$!
run_vws_real --strict verify-reboot --drained node-a --timeout 10s; expect_rc "a detach during the wait exits 0" 0
wait "$detach_pid"
expect_text "a detach during the wait is OK" "$OUT" "OK    drained  node-a is cordoned and has no RBD volume attached; safe to reboot"
expect_text "the wait printed the remaining PV name" "$ERR" "vws: drained node-a: 1 RBD volumes still attached: pv-a; waiting up to 10s"
scenario drain-unreadable
rm "${VWS_FAKE_DIR}/kubectl/get_volumeattachments.json"
run_vws_real verify-reboot --drained node-a; expect_rc "unreadable volumeattachments at the drain gate exits 3" 3
expect_text "the drain gate says do not reboot" "$ERR" "do not reboot node-a"
run_vws_real verify-reboot --drained node-a --snapshot "${WORK}/x.json"; expect_rc "--drained with another mode exits 2" 2

# #168: WARN when the KubeVirt infra pods can run on one node or fewer.
infra_case() { # NAME KUBEVIRT_SPEC NODES_FILTER WANT
  scenario "infra-$1"
  edit_json get_kubevirt.json ".items[0].spec = $2"
  edit_json get_nodes.json "$3"
  run_vws_real verify-reboot --snapshot "${WORK}/snap-infra-$1.json"; expect_rc "infra $1 exits 0" 0
  expect_text "infra $1" "$OUT" "$4"
}
infra_case default-one-cp '{}' '.items[0].metadata.labels = {"node-role.kubernetes.io/control-plane": "true"}' \
  "WARN  infra  KubeVirt infra pods (virt-api, virt-controller, virt-exportproxy) can run on 1 node(s) (control-plane placement)"
infra_case selector-all '{infra: {nodePlacement: {nodeSelector: {"kubernetes.io/os": "linux"}}}}' '.items[].metadata.labels = {"kubernetes.io/os": "linux"}' \
  "OK    infra  KubeVirt infra pods can run on 2 nodes (selected placement)"
infra_case selector-cordoned '{infra: {nodePlacement: {nodeSelector: {"kubernetes.io/os": "linux"}}}}' '.items[].metadata.labels = {"kubernetes.io/os": "linux"} | .items[1].spec.unschedulable = true' \
  "WARN  infra  KubeVirt infra pods (virt-api, virt-controller, virt-exportproxy) can run on 1 node(s) (selected placement)"
scenario infra-unreadable
rm "${VWS_FAKE_DIR}/kubectl/get_kubevirt.json"
run_vws_real verify-reboot --snapshot "${WORK}/snap-infra-x.json"; expect_rc "an unreadable KubeVirt CR does not fail the snapshot" 0
expect_text "an unreadable KubeVirt CR is a WARN" "$OUT" "WARN  infra  cannot read the KubeVirt CR"

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

# #150: HEALTH_WARN passes only when every active check code is allowlisted with --allow-health-check.
ceph_warn() { # CODE...
  local codes
  codes=$(printf '%s\n' "$@" | jq -R . | jq -s 'map({key: ., value: {severity: "HEALTH_WARN", message: "m"}}) | from_entries')
  edit_json get_cephcluster.json ".items[0].status.ceph = {health: \"HEALTH_WARN\", details: ${codes}}"
}
scenario ceph-warn-empty
rebooted
ceph_warn AUTH_INSECURE_KEYS_ALLOWED
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s; expect_rc "HEALTH_WARN with no allowlist exits 3" 3
expect_text "HEALTH_WARN with no allowlist blocks csi" "$(first_line)" "FAIL  gate-csi  blocked at gate csi after 2s (timeout): Ceph HEALTH_WARN with checks not allowed: AUTH_INSECURE_KEYS_ALLOWED"

scenario ceph-warn-allowed
rebooted
ceph_warn AUTH_INSECURE_CLIENT_KEY_TYPE AUTH_INSECURE_KEYS_ALLOWED
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s \
  --allow-health-check AUTH_INSECURE_CLIENT_KEY_TYPE --allow-health-check AUTH_INSECURE_KEYS_ALLOWED
expect_rc "HEALTH_WARN with every code allowlisted exits 0" 0
expect_text "allowlisted HEALTH_WARN passes csi" "$OUT" "OK    gate-csi"

scenario ceph-warn-mixed
rebooted
ceph_warn AUTH_INSECURE_KEYS_ALLOWED OSD_DOWN
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --allow-health-check AUTH_INSECURE_KEYS_ALLOWED
expect_rc "HEALTH_WARN with one code not allowlisted exits 3" 3
expect_text "the code not allowlisted is named" "$(first_line)" "with checks not allowed: OSD_DOWN"
VWS_VERIFY_ALLOW_HEALTH_CHECKS=OSD_DOWN run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --allow-health-check AUTH_INSECURE_KEYS_ALLOWED
expect_rc "the environment cannot widen the allowlist" 3
run_vws_real verify-reboot --check "${WORK}/snap.json" --allow-health-check 'bad code'; expect_rc "a bad health check code exits 2" 2

# #154: single-node has no HEALTH_WARN exception; it uses the same allowlist.
scenario single-node-warn-empty
rebooted
ceph_warn POOL_NO_REDUNDANCY
if jq -e '(.items[0] // .).spec.profile == "single-node"' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >/dev/null; then ok "the fixture is single-node"; else bad "profile" "not single-node"; fi
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s; expect_rc "single-node HEALTH_WARN with no allowlist exits 3" 3
expect_text "single-node HEALTH_WARN names the code" "$(first_line)" "FAIL  gate-csi  blocked at gate csi after 2s (timeout): Ceph HEALTH_WARN with checks not allowed: POOL_NO_REDUNDANCY"

scenario single-node-warn-allowed
rebooted
ceph_warn POOL_NO_REDUNDANCY
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s --allow-health-check POOL_NO_REDUNDANCY
expect_rc "single-node HEALTH_WARN fully allowlisted exits 0" 0
expect_text "single-node allowlisted HEALTH_WARN passes csi" "$OUT" "OK    gate-csi"

# #166: external Ceph (no CephCluster CRD) never passes the csi gate silently.
external_ceph() { # NAME
  scenario "$1"
  rebooted
  echo '{"items": []}' >"${VWS_FAKE_DIR}/kubectl/get_cephcluster.json"
}
health_file() { # STATUS CODE...: write ceph health detail -f json to ${WORK}/health.json, newer than the snapshot
  local status=$1
  shift
  printf '%s\n' "$@" | jq -R 'select(. != "")' | jq -s --arg s "$status" '{status: $s, checks: (map({key: ., value: {severity: $s, summary: {message: "m"}}}) | from_entries), mutes: []}' >"${WORK}/health.json"
  touch -d '+2 seconds' "${WORK}/health.json"
}
external_ceph xceph-none
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "external Ceph without a source exits 1" 1
expect_text "external Ceph says what to give" "$(first_line)" "FAIL  gate-csi  blocked at gate csi: no CephCluster CRD (external Ceph?): give --ceph-health-file"

external_ceph xceph-skip
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s --skip-ceph-gate; expect_rc "--skip-ceph-gate exits 0" 0
expect_text "the skip is a loud WARN" "$OUT" "WARN  gate-csi  SKIPPED by --skip-ceph-gate: no CephCluster and no --ceph-health-file, Ceph health was NOT checked"
if ! grep -q "OK    gate-csi" <<<"$OUT"; then ok "a skipped gate is not reported as passed"; else bad "skip as OK" "$OUT"; fi

external_ceph xceph-ok
health_file HEALTH_OK
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s --ceph-health-file "${WORK}/health.json"; expect_rc "a HEALTH_OK file exits 0" 0
expect_text "a HEALTH_OK file passes csi" "$OUT" "OK    gate-csi"

external_ceph xceph-warn
health_file HEALTH_WARN AUTH_INSECURE_KEYS_ALLOWED
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --ceph-health-file "${WORK}/health.json"; expect_rc "a WARN file without an allowlist exits 3" 3
expect_text "the file's code is named" "$(first_line)" "with checks not allowed: AUTH_INSECURE_KEYS_ALLOWED"
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s --ceph-health-file "${WORK}/health.json" --allow-health-check AUTH_INSECURE_KEYS_ALLOWED
expect_rc "a WARN file with its code allowlisted exits 0" 0
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s --ceph-health-file "${WORK}/health.json" --skip-ceph-gate
expect_rc "a file wins over --skip-ceph-gate" 3

external_ceph xceph-err
health_file HEALTH_ERR MON_DOWN
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s --ceph-health-file "${WORK}/health.json"; expect_rc "a HEALTH_ERR file exits 1" 1

external_ceph xceph-stale
health_file HEALTH_OK
touch -d '-1 hour' "${WORK}/health.json"
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --ceph-health-file "${WORK}/health.json"; expect_rc "a file older than the snapshot exits 3" 3
expect_text "a stale file says so" "$(first_line)" "is not newer than the snapshot"

external_ceph xceph-garbage
echo '{"foo": 1}' >"${WORK}/health.json"; touch -d '+2 seconds' "${WORK}/health.json"
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --ceph-health-file "${WORK}/health.json"; expect_rc "a file that is not ceph health JSON exits 3" 3
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --ceph-health-file "${WORK}/missing.json"; expect_rc "a missing health file exits 3" 3

external_ceph rook-booting
echo '{"metadata": {"name": "cephclusters.ceph.rook.io"}}' >"${VWS_FAKE_DIR}/kubectl/get_crd.json"
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 2s --skip-ceph-gate; expect_rc "Rook without a CephCluster yet waits (3) even with --skip-ceph-gate" 3
expect_text "Rook waits for the CephCluster" "$(first_line)" "no CephCluster yet"

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
edit_json get_pods.json '(.items[] | select(.metadata.name == "compute-0") | .status.containerStatuses[0].restartCount) = 5'
run_vws_real verify-reboot --check "${WORK}/snap.json" --timeout 5s; expect_rc "failed comparisons exit 1" 1
expect_text "PVC not Bound" "$OUT" "FAIL  pvcs  not Bound or gone: data-network-0"
expect_text "fewer ConfigMap entries" "$OUT" "FAIL  configmaps  fewer entries: vws-compute-store 1/2"
expect_text "VM not Running" "$OUT" "FAIL  vms  not Running: vws-vms/vm-a Stopped"
expect_text "restarts over the threshold" "$OUT" "FAIL  restarts  over 3 restarts since the snapshot: compute-0 4"

# Restarts before the snapshot do not count; a pod missing from the snapshot counts from 0.
scenario restarts-before
rebooted
edit_json get_pods.json '(.items[] | select(.metadata.name == "compute-0") | .status.containerStatuses[0].restartCount) = 5'
jq '.restarts["compute-0"].c = 4' "${WORK}/snap.json" >"${WORK}/snap-old.json"
run_vws_real verify-reboot --check "${WORK}/snap-old.json" --timeout 5s; expect_rc "old restarts only exit 0" 0
expect_text "old restarts are not counted" "$OUT" "OK    restarts  no pod over 3 restarts since the snapshot"
jq 'del(.restarts["compute-0"])' "${WORK}/snap.json" >"${WORK}/snap-new.json"
run_vws_real verify-reboot --check "${WORK}/snap-new.json" --timeout 5s; expect_rc "a pod missing from the snapshot exits 1" 1
expect_text "a missing pod counts from 0" "$OUT" "FAIL  restarts  over 3 restarts since the snapshot: compute-0 5"
jq 'del(.restarts)' "${WORK}/snap.json" >"${WORK}/snap-pre.json"
run_vws_real verify-reboot --check "${WORK}/snap-pre.json" --timeout 5s; expect_rc "a snapshot without restarts exits 1" 1
expect_text "an older snapshot keeps the total" "$OUT" "FAIL  restarts  over 3 restarts since the snapshot: compute-0 5"

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
