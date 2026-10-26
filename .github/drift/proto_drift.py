"""The vws-proto cell of drift-check (vws-deploy#404 AC3, AC4); rule: docs/design/proto-module.md.

Per Go repo, on its default branch:
  version  every go.mod (outside third_party/, vendor/ and testdata/) that requires
           github.com/popododo0720/vws-proto requires a vws-proto release tag, not a pseudo-version, with no
           replace, and is not more than MAX_MINOR_LAG minor versions (or any major version) behind the latest tag
  copies   no vws-proto generated code in the tree: nothing under third_party/vws-proto/, no *.pb.go under a
           gen/go/vws/ path, and no other *.pb.go whose header names a vws/ proto source (the
           `rg -l '^// source: vws/' -g '*.pb.go'` check, read through the contents API)
Every read that fails, and a truncated tree listing, is drift, never ok.
"""
import base64
import re

MODULE = "github.com/popododo0720/vws-proto"
MAX_MINOR_LAG = 1
SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
PSEUDO = re.compile(r"-(?:0\.)?(?:[0-9A-Za-z.-]*\.)?\d{14}-[0-9a-f]{12}(?:\+incompatible)?$")
SOURCE = re.compile(rb"^// source: vws/", re.M)
SKIP_DIRS = {"third_party", "vendor", "testdata"}
SHOW = 3


def _strip(line):
    return line.split("//", 1)[0].strip()


def parse_gomod(text):
    """(required vws-proto version or None, whether a replace names vws-proto) for one go.mod."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    version, replaced, block = None, False, None
    for raw in text.splitlines():
        line = _strip(raw)
        if not line:
            continue
        if block:
            if line == ")":
                block = None
                continue
            directive, rest = block, line
        else:
            m = re.match(r"^(require|replace|exclude|retract|tool|ignore|godebug)\s*(\(?)\s*(.*)$", line)
            if not m:
                continue
            if m.group(2) == "(":
                block = m.group(1)
                continue
            directive, rest = m.group(1), m.group(3)
        fields = rest.split()
        if not fields or fields[0] != MODULE:
            continue
        if directive == "require" and len(fields) >= 2:
            version = fields[1]
        elif directive == "replace" and "=>" in fields:
            replaced = True
    return version, replaced


def _semver(v):
    m = SEMVER.match(v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def latest_tag(names):
    """The highest release tag vX.Y.Z among names; pre-releases and other refs are ignored."""
    tags = [n for n in names if _semver(n)]
    return max(tags, key=_semver) if tags else None


def version_issues(version, replaced, latest, tags):
    out = []
    if version is not None:
        got, want = _semver(version), _semver(latest)
        if want is None:
            out.append("latest vws-proto tag unreadable")
        elif got is None:
            out.append(f"{version} is a pseudo-version, not a release tag" if PSEUDO.search(version)
                       else f"{version} is not a release tag")
        elif version not in tags:
            out.append(f"{version} is not a vws-proto tag")
        elif got[0] < want[0]:
            out.append(f"{version} is a major version behind {latest}")
        elif got[0] == want[0] and want[1] - got[1] > MAX_MINOR_LAG:
            out.append(f"{version} is {want[1] - got[1]} minor versions behind {latest} (max {MAX_MINOR_LAG})")
    if replaced:
        out.append("replace for vws-proto in go.mod")
    return out


def _is_copy_path(path):
    p = "/" + path
    return "/third_party/vws-proto/" in p or (path.endswith(".pb.go") and "/gen/go/vws/" in p)


def copy_issues(paths, read):
    """Issue strings for vws-proto generated code in one tree; read(path) returns bytes or None."""
    copies, out = [], []
    for path in paths:
        if _is_copy_path(path):
            copies.append(path)
        elif path.endswith(".pb.go"):
            body = read(path)
            if body is None:
                out.append(f"{path} unreadable")
            elif SOURCE.search(body):
                copies.append(path)
    if copies:
        noun = "copy" if len(copies) == 1 else "copies"
        more = f" (+{len(copies) - SHOW} more)" if len(copies) > SHOW else ""
        out.insert(0, f"{len(copies)} vws-proto pb.go {noun}: " + ", ".join(copies[:SHOW]) + more)
    return out


def proto_tags(owner, api):
    """Tag names of vws-proto (first 100, newest first), or None when unreadable."""
    d = api(f"repos/{owner}/vws-proto/tags?per_page=100")
    if not isinstance(d, list):
        return None
    return [t.get("name") for t in d if isinstance(t, dict) and isinstance(t.get("name"), str)]


def _read(owner, repo, api):
    def read(path):
        d = api(f"repos/{owner}/{repo}/contents/{path}")
        if not isinstance(d, dict) or "content" not in d:
            return None
        return base64.b64decode(d["content"])
    return read


def proto_cell(owner, repo, latest, tags, api):
    """(issues, ok text) of the vws-proto cell for one Go repo's default branch."""
    t = api(f"repos/{owner}/{repo}/git/trees/main?recursive=1")
    if not isinstance(t, dict) or not isinstance(t.get("tree"), list):
        return ["repo tree unreadable"], ""
    issues = ["repo tree truncated; pb.go copy check incomplete"] if t.get("truncated") else []
    paths = [e.get("path") for e in t["tree"] if isinstance(e, dict) and e.get("type") == "blob"
             and isinstance(e.get("path"), str)]
    read = _read(owner, repo, api)
    used = []
    for path in paths:
        if path.rsplit("/", 1)[-1] != "go.mod" or SKIP_DIRS & set(path.split("/")[:-1]):
            continue
        body = read(path)
        if body is None:
            issues.append(f"{path} unreadable")
            continue
        version, replaced = parse_gomod(body)
        issues += [f"{path}: {x}" for x in version_issues(version, replaced, latest, tags or [])]
        if version is not None:
            used.append(version if path == "go.mod" else f"{path} {version}")
    issues += copy_issues(paths, read)
    return issues, (", ".join(used) if used else "not used") + f" (latest {latest})"
