#!/usr/bin/env python3
"""platform.py CR BUNDLE OUT: the platform bundle for this VWSCluster (vws-deploy#127).

A pure function of the CR and the rendered bundle (build/platform). Without spec.network.nodes OUT
is a byte copy of BUNDLE, less csi-driver-nfs unless the driver is nfs. With it:
- kube-ovn-cni gets --iface=<tunnel interface> and --mtu=<tunnel mtu - tunnel overhead> (100, or spec.network.tunnelOverhead / ipv4Only);
- the CephCluster gets network.provider host and addressRanges.public/cluster;
No ovn-external-gw-config: it would delete the underlay bridge and wants a Subnet named external
(vws-deploy#164). Gateway node labels belong to vws-network.
With spec.network.nodes.external, GATEWAY_MTU_FILE: a Deployment that keeps OVN options:gateway_mtu
at the external MTU on every VPC router port to the external subnet (vws-deploy#237), so the router
answers oversized packets bound for the 1500 path with ICMP fragmentation-needed (PMTUD).
With spec.storage.driver nfs, csi-driver-nfs and the vws-nfs StorageClass; otherwise no csi-driver-nfs.
Exit 2 when the CR or the bundle does not fit.
"""
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import EXTERNAL_SUBNET, NFS_CLASS, Invalid, host_interface, tunnel_overhead  # noqa: E402

# csi-driver-nfs (vws-deploy#170): in the bundle for every CR, installed only when spec.storage.driver is
# nfs. Then NFS_CLASS_FILE adds the StorageClass the storage service uses, with the CR's export.
NFS_DRIVER_FILE = "35-csi-driver-nfs.yaml"
NFS_CLASS_FILE = "36-vws-nfs-storageclass.yaml"
NFS_PROVISIONER = "nfs.csi.k8s.io"

# OVN gateway_mtu (vws-deploy#237). Kube-OVN sets no gateway_mtu and has no field for it; vws-network
# only talks to the Kubernetes API. Kube-OVN names a VPC's port to an extraExternalSubnets subnet
# <vpc>-<subnet>. The loop sets the option only where it differs, so it is idempotent and picks up
# new VPCs within GATEWAY_MTU_PERIOD seconds. It reaches the NB DB through the ovn-nb Service
# (kube-system, plain TCP 6641, the bundle has ENABLE_SSL=false) and needs no Kubernetes RBAC.
GATEWAY_MTU_FILE = "22-vws-ovn-gateway-mtu.yaml"
GATEWAY_MTU_NAME = "vws-ovn-gateway-mtu"
GATEWAY_MTU_PERIOD = 30
KUBEOVN_IMAGE = "docker.io/kubeovn/kube-ovn"
VERSIONS = Path(__file__).resolve().parents[3] / "scripts" / "versions.env"
GATEWAY_MTU_SCRIPT = """db="tcp:${OVN_NB_SERVICE_HOST}:${OVN_NB_SERVICE_PORT}"
nb() { ovn-nbctl --db="$db" --timeout=10 "$@"; }
while :; do
  for lrp in $(nb --bare --columns=name find logical_router_port); do
    case $lrp in *-${SUBNET}) ;; *) continue ;; esac
    cur=$(nb --if-exists get logical_router_port "$lrp" options:gateway_mtu | tr -d '"')
    [ "$cur" = "$MTU" ] && continue
    nb set logical_router_port "$lrp" options:gateway_mtu="$MTU" && echo "$lrp gateway_mtu=$MTU (was ${cur:-unset})"
  done
  sleep "$PERIOD"
done
"""


def kubeovn_version():
    for line in VERSIONS.read_text().splitlines():
        if line.startswith("KUBEOVN_VERSION="):
            return line.split("=", 1)[1].strip()
    raise Invalid(f"{VERSIONS}: no KUBEOVN_VERSION")


DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def gateway_mtu_target(spec):
    """(external subnet, MTU) for the gateway_mtu Deployment, or None. spec.network.nodes.external
    names vws-external with its link MTU; otherwise an external subnet wired only through the
    vws-network flag kube-external-subnet gets the default 1500 (#246)."""
    external = (((spec.get("network") or {}).get("nodes")) or {}).get("external")
    if external:
        return EXTERNAL_SUBNET, external.get("mtu", 1500)
    flags = ((spec.get("services") or {}).get("network") or {}).get("flags") or {}
    subnet = flags.get("kube-external-subnet")
    if not subnet:
        return None
    if not DNS_LABEL.match(str(subnet)):
        raise Invalid(f"spec.services.network.flags.kube-external-subnet: {subnet!r} is not a subnet name")
    return str(subnet), 1500


