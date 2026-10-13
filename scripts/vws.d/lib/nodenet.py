#!/usr/bin/env python3
"""nodenet.py CR NODES [LINKS]: preflight of spec.network.nodes (vws-deploy#128). Read only.

CR is the VWSCluster as JSON or YAML, NODES is `kubectl get nodes -o json`, LINKS is the output of
`talosctl -n <every node> get links -o json` (a stream of JSON objects). It prints one
"LEVEL<TAB>name<TAB>message" line per check.
- Every node InternalIP must be inside nodes.management.cidr (FAIL).
- With LINKS, every node must have each interface (host networks: <nic> or <nic>.<vlan>; underlay:
  <nic>) with an MTU of at least the CR's (FAIL). Without LINKS that is WARN: unverified.
Exit 2 when an input cannot be read.
"""
import ipaddress
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import host_interface  # noqa: E402


def wanted(nodes, direct):
    """(interface, mtu, network) every node must have."""
    links = [("management", nodes["management"], False)]
    for key in ("tunnel", "external"):
        if key in nodes:
            links.append((key, nodes[key], key == "external"))
    for key in ("public", "cluster"):
        if key in (nodes.get("storage") or {}):
            links.append((f"storage.{key}", nodes["storage"][key], False))
    links += [(f"direct.{d['name']}", d, True) for d in direct]
    want = {}
    for name, link, underlay in links:
        iface = link["nic"] if underlay else host_interface(link)
        mtu = link.get("mtu", 1500)
        have = want.get(iface)
        want[iface] = (max(mtu, have[0]) if have else mtu, (have[1] + "," if have else "") + name)
    return want


def read_links(text):
    """node address -> {link name: mtu} from a talosctl JSON stream."""
    out, dec, i = {}, json.JSONDecoder(), 0
    text = text.strip()
    while i < len(text):
        obj, i = dec.raw_decode(text, i)
        while i < len(text) and text[i].isspace():
            i += 1
        out.setdefault(obj.get("node", ""), {})[obj["metadata"]["id"]] = obj["spec"].get("mtu")
    return out


def check(cr, nodes_json, links_text):
    lines = []
    emit = lambda level, name, msg: lines.append(f"{level}\t{name}\t{msg}")  # noqa: E731
    network = (cr.get("spec") or {}).get("network") or {}
    nodes = network.get("nodes")
    if not nodes:
        emit("OK", "node-network", "spec.network.nodes is not set: one NIC, nothing to check")
        return lines
    mgmt = ipaddress.IPv4Network(nodes["management"]["cidr"])
    addrs = {}
    for item in nodes_json["items"]:
        name = item["metadata"]["name"]
        ips = [a["address"] for a in (item.get("status") or {}).get("addresses") or [] if a.get("type") == "InternalIP"]
        addrs[name] = ips
        inside = [ip for ip in ips if ipaddress.ip_address(ip) in mgmt]
        if inside:
            emit("OK", f"node-ip/{name}", f"{inside[0]} is inside management {mgmt}")
        else:
            emit("FAIL", f"node-ip/{name}", f"InternalIP {','.join(ips) or 'none'} is outside management {mgmt}")
    want = wanted(nodes, network.get("direct") or [])
    if links_text is None:
        emit("WARN", "node-links", "unverified: NICs and MTUs need --links FILE (talosctl -n <nodes> get links -o json)")
        return lines
    links = read_links(links_text)
    for name, ips in sorted(addrs.items()):
        have = next((links[k] for k in [name, *ips] if k in links), None)
        if have is None:
            emit("FAIL", f"node-links/{name}", "no links of this node in --links (talosctl -n must list every node)")
            continue
        for iface, (mtu, nets) in sorted(want.items()):
            got = have.get(iface)
            if got is None:
                emit("FAIL", f"node-links/{name}", f"interface {iface} ({nets}) does not exist")
            elif got < mtu:
                emit("FAIL", f"node-links/{name}", f"interface {iface} ({nets}) has MTU {got}, want at least {mtu}")
            else:
                emit("OK", f"node-links/{name}", f"interface {iface} ({nets}) MTU {got}")
    return lines


def main(argv):
    if len(argv) not in (3, 4):
        print("usage: nodenet.py CR NODES [LINKS]", file=sys.stderr)
        return 2
    try:
        cr = yaml.safe_load(Path(argv[1]).read_text())
        nodes_json = json.loads(Path(argv[2]).read_text())
        links_text = Path(argv[3]).read_text() if len(argv) == 4 else None
        lines = check(cr or {}, nodes_json, links_text)
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f"nodenet: cannot read the input: {err}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
