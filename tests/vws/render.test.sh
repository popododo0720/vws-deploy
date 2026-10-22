#!/usr/bin/env bash
# vws render: VWSCluster CR -> values overlay. The fake helm stands in for the schema check;
# scripts/guard.sh profiles runs the same examples through the real helm.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

EX="${VWS_TEST_ROOT}/examples/vwscluster"

for profile in single-node small ha; do
  run_vws_real render "${EX}/${profile}.yaml" "${WORK}/${profile}.yaml"; expect_rc "render ${profile} exits 0" 0
  expect_text "render ${profile} sets global.profile" "$(cat "${WORK}/${profile}.yaml")" "profile: ${profile}"
  assert_no_changes "render ${profile} changes nothing"
  expect_text "render ${profile} checks the schema with helm template" "$(cat "$VWS_FAKE_LOG")" "template vws"
  run_vws_real render "${EX}/${profile}.yaml" "${WORK}/${profile}.again.yaml"
  if cmp -s "${WORK}/${profile}.yaml" "${WORK}/${profile}.again.yaml"; then ok "render ${profile} twice gives the same bytes"; else bad "render ${profile} is deterministic" "outputs differ"; fi
done
expect_text "existing driver sets the storage class" "$(cat "${WORK}/small.yaml")" "storageClass: fast-rbd"
expect_text "spec.version becomes global.tag" "$(cat "${WORK}/ha.yaml")" "tag: 0.1.0"
expect_text "a CR replicas override wins over the profile" "$(cat "${WORK}/ha.yaml")" "replicas: 4"

# bad NAME SED: render a broken copy of ha.yaml and expect exit 2.
bad_cr() {
  local name=$1 expr=$2
  sed -e "$expr" "${EX}/ha.yaml" >"${WORK}/bad.yaml"
  run_vws_real render "${WORK}/bad.yaml" "${WORK}/bad.out"
  expect_rc "invalid CR: ${name} exits 2" 2
  if [[ -e ${WORK}/bad.out ]]; then bad "invalid CR: ${name} writes nothing" "output exists"; rm -f "${WORK}/bad.out"; fi
}
bad_cr "unknown network driver" 's/driver: kube-ovn/driver: calico/'
bad_cr "unknown storage driver" 's/driver: rook-ceph/driver: gluster/'
bad_cr "unknown profile" 's/profile: ha/profile: huge/'
bad_cr "unknown field" 's/^  version: 0.1.0/  version: 0.1.0\n  colour: blue/'
bad_cr "unknown service field" 's/      replicas: 4/      replicas: 4\n      image: x/'
bad_cr "secret-shaped field" 's/^  version: 0.1.0/  version: 0.1.0\n  adminPassword: x/'
bad_cr "secret-shaped flag" 's/      replicas: 4/      replicas: 4\n      flags:\n        api-token: fake/'
bad_cr "a secret value under secrets" 's/^  version: 0.1.0/  version: 0.1.0\n  secrets:\n    db:\n      secretName: db\n      value: x/'
bad_cr "wrong name" 's/name: vws$/name: other/'
bad_cr "wrong group" 's#apiVersion: .*#apiVersion: example.com/v1alpha1#'
bad_cr "unknown service" 's/^    console:$/    billing:/'
bad_cr "replicas below one" 's/replicas: 4/replicas: 0/'
bad_cr "otlp endpoint the chart cannot take yet" 's/otlpEndpoint: ""/otlpEndpoint: http:\/\/collector:4317/'
bad_cr "existing driver without a class" 's/driver: rook-ceph/driver: existing/'
bad_cr "no elastic IP pool" '/^    network:$/,/pool:/d'
bad_cr "an RFC 5737 pool" 's/pool: 198.18.1.0\/24/pool: 192.0.2.0\/24/'
bad_cr "version with a slash" 's/version: 0.1.0/version: a\/b/'

# spec.storage.s3.rgw goes to services.storage.s3.rgw so vws apply keeps RGW on (#108).
sed -e 's/    storageClass: fast-rbd/    storageClass: fast-rbd\n    s3:\n      rgw:\n        enabled: true\n        endpoint: http:\/\/10.77.0.101:8080\n        bucketPrefix: vwslab\n        existingSecret: ceph-rgw-s3/' "${EX}/small.yaml" >"${WORK}/rgw.yaml"
run_vws_real render "${WORK}/rgw.yaml" "${WORK}/rgw.out"; expect_rc "s3.rgw renders" 0
if python3 -c 'import sys,yaml; r=yaml.safe_load(open(sys.argv[1]))["services"]["storage"]["s3"]["rgw"]; sys.exit(r != {"enabled": True, "endpoint": "http://10.77.0.101:8080", "bucketPrefix": "vwslab", "existingSecret": "ceph-rgw-s3"})' "${WORK}/rgw.out"; then
  ok "s3.rgw becomes services.storage.s3.rgw"; else bad "s3.rgw values" "$(cat "${WORK}/rgw.out")"; fi
if ! grep -q 's3:' "${WORK}/small.yaml"; then ok "without s3.rgw the overlay leaves the object backend off"; else bad "no s3 by default" "$(cat "${WORK}/small.yaml")"; fi
for prefix in abcdefghij123456789 Acme -acme; do
  sed -e "s/bucketPrefix: vwslab/bucketPrefix: ${prefix}/" "${WORK}/rgw.yaml" >"${WORK}/rgwbad.yaml"
  run_vws_real render "${WORK}/rgwbad.yaml" "${WORK}/rgwbad.out"; expect_rc "bucketPrefix ${prefix} exits 2" 2
done
sed -e 's/        existingSecret: ceph-rgw-s3/        existingSecret: ceph-rgw-s3\n        secretKey: x/' "${WORK}/rgw.yaml" >"${WORK}/rgwkey.yaml"
run_vws_real render "${WORK}/rgwkey.yaml" "${WORK}/rgwkey.out"; expect_rc "a key under s3.rgw exits 2" 2

# spec.storage.driver nfs and existing volumeMode (vws-storage#72, #73).
storage_cr() { # NAME PYTHON: edit spec.storage of small.yaml as s, render to ${WORK}/NAME.out
  python3 - "${EX}/small.yaml" "$2" >"${WORK}/$1.yaml" <<'PY'
import sys, yaml
cr = yaml.safe_load(open(sys.argv[1]))
s = cr["spec"]["storage"]
exec(sys.argv[2], {"s": s})
yaml.safe_dump(cr, sys.stdout)
PY
  run_vws_real render "${WORK}/$1.yaml" "${WORK}/$1.out"
}
storage_values() { # NAME PYTHON-EXPR on v (the storage service values): exit 0 when true
  python3 -c 'import sys,yaml; v=yaml.safe_load(open(sys.argv[1]))["services"]["storage"]; sys.exit(0 if eval(sys.argv[2]) else 1)' "${WORK}/$1.out" "$2"
}
DV='{"namespace": "vms", "apiGroups": ["cdi.kubevirt.io"], "resources": ["datavolumes"], "verbs": ["get", "list", "create", "delete"]}'
storage_cr nfs 's.clear(); s.update(driver="nfs", nfs={"server": "nas.example.internal", "share": "/export/vws"})'
expect_rc "driver nfs renders" 0
if storage_values nfs 'v["flags"] == {"kube-class-standard": "vws-nfs", "kube-class-ssd": "vws-nfs", "kube-volume-mode": "Filesystem", "kube-snapshot-class": ""}'; then
  ok "nfs sets the class, Filesystem mode and no snapshot class"; else bad "nfs flags" "$(cat "${WORK}/nfs.out")"; fi
