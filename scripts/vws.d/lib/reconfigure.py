#!/usr/bin/env python3
"""reconfigure.py: build the new VWSCluster and summarise what changes (vws-deploy#57).

  reconfigure.py build CURRENT_JSON OUT [--from FILE | path=value ...]
      CURRENT_JSON is the CR from the cluster. --from takes a whole CR; path=value sets one field
      under spec ("spec." is optional), the value read as YAML (so 3 is a number, true a boolean).
  reconfigure.py diff CURRENT_JSON NEW
      One "path<TAB>old<TAB>new" line per changed leaf of spec. A secret-shaped field shows
      "(changed)" and never a value; secret references (secretName) are names and are shown.
Exit 2 on bad input.
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import REFERENCE_NAMES, SECRET_NAME  # noqa: E402

MISSING = "(unset)"


def clean(cr):
    return {"apiVersion": cr.get("apiVersion"), "kind": cr.get("kind"),
            "metadata": {"name": (cr.get("metadata") or {}).get("name"),
                         "namespace": (cr.get("metadata") or {}).get("namespace")},
            "spec": cr.get("spec") or {}}


def build(current, out, args):
    cr = clean(current)
    if args[:1] == ["--from"]:
        if len(args) != 2:
            raise ValueError("--from takes one file")
        new = yaml.safe_load(Path(args[1]).read_text())
        if not isinstance(new, dict):
            raise ValueError("the file is not one YAML object")
        new = clean(new)
        new["metadata"]["namespace"] = cr["metadata"]["namespace"]
        cr = new
    else:
        for arg in args:
            path, sep, raw = arg.partition("=")
            if not sep or not path:
                raise ValueError(f"--set wants path=value, got {arg!r}")
            keys = path.split(".")
            if keys[0] == "spec":
                keys = keys[1:]
            if not keys or any(not k for k in keys):
                raise ValueError(f"bad path {path!r}")
            node = cr["spec"]
            for k in keys[:-1]:
                node = node.setdefault(k, {})
                if not isinstance(node, dict):
                    raise ValueError(f"{path}: {k} is not an object")
            node[keys[-1]] = yaml.safe_load(raw) if raw != "" else ""
    Path(out).write_text(yaml.safe_dump(cr, sort_keys=True))


def leaves(value, prefix):
    if isinstance(value, dict) and value:
        for k in sorted(value):
            yield from leaves(value[k], f"{prefix}.{k}")
    else:
        yield prefix, value


def show(path, value):
    if value is MISSING:
        return MISSING
    last = path.rsplit(".", 1)[-1]
    if SECRET_NAME.search(last) and last not in REFERENCE_NAMES:
        return "(hidden)"
    return json.dumps(value)


def diff(current, new):
    have = dict(leaves(clean(current)["spec"], "spec"))
    want = dict(leaves(new.get("spec") or {}, "spec"))
    for path in sorted(set(have) | set(want)):
        a, b = have.get(path, MISSING), want.get(path, MISSING)
        if a != b:
            print(f"{path}\t{show(path, a)}\t{show(path, b)}")


def main(argv):
    try:
        if len(argv) >= 4 and argv[1] == "build":
            build(json.loads(Path(argv[2]).read_text()), argv[3], argv[4:])
        elif len(argv) == 4 and argv[1] == "diff":
            diff(json.loads(Path(argv[2]).read_text()), yaml.safe_load(Path(argv[3]).read_text()))
        else:
            raise ValueError("usage: reconfigure.py build CURRENT OUT [--from FILE | path=value ...] | diff CURRENT NEW")
    except (ValueError, OSError, yaml.YAMLError) as e:
        print(f"reconfigure: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
