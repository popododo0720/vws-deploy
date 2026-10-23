#!/usr/bin/env python3
"""CI-failure marker comments for the vws-* repos (vws-deploy#412).

One comment per (repo, PR, head SHA). The first line is a machine-readable marker, the last
fenced block is the JSON record, and everything between them is informative. A caller workflow
in each repo triggers on `workflow_run` and calls `.github/workflows/ci-fail.yml`, which runs
this script with the caller's GITHUB_TOKEN.

The comment is the only event that wakes an owner: a POST is an `issue_comment` created, a PATCH
is not. Reruns of the same SHA merge their failed jobs into the same comment.

Python 3 stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BOT_LOGIN = "github-actions[bot]"
CAP = 3
MAX_COMMENT_BYTES = 16 * 1024
MAX_TAIL_LINES = 40
MAX_LINE = 200
MAX_TAIL_BYTES = 4 * 1024
MAX_TAILS = 5
API_VERSION = "2022-11-28"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
RETRY_ATTEMPTS = 3

# The exact line-1 format from vws-deploy#412. Any change is a schema change (v=2).
MARKER_RE = re.compile(
    r"\A<!-- vws-ci-fail sha=([0-9a-f]{40}) v=(\d+) repo=(vws-[a-z0-9-]+) "
    r"pr=(\d+) owner=([a-z0-9:-]+) n=(\d+) wake=(true|false) state=(failing|passed) -->$")
FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
SUCCESS_CONCLUSIONS = frozenset({"success"})
AGENT_RE = re.compile(r"(?im)^\s*Agent:\s*(\S.*?)\s*$")
VERSION_SUFFIX_RE = re.compile(r"-v\d+$")
SUPERSEDES_RE = re.compile(r"(?im)^\s*Supersedes\s+#(\d+)")
JSON_BLOCK_RE = re.compile(r"^```json\n(.*?)\n```$", re.M | re.S)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
TOKEN_RE = re.compile(r"gh[opsu]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|Bearer\s+\S+")
JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
AWS_KEY_RE = re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
URL_CREDENTIAL_RE = re.compile(r"(://)[^/\s:@]+:[^@\s]+@")
KEY_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|authorization)"
    r"(\s*[:=]\s*)\S+")


# ---------------------------------------------------------------- values

def utc(value):
    """A timezone-aware UTC datetime from an RFC 3339 string, a datetime, or None."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if not value:
        return dt.datetime.fromtimestamp(0, dt.timezone.utc)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def rfc3339(value):
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def marker_line(record):
    return (f"<!-- vws-ci-fail sha={record['head_sha']} v={record['v']} repo={record['repo']} "
            f"pr={record['pr']} owner={record['owner']} n={record['n']} "
            f"wake={'true' if record['wake'] else 'false'} state={record['state']} -->")


def parse_marker(body):
    """The marker fields of a comment, or None when line 1 is not a marker."""
    first = (body or "").split("\n", 1)[0].rstrip("\r")
    m = MARKER_RE.match(first)
    if not m:
        return None
    return {"sha": m.group(1), "v": int(m.group(2)), "repo": m.group(3), "pr": int(m.group(4)),
            "owner": m.group(5), "n": int(m.group(6)), "wake": m.group(7) == "true", "state": m.group(8)}


def record_from_comment(body):
    """The last ```json block of a comment, parsed, or None."""
    blocks = JSON_BLOCK_RE.findall(body or "")
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- owner and chain

def owner_of(labels, body, branch, repo):
    """(owner, owner_source) by the first-match rules of vws-deploy#412 section 5."""
    labels = set(labels or [])
    if "grok" in labels:
        return repo, "label:grok"
    if "deepseek" in labels:
        return "deepseek", "label:deepseek"
    m = AGENT_RE.search(body or "")
    if m:
        value = " ".join(m.group(1).split()).lower()
        if value.startswith("deepseek"):
            return "deepseek", "agent_line"
        vm = re.match(r"(vws-[a-z0-9-]+)", value)
        if vm:
            return vm.group(1), "agent_line"
        if re.search(r"\beggbot\b", value):
            return "eggbot", "agent_line"
        return "none", "agent_line"
    if (branch or "").startswith("deepseek/"):
        return "deepseek", "branch_prefix"
    return "none", "none"


def wakeable(owner, repo):
    """DeepSeek wakes the orchestrator; vws-* wakes that repo's own Grok routine."""
    return owner == "deepseek" or (owner == repo and bool(re.fullmatch(r"vws-[a-z0-9-]+", owner)))


