#!/usr/bin/env python3
"""Rule E's rebuild job (vws-deploy#398): rebuild one conflicting PR onto main without rewriting history.

    rebuild_job.py --repo OWNER/REPO --pr N --clone DIR --job-id ID [--dry-run]

Reads the PR, then cherry-picks its commits onto origin/main with box/rebuild-pr.sh in a per-job worktree
DIR/.worktrees/ID, which is removed in every case. A clean pick pushes a new -vN branch (a new ref only,
never a force-push), opens a PR with the old title and body plus "Supersedes #N", and closes the old PR
with a "Superseded by #M" comment. A conflicting pick adds the `needs-rebuild` label and pushes nothing;
Rule R (vws-deploy#460) then wakes the owner once per head. A PR that is closed, draft, not into main,
from a fork, labeled `blocked` or `needs-rebuild`, or whose head moved since the read is skipped.

gh and git run with the box's stored gh login (the VM's popododo0720 PAT); GH_TOKEN is dropped. The only
writes are the push of the new ref and the four API calls in WRITES. --dry-run does the local pick only.

Prints one JSON line. Exit 0 rebuilt (or would_rebuild), 3 conflict, 4 skipped, 2 usage, 1 error.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REBUILD = os.path.join(HERE, "box", "rebuild-pr.sh")
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKIP_LABELS = ("blocked", "needs-rebuild")
# (method, path regex) for every API write this job may make.
WRITES = (("POST", r"repos/{r}/pulls"), ("POST", r"repos/{r}/issues/{n}/comments"),
          ("PATCH", r"repos/{r}/pulls/{n}"), ("POST", r"repos/{r}/issues/{n}/labels"))
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CONFLICT, EXIT_SKIP = 0, 1, 2, 3, 4


class JobError(Exception):
    pass


def env():
    """No GH_TOKEN; every git call (including rebuild-pr.sh's fetch) authenticates through gh's stored login."""
    e = {k: v for k, v in os.environ.items()
         if k not in ("GH_TOKEN", "GITHUB_TOKEN") and not k.startswith("GIT_CONFIG_")}
    e.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="",
             GIT_CONFIG_KEY_1="credential.helper", GIT_CONFIG_VALUE_1="!gh auth git-credential",
             GIT_TERMINAL_PROMPT="0")
    return e


def gh(method, path, body=None):
    p = subprocess.run(["gh", "api", "--method", method, path] + (["--input", "-"] if body is not None else []),
                       input=json.dumps(body) if body is not None else None, capture_output=True, text=True, env=env())
    if p.returncode:
        raise JobError(f"gh api {method} {path} failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout or "{}")


def write(repo, n, method, path, body):
    if not any(m == method and re.fullmatch(rx.format(r=re.escape(repo), n=n), path) for m, rx in WRITES):
        raise JobError(f"write not allowed: {method} {path}")
    return gh(method, path, body)


def git(clone, *args, check=True):
    p = subprocess.run(["git", "-C", clone, *args], capture_output=True, text=True, env=env())
    if check and p.returncode:
        raise JobError(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p


def skip_reason(pr, repo, remote_head):
    labels = {x["name"] for x in pr.get("labels", [])}
    for why, bad in (("not open", pr.get("state") != "open"), ("draft", pr.get("draft")),
                     ("base is not main", pr["base"]["ref"] != "main"),
                     ("head is in another repository", (pr["head"].get("repo") or {}).get("full_name") != repo),
                     ("labeled " + ", ".join(sorted(labels & set(SKIP_LABELS))), labels & set(SKIP_LABELS)),
                     ("head moved since the read", remote_head != pr["head"]["sha"])):
        if bad:
            return why
    return ""


def next_branch(clone, head_ref):
    """The next unused <stem>-vN on origin, N >= 2; a head that is already -vK counts from K."""
    m = re.fullmatch(r"(.+)-v(\d+)", head_ref)
    stem, used = (m.group(1), int(m.group(2))) if m else (head_ref, 1)
    refs = git(clone, "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/origin/{stem}-v*").stdout.split()
    for r in refs:
        v = re.fullmatch(re.escape(stem) + r"-v(\d+)", r)
        used = max(used, int(v.group(1))) if v else used
    return f"{stem}-v{used + 1}"


def run(a):
    out = {"repo": a.repo, "pr": a.pr}
    pr = gh("GET", f"repos/{a.repo}/pulls/{a.pr}")
    git(a.clone, "fetch", "-q", "--prune", "origin")
    head_ref = pr["head"]["ref"]
    remote = git(a.clone, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{head_ref}^{{commit}}", check=False)
    why = skip_reason(pr, a.repo, remote.stdout.strip())
    if why:
        return EXIT_SKIP, dict(out, result="skipped", reason=why)
    new = next_branch(a.clone, head_ref)
    base = git(a.clone, "rev-parse", "origin/main").stdout.strip()
    wt = os.path.join(a.clone, ".worktrees", a.job_id)
    pushed = False
    try:
        p = subprocess.run(["bash", REBUILD, "--repo-dir", a.clone, "--old", head_ref, "--new", new,
                            "--worktree", wt, "--base", base], capture_output=True, text=True, env=env())
        if p.returncode:
            if "conflict:" not in p.stderr:
                raise JobError(f"rebuild-pr.sh failed: {p.stderr.strip()[:300]}")
            if not a.dry_run:
                write(a.repo, a.pr, "POST", f"repos/{a.repo}/issues/{a.pr}/labels", {"labels": ["needs-rebuild"]})
            return EXIT_CONFLICT, dict(out, result="conflict", branch=new, base=base)
        if a.dry_run:
            return EXIT_OK, dict(out, result="would_rebuild", branch=new, base=base)
        git(wt, "push", "-q", "origin", f"refs/heads/{new}:refs/heads/{new}")  # a new ref; no "+", no force
        pushed = True
        body = (pr.get("body") or "").rstrip() + (
            f"\n\nSupersedes #{a.pr}: the same commits cherry-picked onto main {base[:7]} by the orchestrator's "
            "rebuild job (vws-deploy#398). No force-push; the old branch is unchanged.")
        new_pr = write(a.repo, a.pr, "POST", f"repos/{a.repo}/pulls",
                       {"title": pr["title"], "head": new, "base": "main", "body": body})
        write(a.repo, a.pr, "POST", f"repos/{a.repo}/issues/{a.pr}/comments",
              {"body": f"Superseded by #{new_pr['number']} (same commits cherry-picked onto main {base[:7]})."})
        write(a.repo, a.pr, "PATCH", f"repos/{a.repo}/pulls/{a.pr}", {"state": "closed"})
        return EXIT_OK, dict(out, result="rebuilt", new_pr=new_pr["number"], branch=new, base=base)
    except JobError as e:
        if pushed:
            raise JobError(f"{e}; branch {new} was pushed and is left for a person") from e
        raise
    finally:
        if os.path.isdir(wt):
            git(wt, "cherry-pick", "--abort", check=False)
            git(a.clone, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
        git(a.clone, "worktree", "prune", check=False)
        git(a.clone, "branch", "-D", new, check=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--clone", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    if not (REPO_RE.match(a.repo) and JOB_RE.match(a.job_id) and a.pr > 0 and os.path.isdir(a.clone)):
        print("rebuild_job: bad --repo, --pr, --clone or --job-id", file=sys.stderr)
        return EXIT_USAGE
    try:
        code, res = run(a)
    except (JobError, KeyError, ValueError) as e:
        print(f"rebuild_job: {e}", file=sys.stderr)
        print(json.dumps({"result": "error", "repo": a.repo, "pr": a.pr}))
        return EXIT_ERROR
    print(json.dumps(res))
    return code


if __name__ == "__main__":
    sys.exit(main())
