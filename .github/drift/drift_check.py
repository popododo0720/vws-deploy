#!/usr/bin/env python3
"""Compare the guardrails of every vws-* repo against one expected baseline (vws-deploy#300).

Checks per repo, on its default branch:
  protection  main is protected, admins included, no force push, no deletion
  ruleset     an active vws-main branch ruleset, with no bypass actors; the bypass list is readable
              only with an admin token, so without one the cell says "unverified", never ok
  checks      required status checks (classic plus the ruleset) include the common set plus the repo's own
  workflows   repo-guard.yml, direct-push-watch.yml and verify-queue.yml exist (verify-queue: #460)
  caller      repo-guard.yml is the short caller of the vws-deploy repo-guard-<job>.yml files (caller_pin.py): a 40-hex
              pin, the same pin in every repo, and that pin is on vws-deploy main (vws-deploy#402). A repo
              that caller-pending.tsv lists is exempt only while its repo-guard.yml is byte for byte the full
              copy whose sha256 that row pins; any other content is checked as a caller. No other workflow
              file may call a repo-guard-*.yml file or report the bare pr-hygiene or verify-gate check name.
              All of this is also checked at the head of every open PR and at each --head REPO=SHA
              (the workflow passes the PR head from its event), against the pin of the default branches
              (vendored and bootstrap are checked at those heads too, against the default-branch script).
              No workflow file, a pending full copy included, runs on both pull_request and
              pull_request_target (vws-deploy#505): the second event reports a skipped check under the same name
  guard       scripts/guard-agents.sh matches vws-deploy's scripts/guard-agents.sh.sha256, or the one
              full sha256 that guard-variants.tsv pins for that repo ("accepted variant")
  vendored    each file that vendored.tsv pins (verify-queue.yml, check-english.sh) matches its full sha256, or
              the one variant pinned for that repo (#439 class 4). Every repo but vws-deploy must have
              check-english.sh (#423); a missing verify-queue.yml is the workflows cell's drift
  bootstrap   every workflow run: step that calls scripts/guard-agents.sh SUB, and every vws-deploy
              repo-guard-*.yml the caller pins, names a SUB that the default-branch script already
              dispatches: the script lands first, then the workflow that calls it (#439 class 4)
  docs        AGENTS.md and QUALITY.md (or docs/QUALITY.md) exist, and AGENTS.md has no stale fact (#395)
  incident    .github/ISSUE_TEMPLATE/incident.yml exists, applies the incident label, has every field
              of vws-deploy's form, no incident.md is left, and the repo has the incident label
  lint        Go repos: the golangci config enables every linter in .github/golangci-common.yml,
              sets lll to 120 and enables gofumpt and goimports
  vws-proto   Go repos: each go.mod's vws-proto version next to the latest vws-proto tag; drift on a replace,
              a pseudo-version, more than one minor version behind, or a copied vws-proto *.pb.go
              (proto_drift.py, vws-deploy#404)
An archived repo is reported and skipped; it is not drift.

--bypass-audit (vws-deploy#458) checks only the ruleset bypass actors, for the admin audit run on the
VM (.github/drift/audit-bypass-actors.sh). It lists every actor and exits 3 on any actor, and also on a
ruleset that is unreadable or whose bypass list is not visible, so a non-admin token can never pass it.

Needs an authenticated gh. With admin access (an audit) the full protection settings are read;
with a read-only token (CI) the branch summary is used and force-push and deletion show as "?".
Exit status: 0 when nothing drifts, 3 when something does, 2 on a usage error or a bad baseline.
Any other non-zero status (1 is an uncaught Python error) is a crash, never drift.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import caller_pin  # noqa: E402
import proto_drift  # noqa: E402

COMMON_CHECKS = ["pr-hygiene", "vws-verify", "verify-gate"]
# verify-queue.yml (#460): without it a repo's PRs never get needs-verify and Rule A never wakes vws-verify.
WORKFLOWS = [".github/workflows/repo-guard.yml", ".github/workflows/direct-push-watch.yml",
             ".github/workflows/verify-queue.yml"]
STALE_PHRASES = (
    "private, free plan",
    "no server-side protection",
    "not a required check yet",
    "detection, not blocking",
)
HERE = os.path.dirname(os.path.abspath(__file__))
DRIFT_EXIT = 3  # distinct from 1, which Python uses for a traceback
ROOT = os.path.dirname(os.path.dirname(HERE))  # .github/drift -> repo root


def gh(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def content(owner, repo, path, ref=None):
    d = gh(f"repos/{owner}/{repo}/contents/{path}" + (f"?ref={ref}" if ref else ""))
    if not d or "content" not in d:
        return None
    return base64.b64decode(d["content"])


def protection(owner, repo):
    full = gh(f"repos/{owner}/{repo}/branches/main/protection")
    if full is not None:
        checks = (full.get("required_status_checks") or {}).get("contexts", [])
        return {
            "protected": True,
            "admins": full.get("enforce_admins", {}).get("enabled", False),
            "force": full.get("allow_force_pushes", {}).get("enabled", True),
            "delete": full.get("allow_deletions", {}).get("enabled", True),
            "checks": checks,
        }
    b = gh(f"repos/{owner}/{repo}/branches/main")
    if b is None:
        return None
    p = b.get("protection") or {}
    rsc = p.get("required_status_checks") or {}
    return {
        "protected": bool(b.get("protected")),
        "admins": rsc.get("enforcement_level") == "everyone",
        "force": None,
        "delete": None,
        "checks": rsc.get("contexts", []),
    }


def _map(v):
    return v if isinstance(v, dict) else {}


def _list(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _bad_baseline(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def load_common(path):
    """The shared lint baseline; exit 2 with a clear message when it is missing, empty or malformed."""
    try:
        with open(path) as f:
            c = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        _bad_baseline(f"drift-check: cannot read {path}: {e}")
    enable = ((c or {}).get("linters") or {}).get("enable") if isinstance(c, dict) else None
    if not isinstance(enable, list) or not enable:
        _bad_baseline(f"drift-check: {path} has no linters.enable list")
    return c


def load_incident(path):
    """The incident form's field ids in order; exit 2 with a clear message when it is missing or malformed."""
    try:
        with open(path) as f:
            c = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        _bad_baseline(f"drift-check: cannot read {path}: {e}")
    body = c.get("body") if isinstance(c, dict) else None
    ids = [x.get("id") for x in body if isinstance(x, dict)] if isinstance(body, list) else []
    ids = [i for i in ids if isinstance(i, str) and i]
    if not ids:
        _bad_baseline(f"drift-check: {path} has no body field ids")
    return ids


