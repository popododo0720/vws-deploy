#!/usr/bin/env bash
# vws preflight: one scenario per check, built from tests/fixtures/preflight/ok by changing one file.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export VWS_SOURCE_CHECKS=0

# scenario NAME: a fresh copy of the ok fixtures in $WORK/NAME, used by the next run.
scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/preflight/ok" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
}
fx() { echo "${VWS_FAKE_DIR}/$1"; }
edit_json() { # FILE JQ
  local f
  f=$(fx "$1")
  jq "$2" "$f" >"${f}.new" && mv "${f}.new" "$f"
}
line() { grep -E "^$1 +$2 " <<<"$OUT" || true; }
expect_line() { # NAME LEVEL CHECK
  if [[ -n $(line "$2" "$3") ]]; then ok "$1"; else bad "$1" "no '$2 $3' line in: $OUT"; fi
}

scenario ok
run_vws_real preflight --profile single-node;     expect_rc "all pass exits 0" 0
assert_no_changes "preflight changes nothing"
expect_line "ok has the nodes line" OK nodes
run_vws_real -o json preflight --profile single-node; expect_rc "json exits 0" 0
if jq -e '.command == "preflight" and .status == "ok" and (.checks | length) >= 15 and all(.checks[]; .level == "OK")' <<<"$OUT" >/dev/null; then
  ok "json lists every check"; else bad "json lists every check" "$OUT"; fi

run_vws_real preflight;                           expect_rc "no profile exits 2" 2
run_vws_real preflight --profile huge;            expect_rc "unknown profile exits 2" 2

scenario few-nodes
run_vws_real preflight --profile small;           expect_rc "1 node for small exits 1" 1
expect_line "small needs 3 nodes" FAIL nodes
run_vws_real preflight --profile ha;              expect_rc "1 node for ha exits 1" 1
edit_json kubectl/get_nodes.json '.items += [.items[0], .items[0]]'
run_vws_real preflight --profile small;           expect_rc "3 nodes for small exits 0" 0
run_vws_real preflight --profile ha;              expect_rc "3 nodes for ha exits 1" 1
edit_json kubectl/get_nodes.json '.items[1].status.conditions[0].status = "False"'
run_vws_real preflight --profile small;           expect_rc "a NotReady node does not count" 1

# #118: k3s nodes must pin --node-ip to their InternalIP; other distributions are not checked.
scenario node-ip
run_vws_real preflight --profile single-node;     expect_rc "a node without k3s args exits 0" 0
expect_line "a node without k3s args is OK" OK node-ip
edit_json kubectl/get_nodes.json '.items[0].status.addresses = [{"type": "InternalIP", "address": "10.0.0.5"}]
  | .items[0].metadata.annotations["k3s.io/node-args"] = "[\"server\",\"--node-ip\",\"10.0.0.5\"]"'
run_vws_real preflight --profile single-node;     expect_rc "a pinned k3s node exits 0" 0
expect_line "a pinned k3s node is OK" OK node-ip
edit_json kubectl/get_nodes.json '.items[0].metadata.annotations["k3s.io/node-args"] = "[\"server\",\"--node-ip=10.0.0.9\"]"'
run_vws_real preflight --profile single-node;     expect_rc "a mismatched --node-ip only warns" 0
expect_line "a mismatched --node-ip warns" WARN node-ip
edit_json kubectl/get_nodes.json '.items[0].metadata.annotations["k3s.io/node-args"] = "[\"server\"]"'
run_vws_real preflight --profile single-node
expect_line "a k3s node without --node-ip warns" WARN node-ip

scenario small-node
edit_json kubectl/get_nodes.json '.items[0].status.allocatable = {"cpu": "500m", "memory": "1Gi"}'
run_vws_real preflight --profile single-node;     expect_rc "too little cpu and memory exits 1" 1
expect_line "resources fail" FAIL node-resources

scenario no-rights
echo no >"$(fx kubectl/auth_can-i.json)"
run_vws_real preflight --profile single-node;     expect_rc "missing rights exit 1" 1
expect_line "install-rights fail" FAIL install-rights
assert_no_changes "a failing preflight changes nothing"

scenario minor
edit_json kubectl/version_.json '.serverVersion.minor = "34"'
run_vws_real preflight --profile single-node;     expect_rc "another minor is a warning" 0
expect_line "k8s-minor warns" WARN k8s-minor
run_vws_real --strict preflight --profile single-node; expect_rc "--strict turns the warning into 1" 1

scenario helm
echo 'v3.10.0+gdeadbee' >"$(fx helm/version.json)"
run_vws_real preflight --profile single-node;     expect_rc "another helm is a warning" 0
expect_line "tools warns" WARN tools

