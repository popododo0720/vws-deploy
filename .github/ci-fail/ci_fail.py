#!/usr/bin/env python3
"""CI-failure marker comment for vws-* PRs (vws-deploy#412).

Run by .github/workflows/ci-fail.yml on workflow_run completed. For a failed run on a same-repo PR
head it posts one comment per head SHA (or edits it on a rerun or another failing workflow): line 1
is the marker, then the failed jobs, a bounded masked log tail per job, and a JSON record. The
format is docs/ci-fail.md. Stdlib only; GitHub calls go through `gh api` with the job's token.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

OWNER = "popododo0720"
BOT = "github-actions[bot]"
CAP = 3
NEEDS_HUMAN = "needs-human"
FAILED = {"failure", "timed_out", "startup_failure", "action_required"}
AUTHORIZED = {"OWNER", "MEMBER", "COLLABORATOR"}
TAIL_LINES = 40
LINE_CHARS = 200
TAIL_BYTES = 4096
MAX_TAILS = 5
TAIL_BUDGET = 12288
WAKEABLE = {"deepseek", "grok"}

SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"vws-[a-z0-9-]+")
AGENT_LINE_RE = re.compile(r"^Agent:[ \t]*([a-z0-9][a-z0-9-]{0,38})[ \t]*$", re.M)
AGENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,38}")
MARKER_RE = re.compile(r"<!-- vws-ci-fail sha=([0-9a-f]{40}) v=(\d+) repo=(vws-[a-z0-9-]+) pr=(\d+) "
                       r"owner=(deepseek|grok|none) agent=([a-z0-9][a-z0-9-]{0,38}|-) n=(\d+) wake=(true|false) -->")
JSON_BLOCK_RE = re.compile(r"^```json\n(.*?)^```$", re.M | re.S)
SUPERSEDES_RE = re.compile(r"^Supersedes #([1-9][0-9]*)[ \t]*$", re.M)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z ?")
NOISE = ("##[group]", "##[endgroup]", "::add-mask::")
SECRET_RES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[redacted]"),
    (re.compile(r"://[^/\s:@]+:[^@\s]+@"), "://[redacted]@"),
    # orchestrate.py TOKEN_RE
    (re.compile(r"(gh[opsu]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|Bearer\s+\S+)"), "[redacted]"),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), "[redacted]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|authorization)"
                r"([\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+"), r"\1\2[redacted]"),
]


def mask(text):
    """Redact secrets GitHub's own masking misses (it masks only the repo's registered secrets)."""
    for rx, repl in SECRET_RES:
        text = rx.sub(repl, text)
    return text


def _split_runs(rx, text):
    return rx.sub(lambda m: "\u200b".join(m.group(0)), text)


def defuse(text):
    """Make log text inert in the comment: no HTML comment (marker), no mention, no fence breaks."""
    text = text.replace("<!--", "<!-\u200b-").replace("-->", "-\u200b->").replace("@", "@\u200b")
    text = _split_runs(re.compile(r"``+"), text)
    return _split_runs(re.compile(r"~~+"), text)


def tail(log):
    """(text, lines, truncated): the masked end of a job log, bounded in lines, chars and bytes."""
    rows = []
    for line in log.replace("\r\n", "\n").split("\n"):
        line = STAMP_RE.sub("", ANSI_RE.sub("", line))
        if not line.startswith(NOISE):
            rows.append(line)
    while rows and not rows[-1].strip():
        rows.pop()
    rows = mask("\n".join(rows)).split("\n") if rows else []
    truncated = len(rows) > TAIL_LINES
    rows = [r if len(r) <= LINE_CHARS else r[:LINE_CHARS] + "\u2026" for r in rows[-TAIL_LINES:]]
    truncated = truncated or any(r.endswith("\u2026") for r in rows)
    while rows and len("\n".join(rows).encode()) > TAIL_BYTES:
        rows.pop(0)
        truncated = True
    return "\n".join(rows), len(rows), truncated


def resolve_owner(repo, labels, body, branch):
    """(owner, agent, source). owner is deepseek, grok or none; agent '' when none."""
    m = AGENT_LINE_RE.search((body or "").replace("\r\n", "\n"))
    agent = m.group(1) if m else ""
    if "grok" in labels:
        return "grok", agent if agent and agent != "deepseek-dev" else repo, "label:grok"
    if "deepseek" in labels:
        return "deepseek", "deepseek-dev", "label:deepseek"
    if agent:
        return ("deepseek" if agent == "deepseek-dev" else "grok"), agent, "agent-line"
    if branch.startswith("deepseek/"):
        return "deepseek", "deepseek-dev", "branch-prefix"
    return "none", "", "none"


def _safe(s, limit=100):
    return re.sub(r"[^A-Za-z0-9 ._()/:+-]", "_", str(s))[:limit]


def marker_line(rec):
    if not (SHA_RE.fullmatch(rec["head_sha"]) and REPO_RE.fullmatch(rec["repo"]) and rec["owner"] in WAKEABLE | {"none"}
            and (rec["agent"] == "" or AGENT_RE.fullmatch(rec["agent"]))):
        raise ValueError("record has an invalid marker field")
    return (f"<!-- vws-ci-fail sha={rec['head_sha']} v={int(rec['v'])} repo={rec['repo']} pr={int(rec['pr'])} "
            f"owner={rec['owner']} agent={rec['agent'] or '-'} n={int(rec['n'])} "
            f"wake={'true' if rec['wake'] else 'false'} -->")


def format_comment(rec, tails, escalation=""):
    """The comment body. tails maps job_id to tail text (already masked); it is defused here."""
    agent = f", agent `{rec['agent']}`" if rec["agent"] else ""
    out = [marker_line(rec),
           f"**CI failed** on `{rec['head_sha'][:7]}` ({rec['repo']}#{rec['pr']}, owner `{rec['owner']}`{agent}, "
           f"failing head {rec['n']} of {rec['cap']} auto-reworks, wake {'yes' if rec['wake'] else 'no'})", ""]
    if rec["failed_jobs"]:
        out.append("Failed jobs:")
        out += [f"- `{_safe(j['workflow'])} / {_safe(j['job'])}`: {_safe(j['conclusion'])} ({_safe(j['url'], 300)})"
                for j in rec["failed_jobs"]]
    else:
        out.append("Failed jobs: none reported (the run failed before any job ran).")
    for j in rec["failed_jobs"]:
        if j["job_id"] in tails:
            out += ["", f"<details><summary>{_safe(j['workflow'])} / {_safe(j['job'])} log tail "
                        f"(last {j['tail_lines']} lines, masked)</summary>", "", "~~~~text",
                    defuse(tails[j["job_id"]]), "~~~~", "</details>"]
    if escalation:
        out += ["", escalation]
    blob = json.dumps(rec, indent=2, ensure_ascii=True)
    for ch in "<>@`~":
        blob = blob.replace(ch, f"\\u{ord(ch):04x}")
    out += ["", "```json", blob, "```", ""]
    return "\n".join(out)


def parse_comment(body):
    """The JSON record of a marker comment, or None when line 1 and the record disagree or are absent."""
    body = (body or "").replace("\r\n", "\n")
    m = MARKER_RE.fullmatch(body.split("\n", 1)[0])
    blocks = JSON_BLOCK_RE.findall(body)
    if not m or not blocks:
        return None
    try:
        rec = json.loads(blocks[-1])
    except ValueError:
        return None
    try:
        if not isinstance(rec, dict) or marker_line(rec) != m.group(0):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return rec


def chain_of(pr, branch, body, all_prs, repository):
    """PR numbers in the head chain: same branch minus a -vN suffix, plus Supersedes #N links."""
    base = re.sub(r"-v\d+$", "", branch)
    same = {p["number"]: p for p in all_prs
            if (((p.get("head") or {}).get("repo") or {}).get("full_name")) == repository}
    chain = {n for n, p in same.items() if re.sub(r"-v\d+$", "", p["head"]["ref"]) == base} | {pr}
    bodies = [body] + [(same[n].get("body") or "") for n in chain if n in same]
    for b in bodies:
        chain |= {int(x) for x in SUPERSEDES_RE.findall(b.replace("\r\n", "\n")) if int(x) in same}
    return sorted(chain)