def load_variants(path):
    """Accepted guard-agents.sh variants: {repo: sha256}. A missing file means none; a bad row exits 2."""
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        _bad_baseline(f"drift-check: cannot read {path}: {e}")
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) != 3 or not f[0] or not f[2].strip():
            _bad_baseline(f"drift-check: {path}:{n} needs repo<TAB>sha256<TAB>reason")
        repo, sha = f[0], f[1]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            _bad_baseline(f"drift-check: {path}:{n} sha256 must be 64 lowercase hex characters")
        if repo in out:
            _bad_baseline(f"drift-check: {path}:{n} repeats {repo}; pin exactly one variant per repo")
        out[repo] = sha
    return out


def load_pending(path):
    """Repos that still carry the old full repo-guard.yml: {repo: sha256 of that full copy}. A bad row exits 2."""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as fh:
        for n, line in enumerate(fh.read().splitlines(), 1):
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) != 3 or not f[0] or not f[2].strip():
                _bad_baseline(f"drift-check: {path}:{n} needs repo<TAB>sha256<TAB>reason")
            if not re.fullmatch(r"[0-9a-f]{64}", f[1]):
                _bad_baseline(f"drift-check: {path}:{n} sha256 must be 64 lowercase hex characters")
            if f[0] in out:
                _bad_baseline(f"drift-check: {path}:{n} repeats {f[0]}")
            out[f[0]] = f[1]
    return out


def parse_heads(args, repos):
    """--head REPO=SHA values as {repo: [sha, ...]}. A bad value exits 2: a typo must not check nothing."""
    out = {}
    for arg in args:
        repo, _, sha = arg.partition("=")
        if repo not in repos or not re.fullmatch(r"[0-9a-f]{40}", sha):
            _bad_baseline(f"drift-check: --head {arg!r} needs REPO=<40-hex SHA> with REPO in repos.tsv")
        out.setdefault(repo, []).append(sha)
    return out


MAX_PR_PAGES = 20  # 2000 open PRs; a longer list is reported, not read forever


def open_pr_heads(owner, repo, api=None):
    """[(label, head sha)] of every open PR, page by page, or None when the list cannot be read in full.

    None is reported as drift, never skipped: an error on any page, an entry without a number and a 40-hex
    head SHA, or more than MAX_PR_PAGES full pages.
    """
    api = api or gh
    out = []
    for page in range(1, MAX_PR_PAGES + 1):
        d = api(f"repos/{owner}/{repo}/pulls?state=open&per_page=100&page={page}")
        if not isinstance(d, list):
            return None
        for p in d:
            sha = _map(_map(p).get("head")).get("sha")
            num = _map(p).get("number")
            if not isinstance(num, int) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                return None
            out.append((f"#{num}", sha))
        if len(d) < 100:
            return out
    return None


WF_DIR = ".github/workflows"
CALLED = re.compile(r"(^|/)repo-guard-[^/@]*\.ya?ml(@|$)", re.I)
EXPR = re.compile(r"\$\{\{.*?\}\}", re.S)
LISTING_CAP = 1000


def workflow_files(owner, repo, ref=None, api=None):
    """{path: bytes or None (unreadable)} of every .yml/.yaml file in .github/workflows at ref (None = default
    branch), or None when the directory cannot be listed. GitHub runs only the files at the top of that folder."""
    api = api or gh
    q = f"?ref={ref}" if ref else ""
    d = api(f"repos/{owner}/{repo}/contents/{WF_DIR}{q}")
    if not isinstance(d, list) or len(d) >= LISTING_CAP:
        return None  # the Contents API lists at most 1,000 entries; a full listing may hide a file
    out = {}
    for e in d:
        e = _map(e)
        path = e.get("path")
        if not isinstance(path, str) or not re.search(r"\.ya?ml$", path, re.I):
            continue
        if e.get("type") != "file":
            out[path] = None  # a symlink or submodule named like a workflow is not checked, so it is flagged
            continue
        f = api(f"repos/{owner}/{repo}/contents/{path}{q}")
        try:
            out[path] = base64.b64decode(f["content"]) if isinstance(f, dict) and f.get("content") else None
        except (ValueError, TypeError):
            out[path] = None
    return out


