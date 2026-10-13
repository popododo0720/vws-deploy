#!/usr/bin/env python3
"""render.py CR OUT: a VWSCluster CR to a values overlay for charts/vws (vws-deploy#53).

A pure function: the same CR gives the same bytes. It reads only the CR, the CRD, the profile
and charts/vws/values.yaml from the repo. Exit 2 when the CR is invalid, 3 when a repo file is
missing. The caller (scripts/vws.d/render.sh) validates the result against values.schema.json
with helm.
"""
import copy
import ipaddress
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


def host_interface(link):
    """The host interface of a host network: the NIC, or its VLAN subinterface <nic>.<vlan>."""
    return f"{link['nic']}.{link['vlan']}" if link.get("vlan") else link["nic"]
MIN_MTU = 1280


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

    if nodes:
        tunnel = nodes.get("tunnel", nodes["management"])
        if tunnel.get("mtu", 1500) - GENEVE_OVERHEAD < MIN_MTU:
            raise Invalid(f"spec.network.nodes.tunnel.mtu: leaves under {MIN_MTU} after the {GENEVE_OVERHEAD}-byte geneve overhead")
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
    if storage.get("driver", "rook-ceph") == "existing":
        cls = storage.get("storageClass", "")
        if not cls:
            raise Invalid("spec.storage.storageClass: required when driver is existing")
        glob["storageClass"] = cls
        flags = services.setdefault("storage", {}).setdefault("flags", {})
        flags["kube-class-standard"] = cls
        flags["kube-class-ssd"] = cls
    elif storage.get("storageClass"):
        raise Invalid("spec.storage.storageClass: only for driver existing; rook-ceph makes ceph-block")
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
        for key in ("replicas", "cpu", "memory"):
            if key in override:
                target[key] = override[key]
        if "flags" in override:
            target.setdefault("flags", {}).update(override["flags"])
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
