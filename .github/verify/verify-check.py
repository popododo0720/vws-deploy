#!/usr/bin/env python3
"""verify-check: two checks vws-verify used to do by hand on every PR head (vws-deploy#517 AC4).

The findings table (docs/lessons/verify-findings-2026-10.md) lists two repeated manual checks:

  ac       Unmet AC while claiming done. Every `AC: <n>` line of the PR body must name a box under the
           `## Acceptance criteria` heading of the `Part of` issue (the boxes the orchestrate AC tick reads).
           Each claimed box is printed with its text as a CHECK line, so the verdict confirms them one by one.
  rebuild  A blocker carried unchanged into a rebuild. The newest vws-verify FAIL (CHANGES_REQUESTED) on an
           earlier head of this PR, or of a PR it rebuilds ("-vN of #M", "rebuild of #M", "supersedes #M",
           "replaces #M" in the title or body, followed up to 5 levels), is compared with the head:
           - its PR changes outside docs (*.md, docs/) must differ from the FAIL head's (per-file patch-id
             of `git diff -U0` from each side's merge base with --base, so main moving does not count);
           - every numbered blocking item of the FAIL review that cites file:line spans must have at least
             one cited span whose lines (blank lines ignored, indentation ignored) are gone from the head file.
           `git range-diff` of the two PR ranges is printed for the reader.

Read-only: GitHub GETs go through the orchestrator's Gh client and read allowlist; git only fetches
refs/pull/N/head from origin into FETCH_HEAD. Output is one "STATUS check: message" line per finding.
STATUS is PASS, FAIL, WARN, CHECK (a box to confirm by hand) or INFO. Exit 0 with no FAIL, 1 with a FAIL,
2 when a check cannot run (bad input or a read error).

Usage:
  verify-check.py ac      --repo vws-deploy --pr N
  verify-check.py rebuild --repo vws-deploy --pr N --dir CHECKOUT [--base origin/main]
  verify-check.py all     --repo vws-deploy --pr N --dir CHECKOUT [--base origin/main]
"""
import argparse
import importlib.util
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("orchestrate", os.path.join(HERE, "..", "orchestrate", "orchestrate.py"))
orch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orch)

REBUILD_RE = re.compile(r"(?i)(?:-v[0-9]+ of|\brebuil(?:d|t) of|\bsupersedes|\breplaces)\s+#([0-9]+)\b")
BLOCKING_RE = re.compile(r"(?im)^[ \t]*(?:\*\*)?Blocking\b")
NON_BLOCKING_RE = re.compile(r"(?im)^[ \t]*(?:\*\*)?(?:Non-blocking|Note:|Notes:)")
ITEM_RE = re.compile(r"(?m)^[ \t]*[0-9]+\.[ \t]")
# path:L or path:L1-L2; a bare :L or :L1-L2 (after a space or "(") reuses the previous path of the item.
SPAN_RE = re.compile(r"(?P<path>[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.[A-Za-z0-9]+)?:(?P<a>[0-9]+)(?:-(?P<b>[0-9]+))?(?![0-9])")
MAX_CHAIN = 5
MIN_SPAN_CHARS = 16  # a cited span shorter than this ("}", "return out") matches anywhere; not judged


class CheckError(Exception):
    """A check cannot run: bad input, a GitHub or git read error."""


class Report:
    def __init__(self, out=sys.stdout):
        self.out, self.failed = out, False

    def add(self, status, check, msg):
        self.failed |= status == "FAIL"
        print(f"{status} {check}: {msg}", file=self.out)


def split_repo(repo):
    owner, _, name = repo.rpartition("/")
    if owner and owner != orch.OWNER:
        raise CheckError(f"only {orch.OWNER}/vws-* repos are read (the orchestrator allowlist), got {repo}")
    if not re.fullmatch(r"vws-[a-z0-9-]+", name):
        raise CheckError(f"repo must be vws-<name>, got {repo}")
    return name


def get(gh, path):
    try:
        data, _ = gh.get(path)
        return data
    except (orch.GhError, PermissionError) as e:
        raise CheckError(f"GET {path}: {e}") from e


# ---- ac -------------------------------------------------------------------------------------------------