def _call_only(on):
    """True when the workflow runs only when called; its jobs then report as "caller / job", never a bare name."""
    if on == "workflow_call" or on == ["workflow_call"]:
        return True
    return isinstance(on, dict) and list(on) == ["workflow_call"]


def _may_report(name):
    """The bare repo-guard check name a job name can produce, or None. ${{ }} may expand to anything."""
    pattern = ".*".join(re.escape(part) for part in EXPR.split(name.strip()))
    return next((n for n in caller_pin.SHIMS if re.fullmatch(pattern, n, re.I | re.S)), None)


def other_workflow_issues(files):
    """Issues for every workflow file but repo-guard.yml: none may call a repo-guard-*.yml file (a second or
    renamed caller) or report the bare pr-hygiene or verify-gate check name (a spoof of a required check)."""
    out = []
    for path, raw in sorted(files.items()):
        if path == WORKFLOWS[0]:
            continue
        name = path.rsplit("/", 1)[1]
        if raw is None:
            out.append(f"cannot read {path}")
            continue
        try:
            wf = yaml.load(raw.decode("utf-8"), Loader=caller_pin._Strict)  # noqa: S506 (SafeLoader subclass)
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            out.append(f"{name} does not parse: {str(e).splitlines()[0]}")
            continue
        if not isinstance(wf, dict):
            out.append(f"{name} is empty or not a YAML mapping")
            continue
        jobs = _map(wf.get("jobs"))
        for jid, job in jobs.items():
            uses = _map(job).get("uses")
            if isinstance(uses, str) and CALLED.search(uses):
                out.append(f"{name}: job {jid} calls {uses}; only repo-guard.yml may call the repo-guard jobs")
        if _call_only(wf.get("on", wf.get(True))):
            continue
        for jid, job in jobs.items():
            label = str(_map(job).get("name", jid))
            if "${{" in label:
                # Never parsed or evaluated: a quoted }} or format() can still produce a bare check name.
                out.append(f"{name}: job {jid} name has an expression, which can report a required check name; only literal job names are allowed")
                continue
            hit = _may_report(label)
            if hit:
                out.append(f"{name}: job {jid} can report the required check name {hit}; only repo-guard.yml may")
    return out


DUAL_EVENTS = {"pull_request", "pull_request_target"}


def _events(on):
    """The trigger names of a workflow's on: value (a string, a list or a mapping)."""
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {e for e in on if isinstance(e, str)}
    return {e for e in on if isinstance(e, str)} if isinstance(on, dict) else set()


def dual_event_issues(files):
    """vws-deploy#505: no workflow file, repo-guard.yml included, runs on both pull_request and
    pull_request_target. Each event gets its own run of every job, so a job gated to one event reports a
    skipped check run under the same name on the other, and Rule D never merges a skipped required check.
    Files that do not parse are reported by the other checks."""
    out = []
    for path, raw in sorted((files or {}).items()):
        if raw is None:
            continue
        try:
            wf = yaml.load(raw.decode("utf-8"), Loader=caller_pin._Strict)  # noqa: S506 (SafeLoader subclass)
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(wf, dict) and DUAL_EVENTS <= _events(wf.get("on", wf.get(True))):
            out.append(f"{path} runs on both pull_request and pull_request_target: a job gated to one event reports "
                       "a skipped check under the same name on the other, and Rule D never merges a skipped "
                       "required check (vws-deploy#505)")
    return out


def ref_issues(files, home, full_copy):
    """([issues], pin) for the workflows at one ref. pin is the caller's 40-hex SHA, "local" (vws-deploy),
    "pending" (the exact full copy that caller-pending.tsv pins, full_copy is its sha256), or None."""
    if files is None:
        return [f"cannot list {WF_DIR}"], None
    others = other_workflow_issues(files) + dual_event_issues(files)
    rg = WORKFLOWS[0]
    if rg not in files:
        return [f"no {rg}"] + others, None
    if files[rg] is None:
        return [f"cannot read {rg}"] + others, None
    if full_copy and hashlib.sha256(files[rg]).hexdigest() == full_copy:
        return others, ("pending" if not others else None)
    try:
        text = files[rg].decode("utf-8")
    except UnicodeDecodeError:
        return [f"{rg} is not UTF-8"] + others, None
    errs, pin = caller_pin.check(text, home=home)
    if errs and full_copy:
        got = hashlib.sha256(files[rg]).hexdigest()
        errs = [f"not the full copy that caller-pending.tsv pins ({full_copy[:12]}, got {got[:12]}) and not a "
                f"valid caller: {errs[0]}"] + errs[1:]
    return errs + others, (pin if not errs and not others else None)


