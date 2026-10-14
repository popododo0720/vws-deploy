#!/usr/bin/env python3
"""Feature map: update last-pass times from vws verify scenario results and render the markdown.

Usage: feature_map.py update MAP RESULT.json...   (then renders MAP's .md sibling)
       feature_map.py render MAP
       feature_map.py check MAP                    (exit 1 when the .md is stale)

MAP is docs/feature-map.json, the only hand-edited source. lastPass and evidence are written only
here, from a scenario result with result "pass" and cluster "vws-test" (vws-deploy#252). A result
for an unknown scenario, another cluster, or an older time changes nothing.
Provenance: the result must also carry "clusterUID", the kube-system namespace UID of the cluster it
ran on, equal to MAP's "vwsTest": {"kubeSystemUID": ...}. A missing, unconfigured or different UID is
refused (FAIL). This stops results from another cluster or a typo'd file; it is not a signature, so a
file written by hand with the committed UID copied in is still accepted. Output lines are
"LEVEL<TAB>NAME<TAB>MESSAGE" for scripts/vws.d/verify.sh to emit.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
CLUSTER = "vws-test"


def parse_time(s):
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time without zone: {s}")
    return t


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    names = set()
    for feat in data["features"]:
        for key in ("feature", "repo"):
            if not feat.get(key):
                raise ValueError(f"feature entry without {key}: {feat}")
        if feat["feature"] in names:
            raise ValueError(f"duplicate feature {feat['feature']}")
        names.add(feat["feature"])
    return data


def md_path(map_path):
    return os.path.splitext(map_path)[0] + ".md"


def render(data):
    rows = [
        "# Feature map",
        "",
        "Generated from docs/feature-map.json by `scripts/vws verify --update-feature-map`. Do not edit by hand.",
        "Last pass is a vws-test scenario run (KST). `unverified` means no scenario exists yet; `never passed` means a scenario exists but no passing vws-test result was recorded.",
        "",
        "| Feature | Repo | Issue | Verify command | Last pass (KST) | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    for f in data["features"]:
        sc = f.get("scenario")
        cmd = f"`scripts/vws verify scenario {sc}`" if sc else "-"
        if not sc:
            last = "unverified"
        elif f.get("lastPass"):
            last = parse_time(f["lastPass"]).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        else:
            last = "never passed"
        rows.append("| {} | {} | {} | {} | {} | {} |".format(
            f["feature"], f["repo"], f.get("issue") or "-", cmd, last, f.get("evidence") or "-"))
    return "\n".join(rows) + "\n"


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def update(map_path, results):
    data = load(map_path)
    by_sc = {f["scenario"]: f for f in data["features"] if f.get("scenario")}
    want_uid = (data.get("vwsTest") or {}).get("kubeSystemUID")
    out, failed = [], False
    for rpath in results:
        try:
            with open(rpath, encoding="utf-8") as fh:
                r = json.load(fh)
            sc, when = r["scenario"], parse_time(r["finishedAt"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            out.append(f"FAIL\tfeature-map\t{rpath}: unreadable scenario result ({e})")
            failed = True
            continue
        if sc not in by_sc:
            out.append(f"WARN\tfeature-map\t{sc}: no feature uses this scenario")
        elif r.get("cluster") != CLUSTER:
            out.append(f"WARN\tfeature-map\t{sc}: result is from {r.get('cluster')!r}, not {CLUSTER}; ignored")
        elif not want_uid:
            out.append(f"FAIL\tfeature-map\t{sc}: no vws-test fingerprint (vwsTest.kubeSystemUID) in {map_path}; refused")
            failed = True
        elif r.get("clusterUID") != want_uid:
            out.append(f"FAIL\tfeature-map\t{sc}: clusterUID {r.get('clusterUID')!r} does not match the vws-test fingerprint; refused")
            failed = True
        elif r.get("result") != "pass":
            out.append(f"WARN\tfeature-map\t{sc}: result {r.get('result')!r}; last pass kept")
        else:
            f = by_sc[sc]
            if f.get("lastPass") and parse_time(f["lastPass"]) >= when:
                out.append(f"OK\tfeature-map\t{sc}: already has a pass at or after {r['finishedAt']}")
            else:
                f["lastPass"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f["evidence"] = r.get("evidence") or None
                out.append(f"OK\tfeature-map\t{sc}: last pass {f['lastPass']}")
    write(map_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    write(md_path(map_path), render(data))
    for f in data["features"]:
        if not f.get("scenario"):
            out.append(f"WARN\tunverified\t{f['feature']}")
    return out, failed


def main(argv):
    if len(argv) < 2 or argv[0] not in ("update", "render", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if argv[0] == "update":
            out, failed = update(argv[1], argv[2:])
            print("\n".join(out))
            return 1 if failed else 0
        text = render(load(argv[1]))
        if argv[0] == "render":
            write(md_path(argv[1]), text)
            return 0
        with open(md_path(argv[1]), encoding="utf-8") as f:
            return 0 if f.read() == text else 1
    except (OSError, ValueError, KeyError) as e:
        print(f"feature-map: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