def has_needs_human(labels):
    return any((l or {}).get("name") == "needs-human" for l in labels or [])


def branch_base(ref):
    return VERSION_SUFFIX_RE.sub("", ref or "")


def chain_numbers(pulls, number, body):
    """PR numbers in the head chain, oldest first, this PR last."""
    out = {number}
    me = next((p for p in pulls or [] if p.get("number") == number), None)
    if me:
        base = branch_base((me.get("head") or {}).get("ref"))
        if base:
            for p in pulls:
                if branch_base((p.get("head") or {}).get("ref")) == base:
                    out.add(p["number"])
    for m in SUPERSEDES_RE.finditer(body or ""):
        out.add(int(m.group(1)))
    return [n for n in sorted(out) if n != number] + [number]


def reset_time(events):
    """The newest `needs-human` unlabeled event, the human reset."""
    times = [utc(e.get("created_at")) for e in events or []
             if e.get("event") == "unlabeled" and ((e.get("label") or {}).get("name") == "needs-human")]
    return max(times, default=dt.datetime.fromtimestamp(0, dt.timezone.utc))


def failing_shas(comments_by_pr, reset):
    """Distinct failing head SHAs in the chain after the reset, as {sha: created_at}."""
    out = {}
    for comments in (comments_by_pr or {}).values():
        for c in comments:
            if ((c.get("user") or {}).get("login")) != BOT_LOGIN:
                continue
            m = parse_marker(c.get("body"))
            if not m or m["state"] != "failing":
                continue
            if utc(c.get("created_at")) <= reset:
                continue
            out.setdefault(m["sha"], c.get("created_at"))
    return out


# ---------------------------------------------------------------- log tail

def mask(text):
    text = PRIVATE_KEY_RE.sub("[redacted]", text)
    text = URL_CREDENTIAL_RE.sub(r"\1[redacted]@", text)
    text = TOKEN_RE.sub("[redacted]", text)
    text = JWT_RE.sub("[redacted]", text)
    text = AWS_KEY_RE.sub("[redacted]", text)
    return KEY_VALUE_RE.sub(r"\1\2[redacted]", text)


def neutralize(text):
    """Make log text unable to forge a marker or ping someone in the comment."""
    text = text.replace("<!--", "<!-\u200b-")
    text = text.replace("-->", "-\u200b->")
    text = text.replace("@", "@\u200b")
    return re.sub(r"~{4,}", "~~~", text)


def clean_tail(text):
    """(tail, lines, truncated) for one job log: last 40 lines, each 200 chars, 4 KiB, masked."""
    lines = []
    for line in (text or "").splitlines():
        line = ANSI_RE.sub("", line)
        line = TIMESTAMP_RE.sub("", line)
        if line.startswith("##[group]") or line.startswith("##[endgroup]") or "::add-mask::" in line:
            continue
        lines.append(line)
    truncated = len(lines) > MAX_TAIL_LINES
    lines = lines[-MAX_TAIL_LINES:]
    out = []
    for line in lines:
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + "\u2026"
            truncated = True
        out.append(line)
    value = neutralize(mask("\n".join(out)))
    raw = value.encode("utf-8")
    if len(raw) > MAX_TAIL_BYTES:
        value = raw[-MAX_TAIL_BYTES:].decode("utf-8", "ignore")
        if "\n" in value:
            value = value.split("\n", 1)[1]
        truncated = True
    value = value.rstrip("\n")
    return value, (len(value.splitlines()) if value else 0), truncated


# ---------------------------------------------------------------- rendering

def _body(record, jobs, tails, escalation, listed):
    sha7 = record["head_sha"][:7]
    if record["state"] == "passed":
        lines = [marker_line(record),
                 f"**CI passed** on `{sha7}` ({record['repo']}#{record['pr']}, owner "
                 f"`{record['owner']}`, rework {record['n']} of {record['cap']})",
                 "", "A later rerun of this head passed; no rework is needed."]
    else:
        lines = [marker_line(record),
                 f"**CI failed** on `{sha7}` ({record['repo']}#{record['pr']}, owner "
                 f"`{record['owner']}`, rework {record['n']} of {record['cap']})"]
    lines += ["", "Failed jobs:"]
    shown = jobs[:listed]
    for j in shown:
        lines.append(f"- `{j['workflow']} / {j['job']}`: {j['conclusion']} ({j['url']})")
    if not shown:
        lines.append("- none")
    elif len(jobs) > len(shown):
        lines.append(f"- ... and {len(jobs) - len(shown)} more")
    for j in shown:
        tail = tails.get(j["job_id"])
        if not tail:
            continue
        lines += ["", f"<details><summary>`{j['workflow']} / {j['job']}` log tail "
                       f"(last {j['tail_lines']} lines, masked)</summary>", "",
                  "~~~~text", tail, "~~~~", "</details>"]
    if escalation:
        lines += ["", escalation]
    lines += ["", "```json", json.dumps(record, indent=2, ensure_ascii=False), "```"]
    return "\n".join(lines)