def head_issues(repo, heads, home, pending, common, on_main):
    """Issues for the workflows at each PR head: [(label, sha, files)] -> ["label sha: reason"].

    files is workflow_files() at that head. A head is held to the same rules as the default branch: a pending
    repo is exempt only for the exact full copy it pins, and a caller must pin the common pin of the default
    branches (or, with no common pin, a SHA on vws-deploy main).
    """
    out = []
    for label, sha, files in heads:
        where = f"{label} {sha[:12]}"
        issues, pin = ref_issues(files, home, pending.get(repo))
        if issues:
            out.append(f"{where}: " + "; ".join(issues[:3]) + (f" (+{len(issues) - 3} more)" if len(issues) > 3 else ""))
            continue
        if pin in ("local", "pending"):
            continue
        if common and pin != common:
            out.append(f"{where}: pin {pin[:12]} is not the common pin {common[:12]}")
        elif not on_main(pin):
            out.append(f"{where}: pin {pin[:12]} is not on vws-deploy main")
    return out


def class4_head_issues(owner, repo, heads, script, vendored, pending, callees, api=None):
    """Class 4 issues at each PR head: ["label sha: reason"]. Vendored files are read at the head; the
    bootstrap rule holds the head's workflows (and the callee at the head's pin) to the default-branch script."""
    out = []
    for label, sha, files in heads:
        if files is None:
            continue  # head_issues already reports an unlistable head
        got = dict(files)
        got[VENDORED_CHECK_ENGLISH] = content(owner, repo, VENDORED_CHECK_ENGLISH, ref=sha)
        issues, _ = vendored_issues(repo, got, vendored)
        _, pin = ref_issues(files, repo == "vws-deploy", pending.get(repo))
        issues += bootstrap_for(owner, files, script, pin, callees, api=api)
        if issues:
            out.append(f"{label} {sha[:12]}: " + "; ".join(issues))
    return out


def on_main(owner, sha):
    """True when sha is vws-deploy main or an ancestor of it."""
    d = gh(f"repos/{owner}/vws-deploy/compare/{sha}...main")
    return bool(d) and d.get("status") in ("ahead", "identical")


def guard_status(got, want, variant=None):
    """(issue or None, ok label) for one repo's guard-agents.sh sha256. Only the exact pinned variant passes."""
    if got == want:
        return None, "ok"
    if variant is not None and got == variant:
        return None, f"accepted variant ({got[:12]})"
    return "guard-agents.sh differs from vws-deploy", "ok"


VENDORED_CHECK_ENGLISH = "scripts/check-english.sh"
# Vendored files every repo must carry: {path: repos exempt}. vws-deploy runs guard.sh english instead (#423).
VENDORED_REQUIRED = {VENDORED_CHECK_ENGLISH: {"vws-deploy"}}


def load_vendored(path):
    """Pinned vendored files: {path: {"*": sha256, repo: variant sha256}}. A bad row exits 2."""
    out = {}
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        _bad_baseline(f"drift-check: cannot read {path}: {e}")
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) != 4 or not f[0] or not f[1] or not f[3].strip():
            _bad_baseline(f"drift-check: {path}:{n} needs path<TAB>repo or *<TAB>sha256<TAB>reason")
        if not re.fullmatch(r"[0-9a-f]{64}", f[2]):
            _bad_baseline(f"drift-check: {path}:{n} sha256 must be 64 lowercase hex characters")
        row = out.setdefault(f[0], {})
        if f[1] in row:
            _bad_baseline(f"drift-check: {path}:{n} repeats {f[0]} for {f[1]}")
        row[f[1]] = f[2]
    for p, row in out.items():
        if "*" not in row:
            _bad_baseline(f"drift-check: {path} has a variant of {p} but no * row")
    return out


def vendored_issues(repo, got, pins):
    """([issues], [notes]) for one ref's vendored files. got is {path: bytes or None}; None or absent is
    skipped unless VENDORED_REQUIRED holds the path for this repo."""
    issues, notes = [], []
    for path in sorted(pins):
        raw = got.get(path)
        if raw is None:
            if path in VENDORED_REQUIRED and repo not in VENDORED_REQUIRED[path]:
                issues.append(f"no {path}")
            continue
        name = path.rsplit("/", 1)[1]
        digest = hashlib.sha256(raw).hexdigest()
        want = pins[path]
        if digest == want["*"]:
            continue
        if digest == want.get(repo):
            notes.append(f"{name} accepted variant ({digest[:12]})")
            continue
        issues.append(f"{name} differs from the vws-deploy pin ({want['*'][:12]}, got {digest[:12]})")
    return issues, notes


DISPATCH = re.compile(r'^case "\$command_name" in$(.*?)^esac$', re.M | re.S)
ARM = re.compile(r"^\s*([A-Za-z0-9_-]+(?:\s*\|\s*[A-Za-z0-9_-]+)*)\)", re.M)
GUARD_CALL = re.compile(r"""(?:^|[\s;&|(`])(?:\./)?scripts/guard-agents\.sh["']?[ \t]+["']?([^\s"';&|)]+)""", re.M)


def guard_subcommands(script):
    """The subcommands that a guard-agents.sh dispatches (its `case "$command_name" in` arms), or None."""
    try:
        m = DISPATCH.search(script.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError):
        return None
    if not m:
        return None
    subs = {name.strip() for arm in ARM.findall(m.group(1)) for name in arm.split("|")}
    return subs or None


