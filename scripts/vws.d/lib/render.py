#!/usr/bin/env python3
"""render.py CR OUT: a VWSCluster CR to a values overlay for charts/vws (vws-deploy#53).

A pure function: the same CR gives the same bytes. It reads only the CR, the CRD, the profile
and charts/vws/values.yaml from the repo. Exit 2 when the CR is invalid, 3 when a repo file is
missing. The caller (scripts/vws.d/render.sh) validates the result against values.schema.json
with helm.
"""
import copy
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