def check_ac(gh, repo, pr, rep):
    pull = get(gh, f"repos/{orch.OWNER}/{repo}/pulls/{pr}")
    body = pull.get("body") or ""
    ac = orch.parse_ac(body)
    if ac["error"]:
        rep.add("FAIL", "ac", f"#{pr}: {ac['error']}")
        return
    ref = orch.PART_OF_RE.search(body)
    if not ref:
        rep.add("FAIL", "ac", f"#{pr} has no 'Part of #N' line, so its AC: lines name no issue")
        return
    irepo, inum = ref.group(1).split("#")
    irepo = irepo or repo
    issue = get(gh, f"repos/{orch.OWNER}/{irepo}/issues/{inum}")
    where = f"{irepo}#{inum}"
    if "pull_request" in issue:
        rep.add("FAIL", "ac", f"Part of {where} is a pull request, not an issue")
        return
    if issue.get("state") != "open":
        rep.add("WARN", "ac", f"Part of {where} is {issue.get('state')}")
    boxes = orch.ac_boxes(issue.get("body") or "")
    if ac["none"]:
        rep.add("PASS", "ac", f"'AC: none - {ac['none']}' claims no box of {where}")
    elif not boxes:
        rep.add("FAIL", "ac", f"{where} has no boxes under '## Acceptance criteria', so AC: {', '.join(map(str, ac['boxes']))}"
                " cannot name a box (the AC tick would skip it); use 'AC: none - <reason>' or fix the issue")
        return
    for n in ac["boxes"]:
        if n > len(boxes):
            rep.add("FAIL", "ac", f"AC: {n} but {where} has {len(boxes)} acceptance-criteria boxes")
        elif boxes[n - 1]["checked"]:
            rep.add("WARN", "ac", f"AC: {n} is already ticked on {where}: {boxes[n - 1]['text']}")
        else:
            rep.add("CHECK", "ac", f"AC: {n} of {where}, confirm the diff and evidence meet it: {boxes[n - 1]['text']}")
    if ac["boxes"] and not any(n > len(boxes) for n in ac["boxes"]):
        rep.add("PASS", "ac", f"AC: {', '.join(map(str, ac['boxes']))} name existing boxes of {where}")
    for b in boxes:
        if b["n"] not in ac["boxes"] and not b["checked"]:
            rep.add("INFO", "ac", f"box {b['n']} of {where} stays open (not claimed): {b['text']}")


# ---- rebuild --------------------------------------------------------------------------------------------

def fail_reviews(gh, repo, pr, head, rep):
    """The vws-verify FAIL reviews on earlier heads of PR `pr` and of the PRs it rebuilds, newest last, and
    the PRs read. A referenced number that cannot be read as a PR is reported and skipped."""
    seen, read, todo, out = set(), [], [(pr, 0)], []
    while todo:
        n, depth = todo.pop(0)
        if n in seen:
            continue
        seen.add(n)
        try:
            p = get(gh, f"repos/{orch.OWNER}/{repo}/pulls/{n}")
            if not isinstance(p, dict) or not p.get("number"):
                raise CheckError(f"#{n} is not a pull request")
        except CheckError as e:
            if n == pr:
                raise
            rep.add("WARN", "rebuild", f"skipped rebuild reference #{n}: {e}")
            continue
        read.append(n)
        try:
            reviews = orch.verify_reviews(gh, {"repo": repo, "number": n, "url": p.get("html_url") or ""}, orch.DEFAULTS)
        except (orch.GhError, PermissionError) as e:
            raise CheckError(f"reviews of #{n}: {e}") from e
        out += [dict(r, pr=n) for r in reviews if r["result"] == "fail" and r["sha"] and r["sha"] != head]
        if depth < MAX_CHAIN:
            text = orch.visible_text((p.get("title") or "") + "\n" + (p.get("body") or ""))
            todo += [(int(m.group(1)), depth + 1) for m in REBUILD_RE.finditer(text)]
    return sorted(out, key=lambda r: r["at"]), sorted(read)