def guard_calls(files):
    """[(path, subcommand)] for every scripts/guard-agents.sh call in a run: step. Comment lines never count;
    a file that does not parse is skipped (other_workflow_issues reports it). A non-literal SUB stays as is."""
    out = []
    for path, raw in sorted(files.items()):
        if raw is None:
            continue
        try:
            wf = yaml.load(raw.decode("utf-8"), Loader=caller_pin._Strict)  # noqa: S506 (SafeLoader subclass)
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        for job in _map(_map(wf).get("jobs")).values():
            steps = _map(job).get("steps")
            for step in steps if isinstance(steps, list) else []:
                run = _map(step).get("run")
                if not isinstance(run, str):
                    continue
                text = "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))
                out += [(path, sub) for sub in GUARD_CALL.findall(text)]
    return out


def bootstrap_issues(files, script, callee=None):
    """Issues when a workflow at this ref, or a vws-deploy repo-guard-*.yml it pins (callee), calls a
    guard-agents.sh subcommand that the base-branch script does not dispatch. The trusted script runs from
    the base, so such a call fails with a usage error until a separate PR lands the script (#439 class 4)."""
    unread = [f"cannot read vws-deploy {p.rsplit('/', 1)[1]}"
              for p, raw in sorted((callee or {}).items()) if raw is None]
    calls = [(p.rsplit("/", 1)[1], s) for p, s in guard_calls(files)]
    calls += [("vws-deploy " + p.rsplit("/", 1)[1], s) for p, s in guard_calls(callee or {})]
    if not calls:
        return unread
    have = guard_subcommands(script) if script is not None else None
    if have is None:
        return unread + ["workflows call guard-agents.sh, but the base branch has no readable "
                         "scripts/guard-agents.sh dispatch"]
    missing = {}
    for where, sub in calls:
        if sub not in have:
            missing.setdefault(where, set()).add(sub)
    return unread + [f"{where} calls guard-agents.sh {', '.join(sorted(subs))}, which the base-branch script does "
                     "not have; land the script first" for where, subs in sorted(missing.items())]


def callee_files(owner, pin, cache, api=None):
    """{path: bytes or None} of the vws-deploy repo-guard-*.yml files at pin (cached), {} when pin is not a SHA,
    or None when the listing cannot be read (never cached, so a failed read is never an empty callee)."""
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin):
        return {}
    if pin not in cache:
        files = workflow_files(owner, "vws-deploy", ref=pin, api=api)
        if files is None:
            return None
        cache[pin] = {p: raw for p, raw in files.items() if CALLED.search(p)}
    return cache[pin]


def bootstrap_for(owner, files, script, pin, cache, api=None):
    """bootstrap_issues for one ref, with the callee at its caller pin; an unlistable callee fails closed."""
    callee = callee_files(owner, pin, cache, api=api)
    if callee is None:
        return [f"cannot list the vws-deploy repo-guard-*.yml files at {pin[:12]}"]
    return bootstrap_issues(files, script, callee)


def rule_checks(rules):
    """Required status check contexts from the effective branch rules (the ruleset)."""
    out = []
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for check in _map(rule.get("parameters")).get("required_status_checks") or []:
            name = check.get("context") if isinstance(check, dict) else None
            if name and name not in out:
                out.append(name)
    return out


def effective_checks(protection_checks, rules):
    """Classic protection and the ruleset both apply, so a required name may come from either."""
    out = list(protection_checks or [])
    for name in rule_checks(rules):
        if name not in out:
            out.append(name)
    return out


def missing_checks(checks, extra):
    """Common checks plus the repo's own (repos.tsv) that main does not require."""
    want = COMMON_CHECKS + extra.split()
    return [name for name in want if name not in checks]


UNVERIFIED_BYPASS = "bypass actors unverified (needs admin token)"


def ruleset_drift(rulesets, details):
    """(drift, unverified) for one repo's branch rulesets. `details` maps an active ruleset id to its
    detail or None.

    GET /rulesets/{id} returns `bypass_actors` only to a caller that can administer the ruleset, so the
    scheduled run (github.token) never sees it. A missing or malformed field is never ok: it is reported
    as unverified, and only a readable, non-empty list is drift.
    """
    active = [r for r in rulesets or [] if r.get("target") == "branch" and r.get("enforcement") == "active"]
    if not active:
        return ["no active branch ruleset"], []
    out, unverified = [], []
    if not any(r.get("name") == "vws-main" for r in active):
        out.append("no active vws-main ruleset")
    for rule in active:
        detail = details.get(rule.get("id"))
        if not isinstance(detail, dict):
            out.append(f"ruleset {rule.get('name')} unreadable")
            continue
        actors = detail.get("bypass_actors")
        if not isinstance(actors, list):
            unverified.append(f"ruleset {rule.get('name')} {UNVERIFIED_BYPASS}")
        elif actors:
            out.append(f"ruleset {rule.get('name')} has {len(actors)} bypass actor(s)")
    return out, unverified


MAIN_RULES = ("pull_request", "non_fast_forward", "deletion")