if storage_values nfs "${DV} in v['rbac'] and len(v['rbac']) > 1"; then ok "nfs adds datavolumes to the storage RBAC"; else bad "nfs rbac" "$(cat "${WORK}/nfs.out")"; fi
if ! grep -q 'storageClass: vws-nfs' "${WORK}/nfs.out"; then ok "nfs leaves the state volumes on the default class"; else bad "nfs global class" "$(cat "${WORK}/nfs.out")"; fi
storage_cr existing-fs 's["volumeMode"] = "Filesystem"'
expect_rc "existing with Filesystem renders" 0
if storage_values existing-fs "v['flags']['kube-volume-mode'] == 'Filesystem' and ${DV} in v['rbac']"; then ok "existing Filesystem sets the mode and the datavolumes rule"; else bad "existing fs" "$(cat "${WORK}/existing-fs.out")"; fi
if ! grep -q 'kube-volume-mode\|datavolumes' "${WORK}/small.yaml"; then ok "Block (the default) renders no volume mode and no datavolumes rule"; else bad "block default" "$(cat "${WORK}/small.yaml")"; fi
# CDI clone rights for image volumes stay under every storage override (vws-storage#86, #201).
storage_roles() { # CHART_YAML -> "ns: [(apiGroups, resources, verbs)...]" per storage Role in vws-vms/vws-images
  printf '%s\n' "$1" | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Role" and d["metadata"]["name"].endswith("-storage") and d["metadata"]["namespace"] in ("vws-vms", "vws-images"):
        print(d["metadata"]["namespace"], [(r["apiGroups"], r["resources"], r["verbs"]) for r in d["rules"]])'
}
for out in small nfs existing-fs; do
  src="${WORK}/${out}.out"; [ "$out" = small ] && src="${WORK}/small.yaml"
  roles=$(storage_roles "$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "$src")")
  expect_text "${out}: storage gets DataVolume get/create/delete in the VM namespace" "$roles" "(['cdi.kubevirt.io'], ['datavolumes'], ['get', 'create', 'delete'])"
  expect_text "${out}: storage gets DataSource get in the images namespace" "$roles" "(['cdi.kubevirt.io'], ['datasources'], ['get'])"
  expect_text "${out}: storage gets datavolumes/source create in the images namespace" "$roles" "(['cdi.kubevirt.io'], ['datavolumes/source'], ['create'])"
done
for case in \
  'nfs-no-export|s.clear(); s["driver"] = "nfs"' \
  'nfs-no-share|s.clear(); s.update(driver="nfs", nfs={"server": "nas"})' \
  'nfs-relative-share|s.clear(); s.update(driver="nfs", nfs={"server": "nas", "share": "export"})' \
  'nfs-with-class|s.update(driver="nfs", nfs={"server": "nas", "share": "/e"})' \
  'nfs-with-mode|s.clear(); s.update(driver="nfs", volumeMode="Block", nfs={"server": "nas", "share": "/e"})' \
  'nfs-fields-on-existing|s["nfs"] = {"server": "nas", "share": "/e"}' \
  'mode-on-rook|s.clear(); s.update(driver="rook-ceph", volumeMode="Filesystem")' \
  'bad-mode|s["volumeMode"] = "Raw"'; do
  storage_cr "${case%%|*}" "${case#*|}"; expect_rc "invalid storage: ${case%%|*} exits 2" 2
done

# spec.network.nodes and spec.network.direct (vws-deploy#125). nodes.external gives the pool, so the
# CR drops the pool of ha.yaml.
net_cr() {
  python3 - "${EX}/ha.yaml" "${VWS_TEST_ROOT}/tests/fixtures/render/node-network.yaml" "$1" >"${WORK}/net.yaml" <<'PY'
import sys, yaml
cr = yaml.safe_load(open(sys.argv[1]))
cr["spec"]["network"] = yaml.safe_load(open(sys.argv[2]))
del cr["spec"]["services"]["network"]
exec(sys.argv[3], {"n": cr["spec"]["network"], "spec": cr["spec"]})
yaml.safe_dump(cr, sys.stdout)
PY
}
net_cr "pass"
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"; expect_rc "a 4-NIC CR with direct networks renders" 0
flags=$(python3 -c 'import sys,yaml,json; f=yaml.safe_load(open(sys.argv[1]))["services"]["network"]["flags"]; print(json.dumps({k: v for k, v in f.items() if "direct" not in k and k != "vm-mtu"}, sort_keys=True))' "${WORK}/net.out")
expect_text "nodes.external becomes the vws-network underlay flags (#130)" "$flags" '{"kube-external-subnet": "vws-external", "kube-provider-network": "vws-pn0", "kube-underlay-cidr": "198.18.10.0/24", "kube-underlay-exclude": "198.18.10.1..198.18.10.9", "kube-underlay-gateway": "198.18.10.1", "kube-underlay-interface": "enp4s0", "kube-underlay-vlan": 100, "pool": "198.18.10.64/26"}'
# spec.network.direct -> -kube-direct-networks, -kube-direct-nad-namespace and a vms Role (#131).
direct=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["services"]["network"]["flags"]["kube-direct-networks"])' "${WORK}/net.out")
expect_text "direct networks become one JSON line in the vws-network contract (#131)" "$direct" '[{"name":"prod","providerNetwork":"vws-pn0","interface":"enp4s0","vlan":200,"cidr":"192.168.200.0/24","gateway":"192.168.200.1"},{"name":"lab","providerNetwork":"vws-pn0","interface":"enp4s0","vlan":0,"cidr":"192.168.10.0/24","gateway":"192.168.10.1","exclude":["192.168.10.1..192.168.10.20"]}]'
expect_text "the direct NADs go to the VM namespace" "$(cat "${WORK}/net.out")" "kube-direct-nad-namespace:
        namespace: vms"
