#!/usr/bin/env python3
"""Compare the guardrails of every vws-* repo against one expected baseline (vws-deploy#300).

Checks per repo, on its default branch:
  protection  main is protected, admins included, no force push, no deletion
  ruleset     an active vws-main branch ruleset, with no bypass actors; the bypass list is readable
              only with an admin token, so without one the cell says "unverified", never ok
  checks      required status checks (classic plus the ruleset) include the common set plus the repo's own
  workflows   repo-guard.yml, direct-push-watch.yml and verify-queue.yml exist (verify-queue: #460)
  guard       scripts/guard-agents.sh matches vws-deploy's scripts/guard-agents.sh.sha256, or the one
              full sha256 that guard-variants.tsv pins for that repo ("accepted variant")
  docs        AGENTS.md and QUALITY.md (or docs/QUALITY.md) exist, and AGENTS.md has no stale fact (#395)
  incident    .github/ISSUE_TEMPLATE/incident.yml exists, applies the incident label, has every field
              of vws-deploy's form, no incident.md is left, and the repo has the incident label
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
import subprocess
import sys

import yaml

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


def guard_status(got, want, variant=None):
    """(issue or None, ok label) for one repo's guard-agents.sh sha256. Only the exact pinned variant passes."""
    if got == want:
        return None, "ok"
    if variant is not None and got == variant:
        return None, f"accepted variant ({got[:12]})"
    return "guard-agents.sh differs from vws-deploy", "ok"


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
    a = ap.parse_args()

    common = load_common(os.path.join(ROOT, ".github", "golangci-common.yml"))
    want_sha = open(os.path.join(ROOT, "scripts", "guard-agents.sh.sha256")).read().split()[0]
    variants = load_variants(a.guard_variants)
    want_incident = load_incident(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "incident.yml"))

    rows, drift, unverified_repos = [], 0, 0
    for line in open(a.repos):
        if not line.strip() or line.startswith("#"):
            continue
        repo, kind, cfgpath, extra = (line.rstrip("\n").split("\t") + ["", "", ""])[:4]
        info = gh(f"repos/{a.owner}/{repo}")
        if info is None:
            rows.append([repo, "unreadable", "", "", "", "", "", "", ""])
            drift += 1
            continue
        if info.get("archived"):
            rows.append([repo, "archived (skipped)", "-", "-", "-", "-", "-", "-", "-"])
            continue
        issues, notes = {}, {}
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
        wf = [w.rsplit("/", 1)[1] for w in WORKFLOWS if content(a.owner, repo, w) is None]
        if wf:
            issues["workflows"] = ["missing " + ",".join(wf)]
        g = content(a.owner, repo, "scripts/guard-agents.sh")
        guard_ok = "ok"
        if g is None:
            issues["guard"] = ["no scripts/guard-agents.sh"]
        else:
            bad_g, guard_ok = guard_status(hashlib.sha256(g).hexdigest(), want_sha, variants.get(repo))
            if bad_g:
                issues["guard"] = [bad_g]
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
        if issues:
            drift += 1
        if notes:
            unverified_repos += 1

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
            cell("guard", guard_ok),
            cell("docs"),
            cell("incident"),
            cell("lint", "ok" if kind == "go" else "n/a"),
        ])

    head = ["repo", "protection", "ruleset", "required checks", "workflows", "guard-agents.sh",
            "AGENTS/QUALITY", "incident form", "lint"]
    table = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    table += ["| " + " | ".join(r) + " |" for r in rows]
    text = "\n".join(table) + f"\n\ndrift: {drift} repo(s)\n"
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
