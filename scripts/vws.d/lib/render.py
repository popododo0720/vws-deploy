#!/usr/bin/env python3
"""render.py CR OUT: a VWSCluster CR to a values overlay for charts/vws (vws-deploy#53).

A pure function: the same CR gives the same bytes. It reads only the CR, the CRD, the profile
and charts/vws/values.yaml from the repo. Exit 2 when the CR is invalid, 3 when a repo file is
missing. The caller (scripts/vws.d/render.sh) validates the result against values.schema.json
with helm.
"""
import copy
import ipaddress
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CRD = ROOT / "charts/vws/crds/vwsclusters.yaml"
VALUES = ROOT / "charts/vws/values.yaml"
PROFILES = ROOT / "profiles"
VERSION = "v1alpha1"
NAME = "vws"
HEADER = "# Rendered by scripts/vws render from VWSCluster vws. Do not edit: change the CR and render again.\n"

# A field or flag name that looks like it holds a secret value. spec.secrets.<x>.secretName is a
# name reference and is allowed; nothing else may look like this (AGENTS.md R18).
SECRET_NAME = re.compile(r"(?i)(passw|token|secret|credential|private|apikey|api-key|api_key|dsn|(^|[-_])key$)")
# spec.storage.driver nfs (vws-storage#72): the StorageClass name the storage service uses. The class
# itself (provisioner nfs.csi.k8s.io, server and share from the CR) belongs to the platform bundle.
NFS_CLASS = "vws-nfs"
# vws-storage#73 README: Filesystem mode creates, reads and deletes CDI DataVolumes in the VM namespace.
DATAVOLUME_RULE = {"namespace": "vms", "apiGroups": ["cdi.kubevirt.io"], "resources": ["datavolumes"], "verbs": ["get", "list", "create", "delete"]}
REFERENCE_NAMES = {"secrets", "secretName", "existingSecret"}


class Invalid(Exception):
    pass


def crd():
    doc = yaml.safe_load(CRD.read_text())
    version = next(v for v in doc["spec"]["versions"] if v["name"] == VERSION)
    return doc["spec"]["group"], version["schema"]["openAPIV3Schema"]