def reset_at(comments, events):
    t = ""
    for c in comments:
        if ((c.get("body") or "").lstrip().startswith("/orchestrate reset-failures")
                and c.get("author_association") in AUTHORIZED):
            t = max(t, c["created_at"])
    for e in events:
        if e.get("event") == "unlabeled" and (e.get("label") or {}).get("name") == NEEDS_HUMAN:
            t = max(t, e["created_at"])
    return t


def bot_records(comments, repo, pr):
    for c in comments:
        if (c.get("user") or {}).get("login") == BOT:
            rec = parse_comment(c.get("body"))
            if rec and rec["repo"] == repo and rec["pr"] == pr:
                yield c, rec


def fetch_tails(gh, a, jobs):
    tails, budget = {}, TAIL_BUDGET
    for j in jobs:
        if len(tails) >= MAX_TAILS or budget <= 0:
            j["tail_lines"], j["tail_truncated"] = 0, False
            continue
        try:
            text, lines, cut = tail(gh.text(f"{a}/actions/jobs/{j['job_id']}/logs"))
        except RuntimeError:
            text, lines, cut = "(log unavailable)", 1, False
        rows = text.split("\n")
        while rows and len("\n".join(rows).encode()) > budget:
            rows.pop(0)
            cut = True
        text, lines = "\n".join(rows), len(rows)
        tails[j["job_id"]] = text
        budget -= len(text.encode()) + 1
        j["tail_lines"], j["tail_truncated"] = lines, cut
    return tails


