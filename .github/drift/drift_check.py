#!/usr/bin/env python3
"""Compare the guardrails of every vws-* repo against one expected baseline (vws-deploy#300).

Checks per repo, on its default branch:
  protection  main is protected, admins included, no force push, no deletion
  checks      required status checks include the common set plus the repo's own (repos.tsv)
  workflows   repo-guard.yml and direct-push-watch.yml exist
  caller      repo-guard.yml is the short caller of the vws-deploy repo-guard-<job>.yml files (caller_pin.py): a 40-hex
              pin, the same pin in every repo, and that pin is on vws-deploy main (vws-deploy#402). A repo
              that caller-pending.tsv lists still carries the old full copy and is reported, not drift.
              The caller is also checked at the head of every open PR and at each --head REPO=SHA
              (the workflow passes the PR head from its event), against the pin of the default branches
  guard       scripts/guard-agents.sh matches vws-deploy's scripts/guard-agents.sh.sha256, or the one
              full sha256 that guard-variants.tsv pins for that repo ("accepted variant")
  docs        AGENTS.md and QUALITY.md (or docs/QUALITY.md) exist
  lint        Go repos: the golangci config enables every linter in .github/golangci-common.yml,
              sets lll to 120 and enables gofumpt and goimports
An archived repo is reported and skipped; it is not drift.

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

COMMON_CHECKS = ["pr-hygiene", "vws-verify", "verify-gate"]
WORKFLOWS = [".github/workflows/repo-guard.yml", ".github/workflows/direct-push-watch.yml"]
HERE = os.path.dirname(os.path.abspath(__file__))
DRIFT_EXIT = 3  # distinct from 1, which Python uses for a traceback
ROOT = os.path.dirname(os.path.dirname(HERE))  # .github/drift -> repo root


def gh(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def content(owner, repo, path):
    d = gh(f"repos/{owner}/{repo}/contents/{path}")
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
    """Repos that still carry the old full repo-guard.yml: {repo: reason}. A bad row exits 2."""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as fh:
        for n, line in enumerate(fh.read().splitlines(), 1):
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) != 2 or not f[0] or not f[1].strip():
                _bad_baseline(f"drift-check: {path}:{n} needs repo<TAB>reason")
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


def content_at(owner, repo, path, ref):
    d = gh(f"repos/{owner}/{repo}/contents/{path}?ref={ref}")
    if not d or "content" not in d:
        return None
    return base64.b64decode(d["content"]).decode("utf-8", "replace")


def head_issues(repo, heads, home, pending, common, on_main):
    """Issues for the caller at each PR head: [(label, sha, text or None)] -> ["label sha: reason"].

    A head is held to the same caller shape as the default branch and must pin the common pin of the
    default branches (or, with no common pin, a SHA on vws-deploy main). A pending repo's head that is still
    the full copy is not drift; one that is already a caller is checked like any other.
    """
    out = []
    for label, sha, text in heads:
        where = f"{label} {sha[:12]}"
        if text is None:
            out.append(f"{where}: no {WORKFLOWS[0]} at the PR head")
            continue
        errs, pin = caller_pin.check(text, home=home)
        if errs:
            if repo not in pending:
                out.append(f"{where}: {errs[0]}")
            continue
        if pin == "local":
            continue
        if common and pin != common:
            out.append(f"{where}: pin {pin[:12]} is not the common pin {common[:12]}")
        elif not on_main(pin):
            out.append(f"{where}: pin {pin[:12]} is not on vws-deploy main")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="popododo0720")
    ap.add_argument("--repos", default=os.path.join(HERE, "repos.tsv"))
    ap.add_argument("--markdown", help="also write the table to this file")
    ap.add_argument("--guard-variants", default=os.path.join(HERE, "guard-variants.tsv"),
                    help="accepted guard-agents.sh variants: repo<TAB>sha256<TAB>reason")
    ap.add_argument("--caller-pending", default=os.path.join(HERE, "caller-pending.tsv"),
                    help="repos not yet moved to the repo-guard caller: repo<TAB>reason")
    ap.add_argument("--head", action="append", default=[], metavar="REPO=SHA",
                    help="also check repo-guard.yml at this PR head (repeatable; the workflow passes its event's head)")
    ap.add_argument("--no-open-prs", action="store_true", help="skip the open PR heads (only --head ones)")
    a = ap.parse_args()

    common = load_common(os.path.join(ROOT, ".github", "golangci-common.yml"))
    want_sha = open(os.path.join(ROOT, "scripts", "guard-agents.sh.sha256")).read().split()[0]
    variants = load_variants(a.guard_variants)
    pending = load_pending(a.caller_pending)

    names = [ln.split("\t")[0] for ln in open(a.repos) if ln.strip() and not ln.startswith("#")]
    extra_heads = parse_heads(a.head, set(names))
    rows, drift, pins, row_of, live = [], 0, {}, {}, []
    for line in open(a.repos):
        if not line.strip() or line.startswith("#"):
            continue
        repo, kind, cfgpath, extra = (line.rstrip("\n").split("\t") + ["", "", ""])[:4]
        info = gh(f"repos/{a.owner}/{repo}")
        if info is None:
            rows.append([repo, "unreadable", "", "", "", "", "", ""])
            drift += 1
            continue
        if info.get("archived"):
            rows.append([repo, "archived (skipped)", "-", "-", "-", "-", "-", "-"])
            continue
        issues = {}
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
        want = COMMON_CHECKS + extra.split()
        miss = [c for c in want if c not in p["checks"]]
        if miss:
            issues["checks"] = ["missing " + ",".join(miss)]
        wf = [w.rsplit("/", 1)[1] for w in WORKFLOWS if content(a.owner, repo, w) is None]
        if wf:
            issues["workflows"] = ["missing " + ",".join(wf)]
        rg = content(a.owner, repo, WORKFLOWS[0])
        caller_ok = "ok"
        if repo in pending:
            caller_ok = "pending (full copy)"
        elif rg is not None:
            errs, pin = caller_pin.check(rg.decode("utf-8", "replace"), home=(repo == "vws-deploy"))
            if errs:
                issues["caller"] = errs[:2] + ([f"(+{len(errs) - 2} more)"] if len(errs) > 2 else [])
            elif pin != "local":
                pins[repo] = pin
                caller_ok = f"ok ({pin[:12]})"
        g = content(a.owner, repo, "scripts/guard-agents.sh")
        guard_ok = "ok"
        if g is None:
            issues["guard"] = ["no scripts/guard-agents.sh"]
        else:
            bad_g, guard_ok = guard_status(hashlib.sha256(g).hexdigest(), want_sha, variants.get(repo))
            if bad_g:
                issues["guard"] = [bad_g]
        docs = []
        if content(a.owner, repo, "AGENTS.md") is None:
            docs.append("AGENTS.md")
        if content(a.owner, repo, "QUALITY.md") is None and content(a.owner, repo, "docs/QUALITY.md") is None:
            docs.append("QUALITY.md")
        if docs:
            issues["docs"] = ["missing " + ",".join(docs)]
        if kind == "go":
            cfg = content(a.owner, repo, cfgpath)
            issues_l = [f"no {cfgpath}"] if cfg is None else lint_drift(cfg, common)
            if issues_l:
                issues["lint"] = issues_l
        if issues:
            drift += 1
        row_of[repo] = (len(rows), bool(issues))
        live.append(repo)

        def cell(k, ok="ok"):
            return "; ".join(issues[k]) if k in issues else ok

        force = {None: "?", False: "no", True: "yes"}.get(p.get("force"))
        rows.append([
            repo,
            cell("protection", f"ok (force push: {force})"),
            cell("checks", ",".join(sorted(p["checks"]))),
            cell("workflows"),
            cell("caller", caller_ok),
            cell("guard", guard_ok),
            cell("docs"),
            cell("lint", "ok" if kind == "go" else "n/a"),
        ])

    for repo, why in caller_pin.pin_drift(pins, lambda sha: on_main(a.owner, sha)).items():
        i, already = row_of[repo]
        rows[i][4] = why
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
                todo.append((label, sha, content_at(a.owner, repo, WORKFLOWS[0], sha)))
        bad = head_issues(repo, todo, repo == "vws-deploy", pending, common, lambda s: on_main(a.owner, s))
        head_rows += [[repo, why] for why in bad]
        drift += len(bad)
        if todo and not bad:
            head_rows.append([repo, f"ok ({len(todo)} PR head(s))"])

    head = ["repo", "protection", "required checks", "workflows", "caller", "guard-agents.sh", "AGENTS/QUALITY", "lint"]
    table = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    table += ["| " + " | ".join(r) + " |" for r in rows]
    table += ["", "| repo | caller at PR heads |", "|---|---|"]
    table += ["| " + " | ".join(r) + " |" for r in head_rows]
    if not head_rows:
        table.append("| - | no open PR heads |")
    text = "\n".join(table) + f"\n\ndrift: {drift} repo(s) or PR head(s)\n"
    print(text)
    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write(text)
    return DRIFT_EXIT if drift else 0


if __name__ == "__main__":
    sys.exit(main())