def check(value, schema, path):
    """The subset of OpenAPI v3 the CRD uses."""
    if schema.get("x-kubernetes-preserve-unknown-fields"):
        return
    kind = schema.get("type")
    types = {"object": dict, "string": str, "array": list, "boolean": bool}
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise Invalid(f"{path}: want an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise Invalid(f"{path}: {value} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise Invalid(f"{path}: {value} is above {schema['maximum']}")
        return
    if kind in types and not isinstance(value, types[kind]):
        raise Invalid(f"{path}: want {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise Invalid(f"{path}: {value!r} is not one of {schema['enum']}")
    if "pattern" in schema and not re.search(schema["pattern"], value):
        raise Invalid(f"{path}: {value!r} does not match {schema['pattern']}")
    if kind == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise Invalid(f"{path}.{key}: required")
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                check(item, props[key], f"{path}.{key}")
            elif isinstance(extra, dict):
                check(item, extra, f"{path}.{key}")
            elif extra is False or props:
                raise Invalid(f"{path}.{key}: unknown field")
    if kind == "array":
        for i, item in enumerate(value):
            check(item, schema.get("items", {}), f"{path}[{i}]")


def secret_shaped(value, path):
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in REFERENCE_NAMES and SECRET_NAME.search(str(key)):
                raise Invalid(f"{path}.{key}: looks like a secret; the CR holds Secret names only (spec.secrets)")
            secret_shaped(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            secret_shaped(item, f"{path}[{i}]")


GENEVE_OVERHEAD = 100
# spec.network.ipv4Only without tunnelOverhead: an IPv4 outer header leaves geneve at about 58 bytes
# (outer IPv4 20, UDP 8, geneve 8 plus an 8-byte option, inner ethernet 14); 60 keeps a margin.
IPV4_ONLY_OVERHEAD = 60
# AWS parity (vws-deploy#236, vws-network#93): VPC VMs get 9001 unless the underlay is smaller.
VM_MTU = 9001
RECOMMENDED_UNDERLAY_MTU = 9216
# The Kube-OVN Subnet of the external network. vws-network creates it (-kube-external-subnet).
EXTERNAL_SUBNET = "vws-external"
UNDERLAY_FLAGS = ("pool", "kube-external-subnet", "kube-provider-network", "kube-underlay-interface",
                  "kube-underlay-vlan", "kube-underlay-cidr", "kube-underlay-gateway", "kube-underlay-exclude")
DIRECT_FLAGS = ("kube-direct-networks", "kube-direct-nad-namespace")
# vws-network makes the NAD vws-direct-<name> of each direct network in the VM namespace at start and
# never deletes it (vws-network docs/rbac.md "direct NAD Role").
DIRECT_NAD_RULE = {"namespace": "vms", "apiGroups": ["k8s.cni.cncf.io"],
                   "resources": ["network-attachment-definitions"], "verbs": ["get", "create"]}


def host_interface(link):
    """The host interface of a host network: the NIC, or its VLAN subinterface <nic>.<vlan>."""
    return f"{link['nic']}.{link['vlan']}" if link.get("vlan") else link["nic"]
MIN_MTU = 1280


def tunnel_overhead(network):
    """Bytes the tunnel adds: spec.network.tunnelOverhead, else 60 with ipv4Only, else 100."""
    if network.get("tunnelOverhead") is not None:
        return network["tunnelOverhead"]
    return IPV4_ONLY_OVERHEAD if network.get("ipv4Only") else GENEVE_OVERHEAD


def underlay_mtu(network):
    """The MTU the tunnel rides on, as far as the CR knows: the smaller of spec.network.underlayMTU and
    the tunnel (or management) link mtu of spec.network.nodes. None when the CR says neither."""
    known = []
    if network.get("underlayMTU") is not None:
        known.append(network["underlayMTU"])
    nodes = network.get("nodes")
    if nodes:
        known.append(nodes.get("tunnel", nodes["management"]).get("mtu", 1500))
    return min(known) if known else None


def vm_mtu(network):
    """The VM MTU vws-network hands out by DHCP (-vm-mtu). An unset vmMTU is 9001 lowered to what the
    underlay carries; a set vmMTU above that is an error (vws preflight writes the lowered value)."""
    limit = underlay_mtu(network)
    room = None if limit is None else limit - tunnel_overhead(network)
    if network.get("vmMTU") is None:
        return VM_MTU if room is None else min(VM_MTU, room)
    if room is not None and network["vmMTU"] > room:
        raise Invalid(f"spec.network.vmMTU: {network['vmMTU']} does not fit the underlay MTU {limit} minus the "
                      f"{tunnel_overhead(network)}-byte tunnel overhead; at most {room} (vws preflight lowers it)")
    return network["vmMTU"]


def prefix(text, path):
    try:
        net = ipaddress.IPv4Network(text)
    except ValueError as err:
        raise Invalid(f"{path}: {err}") from None
    if str(net) != text:
        raise Invalid(f"{path}: {text!r} is not canonical; write {net}")
    return net


def address(text, path):
    try:
        return ipaddress.IPv4Address(text)
    except ValueError as err:
        raise Invalid(f"{path}: {err}") from None


def excluded(items, net, path):
    out = []
    for i, item in enumerate(items):
        lo, _, hi = item.partition("..")
        a, b = address(lo, f"{path}[{i}]"), address(hi or lo, f"{path}[{i}]")
        if a > b or a not in net or b not in net:
            raise Invalid(f"{path}[{i}]: {item} is not an ascending range inside {net}")
        out.append((a, b))
    return out


def check_network(network):
    """Cross-field rules of spec.network.nodes and spec.network.direct (vws-deploy#125)."""
    nodes = network.get("nodes")
    direct = network.get("direct") or []
    links = []  # (path, link, underlay)
    if nodes:
        links.append(("spec.network.nodes.management", nodes["management"], False))
        for key in ("tunnel", "external"):
            if key in nodes:
                links.append((f"spec.network.nodes.{key}", nodes[key], key == "external"))
        for key in ("public", "cluster"):
            if key in (nodes.get("storage") or {}):
                links.append((f"spec.network.nodes.storage.{key}", nodes["storage"][key], False))
    names = set()
    for i, d in enumerate(direct):
        if d["name"] in names:
            raise Invalid(f"spec.network.direct[{i}].name: {d['name']} is used twice")
        names.add(d["name"])
        links.append((f"spec.network.direct[{i}]", d, True))

    seen = {}  # (nic, vlan) -> (path, net, mtu)
    for path, link, underlay in links:
        net = prefix(link["cidr"], f"{path}.cidr")
        key = (link["nic"], link.get("vlan", 0))
        mtu = link.get("mtu", 1500)
        if key in seen:
            other, onet, omtu, ounder = seen[key]
            if underlay or ounder:
                raise Invalid(f"{path}: {key[0]} vlan {key[1]} is already {other}; an underlay network needs its own VLAN")
            if (onet, omtu) != (net, mtu):
                raise Invalid(f"{path}: same nic and vlan as {other} but a different cidr or mtu")
            continue
        for other, onet, _, _ in seen.values():
            if net.overlaps(onet):
                raise Invalid(f"{path}.cidr: {net} overlaps {other}")
        seen[key] = (path, net, mtu, underlay)
        if underlay:
            gw = address(link["gateway"], f"{path}.gateway")
            if gw not in net:
                raise Invalid(f"{path}.gateway: {gw} is outside {net}")
            excluded(link.get("excludeIPs") or [], net, f"{path}.excludeIPs")

    under_nics = {link["nic"] for _, link, u in links if u}
    for nic in sorted(under_nics):
        host = [(p, l.get("vlan", 0)) for p, l, u in links if not u and l["nic"] == nic]
        tagged = [p for p, v in host if v]
        if tagged:
            raise Invalid(f"{tagged[0]}: {nic} carries an underlay network, so host networks on it must be untagged")

    if network.get("underlayMTU") is not None and network["underlayMTU"] - tunnel_overhead(network) < MIN_MTU:
        raise Invalid(f"spec.network.underlayMTU: leaves under {MIN_MTU} after the {tunnel_overhead(network)}-byte tunnel overhead")
    if nodes:
        tunnel = nodes.get("tunnel", nodes["management"])
        overhead = tunnel_overhead(network)
        if tunnel.get("mtu", 1500) - overhead < MIN_MTU:
            raise Invalid(f"spec.network.nodes.tunnel.mtu: leaves under {MIN_MTU} after the {overhead}-byte geneve overhead")
        ext = nodes.get("external")
        if ext:
            net = ipaddress.IPv4Network(ext["cidr"])
            pool = prefix(ext["eipPool"], "spec.network.nodes.external.eipPool")
            if not pool.subnet_of(net):
                raise Invalid(f"spec.network.nodes.external.eipPool: {pool} is outside {net}")
            taken = [(ipaddress.IPv4Address(ext["gateway"]),) * 2] + excluded(ext.get("excludeIPs") or [], net, "")
            for a, b in taken:
                if a <= pool[-1] and b >= pool[0]:
                    raise Invalid(f"spec.network.nodes.external.eipPool: {pool} overlaps the gateway or excludeIPs")


def underlay_nics(network):
    """The sorted NICs of the external and direct networks; a NIC's place N names its ProviderNetwork vws-pn<N>."""
    ext = (network.get("nodes") or {}).get("external")
    nics = {d["nic"] for d in network.get("direct") or []}
    if ext:
        nics.add(ext["nic"])
    return sorted(nics)


def external_flags(network, services):
    """spec.network.nodes.external -> the underlay flags of vws-network (vws-network docs/underlay.md, #130).

    vws-network creates the ProviderNetwork, the Vlan and the Subnet from them. The ProviderNetwork is
    vws-pn<N>, N the place of its NIC among the sorted underlay NICs, so direct networks on the same
    NIC share it (#131).
    """
    ext = (network.get("nodes") or {}).get("external")
    if not ext:
        return
    flags = services.setdefault("network", {}).setdefault("flags", {})
    taken = sorted(set(UNDERLAY_FLAGS) & set(flags))
    if taken:
        raise Invalid(f"spec.services.network.flags.{taken[0]}: comes from spec.network.nodes.external; remove it")
    nics = underlay_nics(network)
    flags.update({
        "pool": ext["eipPool"],
        "kube-external-subnet": EXTERNAL_SUBNET,
        "kube-provider-network": f"vws-pn{nics.index(ext['nic'])}",
        "kube-underlay-interface": ext["nic"],
        "kube-underlay-vlan": ext.get("vlan", 0),
        "kube-underlay-cidr": ext["cidr"],
        "kube-underlay-gateway": ext["gateway"],
    })
    if ext.get("excludeIPs"):
        flags["kube-underlay-exclude"] = ",".join(ext["excludeIPs"])
    # gatewayNodes -> -kube-gateway-nodes: vws-network keeps these nodes at
    # ovn.kubernetes.io/external-gw=true after kube-ovn-controller resets them (#197).
    if "gatewayNodes" in ext:
        gw = ext["gatewayNodes"]
        if not gw or len(set(gw)) != len(gw):
            raise Invalid("spec.network.nodes.external.gatewayNodes: needs one or more distinct node names")
        if "kube-gateway-nodes" in flags:
            raise Invalid("spec.services.network.flags.kube-gateway-nodes: comes from "
                          "spec.network.nodes.external.gatewayNodes; remove it")
        flags["kube-gateway-nodes"] = ",".join(gw)


def mtu_flag(network, services):
    """spec.network.vmMTU -> vws-network -vm-mtu (vws-network#93), which hands it to VMs by DHCP.
    vws-network main (#94) takes 68..9001, default 9001. The installer never sets a NIC or
    host MTU; it only passes this number on."""
    svc = services.setdefault("network", {})
    if "vm-mtu" in (svc.get("flags") or {}):
        raise Invalid("spec.services.network.flags.vm-mtu: comes from spec.network.vmMTU; remove it")
    svc.setdefault("flags", {})["vm-mtu"] = vm_mtu(network)


def direct_flags(network, services, base):
    """spec.network.direct -> -kube-direct-networks and -kube-direct-nad-namespace of vws-network (#131).

    vws-network makes the Kube-OVN Subnet, the Vlan and the NAD vws-direct-<name> in the VM namespace
    (vws-network cmd/vws-network/main.go:40-41). Nothing is rendered without direct networks.
    """
    direct = network.get("direct") or []
    if not direct:
        return
    svc = services.setdefault("network", {})
    flags = svc.setdefault("flags", {})
    taken = sorted(set(DIRECT_FLAGS) & set(flags))
    if taken:
        raise Invalid(f"spec.services.network.flags.{taken[0]}: comes from spec.network.direct; remove it")
    if not flags.get("kube-external-subnet"):
        raise Invalid("spec.network.direct: needs spec.network.nodes.external or "
                      "spec.services.network.flags.kube-external-subnet; vws-network refuses Kube-OVN without it")
    nics = underlay_nics(network)
    line = [{
        "name": d["name"],
        "providerNetwork": f"vws-pn{nics.index(d['nic'])}",
        "interface": d["nic"],
        "vlan": d.get("vlan", 0),
        "cidr": d["cidr"],
        "gateway": d["gateway"],
        **({"exclude": list(d["excludeIPs"])} if d.get("excludeIPs") else {}),
    } for d in direct]
    flags["kube-direct-networks"] = json.dumps(line, separators=(",", ":"))
    flags["kube-direct-nad-namespace"] = {"namespace": DIRECT_NAD_RULE["namespace"]}
    svc["rbac"] = copy.deepcopy(svc.get("rbac") or base["services"]["network"]["rbac"]) + [copy.deepcopy(DIRECT_NAD_RULE)]


def render(cr):
    group, schema = crd()
    if not isinstance(cr, dict):
        raise Invalid("the file is not one YAML object")
    if cr.get("apiVersion") != f"{group}/{VERSION}" or cr.get("kind") != "VWSCluster":
        raise Invalid(f"want apiVersion {group}/{VERSION} and kind VWSCluster")
    if (cr.get("metadata") or {}).get("name") != NAME:
        raise Invalid(f"metadata.name must be {NAME}: VWSCluster is a singleton")
    spec = cr.get("spec")
    check(spec, schema["properties"]["spec"], "spec")
    secret_shaped(spec, "spec")
    check_network(spec.get("network") or {})

    out = copy.deepcopy(yaml.safe_load((PROFILES / f"{spec['profile']}.yaml").read_text()))
    base = yaml.safe_load(VALUES.read_text())
    glob = out.setdefault("global", {})
    services = out.setdefault("services", {})

    if spec.get("version"):
        glob["tag"] = spec["version"]
    storage = spec.get("storage") or {}
    driver = storage.get("driver", "rook-ceph")
    if storage.get("storageClass") and driver != "existing":
        raise Invalid("spec.storage.storageClass: only for driver existing; rook-ceph makes ceph-block, nfs makes vws-nfs")
    if storage.get("volumeMode") and driver != "existing":
        raise Invalid("spec.storage.volumeMode: only for driver existing; rook-ceph is Block, nfs is Filesystem")
    if storage.get("nfs") and driver != "nfs":
        raise Invalid("spec.storage.nfs: only for driver nfs")
    cls, mode = None, "Block"
    if driver == "existing":
        cls = storage.get("storageClass", "")
        if not cls:
            raise Invalid("spec.storage.storageClass: required when driver is existing")
        glob["storageClass"] = cls
        mode = storage.get("volumeMode", "Block")
    elif driver == "nfs":
        if not storage.get("nfs"):
            raise Invalid("spec.storage.nfs: server and share are required when driver is nfs")
        cls, mode = NFS_CLASS, "Filesystem"
    if cls:
        flags = services.setdefault("storage", {}).setdefault("flags", {})
        flags["kube-class-standard"] = cls
        flags["kube-class-ssd"] = cls
    if mode == "Filesystem":
        # vws-storage#73: a Filesystem volume is a CDI blank DataVolume (CDI writes disk.img).
        storage_svc = services.setdefault("storage", {})
        storage_svc.setdefault("flags", {})["kube-volume-mode"] = "Filesystem"
        storage_svc["rbac"] = copy.deepcopy(base["services"]["storage"]["rbac"]) + [DATAVOLUME_RULE]
    if driver == "nfs":
        # csi-driver-nfs takes no snapshots of a plain export: an empty class disables CreateSnapshot.
        services["storage"]["flags"]["kube-snapshot-class"] = ""
    # spec.storage.s3.rgw -> services.storage.s3.rgw, so vws apply keeps RGW on (#108).
    rgw = (storage.get("s3") or {}).get("rgw")
    if rgw is not None:
        services.setdefault("storage", {})["s3"] = {"rgw": dict(rgw)}
    if (spec.get("observability") or {}).get("otlpEndpoint"):
        raise Invalid("spec.observability.otlpEndpoint: the chart has no observability values yet; leave it empty")
    if spec.get("secrets"):
        raise Invalid("spec.secrets: no chart value takes a Secret name from the CR yet; leave it empty")

    for name, override in sorted((spec.get("services") or {}).items()):
        if name not in base["services"]:
            raise Invalid(f"spec.services.{name}: not a service in charts/vws/values.yaml")
        target = services.setdefault(name, {})
        for key in ("enabled", "replicas", "cpu", "memory"):
            if key in override:
                target[key] = override[key]
        if "flags" in override:
            target.setdefault("flags", {}).update(override["flags"])
    external_flags(spec.get("network") or {}, services)
    mtu_flag(spec.get("network") or {}, services)
    direct_flags(spec.get("network") or {}, services, base)
    pool = (((services.get("network") or {}).get("flags") or {}).get("pool")
             or base["services"]["network"].get("flags", {}).get("pool"))
    if not pool:
        raise Invalid("spec.services.network.flags.pool: required; the elastic IP pool CIDR has no default (vws-deploy#148)")
    if re.match(r"^(192\.0\.2|198\.51\.100|203\.0\.113)\.", str(pool)):
        raise Invalid(f"spec.services.network.flags.pool: {pool} is an RFC 5737 documentation range; use the site's elastic IP range")
    return HEADER + yaml.safe_dump(out, sort_keys=True, default_flow_style=False)


def main(argv):
    if len(argv) != 3:
        print("usage: render.py CR OUT", file=sys.stderr)
        return 2
    try:
        docs = list(yaml.safe_load_all(Path(argv[1]).read_text()))
    except FileNotFoundError:
        print(f"render: {argv[1]} does not exist", file=sys.stderr)
        return 2
    except yaml.YAMLError as err:
        print(f"render: {argv[1]} is not YAML: {err}", file=sys.stderr)
        return 2
    if len(docs) != 1:
        print("render: want exactly one VWSCluster document", file=sys.stderr)
        return 2
    try:
        text = render(docs[0])
    except Invalid as err:
        print(f"render: invalid VWSCluster: {err}", file=sys.stderr)
        return 2
    except (FileNotFoundError, KeyError) as err:
        print(f"render: repo file missing or broken: {err}", file=sys.stderr)
        return 3
    if argv[2] == "-":
        sys.stdout.write(text)
    else:
        Path(argv[2]).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