def skip(reason):
    return {"action": "skip", "reason": reason}


def run(gh, event_name, event, repository, now):
    """Post or update the marker comment for one workflow_run event. Returns what it did."""
    m = re.fullmatch(rf"{OWNER}/(vws-[a-z0-9-]+)", repository or "")
    if not m:
        return skip("repository outside popododo0720/vws-*")
    repo, a = m.group(1), f"repos/{repository}"
    wr = (event or {}).get("workflow_run") or {}
    branch, sha = wr.get("head_branch") or "", wr.get("head_sha") or ""
    if event_name != "workflow_run" or wr.get("event") != "pull_request":
        return skip("not a pull_request workflow_run")
    if wr.get("conclusion") not in FAILED:
        return skip(f"conclusion {wr.get('conclusion')}")
    if (wr.get("head_repository") or {}).get("full_name") != repository or branch in ("", "main", "master"):
        return skip("not a same-repo PR head")
    if not SHA_RE.fullmatch(sha):
        return skip("bad head sha")
    prs = [p for p in gh.get_all(f"{a}/pulls?state=open&head={OWNER}:{urllib.parse.quote(branch, safe='/')}&per_page=100")
           if p["head"]["sha"] == sha and (p["head"].get("repo") or {}).get("full_name") == repository]
    if len(prs) != 1:
        return skip(f"{len(prs)} open PRs with this head")
    p = prs[0]
    pr, labels, body = p["number"], [lb["name"] for lb in p.get("labels", [])], p.get("body") or ""

    wf = str(wr.get("name") or "")
    run_entry = {"workflow": wf, "run_id": int(wr["id"]), "run_attempt": int(wr.get("run_attempt") or 1),
                 "conclusion": wr["conclusion"], "url": wr.get("html_url") or ""}
    jobs = gh.get(f"{a}/actions/runs/{run_entry['run_id']}/attempts/{run_entry['run_attempt']}/jobs?per_page=100")
    new_jobs = [{"workflow": wf, "job": str(j.get("name") or ""), "job_id": int(j["id"]), "conclusion": j["conclusion"],
                 "url": j.get("html_url") or "", "tail_lines": 0, "tail_truncated": False}
                for j in jobs.get("jobs", []) if j.get("conclusion") in FAILED]

    comments = gh.get_all(f"{a}/issues/{pr}/comments?per_page=100")
    for c, rec in bot_records(comments, repo, pr):
        if rec["head_sha"] == sha:
            rec["runs"] = [r for r in rec["runs"] if r["workflow"] != wf] + [run_entry]
            rec["failed_jobs"] = [j for j in rec["failed_jobs"] if j["workflow"] != wf] + new_jobs
            rec["updated_at"] = now
            esc = re.search(r"^\*\*needs-human\*\*: .*$", c["body"], re.M)
            gh.patch(f"{a}/issues/comments/{c['id']}",
                     {"body": format_comment(rec, fetch_tails(gh, a, rec["failed_jobs"]), esc.group(0) if esc else "")})
            return {"action": "patch", "pr": pr, "n": rec["n"], "wake": rec["wake"]}

    chain = chain_of(pr, branch, body, gh.get_all(f"{a}/pulls?state=all&per_page=100"), repository)
    history = {n: (comments if n == pr else gh.get_all(f"{a}/issues/{n}/comments?per_page=100"),
                   gh.get_all(f"{a}/issues/{n}/events?per_page=100")) for n in chain}
    since = max(reset_at(cs, es) for cs, es in history.values())
    prior = []
    for n in chain:
        for c, rec in bot_records(history[n][0], repo, n):
            if c["created_at"] > since and rec["head_sha"] != sha and rec["head_sha"] not in prior:
                prior.append(rec["head_sha"])
    count = len(prior) + 1
    owner, agent, source = resolve_owner(repo, labels, body, branch)
    held = NEEDS_HUMAN in labels
    wake = count <= CAP and owner in WAKEABLE and not held
    escalate = count == CAP + 1 and not held
    rec = {"v": 1, "repo": repo, "pr": pr, "head_sha": sha, "head_branch": branch, "owner": owner, "agent": agent,
           "owner_source": source, "chain": chain, "n": count, "cap": CAP, "wake": wake, "runs": [run_entry],
           "failed_jobs": new_jobs, "created_at": now, "updated_at": now}
    esc = ""
    if escalate:
        shas = ", ".join(s[:7] for s in prior + [sha])
        esc = (f"**needs-human**: failing head {count} in this chain ({shas}); auto-rework stopped. "
               f"Remove the `{NEEDS_HUMAN}` label to allow {CAP} more.")
    gh.post(f"{a}/issues/{pr}/comments", {"body": format_comment(rec, fetch_tails(gh, a, new_jobs), esc)})
    if escalate:
        gh.post(f"{a}/issues/{pr}/labels", {"labels": [NEEDS_HUMAN]})
    return {"action": "post", "pr": pr, "n": count, "wake": wake, "owner": owner, "agent": agent}


