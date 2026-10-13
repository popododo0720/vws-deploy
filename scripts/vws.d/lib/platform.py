#!/usr/bin/env python3
"""platform.py CR BUNDLE OUT: the platform bundle for this VWSCluster (vws-deploy#127).

A pure function of the CR and the rendered bundle (build/platform). Without spec.network.nodes OUT
is a byte copy of BUNDLE. With it:
- kube-ovn-cni gets --iface=<tunnel interface> and --mtu=<tunnel mtu - 100> (geneve overhead);
- the CephCluster gets network.provider host and addressRanges.public/cluster;
- with nodes.external, 21-ovn-external-gw-config.yaml turns on the OVN external gateway over the
  underlay Subnet that vws-network creates (external-gw-switch, vws-network docs/underlay.md).
Exit 2 when the CR or the bundle does not fit.
"""
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import GENEVE_OVERHEAD, EXTERNAL_SUBNET, Invalid, host_interface  # noqa: E402

GW_CONFIG = "21-ovn-external-gw-config.yaml"


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


def gw_config(external):
    return yaml.safe_dump({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "ovn-external-gw-config", "namespace": "kube-system"},
        "data": {
            "enable-external-gw": "true",
            "type": "distributed",
            "external-gw-switch": EXTERNAL_SUBNET,
            "external-gw-nic": external["nic"],
        },
    }, sort_keys=True)


def build(cr, bundle, out):
    nodes = ((cr.get("spec") or {}).get("network") or {}).get("nodes")
    files = sorted(bundle.glob("*.yaml"))
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        text = f.read_text()
        if nodes:
            if f.name.startswith("20-kube-ovn"):
                tunnel = nodes.get("tunnel", nodes["management"])
                text = patch_kube_ovn(text, host_interface(tunnel), tunnel.get("mtu", 1500) - GENEVE_OVERHEAD)
            elif "\nkind: CephCluster\n" in text and "storage" in nodes:
                text = patch_ceph(text, nodes["storage"])
        (out / f.name).write_text(text)
    if nodes and "external" in nodes:
        (out / GW_CONFIG).write_text(gw_config(nodes["external"]))


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
