#!/usr/bin/env python3
"""platform.py CR BUNDLE OUT: the platform bundle for this VWSCluster (vws-deploy#127).

A pure function of the CR and the rendered bundle (build/platform). Without spec.network.nodes OUT
is a byte copy of BUNDLE, less csi-driver-nfs unless the driver is nfs. With it:
- kube-ovn-cni gets --iface=<tunnel interface> and --mtu=<tunnel mtu - 100> (geneve overhead);
- the CephCluster gets network.provider host and addressRanges.public/cluster;
No ovn-external-gw-config: it would delete the underlay bridge and wants a Subnet named external
(vws-deploy#164). Gateway node labels belong to vws-network.
With spec.storage.driver nfs, csi-driver-nfs and the vws-nfs StorageClass; otherwise no csi-driver-nfs.
Exit 2 when the CR or the bundle does not fit.
"""
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import GENEVE_OVERHEAD, NFS_CLASS, Invalid, host_interface  # noqa: E402

# csi-driver-nfs (vws-deploy#170): in the bundle for every CR, installed only when spec.storage.driver is
# nfs. Then NFS_CLASS_FILE adds the StorageClass the storage service uses, with the CR's export.
NFS_DRIVER_FILE = "35-csi-driver-nfs.yaml"
NFS_CLASS_FILE = "36-vws-nfs-storageclass.yaml"
NFS_PROVISIONER = "nfs.csi.k8s.io"


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
    for f in files:
        if f.name == NFS_DRIVER_FILE and not nfs:
            continue
        text = f.read_text()
        if nodes:
            if f.name.startswith("20-kube-ovn"):
                tunnel = nodes.get("tunnel", nodes["management"])
                text = patch_kube_ovn(text, host_interface(tunnel), tunnel.get("mtu", 1500) - GENEVE_OVERHEAD)
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