def render(record, jobs, tails, escalation=""):
    """Fit the comment under 16 KiB by dropping tails, then job-list lines, never the record."""
    listed = len(jobs)
    while True:
        text = _body(record, jobs, tails, escalation, listed)
        if len(text.encode("utf-8")) <= MAX_COMMENT_BYTES:
            return text
        dropped = False
        for j in reversed(jobs):
            if j["job_id"] in tails:
                del tails[j["job_id"]]
                j["tail_lines"] = 0
                j["tail_truncated"] = True
                dropped = True
                break
        if dropped:
            continue
        if listed > 20:
            listed = 20
        elif listed > 0:
            listed = 0
        else:
            raise ValueError("comment record is over 16 KiB")


# ---------------------------------------------------------------- GitHub API

class GhError(Exception):
    def __init__(self, status, path, detail):
        super().__init__(f"GitHub API {status} for {path}: {detail}")
        self.status = status


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """A log download redirects to a signed blob URL; never send the GitHub token there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


class Client:
    def __init__(self, repo, token, api="https://api.github.com"):
        self.repo = repo
        self.name = repo.split("/", 1)[1] if "/" in repo else repo
        self.token = token
        self.api = api.rstrip("/")
        self.opener = urllib.request.build_opener(_NoAuthRedirect)

    def _request(self, method, path, params=None, data=None, text=False, retry=True):
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        for attempt in range(RETRY_ATTEMPTS):
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", API_VERSION)
            req.add_header("User-Agent", "vws-ci-fail")
            try:
                with self.opener.open(req, timeout=60) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code in (404, 410):
                    return None
                if retry and exc.code in RETRY_STATUS and attempt + 1 < RETRY_ATTEMPTS:
                    time.sleep(2 ** attempt)
                    continue
                raise GhError(exc.code, path, detail) from exc
        if text:
            return raw.decode("utf-8", "replace")
        return json.loads(raw) if raw else None

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def get_all(self, path, params=None):
        page, out = 1, []
        params = dict(params or {})
        params["per_page"] = params.get("per_page", 100)
        while page <= 20:
            params["page"] = page
            data = self.get(path, params)
            if not data:
                break
            out += data
            if len(data) < params["per_page"]:
                break
            page += 1
        return out

    def get_text(self, path):
        return self._request("GET", path, text=True)

    def post(self, path, data, retry=True):
        return self._request("POST", path, data=data, retry=retry)

    def patch(self, path, data):
        return self._request("PATCH", path, data=data)


# ---------------------------------------------------------------- collection

def find_pr(client, sha):
    pulls = client.get_all(f"repos/{client.repo}/commits/{sha}/pulls")
    open_pulls = [p for p in pulls if p.get("state") == "open"
                  and ((p.get("head") or {}).get("sha") == sha)]
    return open_pulls[0] if len(open_pulls) == 1 else None


def find_marker(client, number, sha):
    for c in client.get_all(f"repos/{client.repo}/issues/{number}/comments"):
        if ((c.get("user") or {}).get("login")) != BOT_LOGIN:
            continue
        m = parse_marker(c.get("body"))
        if m and m["sha"] == sha:
            return c
    return None


def latest_runs(client, sha, names):
    """Newest attempt per workflow name on this SHA, as {name: run}."""
    out = {}
    for r in client.get_all(f"repos/{client.repo}/actions/runs", {"head_sha": sha}):
        if (r.get("head_sha") or sha) != sha:
            continue
        name = r.get("name") or ""
        if names and name not in names:
            continue
        old = out.get(name)
        if old is None or (r.get("run_attempt") or 1, r.get("id") or 0) > (old.get("run_attempt") or 1, old.get("id") or 0):
            out[name] = r
    return out


def failed_runs(client, sha, names, event_run=None):
    """Newest attempt per workflow that is still failing on this SHA."""
    latest = latest_runs(client, sha, names)
    if event_run and (not names or event_run.get("name") in names):
        name = event_run.get("name") or ""
        old = latest.get(name)
        # The list is authoritative for a run it already holds (same attempt == same run). The
        # event is a fallback for a list that has not caught up with this just-finished attempt.
        if old is None or (event_run.get("run_attempt") or 1) > (old.get("run_attempt") or 1):
            if event_run.get("conclusion") in FAIL_CONCLUSIONS:
                latest[name] = event_run
            else:
                latest.pop(name, None)
    return [latest[name] for name in sorted(latest) if latest[name].get("conclusion") in FAIL_CONCLUSIONS]


def failed_jobs(client, run):
    """Failed jobs of one run attempt, in API order."""
    run_id = run.get("id")
    attempt = run.get("run_attempt") or 1
    path = f"repos/{client.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
    data = client.get_all(path)
    out = []
    for j in data or []:
        if j.get("conclusion") in FAIL_CONCLUSIONS:
            out.append({"workflow": run.get("name") or "", "job": j.get("name") or "",
                        "job_id": j.get("id"), "conclusion": j.get("conclusion"),
                        "url": j.get("html_url") or "", "tail_lines": 0, "tail_truncated": False})
    return out


def collect_jobs(client, runs):
    jobs, tails = [], {}
    for run in runs:
        jobs += failed_jobs(client, run)
    for job in jobs[:MAX_TAILS]:
        text = client.get_text(f"repos/{client.repo}/actions/jobs/{job['job_id']}/logs")
        if not text:
            continue
        tail, lines, truncated = clean_tail(text)
        if tail:
            tails[job["job_id"]] = tail
            job["tail_lines"], job["tail_truncated"] = lines, truncated
    return jobs, tails


def run_record(run):
    return {"workflow": run.get("name") or "", "run_id": run.get("id"),
            "run_attempt": run.get("run_attempt") or 1, "conclusion": run.get("conclusion"),
            "url": run.get("html_url") or ""}


# ---------------------------------------------------------------- the tick

def _skip(reason):
    return {"action": "skip", "reason": reason}


def mark_passed(client, pr, sha, marker, now):
    record = record_from_comment(marker.get("body"))
    if not record:
        return _skip("marker has no record")
    if record.get("state") == "passed":
        return _skip("marker is already passed")
    failed = [r.get("workflow") for r in record.get("runs", [])]
    if not failed:
        return _skip("marker has no failed runs")
    latest = latest_runs(client, sha, failed)
    if any(latest.get(name, {}).get("conclusion") not in SUCCESS_CONCLUSIONS for name in failed):
        return _skip("a failed workflow has not rerun green")
    record["state"] = "passed"
    record["updated_at"] = rfc3339(now)
    jobs = [dict(j, tail_lines=0) for j in record.get("failed_jobs", [])]
    record["failed_jobs"] = jobs
    body = render(record, jobs, {})
    client.patch(f"repos/{client.repo}/issues/comments/{marker['id']}", {"body": body})
    return {"action": "passed", "comment": marker["id"], "pr": pr["number"], "sha": sha}


def run(client, event, workflows, now=None):
    """Handle one workflow_run event. Returns a small result dict; raises on API errors."""
    now = now or dt.datetime.now(dt.timezone.utc)
    wr = (event or {}).get("workflow_run") or {}
    if not wr:
        return _skip("not a workflow_run event")
    if wr.get("event") != "pull_request":
        return _skip("not a pull_request run")
    repo = client.name
    branch = wr.get("head_branch") or ""
    default = ((event.get("repository") or {}).get("default_branch")) or "main"
    if not branch or branch == default:
        return _skip("not a PR head branch")
    sha = wr.get("head_sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return _skip("no head SHA")
    if not workflows:
        return _skip("no tracked workflow list")
    name = wr.get("name") or ""
    if workflows and name not in workflows:
        return _skip(f"workflow {name!r} is not tracked")
    conclusion = wr.get("conclusion") or ""
    if conclusion not in FAIL_CONCLUSIONS and conclusion not in SUCCESS_CONCLUSIONS:
        return _skip(f"conclusion {conclusion!r} is neither failed nor success")

    pr = find_pr(client, sha)
    if not pr:
        return _skip("no single open PR for this SHA")
    marker = find_marker(client, pr["number"], sha)
    if conclusion in SUCCESS_CONCLUSIONS:
        if not marker:
            return _skip("no marker for this SHA")
        return mark_passed(client, pr, sha, marker, now)

    if marker:
        record = record_from_comment(marker.get("body")) or {}
        old = parse_marker(marker.get("body")) or {}
        runs = failed_runs(client, sha, workflows, wr)
        jobs, tails = collect_jobs(client, runs)
        record.update({
            "v": 1, "repo": repo, "pr": pr["number"], "head_sha": sha, "head_branch": branch,
            "owner": record.get("owner") or old.get("owner") or "none",
            "owner_source": record.get("owner_source") or "none",
            "chain": record.get("chain") or [pr["number"]],
            "n": int(record.get("n") or old.get("n") or 1), "cap": CAP,
            "wake": bool(record.get("wake", old.get("wake", False))),
            "state": "failing", "runs": [run_record(r) for r in runs],
            "failed_jobs": jobs, "created_at": record.get("created_at") or marker.get("created_at"),
            "updated_at": rfc3339(now),
        })
        escalation = ""
        if record["n"] == 4:
            found = re.search(r"(?m)^\*\*needs-human\*\*: .*$", marker.get("body") or "")
            escalation = found.group(0) if found else ""
        body = render(record, jobs, tails, escalation)
        client.patch(f"repos/{client.repo}/issues/comments/{marker['id']}", {"body": body})
        return {"action": "updated", "comment": marker["id"], "pr": pr["number"], "sha": sha,
                "runs": len(runs), "jobs": len(jobs)}

    owner, source = owner_of([l.get("name") for l in pr.get("labels", [])], pr.get("body"),
                             branch, repo)
    pulls = [p for p in client.get_all(f"repos/{client.repo}/pulls", {"state": "all"})
             if ((p.get("head") or {}).get("repo") or {}).get("full_name") == client.repo]
    chain = chain_numbers(pulls, pr["number"], pr.get("body"))
    events = client.get_all(f"repos/{client.repo}/issues/{pr['number']}/events")
    failing = failing_shas({n: client.get_all(f"repos/{client.repo}/issues/{n}/comments") for n in chain},
                           reset_time(events))
    n = 1 + len(failing)
    labeled = has_needs_human(pr.get("labels"))
    wake = n <= CAP and wakeable(owner, repo) and not labeled
    escalation = ""
    if n == 4 and not labeled:
        try:
            client.post(f"repos/{client.repo}/issues/{pr['number']}/labels", {"labels": ["needs-human"]})
        except GhError as exc:
            if exc.status == 422:
                print(f"warning: needs-human label is missing on {repo}", file=sys.stderr)
            else:
                raise
        ordered = sorted(failing, key=lambda s: str(failing[s])) + [sha]
        escalation = ("**needs-human**: 4th failing head in this chain ("
                      f"{', '.join(s[:7] for s in ordered)}); auto-rework stopped. "
                      "Remove the label to allow 3 more.")
    runs = failed_runs(client, sha, workflows, wr)
    jobs, tails = collect_jobs(client, runs)
    record = {
        "v": 1, "repo": repo, "pr": pr["number"], "head_sha": sha, "head_branch": branch,
        "owner": owner, "owner_source": source, "chain": chain, "n": n, "cap": CAP,
        "wake": wake, "state": "failing", "runs": [run_record(r) for r in runs],
        "failed_jobs": jobs, "created_at": rfc3339(now), "updated_at": rfc3339(now),
    }
    body = render(record, jobs, tails, escalation)
    posted = client.post(f"repos/{client.repo}/issues/{pr['number']}/comments", {"body": body},
                         retry=False)
    return {"action": "created", "comment": (posted or {}).get("id"), "pr": pr["number"], "sha": sha,
            "n": n, "wake": wake, "runs": len(runs), "jobs": len(jobs)}


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description="Update the vws CI-failure marker comment.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="handle one workflow_run event")
    run_parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"),
                            help="event payload path (default: GITHUB_EVENT_PATH)")
    run_parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    run_parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    run_parser.add_argument("--workflows", default=os.environ.get("CI_FAIL_WORKFLOWS", ""))
    run_parser.add_argument("--now", default="", help="RFC 3339 clock override for tests")
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error("unknown command")
    if not args.repo or "/" not in args.repo:
        parser.error("GITHUB_REPOSITORY or --repo is required")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    if not args.event:
        parser.error("GITHUB_EVENT_PATH or --event is required")
    with open(args.event, encoding="utf-8") as fh:
        event = json.load(fh)
    workflows = [w.strip() for w in args.workflows.split(",") if w.strip()]
    if not workflows:
        parser.error("CI_FAIL_WORKFLOWS or --workflows is required")
    now = utc(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    result = run(Client(args.repo, token, args.api_url), event, workflows, now)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