chart=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/net.out")
expect_text "the chart passes the VM namespace to vws-network" "$chart" "-kube-direct-nad-namespace=vws-vms"
role=$(printf '%s\n' "$chart" | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Role" and d["metadata"]["namespace"] == "vws-vms" and "network" in d["metadata"]["name"]:
        print([(r["apiGroups"], r["resources"], r["verbs"]) for r in d["rules"]])')
expect_text "network gets get/create on NADs in the VM namespace" "$role" "(['k8s.cni.cncf.io'], ['network-attachment-definitions'], ['get', 'create'])"
expect_text "direct networks keep the VMI read rule in the VM namespace (vws-network#87)" "$role" "(['kubevirt.io'], ['virtualmachineinstances'], ['get', 'list', 'watch'])"
roles=$(storage_roles "$chart")
expect_text "direct networks keep the storage DataVolume rule (#201)" "$roles" "(['cdi.kubevirt.io'], ['datavolumes'], ['get', 'create', 'delete'])"
expect_text "direct networks keep the storage clone rule (#201)" "$roles" "(['cdi.kubevirt.io'], ['datavolumes/source'], ['create'])"
net_cr 'n["nodes"].pop("external"); n.pop("direct"); spec["services"]["network"] = {"flags": {"pool": "198.18.1.0/24"}}'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"; expect_rc "nodes without external renders" 0
flags=$(python3 -c 'import sys,yaml,json; print(json.dumps(yaml.safe_load(open(sys.argv[1]))["services"]["network"]["flags"], sort_keys=True))' "${WORK}/net.out")
expect_text "without external only the pool and the VM MTU are set" "$flags" '{"pool": "198.18.1.0/24", "vm-mtu": 8900}'
expect_text "without direct networks the network RBAC is the chart default" "$(python3 -c 'import sys,yaml; print("rbac" in yaml.safe_load(open(sys.argv[1]))["services"]["network"])' "${WORK}/net.out")" "False"
net_cr 'n["direct"][0]["nic"] = "enp0s9"'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "the provider network index follows the sorted underlay NICs" "$(cat "${WORK}/net.out")" "kube-provider-network: vws-pn1"
# spec.network.nodes.external.gatewayNodes -> -kube-gateway-nodes (#197).
net_cr 'n["nodes"]["external"]["gatewayNodes"] = ["gw-a", "gw-b.example"]'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"; expect_rc "gatewayNodes renders" 0
expect_text "gatewayNodes becomes one comma-separated flag" "$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["services"]["network"]["flags"]["kube-gateway-nodes"])' "${WORK}/net.out")" "gw-a,gw-b.example"
chart=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/net.out")
expect_text "the chart passes the gateway nodes to vws-network" "$chart" "-kube-gateway-nodes=gw-a,gw-b.example"
net_cr 'pass'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "without gatewayNodes render sets no gateway flag" "$(python3 -c 'import sys,yaml; print("kube-gateway-nodes" in yaml.safe_load(open(sys.argv[1]))["services"]["network"]["flags"])' "${WORK}/net.out")" "False"
chart=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/net.out")
expect_text "the chart default leaves the labels alone" "$chart" "-kube-gateway-nodes="
# The chart default passes the VM namespace and reads VMIs there (vws-network#87).
expect_text "the chart passes the VMI namespace to vws-network" "$chart" "-kube-vm-namespace=vws-vms"
role=$(printf '%s\n' "$chart" | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Role" and d["metadata"]["namespace"] == "vws-vms" and "network" in d["metadata"]["name"]:
        print([(r["apiGroups"], r["resources"], r["verbs"]) for r in d["rules"]])')
expect_text "network gets get/list/watch on VMIs in the VM namespace" "$role" "(['kubevirt.io'], ['virtualmachineinstances'], ['get', 'list', 'watch'])"
bad_net() {
  net_cr "$2"
  run_vws_real render "${WORK}/net.yaml" "${WORK}/net.bad"
  expect_rc "invalid network: $1 exits 2" 2
  rm -f "${WORK}/net.bad"
}
bad_net "a pool flag beside nodes.external" 'spec["services"]["network"] = {"flags": {"pool": "198.18.1.0/24"}}'
bad_net "a direct flag beside spec.network.direct" 'spec["services"]["network"] = {"flags": {"kube-direct-nad-namespace": "x"}}'
bad_net "direct networks without an external subnet" 'n["nodes"].pop("external"); spec["services"]["network"] = {"flags": {"pool": "198.18.1.0/24"}}'
bad_net "an underlay flag beside nodes.external" 'spec["services"]["network"] = {"flags": {"kube-underlay-vlan": 7}}'
bad_net "a gateway flag beside gatewayNodes" 'n["nodes"]["external"]["gatewayNodes"] = ["gw-a"]; spec["services"]["network"] = {"flags": {"kube-gateway-nodes": "x"}}'
bad_net "an empty gatewayNodes list" 'n["nodes"]["external"]["gatewayNodes"] = []'
bad_net "a gateway node name with a comma" 'n["nodes"]["external"]["gatewayNodes"] = ["a,b"]'
bad_net "a duplicate gateway node" 'n["nodes"]["external"]["gatewayNodes"] = ["gw-a", "gw-a"]'
bad_net "an RFC 5737 eipPool" 'n["nodes"]["external"].update(cidr="203.0.113.0/24", gateway="203.0.113.1", excludeIPs=[], eipPool="203.0.113.64/26")'
bad_net "nodes without management" 'del n["nodes"]["management"]'
bad_net "vlan above 4094" 'n["nodes"]["tunnel"]["vlan"] = 4095'
bad_net "mtu below 1280" 'n["nodes"]["tunnel"]["mtu"] = 1000'
bad_net "tunnel mtu too small for geneve" 'n["nodes"]["tunnel"]["mtu"] = 1300'
bad_net "non-canonical cidr" 'n["nodes"]["tunnel"]["cidr"] = "10.20.0.1/24"'
bad_net "overlapping cidrs" 'n["nodes"]["tunnel"]["cidr"] = "10.77.0.0/16"'
bad_net "same nic and vlan, other cidr" 'n["nodes"]["storage"]["cluster"]["vlan"] = 0'
bad_net "tagged host network on an underlay nic" 'n["nodes"]["tunnel"]["nic"] = "enp4s0"'
bad_net "two untagged underlay networks on one nic" 'n["direct"][0].pop("vlan")'
bad_net "underlay sharing a vlan" 'n["direct"][0]["vlan"] = 100'
bad_net "duplicate direct name" 'n["direct"][1]["name"] = "prod"'
bad_net "gateway outside the cidr" 'n["direct"][0]["gateway"] = "10.0.0.1"'
bad_net "external without eipPool" 'del n["nodes"]["external"]["eipPool"]'
bad_net "eipPool outside the external cidr" 'n["nodes"]["external"]["eipPool"] = "198.51.100.0/26"'
bad_net "eipPool over the excluded range" 'n["nodes"]["external"]["eipPool"] = "198.18.10.0/28"'
bad_net "sriov is a reserved mode" 'n["direct"][0]["mode"] = "sriov"'
bad_net "nic name over 15 characters" 'n["nodes"]["management"]["nic"] = "enp1s0abcdefghij"'
bad_net "unknown network field" 'n["nodes"]["management"]["bond"] = "x"'
net_cr 'n["nodes"]["tunnel"]["nic"] = "enp4s0"; n["nodes"]["tunnel"]["vlan"] = 0; n["direct"][1]["vlan"] = 10'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"; expect_rc "an untagged host network may share an underlay nic" 0
# VM MTU (#236, vws-network#93): 9001 by default, lowered to what the tunnel NIC carries -> -vm-mtu.
vmmtu() { python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["services"]["network"]["flags"]["vm-mtu"])' "${WORK}/net.out"; }
net_cr 'n["nodes"]["tunnel"]["mtu"] = 9216'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"; expect_rc "a 9216 tunnel renders" 0
expect_text "a 9216 underlay gives VMs 9001" "$(vmmtu)" "9001"
net_cr 'pass'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "a 9000 tunnel lowers an unset vmMTU to 8900" "$(vmmtu)" "8900"
net_cr 'n["ipv4Only"] = True'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "ipv4Only takes a 60-byte overhead" "$(vmmtu)" "8940"
net_cr 'n["tunnelOverhead"] = 80; n["ipv4Only"] = True'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "tunnelOverhead wins over ipv4Only" "$(vmmtu)" "8920"
net_cr 'n["underlayMTU"] = 8000'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "underlayMTU below the tunnel mtu lowers the VM MTU" "$(vmmtu)" "7900"
net_cr 'n["underlayMTU"] = 8942; n["vmMTU"] = 8842'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "the vws-test override 8942/8842 renders" "$(vmmtu)" "8842"
net_cr 'n["vmMTU"] = 1500'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "a set vmMTU that fits is kept" "$(vmmtu)" "1500"
chart=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/net.out")
expect_text "the chart passes -vm-mtu to vws-network" "$chart" "-vm-mtu=1500"
net_cr 'n["nodes"]["tunnel"]["mtu"] = 1500'
run_vws_real render "${WORK}/net.yaml" "${WORK}/net.out"
expect_text "a 1500 underlay (vws-test ens3) gives VMs 1400" "$(vmmtu)" "1400"
bad_net "a vmMTU over what the tunnel carries" 'n["vmMTU"] = 9001'
bad_net "vmMTU below 68" 'n["vmMTU"] = 67'
bad_net "vmMTU above 9001" 'n["nodes"]["tunnel"]["mtu"] = 9216; n["vmMTU"] = 9100'
bad_net "underlayMTU too small for the overhead" 'n["underlayMTU"] = 1300'
bad_net "tunnelOverhead above 200" 'n["tunnelOverhead"] = 300'
bad_net "a vm-mtu flag beside spec.network.vmMTU" 'spec["services"]["network"] = {"flags": {"vm-mtu": 1500}}'

# The platform bundle for spec.network.nodes (#127).
# shellcheck source=tests/vws/platform_lib.sh
source "${VWS_TEST_ROOT}/tests/vws/platform_lib.sh"
platform_cr 'n.pop("nodes"); n.pop("direct")'
run_platform "$PLATFORM_BUNDLE"
if ((PRC == 0)) && diff -r "$PLATFORM_BUNDLE" "${WORK}/pout" >/dev/null; then ok "without nodes the bundle is a byte copy"; else bad "bundle copy" "rc ${PRC} $(cat "${WORK}/perr")"; fi
platform_cr pass
run_platform "$PLATFORM_BUNDLE"
if ((PRC == 0)); then ok "platform with 4 NICs exits 0"; else bad "platform 4 NICs" "$(cat "${WORK}/perr")"; fi
args=$(python3 -c 'import sys,yaml; print(" ".join(yaml.safe_load(open(sys.argv[1]))["spec"]["template"]["spec"]["containers"][0]["args"]))' "${WORK}/pout/20-kube-ovn.yaml")
expect_text "kube-ovn-cni uses the tunnel VLAN interface" "$args" "--iface=enp2s0.20 --mtu=8900 --network-type=geneve"
ceph=$(python3 -c 'import sys,yaml,json; print(json.dumps(yaml.safe_load(open(sys.argv[1]))["spec"]["network"], sort_keys=True))' "${WORK}/pout/31-rook-ceph-cluster.yaml")
expect_text "Ceph gets the public and cluster networks" "$ceph" '{"addressRanges": {"cluster": ["10.31.0.0/24"], "public": ["10.30.0.0/24"]}, "connections": {"requireMsgr2": true}, "provider": "host"}'
if [[ ! -e ${WORK}/pout/21-ovn-external-gw-config.yaml ]]; then ok "external writes no ovn-external-gw-config (#164)"; else bad "gw config" "$(cat "${WORK}/pout/21-ovn-external-gw-config.yaml")"; fi
# OVN gateway_mtu (#237): with nodes.external a Deployment keeps gateway_mtu on the VPC ports to vws-external.
gw="${WORK}/pout/22-vws-ovn-gateway-mtu.yaml"
gwq() { python3 -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); c=d["spec"]["template"]["spec"]["containers"][0]; print(eval(sys.argv[2]))' "$gw" "$1"; }
if [[ -f $gw ]]; then ok "external writes the gateway_mtu Deployment"; else bad "gateway_mtu file" "$(ls "${WORK}/pout")"; fi
expect_text "it runs in kube-system without a token" "$(gwq '(d["metadata"]["namespace"], d["spec"]["template"]["spec"]["automountServiceAccountToken"])')" "('kube-system', False)"
expect_text "the image is the bundle Kube-OVN version" "$(gwq 'c["image"]')" "docker.io/kubeovn/kube-ovn:$(sed -n 's/^KUBEOVN_VERSION=//p' "${VWS_TEST_ROOT}/scripts/versions.env")"
expect_text "the external MTU defaults to 1500 on vws-external" "$(gwq '{e["name"]: e["value"] for e in c["env"]}')" "{'SUBNET': 'vws-external', 'MTU': '1500', 'PERIOD': '30'}"
expect_text "the container is non-root with a read-only root" "$(gwq '(d["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"], c["securityContext"]["readOnlyRootFilesystem"])')" "(True, True)"
# The loop against a fake ovn-nbctl: it sets only ports to vws-external, once, and leaves an equal value alone.
mkdir -p "${WORK}/nbbin" && : >"${WORK}/nb.log"
printf '%s\n' '#!/usr/bin/env bash' 'shift 2; echo "$*" >>"$NBLOG"' \
  'case $1 in --bare) printf "vpc-a-vws-external\n\nvpc-a-subnet1\n\nvpc-b-vws-external\n" ;;' \
  '  --if-exists) if [[ $4 == vpc-b-vws-external ]] || grep -q "^set logical_router_port $4 " "$NBLOG"; then echo "\"1500\""; fi ;; esac; exit 0' >"${WORK}/nbbin/ovn-nbctl"
chmod +x "${WORK}/nbbin/ovn-nbctl"
script=$(gwq 'c["command"][2]')
PATH="${WORK}/nbbin:$PATH" NBLOG="${WORK}/nb.log" OVN_NB_SERVICE_HOST=10.96.0.9 OVN_NB_SERVICE_PORT=6641 SUBNET=vws-external MTU=1500 PERIOD=1 \
  timeout 3 /bin/sh -c "$script" >"${WORK}/nb.out" 2>&1 || true
sets=$(grep '^set ' "${WORK}/nb.log" || true)
expect_text "only vpc-a-vws-external is set, once" "$sets" "set logical_router_port vpc-a-vws-external options:gateway_mtu=1500"
if [[ $(grep -c '^set ' "${WORK}/nb.log") == 1 ]]; then ok "a second pass sets nothing (idempotent)"; else bad "idempotent" "$sets"; fi
if ! grep -q 'subnet1' <<<"$sets"; then ok "a port to another subnet is left alone"; else bad "other port" "$sets"; fi
platform_cr 'n["nodes"]["external"]["mtu"] = 9000'
run_platform "$PLATFORM_BUNDLE"
expect_text "the external link mtu becomes gateway_mtu" "$(gwq '{e["name"]: e["value"] for e in c["env"]}["MTU"]')" "9000"
platform_cr 'n["nodes"].pop("external"); n["nodes"].pop("storage"); n["nodes"].pop("tunnel"); n.pop("direct")'
run_platform "$PLATFORM_BUNDLE"
args=$(python3 -c 'import sys,yaml; print(" ".join(yaml.safe_load(open(sys.argv[1]))["spec"]["template"]["spec"]["containers"][0]["args"]))' "${WORK}/pout/20-kube-ovn.yaml")
expect_text "without tunnel the management NIC carries geneve" "$args" "--iface=enp1s0 --mtu=1400"
if [[ ! -e ${WORK}/pout/22-vws-ovn-gateway-mtu.yaml ]]; then ok "without external no gateway_mtu Deployment"; else bad "gateway_mtu without external" "$(ls "${WORK}/pout")"; fi
# #246: an external subnet wired only through the vws-network flag (no nodes.external) still gets
# the Deployment, on that subnet, at the default 1500.
platform_cr 'n.clear(); n["driver"] = "kube-ovn"; spec["services"] = {"network": {"flags": {"kube-external-subnet": "lab-ext", "pool": "10.78.0.224/28"}}}'
run_platform "$PLATFORM_BUNDLE"
if ((PRC == 0)) && [[ -f $gw ]]; then ok "flag kube-external-subnet writes the gateway_mtu Deployment"; else bad "gateway_mtu from flag" "$(ls "${WORK}/pout") $(cat "${WORK}/perr")"; fi
expect_text "flag: SUBNET is the flag value, MTU 1500" "$(gwq '{e["name"]: e["value"] for e in c["env"]}')" "{'SUBNET': 'lab-ext', 'MTU': '1500', 'PERIOD': '30'}"
platform_cr 'n.clear(); n["driver"] = "kube-ovn"; spec["services"] = {"network": {"flags": {"kube-external-subnet": "x; rm -rf /"}}}'
run_platform "$PLATFORM_BUNDLE"
if ((PRC == 2)) && grep -q "kube-external-subnet" "${WORK}/perr"; then ok "flag: a non-name subnet exits 2"; else bad "bad flag subnet" "rc=$PRC $(cat "${WORK}/perr")"; fi
platform_cr 'n.clear(); n["driver"] = "kube-ovn"; spec["services"] = {"network": {"flags": {"pool": "10.78.0.224/28"}}}'
run_platform "$PLATFORM_BUNDLE"
if [[ ! -e $gw ]]; then ok "no external subnet at all: no gateway_mtu Deployment"; else bad "gateway_mtu without any external" "$(ls "${WORK}/pout")"; fi
platform_cr 'n["nodes"].pop("external"); n["nodes"].pop("storage"); n["nodes"].pop("tunnel"); n.pop("direct"); n["ipv4Only"] = True'
run_platform "$PLATFORM_BUNDLE"
args=$(python3 -c 'import sys,yaml; print(" ".join(yaml.safe_load(open(sys.argv[1]))["spec"]["template"]["spec"]["containers"][0]["args"]))' "${WORK}/pout/20-kube-ovn.yaml")
expect_text "ipv4Only gives Kube-OVN the 60-byte overhead too" "$args" "--iface=enp1s0 --mtu=1440"
if [[ ! -e ${WORK}/pout/21-ovn-external-gw-config.yaml ]] && cmp -s "$PLATFORM_BUNDLE/31-rook-ceph-cluster.yaml" "${WORK}/pout/31-rook-ceph-cluster.yaml"; then ok "no Ceph network without storage"; else bad "optional parts" "$(ls "${WORK}/pout")"; fi
# #170: csi-driver-nfs is in the bundle; only driver nfs installs it, with the vws-nfs StorageClass.
mkdir -p "${WORK}/nfsbundle" && cp "$PLATFORM_BUNDLE"/*.yaml "${WORK}/nfsbundle/"
printf 'apiVersion: storage.k8s.io/v1\nkind: CSIDriver\nmetadata:\n  name: nfs.csi.k8s.io\n' >"${WORK}/nfsbundle/35-csi-driver-nfs.yaml"
nfs_cr() { # PYTHON_EDIT of s = spec.storage -> ${WORK}/pcr.yaml
  python3 - "${VWS_TEST_ROOT}/examples/vwscluster/ha.yaml" "$1" >"${WORK}/pcr.yaml" <<'PY'
import sys, yaml
cr = yaml.safe_load(open(sys.argv[1]))
cr["spec"].pop("network", None)
exec(sys.argv[2], {"s": cr["spec"].setdefault("storage", {})})
yaml.safe_dump(cr, sys.stdout)
PY
}
nfs_cr pass
run_platform "${WORK}/nfsbundle"
if ((PRC == 0)) && [[ ! -e ${WORK}/pout/35-csi-driver-nfs.yaml && ! -e ${WORK}/pout/36-vws-nfs-storageclass.yaml ]] && diff -r "$PLATFORM_BUNDLE" "${WORK}/pout" >/dev/null; then
  ok "rook-ceph installs no csi-driver-nfs and no vws-nfs class"; else bad "non-nfs drops the driver" "rc ${PRC} $(ls "${WORK}/pout")"; fi
nfs_cr 's.clear(); s.update(driver="nfs", nfs={"server": "nas.example.internal", "share": "/export/vws"})'
run_platform "${WORK}/nfsbundle"
if ((PRC == 0)) && cmp -s "${WORK}/nfsbundle/35-csi-driver-nfs.yaml" "${WORK}/pout/35-csi-driver-nfs.yaml"; then ok "nfs installs csi-driver-nfs as rendered"; else bad "nfs driver" "rc ${PRC} $(cat "${WORK}/perr")"; fi
sc=$(python3 -c 'import sys,yaml,json; print(json.dumps(yaml.safe_load(open(sys.argv[1])), sort_keys=True))' "${WORK}/pout/36-vws-nfs-storageclass.yaml")
expect_text "nfs makes the vws-nfs StorageClass from the CR export" "$sc" '{"allowVolumeExpansion": true, "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "vws-nfs"}, "parameters": {"server": "nas.example.internal", "share": "/export/vws"}, "provisioner": "nfs.csi.k8s.io", "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate"}'
cp -r "${WORK}/pout" "${WORK}/pout1" && run_platform "${WORK}/nfsbundle"
if diff -r "${WORK}/pout1" "${WORK}/pout" >/dev/null; then ok "the nfs bundle is the same bytes on a second run"; else bad "nfs idempotent" "$(diff -r "${WORK}/pout1" "${WORK}/pout")"; fi
run_platform "$PLATFORM_BUNDLE"
if ((PRC == 2)) && [[ ! -e ${WORK}/pout ]] && grep -q 'csi-driver-nfs' "${WORK}/perr"; then ok "nfs on a bundle without the driver exits 2 and writes nothing"; else bad "nfs without driver" "rc ${PRC}"; fi
nfs_cr 's.clear(); s.update(driver="nfs", nfs={"server": "nas", "share": "export; rm -rf /"})'
run_platform "${WORK}/nfsbundle"
if ((PRC == 2)) && [[ ! -e ${WORK}/pout ]]; then ok "a relative nfs share exits 2"; else bad "relative share" "rc ${PRC}"; fi

mkdir -p "${WORK}/oldovn" && sed 's/--iface=$/--iface=eth9/' "$PLATFORM_BUNDLE/20-kube-ovn.yaml" >"${WORK}/oldovn/20-kube-ovn.yaml"
platform_cr pass
run_platform "${WORK}/oldovn"
if ((PRC == 2)) && [[ ! -e ${WORK}/pout ]]; then ok "a changed Kube-OVN chart exits 2 and writes nothing"; else bad "changed chart" "rc ${PRC}"; fi

printf -- '---\na: 1\n---\nb: 2\n' >"${WORK}/two.yaml"
run_vws_real render "${WORK}/two.yaml" "${WORK}/x.yaml"; expect_rc "two documents exit 2" 2
run_vws_real render "${WORK}/missing.yaml" "${WORK}/x.yaml"; expect_rc "a missing file exits 2" 2
run_vws_real render "${EX}/ha.yaml"; expect_rc "a missing output argument exits 2" 2

# popododo0720/vws-console-api#95: console-api holds sessions in memory, so it runs one replica in
# every profile, and the CR, the chart schema and check_render.py refuse any other count.
for profile in single-node small ha; do
  got=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/profiles/${profile}.yaml" --set services.network.flags.pool=198.18.0.0/24 |
    python3 -c 'import sys,yaml; print([d["spec"]["replicas"] for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "console-api"])')
  expect_text "console-api: ${profile} renders one replica" "$got" "[1]"
done
python3 -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); d["spec"]["services"]["console-api"]["replicas"]=4; yaml.safe_dump(d,sys.stdout)' "${EX}/ha.yaml" >"${WORK}/ca4.yaml"
run_vws_real render "${WORK}/ca4.yaml" "${WORK}/x.yaml";  expect_rc "console-api: a CR with replicas 4 exits 2" 2
expect_text "console-api: the CR error says why" "$ERR" "sessions in memory"
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws --set services.console-api.replicas=2 >/dev/null 2>"${WORK}/ca.err"; then
  bad "console-api: schema refuses 2" "rendered"; else expect_text "console-api: the schema refuses 2" "$(cat "${WORK}/ca.err")" "services.console-api.replicas"; fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws --set services.console-api.autoscaling.enabled=true >/dev/null 2>&1; then
  bad "console-api: schema refuses autoscaling" "rendered"; else ok "console-api: the schema refuses an autoscaling value"; fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws --set services.console.replicas=1 >/dev/null 2>&1; then
  bad "console: one replica still refused" "rendered"; else ok "console: other Deployments still need two replicas"; fi
helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" >"${WORK}/ca.yaml"
python3 - "${WORK}/ca.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
two = [dict(d, spec=dict(d["spec"], replicas=2)) if d["kind"] == "Deployment" and d["metadata"]["name"] == "console-api" else d for d in docs]
yaml.safe_dump_all(two, open(sys.argv[1] + ".two", "w"))
hpa = {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": {"name": "console-api", "namespace": "vws"},
       "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "console-api"}, "maxReplicas": 3}}
yaml.safe_dump_all(docs + [hpa], open(sys.argv[1] + ".hpa", "w"))
PY
cp "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" "${WORK}/ca-values.yaml"
out=$(python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/ca.yaml.two" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${WORK}/ca-values.yaml" 2>&1 || true)
expect_text "console-api: check_render refuses two replicas" "$out" "R4: Deployment console-api must have exactly one replica: sessions are held in memory"
out=$(python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/ca.yaml.hpa" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${WORK}/ca-values.yaml" 2>&1 || true)
expect_text "console-api: check_render refuses an HPA" "$out" "R10: kind HorizontalPodAutoscaler is not a control-plane object"
out=$(python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/ca.yaml" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${WORK}/ca-values.yaml" 2>&1) && ok "console-api: the default render passes check_render" || bad "default render" "$out"

# popododo0720/vws-console-api#137 (5b42eb7+): console-api answers 403 to sign-in and cookie
# writes from any Origin it was not given, and vws cannot know the LoadBalancer address, so the
# CR must name it. Needs a console-api image from #137 (5b42eb7) or later. vws-test: on a default
# install (-iam=static) with -console-origins set to the console URL, the console works without
# login (all 200).
python3 -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); del d["spec"]["services"]["console-api"]; yaml.safe_dump(d,sys.stdout)' "${EX}/small.yaml" >"${WORK}/noorigin.yaml"
run_vws_real render "${WORK}/noorigin.yaml" "${WORK}/x.yaml"; expect_rc "console-origins: a CR without it exits 2" 2
expect_text "console-origins: the CR error names the field" "$ERR" "spec.services.console-api.flags.console-origins: required"
run_vws_real render "${EX}/small.yaml" "${WORK}/origin.yaml"; expect_rc "console-origins: the example renders" 0
got=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/origin.yaml" |
  python3 -c 'import sys,yaml; print([a for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "console-api" for a in d["spec"]["template"]["spec"]["containers"][0]["args"] if "origins" in a or "proxies" in a])')
expect_text "console-origins: the flag reaches console-api" "$got" "['-console-origins=https://console.example.invalid']"
# cr_case NAME VALUES: render the chart with these site values and run check_render.py on it.
cr_case() {
  printf '%s\n' "$2" >"${WORK}/cr-$1.yaml"
  helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/cr-$1.yaml" >"${WORK}/cr-$1.out" 2>&1 || true
  python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/cr-$1.out" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${WORK}/cr-$1.yaml" 2>&1
}
pool='services: {network: {flags: {pool: 198.18.0.0/24}}'
out=$(cr_case noorigin "${pool}}" || true)
expect_text "console-origins: check_render refuses a render without it" "$out" "R15: console-api needs -console-origins"
out=$(cr_case badorigin "${pool}, console-api: {flags: {console-origins: 'https://console.example/login'}}}" || true)
expect_text "console-origins: check_render refuses an origin with a path" "$out" "'https://console.example/login' must be scheme://host[:port]"
out=$(cr_case proxies "${pool}, console-api: {flags: {console-origins: 'https://a.example,http://10.0.0.5:8080', trusted-proxies: '10.0.0.0/8,192.0.2.1'}}}") &&
  ok "console-origins/trusted-proxies: valid lists pass check_render" || bad "trusted-proxies valid" "$out"
out=$(cr_case badproxies "${pool}, console-api: {flags: {console-origins: 'https://a.example', trusted-proxies: '10.0.0.0/33'}}}" || true)
expect_text "trusted-proxies: check_render refuses a bad CIDR" "$out" "'10.0.0.0/33' is not an IP or CIDR"
got=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" | grep -c -- '-trusted-proxies' || true)
expect_text "trusted-proxies: not passed by default" "$got" "0"
got=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set-string 'services.console-api.flags.trusted-proxies=10.0.0.0/8' | grep -c -- '-trusted-proxies=10.0.0.0/8' || true)
expect_text "trusted-proxies: passed when set" "$got" "1"

# vws-deploy#280: console-api is pinned by services.console-api.imageTag to sha-a003d96, the
# console-api#156 build (env credentials, S1) that vws-console-api publish.yml pushes as ghcr.io/popododo0720/vws/console-api:sha-<commit>.
# compute is pinned the same way to sha-07b591c (vws-compute#91, published by vws-compute publish.yml).
# Other services keep global.tag / appVersion, and spec.version moves them but not the pinned ones.
images() {
  helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws "$@" |
    python3 -c 'import sys,yaml; print(sorted(d["metadata"]["name"]+"="+d["spec"]["template"]["spec"]["containers"][0]["image"] for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] in ("Deployment","StatefulSet") and d["metadata"]["namespace"] == "vws"))'
}
got=$(images -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml")
expect_text "image pin: console-api renders sha-a003d96" "$got" "'console-api=ghcr.io/popododo0720/vws/console-api:sha-a003d96'"
expect_text "image pin: compute renders sha-07b591c" "$got" "'compute=ghcr.io/popododo0720/vws/compute:sha-07b591c'"
expect_text "image pin: other services keep appVersion" "$got" "'storage=ghcr.io/popododo0720/vws/storage:0.1.0'"
# vws-iam#113 published ghcr.io/popododo0720/vws/iam:sha-74a29dc. The default render
# leaves iam out (enabled false). With iam on, the image is that pin and global.tag does not move it.
got=$(images -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set services.iam.enabled=true)
expect_text "image pin: iam renders sha-74a29dc" "$got" "'iam=ghcr.io/popododo0720/vws/iam:sha-74a29dc'"
run_vws_real render "${EX}/ha.yaml" "${WORK}/pin.yaml"; expect_rc "image pin: ha example renders" 0
got=$(images -f "${WORK}/pin.yaml" --set global.tag=9.9.9)
expect_text "image pin: global.tag moves other services" "$got" "'storage=ghcr.io/popododo0720/vws/storage:9.9.9'"
expect_text "image pin: global.tag does not move compute" "$got" "'compute=ghcr.io/popododo0720/vws/compute:sha-07b591c'"
expect_text "image pin: global.tag does not move console-api" "$got" "'console-api=ghcr.io/popododo0720/vws/console-api:sha-a003d96'"
got=$(images -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set services.iam.enabled=true --set global.tag=9.9.9)
expect_text "image pin: global.tag does not move iam" "$got" "'iam=ghcr.io/popododo0720/vws/iam:sha-74a29dc'"
got=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" | python3 -c 'import sys,yaml; print(sum(1 for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] in ("Deployment","StatefulSet") for c in d["spec"]["template"]["spec"]["containers"] if "command" in c))')
expect_text "image pin: no workload sets command (ko entrypoint /ko-app/<name> is kept)" "$got" "0"
got=$(images -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set services.console-api.imageTag=sha-1234567)
expect_text "image pin: imageTag is the only knob" "$got" "'console-api=ghcr.io/popododo0720/vws/console-api:sha-1234567'"
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set 'services.console-api.imageTag=a/b' >/dev/null 2>"${WORK}/tag.err"; then
  bad "image pin: schema refuses a slash" "rendered"; else expect_text "image pin: the schema refuses a tag with a slash" "$(cat "${WORK}/tag.err")" "imageTag"; fi

# vws-compute#73: compute keeps the cloud-init Secret <instance>-cloudinit in the VM namespace. Only
# the namespaced Role vws-compute in vws-vms may touch secrets: get, create, update, delete, no list
# or watch (compute uses an uncached client), and no other Role or ClusterRole gains secrets.
secret_rules=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] in ("Role", "ClusterRole"):
        for r in d["rules"]:
            if "secrets" in r["resources"]:
                print(d["kind"], d["metadata"]["name"], d["metadata"].get("namespace"), r["resources"], r["verbs"], r.get("resourceNames"))')
expect_text "compute: secrets get/create/update/delete in vws-vms" "$secret_rules" "Role vws-compute vws-vms ['secrets'] ['get', 'create', 'update', 'delete'] None"
if [[ $(wc -l <<<"$secret_rules") == 1 ]]; then ok "compute: that is the only rule on secrets"; else bad "secrets rules" "$secret_rules"; fi
if ! grep -qE "list|watch" <<<"$secret_rules"; then ok "compute: no list or watch on secrets"; else bad "secrets list/watch" "$secret_rules"; fi

# vws-compute#105: instance destroy removes the root claim and DataVolume in the VM namespace. get
# stays for checkAttachable and the health probe; the DataVolume get reads it before delete.
vm_rights=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws | python3 -c '
import sys, yaml
rights = {}
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Role" and d["metadata"]["name"] == "vws-compute" and d["metadata"].get("namespace") == "vws-vms":
        for r in d["rules"]:
            for key in ((g, res) for g in r["apiGroups"] for res in r["resources"]):
                rights.setdefault(key, set()).update(r["verbs"])
print(sorted((g, res, sorted(v)) for (g, res), v in rights.items()))')
expect_text "compute: exact rights in vws-vms, including datavolume and PVC delete (vws-compute#105)" "$vm_rights" "[('', 'persistentvolumeclaims', ['delete', 'get']), ('', 'secrets', ['create', 'delete', 'get', 'update']), ('cdi.kubevirt.io', 'datavolumes', ['delete', 'get']), ('kubevirt.io', 'virtualmachineinstances', ['delete', 'get']), ('kubevirt.io', 'virtualmachines', ['create', 'delete', 'get', 'list', 'patch'])]"

image_rights=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws | python3 -c '
import sys, yaml
rights = {}
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Role" and d["metadata"]["name"] == "vws-compute" and d["metadata"].get("namespace") == "vws-images":
        for r in d["rules"]:
            for key in ((g, res) for g in r["apiGroups"] for res in r["resources"]):
                rights.setdefault(key, set()).update(r["verbs"])
print(sorted((g, res, sorted(v)) for (g, res), v in rights.items()))')
expect_text "compute: exact rights in vws-images, list and patch only on datasources" "$image_rights" "[('', 'persistentvolumeclaims', ['get']), ('cdi.kubevirt.io', 'datasources', ['create', 'delete', 'get', 'list', 'patch']), ('cdi.kubevirt.io', 'datavolumes', ['create', 'delete', 'get'])]"

# S1: console-api credentials go only by secretKeyRef env. No -console-token, -access-key or -secret
# in args, not even as a $(VAR) reference, so the values never reach the process arguments.
creds() {
  helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws "$@" | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] in ("Deployment", "StatefulSet") and d["metadata"]["name"] == "console-api":
        c = d["spec"]["template"]["spec"]["containers"][0]
        bad = [a.split("=")[0] for a in c.get("args", []) if a.split("=")[0].lstrip("-") in ("console-token", "access-key", "secret") or "$(VWS_" in a]
        env = {e["name"]: e.get("valueFrom", {}).get("secretKeyRef") for e in c.get("env", []) if e["name"] in ("VWS_CONSOLE_TOKEN", "VWS_ACCESS_KEY", "VWS_SECRET_KEY")}
        lit = [e["name"] for e in c.get("env", []) if e["name"] in env and "value" in e]
        print("args", bad); print("env", sorted((k, v["name"], v["key"]) for k, v in env.items() if v)); print("literal", lit)'
}
got=$(creds -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml")
expect_text "S1: no credential flag in console-api args" "$got" "args []"
expect_text "S1: credentials are secretKeyRef env" "$got" "env [('VWS_ACCESS_KEY', 'vws-console-api-credentials', 'access-key'), ('VWS_CONSOLE_TOKEN', 'vws-console-api-credentials', 'console-token'), ('VWS_SECRET_KEY', 'vws-console-api-credentials', 'secret-key')]"
expect_text "S1: no credential as a literal env value" "$got" "literal []"
run_vws_real render "${EX}/ha.yaml" "${WORK}/s1.yaml"; expect_rc "S1: ha example renders" 0
got=$(creds -f "${WORK}/s1.yaml")
expect_text "S1: ha render has no credential flag" "$got" "args []"
helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" >"${WORK}/s1.out"
python3 - "${WORK}/s1.out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
for d in docs:
    if d["kind"] == "Deployment" and d["metadata"]["name"] == "console-api":
        d["spec"]["template"]["spec"]["containers"][0]["args"].append("-secret=$(VWS_SECRET_KEY)")
yaml.safe_dump_all(docs, open(sys.argv[1] + ".flag", "w"))
PY
cp "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" "${WORK}/s1-values.yaml"
out=$(python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/s1.out.flag" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${WORK}/s1-values.yaml" 2>&1 || true)
expect_text "S1: check_render refuses a credential flag in args" "$out" "R16: console-api passes credential flag -secret in args"

cat >"${WORK}/iam-listeners.yaml" <<'YAML'
services:
  iam:
    enabled: true
  console-api:
    flags:
      iam: grpc
      ec2-addr: 0.0.0.0:8081
      s3-addr: 0.0.0.0:8082
      iam-addr: 0.0.0.0:8084
YAML
helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" -f "${WORK}/iam-listeners.yaml" >"${WORK}/iam-listeners.out"
got=$(python3 - "${WORK}/iam-listeners.out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
def one(kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)
iam = one("Service", "iam")
idep = one("Deployment", "iam")["spec"]["template"]["spec"]["containers"][0]
ca = one("Service", "console-api")
cdep = one("Deployment", "console-api")["spec"]["template"]["spec"]["containers"][0]
pol = one("NetworkPolicy", "iam")
policy_ports = [p.get("port") for rule in pol["spec"]["ingress"] for p in rule.get("ports") or []]
print("iam-type", iam["spec"]["type"])
print("iam-ports", iam["spec"]["ports"])
print("iam-cports", [p["containerPort"] for p in idep["ports"]])
print("iam-replicas", one("Deployment", "iam")["spec"]["replicas"])
print("iam-args", idep["args"])
print("iam-policy-ports", policy_ports)
print("ca-ports", [(p["name"], p["port"], p["targetPort"]) for p in ca["spec"]["ports"]])
print("ca-cports", [p["containerPort"] for p in cdep["ports"]])
print("ca-args", cdep.get("args"))
PY
)
expect_text "iam listeners: Service iam is ClusterIP" "$got" "iam-type ClusterIP"
expect_text "iam listeners: Service iam is http 9095" "$got" "iam-ports [{'name': 'http', 'port': 9095, 'targetPort': 'http'}]"
expect_text "iam listeners: Deployment iam containerPorts are 9095" "$got" "iam-cports [9095]"
expect_text "iam listeners: Deployment iam has two replicas" "$got" "iam-replicas 2"
expect_text "iam listeners: Deployment iam args are listen and the store namespace" "$got" "iam-args ['-listen=0.0.0.0:9095', '-namespace=vws']"
expect_text "iam listeners: NetworkPolicy port is numeric 9095" "$got" "iam-policy-ports [9095]"
expect_text "iam listeners: console-api Service ports" "$got" "ca-ports [('http', 8080, 'http'), ('ec2', 8081, 8081), ('s3', 8082, 8082), ('iam', 8084, 8084)]"
expect_text "iam listeners: console-api containerPorts stay 8080" "$got" "ca-cports [8080]"
expect_text "iam listeners: console-api args include -iam=grpc" "$got" "-iam=grpc"
expect_text "iam listeners: console-api args include -iam-endpoint=iam:9095" "$got" "-iam-endpoint=iam:9095"
expect_text "iam listeners: console-api args include -ec2-addr=0.0.0.0:8081" "$got" "-ec2-addr=0.0.0.0:8081"
expect_text "iam listeners: console-api args include -s3-addr=0.0.0.0:8082" "$got" "-s3-addr=0.0.0.0:8082"
expect_text "iam listeners: console-api args include -iam-addr=0.0.0.0:8084" "$got" "-iam-addr=0.0.0.0:8084"
if python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/iam-listeners.out" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" "${WORK}/iam-listeners.yaml" >"${WORK}/iam-listeners.check" 2>&1; then
  ok "iam listeners: check_render accepts the render"
else
  bad "iam listeners: check_render" "$(cat "${WORK}/iam-listeners.check")"
fi
python3 - "${WORK}/iam-listeners.out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
for doc in docs:
    if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "iam":
        doc["spec"]["ingress"][0]["ports"] = [{"port": "http"}]
yaml.safe_dump_all(docs, open(sys.argv[1] + ".named", "w"))
PY
if python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/iam-listeners.out.named" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" "${WORK}/iam-listeners.yaml" >"${WORK}/iam-named.check" 2>&1; then
  bad "iam listeners: named NetworkPolicy port accepted" "exit 0"
else
  expect_text "iam listeners: a named NetworkPolicy port fails" "$(cat "${WORK}/iam-named.check")" "numeric service port"
fi
python3 - "${WORK}/iam-listeners.out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
for doc in docs:
    if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "iam":
        doc["spec"]["ingress"][0]["ports"] = [{"port": 9095, "endPort": 9099}]
yaml.safe_dump_all(docs, open(sys.argv[1] + ".range", "w"))
PY
if python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/iam-listeners.out.range" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" "${WORK}/iam-listeners.yaml" >"${WORK}/iam-range.check" 2>&1; then
  bad "iam listeners: NetworkPolicy endPort accepted" "exit 0"
else
  expect_text "iam listeners: a NetworkPolicy endPort fails" "$(cat "${WORK}/iam-range.check")" "endPort"
fi
python3 - "${WORK}/iam-listeners.out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
for doc in docs:
    if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "iam":
        doc["spec"]["ingress"][0]["ports"] = [{"protocol": "UDP", "port": 9095}]
yaml.safe_dump_all(docs, open(sys.argv[1] + ".udp", "w"))
PY
if python3 "${VWS_TEST_ROOT}/scripts/check_render.py" "${WORK}/iam-listeners.out.udp" "${VWS_TEST_ROOT}/charts/vws/values.yaml" vws "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" "${WORK}/iam-listeners.yaml" >"${WORK}/iam-udp.check" 2>&1; then
  bad "iam listeners: NetworkPolicy UDP accepted" "exit 0"
else
  expect_text "iam listeners: a non-TCP NetworkPolicy protocol fails" "$(cat "${WORK}/iam-udp.check")" "must be TCP"
fi
# #369: iam shares its kube store, so it is a normal stateless service with at least two replicas.
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws --set services.iam.enabled=true --set services.iam.replicas=1 >/dev/null 2>"${WORK}/iam-r1.err"; then
  bad "iam: one replica rendered" "exit 0"
else
  ok "iam: schema refuses one replica"
fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws --set services.iam.enabled=true --set services.iam.replicas=3 >/dev/null 2>"${WORK}/iam-r3.err"; then
  ok "iam: schema allows three replicas"
else
  bad "iam: schema allows three replicas" "$(cat "${WORK}/iam-r3.err")"
fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set-string services.console-api.flags.iam=grpc >/dev/null 2>"${WORK}/iam-off.err"; then
  bad "iam listeners: grpc with iam disabled rendered" "exit 0"
else
  ok "iam listeners: grpc with iam disabled fails"
fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" -f "${WORK}/iam-listeners.yaml" --set-string services.console-api.flags.iam-endpoint=iam:9095 >/dev/null 2>"${WORK}/iam-ep.err"; then
  bad "iam listeners: a caller iam-endpoint rendered" "exit 0"
else
  ok "iam listeners: a caller iam-endpoint fails"
fi
cat >"${WORK}/iam-loop.yaml" <<'YAML'
services:
  iam:
    enabled: true
  console-api:
    flags:
      iam: grpc
      ec2-addr: 127.0.0.1:8081
      s3-addr: 0.0.0.0:8082
      iam-addr: 0.0.0.0:8084
YAML
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" -f "${WORK}/iam-loop.yaml" >/dev/null 2>"${WORK}/iam-loop.err"; then
  bad "iam listeners: loopback ec2-addr rendered" "exit 0"
else
  ok "iam listeners: loopback ec2-addr fails"
fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set-string services.console-api.flags.ec2-addr=0.0.0.0:80 >/dev/null 2>"${WORK}/iam-low.err"; then
  bad "iam listeners: port 80 rendered" "exit 0"
else
  expect_text "iam listeners: port 80 is outside the Service range" "$(cat "${WORK}/iam-low.err")" "outside 1024..65535"
fi
if helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${VWS_TEST_ROOT}/charts/vws/ci/site.yaml" --set-string services.console-api.flags.ec2-addr=0.0.0.0:8080 >/dev/null 2>"${WORK}/iam-dup.err"; then
  bad "iam listeners: a second listener on 8080 rendered" "exit 0"
else
  expect_text "iam listeners: a second listener on the primary port fails" "$(cat "${WORK}/iam-dup.err")" "primary port"
fi
python3 - "${EX}/single-node.yaml" "${WORK}/iam-cr.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
doc["spec"]["services"]["iam"] = {"enabled": True}
flags = doc["spec"]["services"]["console-api"]["flags"]
flags["iam"] = "grpc"
flags["ec2-addr"] = "0.0.0.0:8081"
flags["s3-addr"] = "0.0.0.0:8082"
flags["iam-addr"] = "0.0.0.0:8084"
yaml.safe_dump(doc, open(sys.argv[2], "w"), sort_keys=False)
PY
run_vws_real render "${WORK}/iam-cr.yaml" "${WORK}/iam-cr.values.yaml"; expect_rc "iam listeners: vws render exits 0" 0
run_vws_real render "${WORK}/iam-cr.yaml" "${WORK}/iam-cr.again.yaml"; expect_rc "iam listeners: vws render again exits 0" 0
if cmp -s "${WORK}/iam-cr.values.yaml" "${WORK}/iam-cr.again.yaml"; then ok "iam listeners: vws render twice gives the same bytes"; else bad "iam listeners: vws render is deterministic" "outputs differ"; fi
if grep -q 'iam-endpoint' "${WORK}/iam-cr.values.yaml"; then bad "iam listeners: render writes iam-endpoint" "present"; else ok "iam listeners: the values overlay has no iam-endpoint"; fi
chart=$(helm template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/iam-cr.values.yaml")
expect_text "iam listeners: the chart sets -iam-endpoint from the overlay" "$chart" "-iam-endpoint=iam:9095"
python3 - "${EX}/single-node.yaml" "${WORK}/iam-r2.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
doc["spec"]["services"]["iam"] = {"enabled": True, "replicas": 2}
yaml.safe_dump(doc, open(sys.argv[2], "w"), sort_keys=False)
PY
run_vws_real render "${WORK}/iam-r2.yaml" "${WORK}/iam-r2.values.yaml"; expect_rc "iam: a CR with replicas 2 exits 0" 0
expect_text "iam: the CR replicas reach the values" "$(cat "${WORK}/iam-r2.values.yaml")" "replicas: 2"

finish_tests