def main_rules_drift(rulesets, rules):
    """Issue strings for the rules that the vws-main ruleset must apply to main (AGENTS.md facts).

    `rules` is GET /repos/{o}/{r}/rules/branches/main, readable with github.token: every entry has a
    `type` and the `ruleset_id` it comes from. vws-main must apply to main and must carry a pull request
    rule that allows only a squash merge, plus the non_fast_forward and deletion rules.
    """
    ids = {r.get("id") for r in rulesets or [] if isinstance(r, dict) and r.get("name") == "vws-main"
           and r.get("target") == "branch" and r.get("enforcement") == "active"}
    if not ids:
        return []
    mine = [r for r in rules or [] if isinstance(r, dict) and r.get("ruleset_id") in ids]
    if not mine:
        return ["vws-main does not apply to main"]
    out = []
    types = {r.get("type") for r in mine}
    for name in MAIN_RULES:
        if name not in types:
            out.append(f"vws-main has no {name} rule")
    for r in mine:
        if r.get("type") == "pull_request":
            methods = _map(r.get("parameters")).get("allowed_merge_methods")
            if methods != ["squash"]:
                out.append(f"vws-main allows merge methods {methods}, not squash only")
    return out


def bypass_audit(owner, repo):
    """(lines, failures) for one repo's active branch rulesets, for --bypass-audit (vws-deploy#458).

    Every bypass actor is listed as actor_type/actor_id/bypass_mode. Any actor fails, and so does a
    bypass list that is missing (a non-admin token) or malformed: the audit never reports unverified as ok.
    """
    rulesets = gh(f"repos/{owner}/{repo}/rulesets")
    if not isinstance(rulesets, list):
        return [], ["rulesets unreadable"]
    active = [r for r in rulesets if isinstance(r, dict) and r.get("target") == "branch"
              and r.get("enforcement") == "active"]
    lines, fails = [], []
    if not any(r.get("name") == "vws-main" for r in active):
        fails.append("no active vws-main ruleset")
    for r in active:
        detail = gh(f"repos/{owner}/{repo}/rulesets/{r.get('id')}")
        name = r.get("name")
        actors = detail.get("bypass_actors") if isinstance(detail, dict) else None
        if not isinstance(detail, dict):
            fails.append(f"ruleset {name} unreadable")
        elif not isinstance(actors, list):
            fails.append(f"ruleset {name} {UNVERIFIED_BYPASS}")
        else:
            lines.append(f"ruleset {name} (id {r.get('id')}): {len(actors)} bypass actor(s)")
            for a in actors:
                a = _map(a)
                lines.append(f"  actor_type={a.get('actor_type')} actor_id={a.get('actor_id')} "
                             f"bypass_mode={a.get('bypass_mode')}")
            if actors:
                fails.append(f"ruleset {name} has {len(actors)} bypass actor(s)")
    return lines, fails


def run_bypass_audit(owner, repos_file):
    """Print the per-repo audit and return 0 when no repo has a bypass actor, else DRIFT_EXIT."""
    failed = 0
    for line in open(repos_file):
        if not line.strip() or line.startswith("#"):
            continue
        repo = line.split("\t", 1)[0].strip()
        info = gh(f"repos/{owner}/{repo}")
        if not isinstance(info, dict):
            print(f"{repo}: FAIL repo unreadable")
            failed += 1
            continue
        if info.get("archived"):
            print(f"{repo}: archived (skipped)")
            continue
        lines, fails = bypass_audit(owner, repo)
        print(f"{repo}: {'FAIL ' + '; '.join(fails) if fails else 'ok'}")
        for detail in lines:
            print(f"  {detail}")
        failed += bool(fails)
    print(f"\nbypass audit: {failed} repo(s) failed")
    return DRIFT_EXIT if failed else 0


def stale_phrases(text):
    """The stale protection facts from #395 that must not come back into an AGENTS.md."""
    return [f"stale phrase: {phrase}" for phrase in STALE_PHRASES if phrase in text]


def lint_drift(cfg, common):
    try:
        c = yaml.safe_load(cfg)
    except yaml.YAMLError as e:
        return [f"config does not parse: {e}"]
    if not isinstance(c, dict):
        return ["config is empty or not a YAML mapping"]
    out = []
    linters = c.get("linters") or {}
    if not isinstance(linters, dict):
        return ["config 'linters' is not a mapping"]
    enabled = set(_list(linters.get("enable")))
    missing = [n for n in common["linters"]["enable"] if n not in enabled]
    if missing:
        out.append("missing linters: " + ",".join(missing))
    lll = _map(_map(linters.get("settings")).get("lll")).get("line-length")
    if lll != 120:
        out.append(f"lll line-length is {lll}, not 120")
    fmts = set(_list(_map(c.get("formatters")).get("enable")))
    for f in ("gofumpt", "goimports"):
        if f not in fmts:
            out.append(f"formatter {f} is off")
    return out


def incident_drift(tpl, legacy, want):
    """Issue strings for one repo's incident form; empty means ok."""
    out = []
    if tpl is None:
        out.append("no .github/ISSUE_TEMPLATE/incident.yml")
    else:
        try:
            c = yaml.safe_load(tpl)
        except yaml.YAMLError as e:
            out.append(f"incident.yml does not parse: {e}")
            c = None
        else:
            if not isinstance(c, dict):
                out.append("incident.yml is not a YAML mapping")
                c = None
        if c is not None:
            labels = c.get("labels")
            labels = [x.strip() for x in labels.split(",")] if isinstance(labels, str) else _list(labels)
            if "incident" not in labels:
                out.append("does not apply the incident label")
            body = c.get("body")
            if not isinstance(body, list) or any(not isinstance(x, dict) for x in body):
                ids = []
            else:
                ids = [x.get("id") for x in body]
            missing = [i for i in want if i not in ids]
            if missing:
                out.append("missing fields: " + ",".join(missing))
    if legacy is not None:
        out.append("incident.md still exists")
    return out


