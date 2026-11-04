"""The vws-lib cell of drift-check (vws-deploy#321).

Per Go repo, on its default branch, each vendored copy third_party/vws-lib/ (scripts/vendor-lib.sh in vws-lib):
  stale     the copy's files, except the VENDOR record that vendor-lib.sh writes, are byte for byte vws-lib at
            its latest release tag (git blob shas from both trees)
  go.mod    every go.mod in the copy has `toolchain` (or, without one, `go`) at least TOOLCHAIN_MIN and, when it requires
            golang.org/x/net, a release at least XNET_MIN (the floors govulncheck needed on 2026-10-17)
A repo without a copy is "not vendored". Every read that fails, and a truncated tree, is drift, never ok.
"""
import re

import proto_drift

COPY = "third_party/vws-lib"
RECORD = "VENDOR"
TOOLCHAIN_MIN = "go1.26.9"
XNET = "golang.org/x/net"
XNET_MIN = "v0.60.0"
GO_VERSION = re.compile(r"^go(\d+)\.(\d+)(?:\.(\d+))?$")
SHOW = 3


def _go_version(v):
    m = GO_VERSION.match(v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def blobs(t):
    """{path: blob sha} of a recursive tree reply, or None when it is unreadable, truncated or has a blob
    without a path or sha."""
    if not isinstance(t, dict) or not isinstance(t.get("tree"), list) or t.get("truncated"):
        return None
    out = {e.get("path"): e.get("sha") for e in t["tree"] if isinstance(e, dict) and e.get("type") == "blob"}
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in out.items()):
        return None
    return out


def release(owner, api):
    """(latest vws-lib release tag, {path: blob sha} at that tag); the map is None when unreadable."""
    tags = api(f"repos/{owner}/vws-lib/tags?per_page=100")
    names = [t.get("name") for t in tags if isinstance(t, dict)] if isinstance(tags, list) else []
    latest = proto_drift.latest_tag([n for n in names if isinstance(n, str)])
    if latest is None:
        return None, None
    return latest, blobs(api(f"repos/{owner}/vws-lib/git/trees/{latest}?recursive=1"))


def gomod_issues(text):
    """Issue strings for one go.mod in a copy: a missing or old toolchain, an old golang.org/x/net."""
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    out = []
    lines = [ln.split("//", 1)[0].split() for ln in text.splitlines()]
    toolchain = next((f[1] for f in lines if len(f) == 2 and f[0] == "toolchain"), None)
    if toolchain is None:  # without a toolchain line, the go line is the minimum toolchain
        toolchain = next((f"go{f[1]}" for f in lines if len(f) == 2 and f[0] == "go"), None)
    got = _go_version(toolchain)
    if got is None:
        out.append(f"no toolchain or go line (want >= {TOOLCHAIN_MIN})" if toolchain is None
                   else f"toolchain {toolchain} unreadable")
    elif got < _go_version(TOOLCHAIN_MIN):
        out.append(f"toolchain {toolchain} < {TOOLCHAIN_MIN}")
    xnet, _ = proto_drift.parse_gomod(text, XNET)
    if xnet is not None:
        v = proto_drift._semver(xnet)
        if v is None:
            out.append(f"{XNET} {xnet} is not a release (want >= {XNET_MIN})")
        elif v < proto_drift._semver(XNET_MIN):
            out.append(f"{XNET} {xnet} < {XNET_MIN}")
    return out


def _paths(names):
    more = f" (+{len(names) - SHOW} more)" if len(names) > SHOW else ""
    return ", ".join(names[:SHOW]) + more


def lib_cell(owner, repo, t, rel, api):
    """(issues, ok text) of the vws-lib cell; t is the repo's main_tree reply, rel() returns release()."""
    have = blobs(t)
    if have is None:
        return ["repo tree unreadable or truncated; vws-lib copy check incomplete"], ""
    marks = {p: ("/" + p).find(f"/{COPY}/") for p in have}
    roots = sorted({p[:i + len(COPY)] for p, i in marks.items() if i >= 0})
    if not roots:
        return [], "not vendored"
    latest, want = rel()
    if want is None:
        return [f"vws-lib release {latest or 'tag'} unreadable"], ""
    issues, ok = [], []
    for root in roots:
        got = {p[len(root) + 1:]: s for p, s in have.items() if p.startswith(root + "/")}
        got.pop(RECORD, None)
        differ = sorted(p for p in set(got) | set(want) if got.get(p) != want.get(p))
        if differ:
            issues.append(f"{root} is stale: {len(differ)} file(s) differ from vws-lib {latest}: {_paths(differ)}")
        for rel_path in sorted(p for p in got if p.rsplit("/", 1)[-1] == "go.mod"):
            path = f"{root}/{rel_path}"
            body = proto_drift.read_file(owner, repo, path, api, sha=have[path])
            if body is None:
                issues.append(f"{path} unreadable")
            else:
                issues += [f"{path}: {x}" for x in gomod_issues(body)]
        ok.append(f"{root} = {latest}")
    return issues, ", ".join(ok)