class Gh:
    """`gh api` with a timeout; reads retry with backoff, writes are one try."""

    def __init__(self, timeout=60, tries=3, pages=10):
        self.timeout, self.tries, self.pages = timeout, tries, pages

    def _call(self, args, data=None, write=False):
        err = ""
        for i in range(1 if write else self.tries):
            try:
                r = subprocess.run(["gh", "api", *args], input=data, capture_output=True, text=True, timeout=self.timeout)
                if r.returncode == 0:
                    return r.stdout
                err = r.stderr
            except subprocess.TimeoutExpired:
                err = "timeout"
            if not write and i < self.tries - 1:
                time.sleep(2 ** (i + 1))
        raise RuntimeError(f"gh api {args[-1] if not write else args[2]} failed: {mask(err)[:300]}")

    def get(self, path):
        return json.loads(self._call([path]))

    def text(self, path):
        return self._call([path])

    def get_all(self, path):
        out = []
        for page in range(1, self.pages + 1):
            batch = self.get(f"{path}&page={page}")
            out += batch
            if len(batch) < 100:
                break
        return out

    def post(self, path, payload):
        return self._call(["-X", "POST", path, "--input", "-"], json.dumps(payload), write=True)

    def patch(self, path, payload):
        return self._call(["-X", "PATCH", path, "--input", "-"], json.dumps(payload), write=True)


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps(run(Gh(), os.environ.get("GITHUB_EVENT_NAME", ""), event, os.environ.get("GITHUB_REPOSITORY", ""), now)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