scenario no-default-class
edit_json kubectl/get_storageclass.json '.items |= map(del(.metadata.annotations))'
run_vws_real preflight --profile single-node;     expect_rc "no default StorageClass exits 1" 1
expect_line "default-storageclass fails" FAIL default-storageclass

scenario before-bundle
edit_json kubectl/get_storageclass.json '.items |= map(select(.metadata.name != "ceph-block"))'
echo '{"items":[]}' >"$(fx kubectl/get_volumesnapshotclass.json)"
echo '{"items":[]}' >"$(fx kubectl/get_crd.json)"
rm "$(fx kubectl/get_kubevirt.json)" "$(fx kubectl/get_cdi.json)"
edit_json kubectl/get_nodes.json '.items |= map(del(.metadata.labels))'
run_vws_real preflight --profile single-node;     expect_rc "a cluster before the bundle only warns" 0
expect_line "missing ceph-block warns" WARN ceph-block
expect_line "missing kubevirt CRDs warn" WARN crd-kubevirt.io
expect_line "missing KubeVirt CR warns" WARN kubevirt-phase
expect_line "kernel modules not confirmed warns" WARN kernel-indirect

scenario crd-not-ready
edit_json kubectl/get_crd.json '.items[0].status.conditions[0].status = "False"'
run_vws_real preflight --profile single-node;     expect_rc "a CRD not Established exits 1" 1
expect_line "crd fails" FAIL crd-kubevirt.io
edit_json kubectl/get_cdi.json '.items[0].status.phase = "Deploying"'
run_vws_real preflight --profile single-node;     expect_rc "CDI not Deployed exits 1" 1
expect_line "cdi phase fails" FAIL cdi-phase

scenario release-failed
echo '[{"name":"vws","namespace":"vws","status":"pending-upgrade","revision":"3"}]' >"$(fx helm/list.json)"
run_vws_real preflight --profile single-node;     expect_rc "a pending release exits 1" 1
expect_line "release fails" FAIL release

# spec.network.nodes (#128): node IPs in management, and NICs and MTUs from talosctl links.
LINKS="${FIXTURES}/preflight/links"
net_scenario() { # NAME: ok fixtures plus a CR with tests/fixtures/preflight/links/network.json and node-a at 10.77.0.11
  scenario "$1"
  jq -n --slurpfile n "${LINKS}/network.json" '{kind: "VWSCluster", metadata: {name: "vws"}, spec: {profile: "single-node", network: $n[0]}}' >"$(fx kubectl/get_vwsclusters.json)"
  edit_json kubectl/get_nodes.json '.items[0].status.addresses = [{type: "InternalIP", address: "10.77.0.11"}, {type: "Hostname", address: "node-a"}]'
}
expect_line "no VWSCluster yet is OK" OK node-network

net_scenario net-ok
run_vws_real preflight --profile single-node --links "${LINKS}/links.json"; expect_rc "node networks that match exit 0" 0
expect_line "the node IP is in management" OK node-ip/node-a
if [[ $(line OK node-links/node-a | wc -l) == 3 ]]; then ok "three interfaces are checked"; else bad "three interfaces" "$OUT"; fi
expect_text "the tunnel VLAN interface is checked" "$OUT" "interface enp1s0.20 (tunnel) MTU 9000"
assert_no_changes "the node network check changes nothing"

net_scenario net-nolinks
run_vws_real preflight --profile single-node;     expect_rc "without --links it is WARN only, exit 0" 0
expect_line "without --links NICs are unverified" WARN node-links
run_vws_real --strict preflight --profile single-node; expect_rc "unverified NICs fail --strict" 1

net_scenario net-badip
edit_json kubectl/get_nodes.json '.items[0].status.addresses[0].address = "192.168.5.5"'
run_vws_real preflight --profile single-node --links "${LINKS}/links.json"; expect_rc "a node outside management exits 1" 1
expect_line "node IP outside management fails" FAIL node-ip/node-a

net_scenario net-nonic
grep -v '"enp1s0.20"' "${LINKS}/links.json" >"${WORK}/links.json"
run_vws_real preflight --profile single-node --links "${WORK}/links.json"; expect_rc "a missing VLAN interface exits 1" 1
expect_text "the missing interface is named" "$OUT" "interface enp1s0.20 (tunnel) does not exist"

net_scenario net-mtu
sed 's/"enp1s0"},"spec":{"type":"ether","mtu":9000}/"enp1s0"},"spec":{"type":"ether","mtu":1500}/' "${LINKS}/links.json" >"${WORK}/links.json"
run_vws_real preflight --profile single-node --links "${WORK}/links.json"; expect_rc "a small MTU exits 1" 1
expect_text "the MTU gap is named" "$OUT" "interface enp1s0 (management) has MTU 1500, want at least 9000"