def gateway_mtu(subnet, mtu):
    """The Deployment that keeps gateway_mtu=MTU on the VPC ports to the external SUBNET."""
    labels = {"app.kubernetes.io/name": GATEWAY_MTU_NAME, "app.kubernetes.io/part-of": "vws"}
    env = [{"name": "SUBNET", "value": subnet},
           {"name": "MTU", "value": str(mtu)},
           {"name": "PERIOD", "value": str(GATEWAY_MTU_PERIOD)}]
    return yaml.safe_dump({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": GATEWAY_MTU_NAME, "namespace": "kube-system", "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": True,
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "gateway-mtu",
                        "image": f"{KUBEOVN_IMAGE}:{kubeovn_version()}",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-c", GATEWAY_MTU_SCRIPT],
                        "env": env,
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                      "limits": {"memory": "64Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                    }],
                },
            },
        },
    }, sort_keys=True)


def nfs_class(nfs):
    if not (nfs or {}).get("server") or not str(nfs.get("share", "")).startswith("/"):
        raise Invalid("spec.storage.nfs: server and an absolute share are required when driver is nfs")
    return yaml.safe_dump({
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": NFS_CLASS},
        "provisioner": NFS_PROVISIONER,
        "parameters": {"server": nfs["server"], "share": nfs["share"]},
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
        "allowVolumeExpansion": True,
    }, sort_keys=True)

def patch_kube_ovn(text, iface, mtu):
    old = "          - --iface=\n"
    if text.count(old) != 1:
        raise Invalid("bundle: kube-ovn-cni has no single empty --iface= argument; the Kube-OVN chart changed")
    return text.replace(old, f"          - --iface={iface}\n          - --mtu={mtu}\n")


def patch_ceph(text, storage):
    docs = list(yaml.safe_load_all(text))
    clusters = [d for d in docs if d and d.get("kind") == "CephCluster"]
    if not clusters:
        return text
    if len(clusters) != 1:
        raise Invalid("bundle: want at most one CephCluster per file")
    net = clusters[0]["spec"].setdefault("network", {})
    net["provider"] = "host"
    ranges = {"public": [storage["public"]["cidr"]]}
    if "cluster" in storage:
        ranges["cluster"] = [storage["cluster"]["cidr"]]
    net["addressRanges"] = ranges
    return yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False)


def build(cr, bundle, out):
    spec = cr.get("spec") or {}
    nodes = (spec.get("network") or {}).get("nodes")
    storage = spec.get("storage") or {}
    nfs = storage.get("driver") == "nfs"
    files = sorted(bundle.glob("*.yaml"))
    if nfs and not (bundle / NFS_DRIVER_FILE).is_file():
        raise Invalid(f"bundle: driver nfs needs {NFS_DRIVER_FILE}; the bundle has no csi-driver-nfs")
    out.mkdir(parents=True, exist_ok=True)
    if nfs:
        (out / NFS_CLASS_FILE).write_text(nfs_class(storage.get("nfs")))
    target = gateway_mtu_target(spec)
    if target:
        (out / GATEWAY_MTU_FILE).write_text(gateway_mtu(*target))
    for f in files:
        if f.name == NFS_DRIVER_FILE and not nfs:
            continue
        text = f.read_text()
        if nodes:
            if f.name.startswith("20-kube-ovn"):
                tunnel = nodes.get("tunnel", nodes["management"])
                text = patch_kube_ovn(text, host_interface(tunnel), tunnel.get("mtu", 1500) - tunnel_overhead(spec.get("network") or {}))
            elif "\nkind: CephCluster\n" in text and "storage" in nodes:
                text = patch_ceph(text, nodes["storage"])
        (out / f.name).write_text(text)


def main(argv):
    if len(argv) != 4:
        print("usage: platform.py CR BUNDLE OUT", file=sys.stderr)
        return 2
    try:
        cr = yaml.safe_load(Path(argv[1]).read_text())
        build(cr, Path(argv[2]), Path(argv[3]))
    except Invalid as err:
        print(f"platform: {err}", file=sys.stderr)
        shutil.rmtree(argv[3], ignore_errors=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