def label_exists(owner, repo, name):
    """True when the repo has the label. A deleted label would make the form's label do nothing."""
    return gh(f"repos/{owner}/{repo}/labels/{name}") is not None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="popododo0720")
    ap.add_argument("--repos", default=os.path.join(HERE, "repos.tsv"))
    ap.add_argument("--markdown", help="also write the table to this file")
    ap.add_argument("--guard-variants", default=os.path.join(HERE, "guard-variants.tsv"),
                    help="accepted guard-agents.sh variants: repo<TAB>sha256<TAB>reason")
    ap.add_argument("--bypass-audit", action="store_true",
                    help="admin audit run: check only ruleset bypass actors and fail on any (#458)")
    ap.add_argument("--caller-pending", default=os.path.join(HERE, "caller-pending.tsv"),
                    help="repos not yet moved to the repo-guard caller: repo<TAB>sha256 of the full copy<TAB>reason")
    ap.add_argument("--vendored", default=os.path.join(HERE, "vendored.tsv"),
                    help="vendored files pinned by sha256: path<TAB>repo or *<TAB>sha256<TAB>reason (#439)")
    ap.add_argument("--head", action="append", default=[], metavar="REPO=SHA",
                    help="also check the workflows at this PR head (repeatable; the workflow passes its event's head)")
    ap.add_argument("--no-open-prs", action="store_true", help="skip the open PR heads (only --head ones)")
    a = ap.parse_args()
    if a.bypass_audit:
        return run_bypass_audit(a.owner, a.repos)

    common = load_common(os.path.join(ROOT, ".github", "golangci-common.yml"))
    want_sha = open(os.path.join(ROOT, "scripts", "guard-agents.sh.sha256")).read().split()[0]
    variants = load_variants(a.guard_variants)
    want_incident = load_incident(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "incident.yml"))
    pending = load_pending(a.caller_pending)
    vendored = load_vendored(a.vendored)
    scripts, callees = {}, {}
    proto = {}  # the vws-proto tags, read once for the first Go repo (#404)

    names = [ln.split("\t")[0] for ln in open(a.repos) if ln.strip() and not ln.startswith("#")]
    extra_heads = parse_heads(a.head, set(names))
    rows, drift, unverified_repos, pins, row_of, live = [], 0, 0, {}, {}, []
    for line in open(a.repos):
        if not line.strip() or line.startswith("#"):
            continue
        repo, kind, cfgpath, extra = (line.rstrip("\n").split("\t") + ["", "", ""])[:4]
        info = gh(f"repos/{a.owner}/{repo}")
        if info is None:
            rows.append([repo, "unreadable", "", "", "", "", "", "", "", "", "", "", ""])
            drift += 1
            continue
        if info.get("archived"):
            rows.append([repo, "archived (skipped)", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-"])
            continue
        issues, notes = {}, {}
        proto_ok = "n/a"
        p = protection(a.owner, repo)
        if not p or not p["protected"]:
            issues["protection"] = ["main is not protected"]
            p = p or {"checks": []}
        else:
            bad = []
            if not p["admins"]:
                bad.append("admins not included")
            if p["force"]:
                bad.append("force push allowed")
            if p["delete"]:
                bad.append("deletion allowed")
            if bad:
                issues["protection"] = bad
        rules = gh(f"repos/{a.owner}/{repo}/rules/branches/main?per_page=100")
        if rules is None:
            issues["ruleset"] = ["branch rules unreadable"]
            rules = []
        else:
            rulesets = gh(f"repos/{a.owner}/{repo}/rulesets")
            if rulesets is None:
                issues["ruleset"] = issues.get("ruleset", []) + ["rulesets unreadable"]
            else:
                details = {
                    r.get("id"): gh(f"repos/{a.owner}/{repo}/rulesets/{r.get('id')}")
                    for r in rulesets
                    if r.get("target") == "branch" and r.get("enforcement") == "active"
                }
                bad, unverified = ruleset_drift(rulesets, details)
                bad += main_rules_drift(rulesets, rules)
                if bad:
                    issues["ruleset"] = issues.get("ruleset", []) + bad
                if unverified:
                    notes["ruleset"] = unverified
        checks = effective_checks(p["checks"], rules)
        miss = missing_checks(checks, extra)
        if miss:
            issues["checks"] = ["missing " + ",".join(miss)]
        files = workflow_files(a.owner, repo)
        wf = [w.rsplit("/", 1)[1] for w in WORKFLOWS if files is not None and w not in files]
        if wf:
            issues["workflows"] = ["missing " + ",".join(wf)]
        caller_ok = "ok"
        errs, pin = ref_issues(files, repo == "vws-deploy", pending.get(repo))
        if errs:
            issues["caller"] = errs[:3] + ([f"(+{len(errs) - 3} more)"] if len(errs) > 3 else [])
        elif pin == "pending":
            caller_ok = "pending (full copy)"
        elif pin != "local":
            pins[repo] = pin
            caller_ok = f"ok ({pin[:12]})" + ("; drop its caller-pending.tsv row" if repo in pending else "")
        g = content(a.owner, repo, "scripts/guard-agents.sh")
        guard_ok = "ok"
        if g is None:
            issues["guard"] = ["no scripts/guard-agents.sh"]
        else:
            bad_g, guard_ok = guard_status(hashlib.sha256(g).hexdigest(), want_sha, variants.get(repo))
            if bad_g:
                issues["guard"] = [bad_g]
        scripts[repo] = g
        got = dict(files or {})
        got[VENDORED_CHECK_ENGLISH] = content(a.owner, repo, VENDORED_CHECK_ENGLISH)
        bad_v, notes_v = vendored_issues(repo, got, vendored)
        if bad_v:
            issues["vendored"] = bad_v
        if notes_v:
            notes["vendored"] = notes_v
        boot = bootstrap_for(a.owner, files or {}, g, pin, callees)
        if boot:
            issues["bootstrap"] = boot
        docs = []
        agents = content(a.owner, repo, "AGENTS.md")
        if agents is None:
            docs.append("missing AGENTS.md")
        else:
            docs += stale_phrases(agents.decode("utf-8", "replace"))
        if content(a.owner, repo, "QUALITY.md") is None and content(a.owner, repo, "docs/QUALITY.md") is None:
            docs.append("missing QUALITY.md")
        if docs:
            issues["docs"] = docs
        inc = incident_drift(
            content(a.owner, repo, ".github/ISSUE_TEMPLATE/incident.yml"),
            content(a.owner, repo, ".github/ISSUE_TEMPLATE/incident.md"),
            want_incident,
        )
        if not label_exists(a.owner, repo, "incident"):
            inc.append("no incident label")
        if inc:
            issues["incident"] = inc
        if kind == "go":
            cfg = content(a.owner, repo, cfgpath)
            issues_l = [f"no {cfgpath}"] if cfg is None else lint_drift(cfg, common)
            if issues_l:
                issues["lint"] = issues_l
            if not proto:
                proto["tags"] = proto_drift.proto_tags(a.owner, gh)
                proto["latest"] = proto_drift.latest_tag(proto["tags"] or [])
            bad_p, proto_ok = proto_drift.proto_cell(a.owner, repo, proto["latest"], proto["tags"], gh)
            if bad_p:
                issues["vws-proto"] = bad_p
        if issues:
            drift += 1
        if notes:
            unverified_repos += 1
        row_of[repo] = (len(rows), bool(issues))
        live.append(repo)

        def cell(k, ok="ok"):
            if k in issues:
                return "; ".join(issues[k] + notes.get(k, []))
            return "; ".join(notes[k]) if k in notes else ok

        force = {None: "?", False: "no", True: "yes"}.get(p.get("force"))
        rows.append([
            repo,
            cell("protection", f"ok (force push: {force})"),
            cell("ruleset"),
            cell("checks", ",".join(sorted(checks))),
            cell("workflows"),
            cell("caller", caller_ok),
            cell("guard", guard_ok),
            cell("docs"),
            cell("incident"),
            cell("lint", "ok" if kind == "go" else "n/a"),
            cell("vendored"),
            cell("bootstrap"),
            cell("vws-proto", proto_ok),
        ])

    for repo, why in caller_pin.pin_drift(pins, lambda sha: on_main(a.owner, sha)).items():
        i, already = row_of[repo]
        rows[i][5] = why
        if not already:
            drift += 1

    common = caller_pin.common_pin(pins)
    head_rows = []
    for repo in live:
        heads = [] if a.no_open_prs else open_pr_heads(a.owner, repo)
        if heads is None:
            head_rows.append([repo, "cannot list open PRs"])
            drift += 1
            heads = []
        heads = heads + [("--head", sha) for sha in extra_heads.get(repo, [])]
        seen, todo = set(), []
        for label, sha in heads:
            if sha not in seen:
                seen.add(sha)
                todo.append((label, sha, workflow_files(a.owner, repo, ref=sha)))
        bad = head_issues(repo, todo, repo == "vws-deploy", pending, common, lambda s: on_main(a.owner, s))
        bad += class4_head_issues(a.owner, repo, todo, scripts.get(repo), vendored, pending, callees)
        head_rows += [[repo, why] for why in bad]
        drift += len(bad)
        if todo and not bad:
            head_rows.append([repo, f"ok ({len(todo)} PR head(s))"])

    head = ["repo", "protection", "ruleset", "required checks", "workflows", "caller", "guard-agents.sh",
            "AGENTS/QUALITY", "incident form", "lint", "vendored copies", "bootstrap", "vws-proto"]
    table = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    table += ["| " + " | ".join(r) + " |" for r in rows]
    table += ["", "| repo | caller, vendored copies and bootstrap at PR heads |", "|---|---|"]
    table += ["| " + " | ".join(r) + " |" for r in head_rows]
    if not head_rows:
        table.append("| - | no open PR heads |")
    text = "\n".join(table) + f"\n\ndrift: {drift} repo(s) or PR head(s)\n"
    if unverified_repos:
        text += (f"unverified: {unverified_repos} repo(s); ruleset bypass actors need an admin token "
                 "(run .github/drift/drift-check.sh with an admin gh)\n")
    print(text)
    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write(text)
    return DRIFT_EXIT if drift else 0


if __name__ == "__main__":
    sys.exit(main())