# vm-mtu (#236): the tunnel NIC must carry VM MTU 9001 plus the overhead; short of that preflight
# writes the lowered spec.network.vmMTU and the read underlayMTU into the VWSCluster, never a NIC.
expect_line "a vmMTU the tunnel carries is OK" OK vm-mtu
net_scenario net-lower
edit_json kubectl/get_vwsclusters.json 'del(.spec.network.vmMTU)'
echo '{}' >"$(fx kubectl/patch_vwsclusters.json)"
run_vws_real preflight --profile single-node --links "${LINKS}/links.json"; expect_rc "a lowered VM MTU is WARN, exit 0" 0
expect_text "the gap is named" "$OUT" "underlay MTU 9000 is under 9101"
expect_line "the lowered value is written" OK vm-mtu-set
expect_text "only vmMTU and underlayMTU are patched" "$(changing_calls)" 'kubectl patch patch vwsclusters vws --type merge -p {"spec":{"network":{"vmMTU":8900,"underlayMTU":9000}}}'
if [[ $(changing_calls | wc -l) == 1 ]]; then ok "the patch is the only change"; else bad "one change" "$(changing_calls)"; fi
edit_json kubectl/get_vwsclusters.json '.spec.network.ipv4Only = true'
run_vws_real preflight --profile single-node --links "${LINKS}/links.json"
expect_text "ipv4Only lowers to 8940" "$(changing_calls)" '"vmMTU":8940'
edit_json kubectl/get_vwsclusters.json '.spec.network.ipv4Only = false'
# vws-test (OpenStack geneve under the nodes): NIC 8942 -> vmMTU 8842, 8882 with ipv4Only.
sed 's/"mtu":9000}/"mtu":8942}/' "${LINKS}/links.json" >"${WORK}/links8942.json"
edit_json kubectl/get_vwsclusters.json '.spec.network.nodes.management.mtu = 8942 | .spec.network.nodes.tunnel.mtu = 8942'
run_vws_real preflight --profile single-node --links "${WORK}/links8942.json"; expect_rc "an 8942 underlay is WARN, exit 0" 0
expect_text "8942 lowers vmMTU to 8842" "$(changing_calls)" '{"vmMTU":8842,"underlayMTU":8942}'
edit_json kubectl/get_vwsclusters.json '.spec.network.ipv4Only = true'
run_vws_real preflight --profile single-node --links "${WORK}/links8942.json"
expect_text "8942 with ipv4Only lowers vmMTU to 8882" "$(changing_calls)" '{"vmMTU":8882,"underlayMTU":8942}'
edit_json kubectl/get_vwsclusters.json '.spec.network.ipv4Only = false | .spec.network.vmMTU = 8842'
run_vws_real preflight --profile single-node --links "${WORK}/links8942.json"; expect_rc "8842 on 8942 exits 0" 0
expect_line "the lowered value then is OK" OK vm-mtu
assert_no_changes "a written 8842 is not written again"
edit_json kubectl/get_vwsclusters.json 'del(.spec.network.vmMTU)'
sed 's/"mtu":9000}/"mtu":9216}/' "${LINKS}/links.json" >"${WORK}/links.json"
run_vws_real preflight --profile single-node --links "${WORK}/links.json"; expect_rc "a 9216 underlay exits 0" 0
expect_text "9216 carries 9001" "$OUT" "underlay MTU 9216 carries VM MTU 9001 plus the 100-byte overhead"
assert_no_changes "a big enough underlay changes nothing"
rm "$(fx kubectl/patch_vwsclusters.json)"; echo forbidden >"$(fx kubectl/patch_vwsclusters.fail)"
run_vws_real preflight --profile single-node --links "${LINKS}/links.json"; expect_rc "a refused patch is WARN, exit 0" 0
expect_line "a refused patch names reconfigure" WARN vm-mtu-set
run_vws_real preflight --profile single-node; expect_rc "without --links the VM MTU is unverified, exit 0" 0
expect_line "without --links vm-mtu is WARN" WARN vm-mtu

net_scenario net-othernode
sed 's/10.77.0.11/10.77.0.99/' "${LINKS}/links.json" >"${WORK}/links.json"
run_vws_real preflight --profile single-node --links "${WORK}/links.json"; expect_rc "links without this node exit 1" 1
expect_line "a node missing from --links fails" FAIL node-links/node-a

run_vws_real preflight --profile single-node --links "${WORK}/nope.json"; expect_rc "a missing --links file exits 2" 2

scenario api-down
rm "$(fx kubectl/version_.json)"
run_vws_real preflight --profile single-node;     expect_rc "an unreachable API exits 3" 3

finish_tests
