#!/usr/bin/env python3
"""Compare the guardrails of every vws-* repo against one expected baseline (vws-deploy#300).

Checks per repo, on its default branch:
  protection  main is protected, admins included, no force push, no deletion
  checks      required status checks include the common set plus the repo's own (repos.tsv)
  workflows   repo-guard.yml and direct-push-watch.yml exist
  guard       scripts/guard-agents.sh matches vws-deploy's scripts/guard-agents.sh.sha256
  docs        AGENTS.md and QUALITY.md (or docs/QUALITY.md) exist
  lint        Go repos: the golangci config enables every linter in lint/golangci-common.yml,
              sets lll to 120 and enables gofumpt and goimports
An archived repo is reported and skipped; it is not drift.

Needs an authenticated gh. With admin access (an audit) the full protection settings are read;
with a read-only token (CI) the branch summary is used and force-push and deletion show as "?".
Exit status: 0 when nothing drifts, 1 when something does, 2 on a usage error.
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
WORKFLOWS = [".github/workflows/repo-guard.yml", ".github/workflows/direct-push-watch.yml"]
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


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


def lint_drift(cfg, common):
    try:
        c = yaml.safe_load(cfg)
    except yaml.YAMLError as e:
        return [f"config does not parse: {e}"]
    out = []
    linters = c.get("linters") or {}
    enabled = set(linters.get("enable") or [])
    missing = [n for n in common["linters"]["enable"] if n not in enabled]
    if missing:
        out.append("missing linters: " + ",".join(missing))
    lll = ((linters.get("settings") or {}).get("lll") or {}).get("line-length")
    if lll != 120:
        out.append(f"lll line-length is {lll}, not 120")
    fmts = set(((c.get("formatters") or {}).get("enable")) or [])
    for f in ("gofumpt", "goimports"):
        if f not in fmts:
            out.append(f"formatter {f} is off")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="popododo0720")
    ap.add_argument("--repos", default=os.path.join(HERE, "repos.tsv"))
    ap.add_argument("--markdown", help="also write the table to this file")
    a = ap.parse_args()

    common = yaml.safe_load(open(os.path.join(ROOT, "lint", "golangci-common.yml")))
    want_sha = open(os.path.join(ROOT, "scripts", "guard-agents.sh.sha256")).read().split()[0]

    rows, drift = [], 0
    for line in open(a.repos):
        if not line.strip() or line.startswith("#"):
            continue
        repo, kind, cfgpath, extra = (line.rstrip("\n").split("\t") + ["", "", ""])[:4]
        info = gh(f"repos/{a.owner}/{repo}")
        if info is None:
            rows.append([repo, "unreadable", "", "", "", "", ""])
            drift += 1
            continue
        if info.get("archived"):
            rows.append([repo, "archived (skipped)", "-", "-", "-", "-", "-"])
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
        g = content(a.owner, repo, "scripts/guard-agents.sh")
        if g is None:
            issues["guard"] = ["no scripts/guard-agents.sh"]
        elif hashlib.sha256(g).hexdigest() != want_sha:
            issues["guard"] = ["guard-agents.sh differs from vws-deploy"]
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

        def cell(k, ok="ok"):
            return "; ".join(issues[k]) if k in issues else ok

        force = {None: "?", False: "no", True: "yes"}.get(p.get("force"))
        rows.append([
            repo,
            cell("protection", f"ok (force push: {force})"),
            cell("checks", ",".join(sorted(p["checks"]))),
            cell("workflows"),
            cell("guard"),
            cell("docs"),
            cell("lint", "ok" if kind == "go" else "n/a"),
        ])

    head = ["repo", "protection", "required checks", "workflows", "guard-agents.sh", "AGENTS/QUALITY", "lint"]
    table = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    table += ["| " + " | ".join(r) + " |" for r in rows]
    text = "\n".join(table) + f"\n\ndrift: {drift} repo(s)\n"
    print(text)
    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write(text)
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
