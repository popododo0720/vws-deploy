#!/usr/bin/env python3
"""Check that a repo-guard.yml is the short caller of the shared repo-guard jobs (vws-deploy#402).

The jobs live in vws-deploy .github/workflows/repo-guard-reusable.yml. Every other repo calls it from a
caller pinned by a full 40-hex commit SHA on vws-deploy main, never a branch or tag. vws-deploy itself
(--home) calls it as ./.github/workflows/repo-guard-reusable.yml, which GitHub runs from the same commit.
The caller must match the expected shape exactly: the triggers, one call per job with that job's least
permissions and no secrets, and the two shim jobs that keep the bare pr-hygiene and verify-gate check
names (a called job reports as "caller / called"). docs/design/repo-guard-reusable.md has the model.

Usage: caller_pin.py [--home] FILE   prints the pin ("local" for --home); exit 1 with the reasons otherwise.
"""
import re
import sys

import yaml

MAX_LINES = 40
PATH = ".github/workflows/repo-guard-reusable.yml"
REMOTE = re.compile(r"^popododo0720/vws-deploy/" + re.escape(PATH) + r"@([0-9a-f]{40})$")
LOCAL = "./" + PATH
PR = "github.event_name == 'pull_request'"
TYPES = ["opened", "edited", "reopened", "synchronize", "labeled", "unlabeled"]
COMMENT_TYPES = ["created", "edited", "deleted"]
# caller job -> (called job, if, permissions)
CALLS = {
    "hygiene": ("pr-hygiene", PR, {"contents": "read", "pull-requests": "read"}),
    "gate": ("verify-gate", PR, {"contents": "read", "pull-requests": "read", "issues": "read"}),
    "reset": ("verify-reset", PR + " && github.event.action == 'synchronize'",
              {"issues": "write", "pull-requests": "write"}),
    "rerun": ("verify-rerun", "github.event_name == 'issue_comment' && github.event.issue.pull_request",
              {"actions": "write", "pull-requests": "read"}),
}
# required check name -> caller job it reports. A skipped required check counts as passed, so the shim
# runs always() and passes only on "success/<name>": the call succeeded and its last step ran.
SHIMS = {"pr-hygiene": "hygiene", "verify-gate": "gate"}


def shim(name, call):
    env = "${{ needs.%s.result }}/${{ needs.%s.outputs.passed }}" % (call, call)
    return {"needs": call, "if": "always() && " + PR, "runs-on": "ubuntu-latest",
            "steps": [{"env": {"R": env}, "run": 'test "$R" = success/' + name}]}


def check(text, home=False):
    """([reasons], pin) for one repo-guard.yml. pin is the 40-hex SHA, "local" for --home, or None."""
    errs = []
    if len(text.splitlines()) > MAX_LINES:
        errs.append(f"caller has {len(text.splitlines())} lines; a caller is at most {MAX_LINES} lines")
    try:
        wf = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return errs + [f"caller does not parse: {e}"], None
    if not isinstance(wf, dict):
        return errs + ["caller is empty or not a YAML mapping"], None
    on = wf.get("on", wf.get(True))
    want_on = {"pull_request": {"types": TYPES}, "issue_comment": {"types": COMMENT_TYPES}}
    if on != want_on:
        errs.append(f"triggers must be exactly {want_on} (never pull_request_target or push), got {on}")
    if wf.get("permissions") != {}:
        errs.append("top-level permissions must be {} so each job grants only its own")
    jobs = wf.get("jobs") if isinstance(wf.get("jobs"), dict) else {}
    want_jobs = sorted(list(CALLS) + list(SHIMS))
    if sorted(jobs) != want_jobs:
        errs.append(f"jobs must be exactly {want_jobs}, got {sorted(jobs)}")
    pins = set()
    for cid, (job, cond, perms) in CALLS.items():
        j = jobs.get(cid)
        if not isinstance(j, dict):
            continue
        uses = j.get("uses", "")
        m = REMOTE.match(uses) if isinstance(uses, str) else None
        if home:
            if uses != LOCAL:
                errs.append(f"{cid}: vws-deploy calls {LOCAL} (same commit), got {uses!r}")
            pins.add("local")
        elif not m:
            errs.append(f"{cid}: uses must be popododo0720/vws-deploy/{PATH}@<40-hex commit SHA>, got {uses!r}")
        else:
            pins.add(m.group(1))
        want = {"if": cond, "uses": uses, "with": {"job": job}, "permissions": perms}
        if j != want:
            extra = sorted(set(j) - set(want))
            errs.append(f"{cid}: must be exactly {want}" + (f"; drop {extra}" if extra else ""))
    for name, cid in SHIMS.items():
        if name in jobs and jobs[name] != shim(name, cid):
            errs.append(f"{name}: the check-name shim must be exactly {shim(name, cid)}")
    if len(pins) > 1:
        errs.append(f"every call must pin the same SHA, got {sorted(pins)}")
    pin = next(iter(pins)) if len(pins) == 1 and not errs else None
    return errs, pin


def pin_drift(pins, on_main):
    """{repo: reason} for callers whose pin differs from the common one or is not on vws-deploy main.

    pins is {repo: sha}; on_main(sha) says whether sha is an ancestor of (or equal to) vws-deploy main.
    With no single most common pin every caller is flagged.
    """
    out = {}
    if not pins:
        return out
    counts = {}
    for sha in pins.values():
        counts[sha] = counts.get(sha, 0) + 1
    top = max(counts.values())
    leaders = [s for s, n in counts.items() if n == top]
    common = leaders[0] if len(leaders) == 1 else None
    checked = {}
    for repo, sha in sorted(pins.items()):
        if sha != common:
            out[repo] = f"pin {sha[:12]} is not the common pin" + (f" {common[:12]}" if common else " (no majority)")
            continue
        if sha not in checked:
            checked[sha] = on_main(sha)
        if not checked[sha]:
            out[repo] = f"pin {sha[:12]} is not on vws-deploy main"
    return out


def main(argv):
    home = "--home" in argv
    args = [a for a in argv if a != "--home"]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    errs, pin = check(open(args[0]).read(), home=home)
    for e in errs:
        print(f"caller-pin: {args[0]}: {e}", file=sys.stderr)
    if errs:
        return 1
    print(pin)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