def git(d, *args, check=True):
    r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=120, check=False)
    if check and r.returncode:
        raise CheckError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def ensure_commit(d, sha, pr):
    if subprocess.run(["git", "-C", d, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False).returncode:
        git(d, "fetch", "-q", "--no-tags", "origin", f"refs/pull/{pr}/head")
    if subprocess.run(["git", "-C", d, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False).returncode:
        raise CheckError(f"commit {sha[:12]} of #{pr} is not in {d} after fetching refs/pull/{pr}/head")


def is_docs(path):
    return path.startswith("docs/") or path.lower().endswith(".md")


def pr_patch_ids(d, mb, sha):
    """{path: patch-id of `git diff -U0 mb sha -- path`} for the PR's non-docs files."""
    out = {}
    for path in git(d, "diff", "--name-only", "--no-renames", mb, sha).split("\n"):
        if not path or is_docs(path):
            continue
        patch = git(d, "diff", "-U0", "--no-renames", mb, sha, "--", path)
        r = subprocess.run(["git", "-C", d, "patch-id", "--stable"], input=patch, capture_output=True, text=True,
                           timeout=60, check=False)
        out[path] = (r.stdout.split() or [""])[0]
    return out


def blocking_items(body):
    """The numbered items of a FAIL review's blocking section (the whole section when it is not numbered)."""
    text = orch.visible_text(body or "")
    m = BLOCKING_RE.search(text)
    if m:
        text = text[m.end():]
    m = NON_BLOCKING_RE.search(text)
    if m:
        text = text[:m.start()]
    starts = [m.start() for m in ITEM_RE.finditer(text)]
    if not starts:
        return [text] if text.strip() else []
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


def item_spans(item):
    spans, last = [], ""
    for m in SPAN_RE.finditer(item):
        path = m.group("path") or ""
        if not path:
            if not last or (m.start() and item[m.start() - 1] not in " \t(,"):
                continue
            path = last
        last = path
        a = int(m.group("a"))
        spans.append((path, a, int(m.group("b") or a)))
    return spans


def resolve(path, files):
    if path in files:
        return path
    hits = [f for f in files if f.endswith("/" + path)]
    return hits[0] if len(hits) == 1 else ""


def contains(haystack, needle):
    n = len(needle)
    return next((i for i in range(len(haystack) - n + 1) if haystack[i:i + n] == needle), -1)


def check_rebuild(gh, repo, pr, d, base, rep):
    pull = get(gh, f"repos/{orch.OWNER}/{repo}/pulls/{pr}")
    head = (pull.get("head") or {}).get("sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise CheckError(f"#{pr} has no head sha")
    fails, chain = fail_reviews(gh, repo, pr, head, rep)
    if not fails:
        rep.add("PASS", "rebuild", f"no vws-verify FAIL on an earlier head of #{', #'.join(map(str, chain))}; nothing carried")
        return
    f = fails[-1]
    rep.add("INFO", "rebuild", f"head {head[:7]} of #{pr} against the newest FAIL: #{f['pr']} head {f['sha'][:7]} ({f['url']})")
    ensure_commit(d, head, pr)
    ensure_commit(d, f["sha"], f["pr"])
    mb_f, mb_h = (git(d, "merge-base", base, s).strip() for s in (f["sha"], head))
    rd = git(d, "range-diff", "--no-color", f"{mb_f}..{f['sha']}", f"{mb_h}..{head}", check=False)
    for line in rd.split("\n"):
        if re.match(r"^\s*[0-9-]+:\s+[0-9a-f-]+ [=!<>] ", line):
            rep.add("INFO", "rebuild", "range-diff " + line.strip())
    old, new = pr_patch_ids(d, mb_f, f["sha"]), pr_patch_ids(d, mb_h, head)
    changed = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))
    if changed:
        rep.add("PASS", "rebuild", f"non-docs changes differ from the FAIL head in {', '.join(changed)}")
    else:
        rep.add("FAIL", "rebuild", f"the PR's non-docs changes are the same as at FAIL head {f['sha'][:7]} (#{f['pr']}): "
                "only docs, the PR body or nothing changed, so the blocker is carried unchanged")
    items = blocking_items(f["body"])
    files_f = set(git(d, "ls-tree", "-r", "--name-only", f["sha"]).split("\n"))
    judged = 0
    for i, item in enumerate(items, 1):
        cited, gone = [], []
        for path, a, b in item_spans(item):
            full = resolve(path, files_f)
            if not full:
                continue
            lines = git(d, "show", f"{f['sha']}:{full}").split("\n")
            want = [x.strip() for x in lines[a - 1:b] if x.strip()]
            if b < a or b > len(lines) or sum(map(len, want)) < MIN_SPAN_CHARS:
                continue
            now = git(d, "show", f"{head}:{full}", check=False)
            at = contains([x.strip() for x in now.split("\n") if x.strip()], want) if now else -1
            cited.append(f"{full}:{a}-{b}" if b > a else f"{full}:{a}")
            if at < 0:
                gone.append(cited[-1])
        if not cited:
            continue
        judged += 1
        if gone:
            rep.add("PASS", "rebuild", f"blocking item {i}: cited lines changed at the head ({', '.join(gone)})")
        else:
            rep.add("FAIL", "rebuild", f"blocking item {i} of the FAIL review is carried unchanged: every cited span "
                    f"({', '.join(cited)} at {f['sha'][:7]}) is still in the head {head[:7]}")
    if not judged:
        rep.add("WARN", "rebuild", "the FAIL review cites no file:line span this check can judge; compare by hand")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=("ac", "rebuild", "all"))
    ap.add_argument("--repo", required=True, help="vws-<name> or popododo0720/vws-<name>")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--dir", help="a git checkout of the repo with an 'origin' remote (rebuild, all)")
    ap.add_argument("--base", default="origin/main", help="base ref for the merge bases (default origin/main)")
    a = ap.parse_args(argv)
    rep = Report()
    try:
        repo = split_repo(a.repo)
        if a.check != "ac" and not (a.dir and os.path.isdir(a.dir)):
            raise CheckError("--dir CHECKOUT is required for rebuild and all")
        gh = orch.Gh()
        if a.check in ("ac", "all"):
            check_ac(gh, repo, a.pr, rep)
        if a.check in ("rebuild", "all"):
            check_rebuild(gh, repo, a.pr, a.dir, a.base, rep)
    except (CheckError, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"RESULT {'FAIL' if rep.failed else 'PASS'}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
