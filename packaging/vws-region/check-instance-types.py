#!/usr/bin/env python3
"""Reject region.yaml instance_types that vws-region would refuse at registration."""
import re
import sys

# Source of truth: vws-console-api internal/resource/instance.go InstanceTypes
# plus internal/backend/proxmox/instance_types.go InstanceTypes. Update this
# table when either changes. vws-region rejects a listed type whose name is
# unknown or whose non-zero cores or memory_mib differ (validateInstanceTypes).
DEFAULTS = {
    "vws.small": (1, 1024),
    "vws.medium": (2, 4096),
    "vws.large": (4, 8192),
    "vws.xlarge": (8, 16384),
    "vws.2xlarge": (16, 32768),
    "vws.4xlarge": (32, 65536),
    "vws.8xlarge": (64, 131072),
}


def listed_types(text):
    types = {}
    inside = False
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.split("#")[0].rstrip() == "instance_types:"
            current = None
            continue
        if not inside:
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.split("#")[0].strip().partition(":")
        value = value.strip()
        if indent == 2:
            current = key.strip("\"'")
            types[current] = {}
        elif current is not None and key in ("cores", "memory_mib"):
            types[current][key] = int(value)
    return types


def problems(text):
    found = []
    for name, size in listed_types(text).items():
        if name not in DEFAULTS:
            found.append(f"instance_types.{name}: unknown default type")
            continue
        cores, memory = DEFAULTS[name]
        if size.get("cores", cores) != cores:
            found.append(f"instance_types.{name}.cores: {size['cores']}, must be {cores}")
        if size.get("memory_mib", memory) != memory:
            found.append(f"instance_types.{name}.memory_mib: {size['memory_mib']}, must be {memory}")
    return found


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as handle:
        errors = problems(handle.read())
    for error in errors:
        print(f"{sys.argv[1]}: {error}", file=sys.stderr)
    sys.exit(1 if errors else 0)
