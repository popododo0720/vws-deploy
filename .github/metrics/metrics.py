#!/usr/bin/env python3
"""Daily factory metrics for the vws-* repos (vws-deploy#345). Python 3 stdlib only, no LLM.

For one KST day it reads GitHub (box gh login, GH_TOKEN removed) and the orchestrator's log, computes
the numbers with a pure function, and writes metrics/daily/YYYY-MM-DD.json plus a rolling summary.
Dry-run (the default) writes only to the local state directory. With METRICS_MODE=apply it adds one
fast-forward commit to the orphan branch factory-metrics of vws-deploy through the Git Data API
(force: false). It never writes main and never opens a PR.

It reuses the orchestrator's gh wrapper (allowlist, ETag cache, retries), log format, config file,
rate floor and comment parsers; see docs/factory-metrics.md.

Nothing polls the metrics. On a normal day the script is silent: it only appends to the branch. When a
day is abnormal (stalled tasks, a failure or grok-escalation rate well above the trailing baseline, a
token-usage spike; thresholds are the constants below) it posts one comment whose first line is
<!-- vws-orchestrate:metrics-alert --> on the status issue (ORCHESTRATE_STATUS_ISSUE, vws-deploy#416), and,
when VWS_ALERT_WEBHOOK_URL and VWS_ALERT_WEBHOOK_KEY are both in the environment, POSTs a small JSON to
that webhook (best effort: a failed POST is logged and never fails the run; the key is never logged).

Commands:
  --if-due             the loop calls this after each tick: publish the oldest unpublished day of the
                       last METRICS_BACKFILL_DAYS once METRICS_TIME (KST) has passed; no gh call when nothing is due
  --date D             compute day D (YYYY-MM-DD, KST) now
  --recompute D        like --date, and in apply mode replace a published file whose content changed
  --init               create the orphan branch factory-metrics (apply mode only; once)

Exit status: 0 done, skipped or not due; 2 usage or config error; 4 failed (GitHub, budget); 5 refused
(foreign commit on the branch, non-ASCII output); 1 is an uncaught Python error.
"""
import argparse
import base64
import datetime as dt
import fcntl
from fractions import Fraction
import glob
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH_PATH = os.path.join(HERE, "..", "orchestrate", "orchestrate.py")
_spec = importlib.util.spec_from_file_location("orchestrate", ORCH_PATH)
orch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orch)

OWNER = orch.OWNER
DATA_REPO = "vws-deploy"
BRANCH = "factory-metrics"
KST = dt.timezone(dt.timedelta(hours=9), "KST")
FAIL_EXIT, REFUSE_EXIT = 4, 5
VERIFY_APP_ID = 5313004  # the vws-verify GitHub App; its check run is named vws-verify
GENERATOR_TRAILER = "Metrics-Generator: vws-metrics"
DAILY_SUBJECT_RE = re.compile(r"^metrics: daily [0-9]{4}-[0-9]{2}-[0-9]{2}$")
INIT_SUBJECT = "metrics: start the factory-metrics branch"
AGENT_RE = re.compile(r"(?im)^Agent:[ \t]*([A-Za-z0-9._-]{1,40})[ \t]*$")
CODEX_ROUNDS_RE = re.compile(r"(?i)Codex review, ([0-9]+) rounds?")
CODEX_BLOCKING_RE = re.compile(r"(?m)Codex review: \S+ blocking=([0-9]+)")
QUEUE_LABELS = ("ready", "deepseek", "grok", "blocked", "in-review", "priority/blocker")
CI_FAIL = {"failure", "timed_out", "startup_failure"}
CI_EVENTS = {"pull_request", "push", "merge_group"}
SEPARATE_WORKFLOWS = {"repo-guard"}  # red by design until vws-verify approves; reported apart
VOLATILE = ("generated_at", "release", "snapshot_at", "queue_depth", "stalls", "parked", "alerts", "content_sha256")
SUMMARY_DAYS = 28
# Orchestrator markers of comments that hand a task to grok (vws-deploy#400): the Rule C swap notice and
# the Rule G grok wake's last line <!-- orchestrate:grok-wake item=... --> (vws-deploy#378).
GROK_DISPATCH_MARKERS = ("swap", "grok-wake")
STATE_KEEP_DAYS = 60  # done days kept per mode in metrics-state.json
ALERT_MARKER = "<!-- vws-orchestrate:metrics-alert -->"
WEBHOOK_TIMEOUT_S, WEBHOOK_TRIES = 10, 3
SNAPSHOT_GAP = "queue_depth and stalls are a live snapshot; not available for a back-filled day"

# Anomaly thresholds (tested in tests/metrics/test_alerts.py). A normal day raises no alert.
STALL_HOURS = 48           # an open task or PR with no update for this long is stalled
STALL_LABELS = ("ready", "deepseek", "grok", "in-review")  # tasks; parked ones wait by design
PARK_LABELS = tuple(orch.OPT_OUT_LABELS) + ("needs-human",)  # vws-deploy#463
BASELINE_DAYS = 7          # trailing days (before D) that form the baseline
BASELINE_MIN_DAYS = 3      # fewer days with data: no rate or token alert (not enough history)
RATE_MIN_EVENTS = 5        # the day needs at least this many events in the rate's denominator
RATE_MIN_RISE = 0.20       # the day's rate is at least baseline + 0.20 ...
RATE_MIN_RATIO = 2.0       # ... and at least 2x the baseline (when the baseline is above 0)
TOKEN_SPIKE_RATIO = 3.0    # the day's tokens are at least 3x the trailing median ...
TOKEN_MIN_DAILY = 1_000_000  # ... and at least this many tokens

DEFAULTS = {
    "METRICS_MODE": "dry-run",
    "METRICS_TIME": "00:20",
    "METRICS_BACKFILL_DAYS": "7",
    "METRICS_MAX_GETS": "1500",
}

# ---------------------------------------------------------------- allowlist

_R = r"repos/" + OWNER + r"/vws-[a-z0-9-]+"
_D = r"repos/" + OWNER + "/" + DATA_REPO
_SHA = r"[0-9a-f]{40}"
ALLOWED = {
    "GET": [
        re.compile(r"^rate_limit$"),
        re.compile(rf"^{_R}$"),
        re.compile(rf"^{_R}/(pulls|issues|issues/comments|issues/events|actions/runs)\?.*$"),
        re.compile(rf"^{_R}/pulls/[0-9]+/commits(\?.*)?$"),
        re.compile(rf"^{_R}/commits/{_SHA}/check-runs(\?.*)?$"),
        re.compile(rf"^{_D}/git/ref/heads/{BRANCH}$"),
        re.compile(rf"^{_D}/git/trees/{_SHA}(\?recursive=1)?$"),
        re.compile(rf"^{_D}/git/blobs/{_SHA}$"),
        re.compile(rf"^{_D}/commits\?sha={_SHA}&per_page=100(&page=[0-9]+)?$"),
        re.compile(rf"^{_D}/commits/{_SHA}$"),
        re.compile(rf"^{_D}/issues/[0-9]+/comments\?per_page=100(&page=[0-9]+)?$"),
    ],
    "POST": [re.compile(rf"^{_D}/git/(trees|commits|refs)$"), re.compile(rf"^{_D}/issues/[0-9]+/comments$")],
    "PATCH": [re.compile(rf"^{_D}/git/refs/heads/{BRANCH}$")],
}


def allowed(method, path):
    return any(p.match(path) for p in ALLOWED.get(method, []))


class Budget(Exception):
    pass


class RateLow(Exception):
    pass


class MetricsGh(orch.Gh):
    """The orchestrator's wrapper with this script's allowlist, a GET budget and write-payload rules."""

    def __init__(self, cache_path=None, max_gets=1500, init=False, rate_floor=1000, status_issue=None, **kw):
        super().__init__(cache_path, allow=allowed, **kw)
        self.max_gets, self.init, self.rate_floor = max_gets, init, rate_floor
        self.alert_issue = int(status_issue) if status_issue else None

    def get(self, path):
        if not self.allow("GET", path):
            raise PermissionError(f"gh call not allowed: GET {path}")
        if self.stats["get"] >= self.max_gets:
            raise Budget(f"more than METRICS_MAX_GETS={self.max_gets} reads")
        if path != "rate_limit" and self.stats["get"] % 100 == 0:
            # Other agents share this login: check the floor before the first read and every 100 reads.
            r, _ = super().get("rate_limit")
            remaining = r["resources"]["core"]["remaining"]
            if remaining < self.rate_floor:
                raise RateLow(remaining)
        return super().get(path)

    def write(self, method, path, payload=None):
        if path.endswith("/comments"):
            # The only comment this script ever posts: a metrics alert on the status issue.
            body = (payload or {}).get("body") if isinstance(payload, dict) else None
            if not self.alert_issue or path != f"repos/{OWNER}/{DATA_REPO}/issues/{self.alert_issue}/comments" \
                    or not isinstance(body, str) or body.split("\n", 1)[0] != ALERT_MARKER:
                raise PermissionError("only a metrics-alert comment on the status issue may be posted")
        if path.endswith(f"/git/refs/heads/{BRANCH}") and (not isinstance(payload, dict) or payload.get("force") is not False):
            raise PermissionError("the factory-metrics ref moves only with force: false")
        if path.endswith("/git/refs"):
            if not self.init or not isinstance(payload, dict) or payload.get("ref") != f"refs/heads/{BRANCH}":
                raise PermissionError(f"only --init may create a ref, and only refs/heads/{BRANCH}")
        return super().write(method, path, payload)


# ---------------------------------------------------------------- time

def kst_day_window(day):
    """[start, end) of a KST day in epoch seconds."""
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST)
    return d.timestamp(), (d + dt.timedelta(days=1)).timestamp()


def kst_date(epoch):
    return dt.datetime.fromtimestamp(epoch, KST).date()


def due_days(now, cfg, done):
    """Pure. Unpublished days of the backfill window, oldest first. Day D is due from METRICS_TIME on D+1."""
    hh, mm = (int(x) for x in cfg["METRICS_TIME"].split(":"))
    local = dt.datetime.fromtimestamp(now, KST)
    latest = local.date() - dt.timedelta(days=1)
    if (local.hour, local.minute) < (hh, mm):
        latest -= dt.timedelta(days=1)
    n = int(cfg["METRICS_BACKFILL_DAYS"])
    days = [(latest - dt.timedelta(days=k)).isoformat() for k in range(n)]
    return sorted(d for d in days if d not in set(done))


# ---------------------------------------------------------------- collect (reads only)

def pages(gh, path, stop=None, limit=30):
    """Pages of a list endpoint, newest first; stop(item) True ends the walk after that page."""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        data, nxt = gh.get(f"{path}{sep}page={page}" if page > 1 else path)
        data = data or []
        if isinstance(data, dict):  # actions/runs wraps its list
            data = data.get("workflow_runs", [])
        out.extend(data)
        if not nxt or (stop and data and stop(data[-1])):
            return out
        if page >= limit:
            raise orch.GhError(0, f"more than {limit} pages at {path}")
        page += 1


def verify_runs(gh, repo, sha):
    data, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/check-runs?check_name=vws-verify&filter=all&per_page=100")
    return [{"id": r["id"], "conclusion": r.get("conclusion"), "completed_at": r.get("completed_at"),
             "started_at": r.get("started_at")}
            for r in (data or {}).get("check_runs", []) if (r.get("app") or {}).get("id") == VERIFY_APP_ID]


def collect(gh, repo_names, day, cfg, snapshot=False):
    """Every GitHub read for one day, reduced to the fields compute() uses."""
    start, end = kst_day_window(day)
    lookback = start - orch.num(cfg, "DEEPSEEK_TIMEOUT_HOURS") * 3600
    since = orch.iso(int(lookback) // 86400 * 86400)  # floored to the UTC day: a stable URL keeps ETags useful
    s_iso, e_iso = orch.iso(start), orch.iso(end - 1)
    raw = {"day": day, "repos": {}, "archived": []}
    for name in repo_names:
        meta, _ = gh.get(f"repos/{OWNER}/{name}")
        if meta.get("archived"):
            raw["archived"].append(name)
            continue
        r = raw["repos"][name] = {"prs": [], "comments": [], "events": [], "runs": [], "verify": {}, "merged_commits": {}}
        for p in pages(gh, f"repos/{OWNER}/{name}/pulls?state=all&sort=updated&direction=desc&per_page=100",
                       stop=lambda p: orch.utc(p["updated_at"]) < start):
            if orch.utc(p["updated_at"]) < start:
                continue
            r["prs"].append({"number": p["number"], "created_at": p["created_at"], "merged_at": p.get("merged_at"),
                             "closed_at": p.get("closed_at"), "head_sha": p["head"]["sha"], "body": p.get("body") or "",
                             "base": p["base"]["ref"], "draft": bool(p.get("draft"))})
        for c in pages(gh, f"repos/{OWNER}/{name}/issues/comments?since={since}&sort=created&direction=asc&per_page=100"):
            if lookback <= orch.utc(c["created_at"]) < end:
                r["comments"].append({"id": c["id"], "url": c["html_url"], "created_at": c["created_at"],
                                      "issue": int(c["issue_url"].rsplit("/", 1)[1]), "body": c.get("body") or "",
                                      "author": (c.get("user") or {}).get("login") or ""})
        for e in pages(gh, f"repos/{OWNER}/{name}/issues/events?per_page=100",
                       stop=lambda e: orch.utc(e["created_at"]) < start):
            if e.get("event") in ("labeled", "unlabeled") and start <= orch.utc(e["created_at"]) < end:
                r["events"].append({"event": e["event"], "label": (e.get("label") or {}).get("name"),
                                    "created_at": e["created_at"], "issue": (e.get("issue") or {}).get("number")})
        for w in pages(gh, f"repos/{OWNER}/{name}/actions/runs?created={s_iso}..{e_iso}&per_page=100"):
            r["runs"].append({"id": w["id"], "name": w.get("name") or "", "event": w.get("event"),
                              "status": w.get("status"), "conclusion": w.get("conclusion")})
        for p in r["prs"]:
            r["verify"][p["head_sha"]] = verify_runs(gh, name, p["head_sha"])
            if p["merged_at"] and start <= orch.utc(p["merged_at"]) < end:
                shas = [c["sha"] for c in pages(gh, f"repos/{OWNER}/{name}/pulls/{p['number']}/commits?per_page=100")]
                r["merged_commits"][str(p["number"])] = shas
                for sha in shas:
                    if sha not in r["verify"]:
                        r["verify"][sha] = verify_runs(gh, name, sha)
        if snapshot:
            r["open_issues"] = [{"number": i["number"], "labels": sorted(lb["name"] for lb in i.get("labels", [])),
                                 "is_pr": "pull_request" in i, "updated_at": i.get("updated_at"),
                                 "assignees": len(i.get("assignees") or [])}
                                for i in pages(gh, f"repos/{OWNER}/{name}/issues?state=open&per_page=100")]
            r["open_pulls"] = [{"number": p["number"], "draft": bool(p.get("draft")), "base": p["base"]["ref"],
                                "labels": sorted(lb["name"] for lb in p.get("labels", [])), "updated_at": p.get("updated_at")}
                               for p in pages(gh, f"repos/{OWNER}/{name}/pulls?state=open&per_page=100")]
    return raw


def read_orchestrator_log(log_dir, start, end):
    """Orchestrator log records in [start, end), and whether the log covers the whole window."""
    files = sorted(glob.glob(os.path.join(log_dir, "orchestrate.jsonl*")))
    recs, earliest = [], None
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    t = orch.utc(r["ts"])
                except (ValueError, KeyError):
                    continue
                earliest = t if earliest is None else min(earliest, t)
                if start <= t < end:
                    recs.append(r)
    covered = earliest is not None and earliest <= start
    return sorted(recs, key=lambda r: (r["ts"], r.get("event", ""))), covered


# ---------------------------------------------------------------- compute (pure)

def rate(num, den):
    return round(num / den, 3) if den else None


def stats(values):
    v = sorted(values)
    if not v:
        return {"n": 0, "median": None, "p90": None, "max": None}
    mid = len(v) // 2
    median = v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2
    p90 = v[max(0, -(-len(v) * 9 // 10) - 1)]  # nearest rank
    return {"n": len(v), "median": round(median, 2), "p90": round(p90, 2), "max": round(v[-1], 2)}


def agent_of(body):
    m = AGENT_RE.search(body or "")
    return m.group(1).lower() if m else "unattributed"


def bump(d, *keys):
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = d.get(keys[-1], 0) + 1


def is_parked(issue, key, grok_allow):
    """vws-deploy#463: an open issue that waits by design, so it is never a stall. It has an opt-out label
    or needs-human, or it is Grok work no rule may pick up: grok, no deepseek, no assignee (an assignee
    holds a Grok slot, Rule G) and not on ORCHESTRATE_GROK_ALLOWLIST."""
    labels = set(issue["labels"])
    if labels & set(PARK_LABELS):
        return True
    return ("grok" in labels and "deepseek" not in labels and not issue.get("assignees")
            and key not in grok_allow)


def compute(raw, log_recs, log_covered, cfg, exclusions=frozenset(), snapshot_at=None, tokens=None, lead_wakes=None):
    """Pure: the same inputs give the same output."""
    day = raw["day"]
    start, end = kst_day_window(day)
    inwin = lambda ts: ts is not None and start <= orch.utc(ts) < end
    timeout = orch.num(cfg, "DEEPSEEK_TIMEOUT_HOURS") * 3600
    prs = {"opened": {}, "merged": {}, "closed_unmerged": {}, "by_agent": {"opened": {}, "merged": {}}}
    lead_all, lead_repo = [], {}
    verify = {"runs": 0, "success": 0, "failure": 0, "first_pass": 0, "first_pass_n": 0, "approved_labels": 0}
    ds = {"dispatched": 0, "done": 0, "failed": 0, "timeout": 0, "infra": 0, "excluded": 0, "duplicate_reports": 0,
          "by_repo": {}}
    grok = 0
    wakes = {"verify_wake_comments": 0, "dispatches": {"deepseek": 0, "grok": 0}, "rework_dispatches": 0,
             "lead_wakes": lead_wakes}
    ci = {"by_repo": {}, "separate": {}}
    codex = {"merged_prs_with_codex": 0, "rounds_declared": 0, "verify_reports": 0, "blocking_sum": 0}
    queue = {lb: 0 for lb in QUEUE_LABELS} if snapshot_at else None
    stalls = [] if snapshot_at else None
    parked = 0 if snapshot_at else None
    grok_allow, _ = orch.grok_allowlist(cfg.get("ORCHESTRATE_GROK_ALLOWLIST", ""))
    if queue is not None:
        queue["verify_backlog"] = 0
    for name in sorted(raw["repos"]):
        r = raw["repos"][name]
        for p in r["prs"]:
            agent = agent_of(p["body"])
            if inwin(p["created_at"]):
                bump(prs["opened"], name, agent)
                bump(prs["by_agent"]["opened"], agent)
            if inwin(p["merged_at"]):
                bump(prs["merged"], name, agent)
                bump(prs["by_agent"]["merged"], agent)
                h = (orch.utc(p["merged_at"]) - orch.utc(p["created_at"])) / 3600
                lead_all.append(h)
                lead_repo.setdefault(name, []).append(h)
                body = p["body"]
                if "codex review" in body.lower():
                    codex["merged_prs_with_codex"] += 1
                codex["rounds_declared"] += sum(int(x) for x in CODEX_ROUNDS_RE.findall(body))
                runs = sorted((run for sha in r["merged_commits"].get(str(p["number"]), [])
                               for run in r["verify"].get(sha, []) if run["completed_at"]),
                              key=lambda run: (run["completed_at"], run["id"]))
                runs = [run for run in runs if run["conclusion"] in ("success", "failure")]
                if runs:
                    verify["first_pass_n"] += 1
                    verify["first_pass"] += runs[0]["conclusion"] == "success"
            elif p["merged_at"] is None and inwin(p["closed_at"]):
                bump(prs["closed_unmerged"], name)
        seen = set()
        for sha in sorted(r["verify"]):
            for run in r["verify"][sha]:
                if run["id"] in seen or not inwin(run["completed_at"]):
                    continue
                seen.add(run["id"])
                verify["runs"] += 1
                if run["conclusion"] in ("success", "failure"):
                    verify[run["conclusion"]] += 1
        for e in r["events"]:
            if not inwin(e["created_at"]):
                continue
            if e["event"] == "labeled" and e["label"] == "verify-approved":
                verify["approved_labels"] += 1
            if e["event"] == "labeled" and e["label"] == "grok":
                grok += 1
        # DeepSeek: dispatch comments and poller reports, judged by the orchestrator's own rules
        # (dispatch_history, vws-deploy#462): only the first report per dispatch URL counts, a provider
        # error is infra and an exclusions.tsv entry removes the attempt.
        by_issue, first_report = {}, {}
        for c in sorted(r["comments"], key=lambda c: (c["created_at"], c["id"])):
            k = orch.classify_comment(c["body"])
            if k and k[0] == "report":
                first_report.setdefault(k[1]["for"], c["id"])
        for c in r["comments"]:
            by_issue.setdefault(c["issue"], []).append(c)
            k = orch.classify_comment(c["body"])
            if inwin(c["created_at"]):
                wakes["verify_wake_comments"] += c["body"].split("\n", 1)[0].strip() == orch.WAKE_MARKER
                wakes["rework_dispatches"] += orch.REWORK_MARKER in c["body"]
                if k and k[0] == "marker" and k[1]["marker"] in GROK_DISPATCH_MARKERS:
                    wakes["dispatches"]["grok"] += 1
                codex["verify_reports"] += len(CODEX_BLOCKING_RE.findall(c["body"]))
                codex["blocking_sum"] += sum(int(x) for x in CODEX_BLOCKING_RE.findall(c["body"]))
            if not k or not inwin(c["created_at"]):
                continue
            if k[0] == "dispatch":
                ds["dispatched"] += 1
                bump(ds["by_repo"], name, "dispatched")
            elif k[0] == "report":
                if first_report[k[1]["for"]] != c["id"]:
                    key = "duplicate_reports"
                elif k[1]["result"] == "done":
                    key = "done"
                elif k[1]["for"] in exclusions or c["url"] in exclusions:
                    key = "excluded"  # as in dispatch_history, an exclusion removes failed attempts only
                else:
                    key = "infra" if orch.infra_error(c["body"]) else "failed"
                ds[key] += 1
                bump(ds["by_repo"], name, key)
        for comments in by_issue.values():
            for c in comments:
                k = orch.classify_comment(c["body"])
                if not k or k[0] != "dispatch":
                    continue
                deadline = orch.utc(c["created_at"]) + timeout
                if not start <= deadline < end:
                    continue
                before = [x for x in comments if orch.utc(x["created_at"]) < deadline]
                hist = orch.dispatch_history({"comments": before}, deadline, cfg, exclusions)
                if any(h["url"] == c["url"] and h["kind"] == "timeout" and h["outcome"] == "failed" for h in hist):
                    ds["timeout"] += 1
                    bump(ds["by_repo"], name, "timeout")
        for w in r["runs"]:
            if w["event"] not in CI_EVENTS or w["status"] != "completed":
                continue
            if w["conclusion"] in CI_FAIL:
                result = "failure"
            elif w["conclusion"] == "success":
                result = "success"
            else:
                continue  # cancelled, skipped, neutral: not a CI verdict
            bucket = ci["separate"] if w["name"] in SEPARATE_WORKFLOWS else ci["by_repo"]
            bump(bucket, name, w["name"], result)
        if queue is not None:
            waiting = {}
            for i in r.get("open_issues", []):
                for lb in QUEUE_LABELS:
                    queue[lb] += lb in i["labels"]
                if not i["is_pr"] and is_parked(i, f"{name}#{i['number']}", grok_allow):
                    parked += 1
                elif set(i["labels"]) & set(STALL_LABELS) and "blocked" not in i["labels"]:
                    waiting[i["number"]] = i.get("updated_at")
            for p in r.get("open_pulls", []):
                if not p["draft"] and p["base"] == "main" and "verify-approved" not in p["labels"]:
                    queue["verify_backlog"] += 1
                    if "blocked" not in p["labels"]:
                        waiting[p["number"]] = p.get("updated_at")
            for number, updated in sorted(waiting.items()):
                if updated:
                    age = (orch.utc(snapshot_at) - orch.utc(updated)) / 3600
                    if age >= STALL_HOURS:
                        stalls.append({"target": f"{name}#{number}", "idle_hours": round(age, 1)})
    for bucket in (ci["by_repo"], ci["separate"]):
        for wfs in bucket.values():
            for v in wfs.values():
                v.setdefault("success", 0)
                v.setdefault("failure", 0)
                v["failure_rate"] = rate(v["failure"], v["success"] + v["failure"])
    ok = sum(v["success"] for w in ci["by_repo"].values() for v in w.values())
    bad = sum(v["failure"] for w in ci["by_repo"].values() for v in w.values())
    ci.update(success=ok, failure=bad, failure_rate=rate(bad, ok + bad))
    verify["pass_rate"] = rate(verify["success"], verify["success"] + verify["failure"])
    verify["first_pass_rate"] = rate(verify["first_pass"], verify["first_pass_n"])
    totals = {k: sum(sum(a.values()) for a in prs[k].values()) for k in ("opened", "merged")}
    totals["closed_unmerged"] = sum(prs["closed_unmerged"].values())
    prs["totals"] = totals
    gaps = []
    orch_stats, by_orch = None, {"dispatch": None, "swap_label": None}
    if log_covered:
        orch_stats = {"ticks": 0, "failed_ticks": 0, "apply_failed": 0, "rate_low": 0, "failure_alerts": 0,
                      "gh_get": 0, "gh_not_modified": 0, "gh_write": 0, "modes": {}}
        by_orch = {"dispatch": 0, "swap_label": 0}
        for rec in log_recs:
            ev = rec.get("event")
            if ev == "tick_start":
                orch_stats["ticks"] += 1
                bump(orch_stats["modes"], rec.get("mode", "?"))
            elif ev in ("auth_failed", "collect_failed") or (ev == "tick_end" and rec.get("errors")):
                orch_stats["failed_ticks"] += 1
            if ev == "tick_end":
                g = rec.get("gh") or {}
                orch_stats["gh_get"] += g.get("get", 0)
                orch_stats["gh_not_modified"] += g.get("not_modified", 0)
                orch_stats["gh_write"] += g.get("write", 0)
            elif ev == "apply_failed":
                orch_stats["apply_failed"] += 1
            elif ev == "rate_low":
                orch_stats["rate_low"] += 1
            elif ev == "failure_alerted":
                orch_stats["failure_alerts"] += 1
            elif ev == "applied" and rec.get("mode") == "apply" and rec.get("action") in by_orch:
                by_orch[rec["action"]] += 1
    else:
        gaps.append("orchestrator log does not cover the whole day")
    if snapshot_at is None:
        gaps.append(SNAPSHOT_GAP)
    if tokens is None:
        gaps.append("no token-usage input for this day")
    if lead_wakes is None:
        gaps.append("no lead-wake input for this day")
    ds["dispatched_by_orchestrator"] = by_orch["dispatch"]
    wakes["dispatches"]["deepseek"] = ds["dispatched"]
    out = {
        "schema": 1, "day": day, "timezone": "Asia/Seoul (UTC+9)", "window_utc": [orch.iso(start), orch.iso(end)],
        "repos": sorted(raw["repos"]), "archived_skipped": sorted(raw["archived"]),
        "prs": prs,
        "lead_time_hours": {"all": stats(lead_all), "by_repo": {k: stats(v) for k, v in sorted(lead_repo.items())}},
        "verify": verify, "deepseek": ds,
        "grok_swaps": {"labeled": grok, "by_orchestrator": by_orch["swap_label"]},
        "ci": ci, "codex": codex, "orchestrator": orch_stats,
        "queue_depth": queue, "stalls": stalls, "parked": parked, "snapshot_at": snapshot_at, "tokens": tokens, "source_gaps": gaps,
        "wakes": wakes,
    }
    out["content_sha256"] = content_hash(out)
    return out


def content_hash(doc):
    stable = {k: v for k, v in doc.items() if k not in VOLATILE}
    # Whether the live snapshot was taken depends on when the day is computed, not on the data.
    stable["source_gaps"] = [g for g in doc.get("source_gaps", []) if g != SNAPSHOT_GAP]
    return "sha256:" + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _rate_inputs(doc):
    """(numerator, denominator) per watched rate; None when the doc lacks the numbers."""
    try:
        ds = doc["deepseek"]
        return {
            "ci_failure_rate": (doc["ci"]["failure"], doc["ci"]["success"] + doc["ci"]["failure"]),
            "deepseek_failure_rate": (ds["failed"] + ds["timeout"], ds["done"] + ds["failed"] + ds["timeout"]),
            "grok_escalation_rate": (doc["grok_swaps"]["labeled"], ds["dispatched"]),
        }
    except (KeyError, TypeError):
        return None


def anomalies(doc, history):
    """Pure. Alerts for one day against the BASELINE_DAYS days before it. An empty list is a normal day."""
    alerts = []
    if doc.get("stalls"):
        alerts.append({"kind": "stall", "count": len(doc["stalls"]), "threshold_hours": STALL_HOURS,
                       "items": doc["stalls"][:20]})
    day = dt.date.fromisoformat(doc["day"])
    window = {(day - dt.timedelta(days=k)).isoformat() for k in range(1, BASELINE_DAYS + 1)}
    base = [h for h in sorted(history, key=lambda h: h["day"]) if h.get("day") in window]
    today = _rate_inputs(doc) or {}
    base_inputs = [x for x in (_rate_inputs(h) for h in base) if x]
    if len(base_inputs) >= BASELINE_MIN_DAYS:
        for key, (num_, den) in sorted(today.items()):
            if den < RATE_MIN_EVENTS:
                continue
            bnum = sum(b[key][0] for b in base_inputs)
            bden = sum(b[key][1] for b in base_inputs)
            # Exact fractions: a rate exactly at a threshold alerts (no float rounding).
            rate_now, rate_base = Fraction(num_, den), (Fraction(bnum, bden) if bden else Fraction(0))
            rise, ratio = Fraction(str(RATE_MIN_RISE)), Fraction(str(RATE_MIN_RATIO))
            if rate_now >= rate_base + rise and (rate_base == 0 or rate_now >= ratio * rate_base):
                alerts.append({"kind": key, "today": round(float(rate_now), 3), "baseline": round(float(rate_base), 3),
                               "events": den, "baseline_days": len(base_inputs)})
    tok = (doc.get("tokens") or {}).get("total")
    base_tok = sorted(h["tokens"]["total"] for h in base if (h.get("tokens") or {}).get("total") is not None)
    if tok is not None and len(base_tok) >= BASELINE_MIN_DAYS:
        mid = len(base_tok) // 2
        median = Fraction(base_tok[mid]) if len(base_tok) % 2 else Fraction(base_tok[mid - 1] + base_tok[mid], 2)
        if tok >= TOKEN_MIN_DAILY and tok >= Fraction(str(TOKEN_SPIKE_RATIO)) * median:
            median = int(median) if median.denominator == 1 else float(median)
            alerts.append({"kind": "token_spike", "today": tok, "baseline_median": median, "baseline_days": len(base_tok)})
    return alerts


def alert_key(doc):
    kinds = sorted(a["kind"] for a in doc["alerts"])
    return "sha256:" + hashlib.sha256(f"metrics-alert:{doc['day']}:{','.join(kinds)}".encode()).hexdigest()


def alert_body(doc, where):
    lines = [ALERT_MARKER, f"vws-metrics: {len(doc['alerts'])} anomalies for the KST day {doc['day']}.", ""]
    for a in doc["alerts"]:
        if a["kind"] == "stall":
            items = ", ".join(f"{s['target']} ({s['idle_hours']}h)" for s in a["items"])
            lines.append(f"- stall: {a['count']} open tasks or PRs without an update for {a['threshold_hours']}h or more: {items}")
        elif a["kind"] == "token_spike":
            lines.append(f"- token_spike: {a['today']} tokens vs a trailing median of {a['baseline_median']} "
                         f"({a['baseline_days']} days)")
        else:
            lines.append(f"- {a['kind']}: {a['today']} vs a baseline of {a['baseline']} over {a['baseline_days']} days "
                         f"({a['events']} events today)")
    lines += ["", f"Daily file: {where}",
              f"Thresholds: stall {STALL_HOURS}h; rate +{RATE_MIN_RISE} and {RATE_MIN_RATIO}x over {BASELINE_DAYS} days "
              f"(min {RATE_MIN_EVENTS} events, {BASELINE_MIN_DAYS} baseline days); tokens {TOKEN_SPIKE_RATIO}x the median "
              f"and at least {TOKEN_MIN_DAILY}.",
              f"Idempotency key: `{alert_key(doc)}`"]
    return "\n".join(lines)


def post_alert(gh, ctx, log, pending, state):
    """Post one pending alert ({day, key, kinds, body}). A timed-out POST may have landed, so the thread is
    searched for the key first; the alert stays pending in metrics-state.json until it is on the thread."""
    if pending["key"] in state.get("alerted", []):
        log("metrics_alert_skipped", day=pending["day"], reason="already posted")
        return
    number = int(ctx.cfg["ORCHESTRATE_STATUS_ISSUE"])
    posted = pages(gh, f"repos/{OWNER}/{DATA_REPO}/issues/{number}/comments?per_page=100")
    if not any(f"Idempotency key: `{pending['key']}`" in (c.get("body") or "") for c in posted):
        resp = gh.write("POST", f"repos/{OWNER}/{DATA_REPO}/issues/{number}/comments", {"body": pending["body"]})
        log("metrics_alerted", day=pending["day"], target=f"{DATA_REPO}#{number}", kinds=pending["kinds"], key=pending["key"])
        url = (resp or {}).get("html_url") if isinstance(resp, dict) else None
        url = url or f"https://github.com/{OWNER}/{DATA_REPO}/issues/{number}"
        summary = f"{len(pending['kinds'])} anomalies for the KST day {pending['day']}: {', '.join(pending['kinds'])}"
        alert_webhook(ctx.environ, log, summary, url)
    else:
        log("metrics_alert_skipped", day=pending["day"], reason="the status issue already has this key")
    state["alerted"] = (state.get("alerted", []) + [pending["key"]])[-STATE_KEEP_DAYS:]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the key to another URL: treat any 3xx as a failed POST instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _webhook_url_ok(url):
    """https only; plain http only to the local host (tests and a local relay)."""
    return re.match(r"^https://[^/\s]+", url) is not None or \
        re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?(/|$)", url) is not None


def alert_webhook(environ, log, summary, comment_url, opener=None, sleep=None):
    """Best effort: POST {"kind": "metrics-alert", "summary", "comment_url"} to VWS_ALERT_WEBHOOK_URL with
    the header Authorization: Bearer $VWS_ALERT_WEBHOOK_KEY (a Cursor automation webhook key; a value that
    already starts with "Bearer " is sent as is). Both come from the process environment only.
    Skipped when either is unset. WEBHOOK_TIMEOUT_S per try, at most WEBHOOK_TRIES tries with backoff on a
    network error, 429 or 5xx. Never raises; the log never has the URL or the key. Returns True when sent."""
    url, key = environ.get("VWS_ALERT_WEBHOOK_URL", ""), environ.get("VWS_ALERT_WEBHOOK_KEY", "")
    if not url or not key:
        log("metrics_webhook_skipped", reason="VWS_ALERT_WEBHOOK_URL or VWS_ALERT_WEBHOOK_KEY is unset")
        return False
    if not _webhook_url_ok(url):
        log("metrics_webhook_skipped", reason="VWS_ALERT_WEBHOOK_URL is not https")
        return False
    auth = key if key.startswith("Bearer ") else f"Bearer {key}"  # a pasted "Bearer crsr_..." header works too
    data = json.dumps({"kind": "metrics-alert", "summary": summary, "comment_url": comment_url}).encode()
    opener = opener or urllib.request.build_opener(_NoRedirect)
    sleep = sleep or time.sleep
    try:
        # Inside the try: urllib parses the URL here, and its exception text can carry part of it (for
        # example a bracketed host). A URL that does not parse does not get better with a retry.
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": auth, "User-Agent": "vws-metrics"})
    except Exception as e:  # noqa: BLE001 - only the class name is logged (no URL, no key)
        log("metrics_webhook_failed", error=type(e).__name__, http_status=0, attempts=0)
        return False
    status, error, attempt = 0, "", 0
    for attempt in range(1, WEBHOOK_TRIES + 1):
        try:
            with opener.open(req, timeout=WEBHOOK_TIMEOUT_S) as r:
                status = r.status
            if 200 <= status < 300:
                log("metrics_webhook_sent", http_status=status, attempts=attempt)
                return True
            error = f"HTTP {status}"
        except urllib.error.HTTPError as e:
            status, error = e.code, f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 - best effort; only the class name is logged (no URL, no key)
            status, error = 0, type(e).__name__
        if status and status < 500 and status != 429:
            break  # a client error does not get better with a retry
        if attempt < WEBHOOK_TRIES:
            sleep(2 ** (attempt - 1))
    log("metrics_webhook_failed", error=error, http_status=status, attempts=attempt)
    return False


def read_tokens(path):
    """Optional token-usage input: {"by_agent": {"<agent>": <int tokens>}}. None when absent or invalid."""
    data = orch.read_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("by_agent"), dict):
        return None
    by = {}
    for k, v in sorted(data["by_agent"].items()):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(k)) or not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return None
        by[k.lower()] = by.get(k.lower(), 0) + v
    return {"total": sum(by.values()), "by_agent": by}


def read_lead_wakes(path):
    """Optional "lead_wakes": <int> in the same daily usage file. None when absent or invalid, never 0."""
    data = orch.read_json(path, None)
    v = data.get("lead_wakes") if isinstance(data, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def lead_wakes_from_github(raw, cfg):
    """Pure (vws-deploy#410, #543). The lead wakes of the day: comments on the wake thread
    (ORCHESTRATE_WAKE_THREAD in vws-deploy) created in the KST day whose first line is the lead-wake marker
    and whose author is the orchestrator login (trusted_read.own_comment: the box gh login). A marker posted
    by anyone else is ignored. None when there is no source (wake thread unset or not a positive integer, or
    vws-deploy not collected), never 0. A non-numeric thread returns None and does not raise; config load
    refuses it before a tick."""
    thread = str(cfg.get("ORCHESTRATE_WAKE_THREAD", "") or "")
    repo = raw["repos"].get(orch.STATUS_REPO)
    if not re.fullmatch(r"[1-9][0-9]*", thread) or repo is None:
        return None
    start, end = kst_day_window(raw["day"])
    number = int(thread)
    return sum(1 for c in repo["comments"] if c["issue"] == number and start <= orch.utc(c["created_at"]) < end
               and c["body"].split("\n", 1)[0].strip() == orch.LEAD_WAKE_MARKER and orch.tr.own_comment(c))


def write_usage(path, lead_wakes):
    """The daily usage writer (vws-deploy#410): set "lead_wakes" in the usage file, keeping every other key
    (by_agent belongs to whoever reports tokens; this writer has no token source and never writes it,
    so tokens stay null, never 0, until #557).
    Returns True when the file changed, False when nothing was written (no value, or the same value: a rerun
    writes nothing), None when the existing file is not a JSON object (left alone)."""
    if lead_wakes is None:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("lead_wakes") == lead_wakes and type(data["lead_wakes"]) is int:
        return False
    data["lead_wakes"] = lead_wakes
    orch.write_json(path, data)
    return True


def headline(doc):
    w = doc.get("wakes") or {}  # days written before vws-deploy#400 have none
    return {"day": doc["day"], "prs_opened": doc["prs"]["totals"]["opened"], "prs_merged": doc["prs"]["totals"]["merged"],
            "lead_time_median_h": doc["lead_time_hours"]["all"]["median"], "verify_pass_rate": doc["verify"]["pass_rate"],
            "deepseek_dispatched": doc["deepseek"]["dispatched"], "deepseek_done": doc["deepseek"]["done"],
            "deepseek_failed": doc["deepseek"]["failed"], "deepseek_timeout": doc["deepseek"]["timeout"],
            "grok_swaps": doc["grok_swaps"]["labeled"], "ci_failure_rate": doc["ci"]["failure_rate"],
            "verify_backlog": (doc.get("queue_depth") or {}).get("verify_backlog"),
            "stalls": None if doc.get("stalls") is None else len(doc["stalls"]),
            "tokens": (doc.get("tokens") or {}).get("total"), "alerts": [a["kind"] for a in doc.get("alerts") or []],
            "verify_wakes": w.get("verify_wake_comments"), "grok_dispatches": (w.get("dispatches") or {}).get("grok"),
            "rework_dispatches": w.get("rework_dispatches"), "lead_wakes": w.get("lead_wakes")}


def summarize(docs, last_day):
    """Pure: summary.json and SUMMARY.md from the daily files of the last SUMMARY_DAYS days."""
    end = dt.date.fromisoformat(last_day)
    keep = {(end - dt.timedelta(days=k)).isoformat() for k in range(SUMMARY_DAYS)}
    rows = [headline(d) for d in sorted(docs, key=lambda d: d["day"]) if d["day"] in keep]
    summary = {"schema": 1, "through": last_day, "days": rows}
    fmt = lambda v: "-" if v is None or v == [] else (" ".join(v) if isinstance(v, list) else str(v))
    cols = ["day", "prs_opened", "prs_merged", "lead_time_median_h", "verify_pass_rate", "deepseek_dispatched",
            "deepseek_done", "deepseek_failed", "deepseek_timeout", "grok_swaps", "ci_failure_rate", "verify_backlog",
            "stalls", "tokens", "verify_wakes", "grok_dispatches", "rework_dispatches", "lead_wakes", "alerts"]
    lines = ["# Factory metrics summary", "", f"Through {last_day} (KST days). Written by .github/metrics/metrics.py "
             "in vws-deploy (#345); one file per day in metrics/daily/.", ""]
    for span in (7, SUMMARY_DAYS):
        since = (end - dt.timedelta(days=span - 1)).isoformat()  # calendar days, not the last N files
        part = [r for r in rows if r["day"] >= since]
        known = lambda k: [r[k] for r in part if r[k] is not None]
        total = lambda k: sum(known(k)) if known(k) else "-"  # a total over no day with data is unknown, not 0
        lines.append(f"- Last {span} days ({len(part)} with data): {total('prs_merged')} PRs merged, "
                     f"{total('prs_opened')} opened, {total('deepseek_dispatched')} DeepSeek dispatches "
                     f"({total('deepseek_done')} done, {total('deepseek_failed')} failed, {total('deepseek_timeout')} "
                     f"timed out), {total('grok_swaps')} grok swaps.")
        lead = [r["lead_wakes"] for r in part if r["lead_wakes"] is not None]
        tok = [(r["tokens"], r["prs_merged"]) for r in part if r["tokens"] is not None]
        merged = sum(m for _, m in tok)
        per_pr = round(sum(t for t, _ in tok) / merged) if merged else "-"
        wake_days = len(known("verify_wakes"))  # days with a wakes block; days before #400 have none
        partial = f" ({wake_days} days with data)" if wake_days < len(part) else ""
        lines.append(f"- Last {span} days wakes: {total('verify_wakes')} verify wakes, {total('deepseek_dispatched')} "
                     f"DeepSeek and {total('grok_dispatches')} grok dispatches ({total('rework_dispatches')} reworks)"
                     f"{partial}, "
                     f"lead wakes: {sum(lead) if lead else '-'} ({len(lead)} days with input); "
                     f"tokens per merged PR: {per_pr} ({len(tok)} days with input).")
    lines += ["", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(r[c]) for c in cols) + " |" for r in reversed(rows)]
    return summary, "\n".join(lines) + "\n"


def dumps(doc):
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def ascii_only(*texts):
    return all(t.isascii() for t in texts)


# ---------------------------------------------------------------- the branch (Git Data API)

def branch_head(gh):
    try:
        ref, _ = gh.get(f"repos/{OWNER}/{DATA_REPO}/git/ref/heads/{BRANCH}")
    except orch.GhError as e:
        if e.status == 404:
            return None
        raise
    return ref["object"]["sha"]


def check_history(gh, head, verified):
    """Refuse a branch that has any commit this script did not write. Returns the problem or ''."""
    sha_list = pages(gh, f"repos/{OWNER}/{DATA_REPO}/commits?sha={head}&per_page=100",
                     stop=lambda c: c["sha"] == verified)
    for c in sha_list:
        if c["sha"] == verified:
            break
        msg = c["commit"]["message"]
        subject = msg.split("\n", 1)[0]
        parents = len(c.get("parents", []))
        if GENERATOR_TRAILER not in msg.splitlines():
            return f"{c['sha'][:7]} has no '{GENERATOR_TRAILER}' trailer"
        if not (DAILY_SUBJECT_RE.match(subject) and parents == 1) and not (subject == INIT_SUBJECT and parents == 0):
            return f"{c['sha'][:7]} subject or parents are not ours ({subject!r}, {parents} parents)"
        full, _ = gh.get(f"repos/{OWNER}/{DATA_REPO}/commits/{c['sha']}")
        for f in full.get("files", []):
            if not f["filename"].startswith("metrics/"):
                return f"{c['sha'][:7]} touches {f['filename']} outside metrics/"
    return ""


def branch_files(gh, head):
    """{path: blob sha} of metrics/daily/*.json on the branch head."""
    commit, _ = gh.get(f"repos/{OWNER}/{DATA_REPO}/commits/{head}")
    tree, _ = gh.get(f"repos/{OWNER}/{DATA_REPO}/git/trees/{commit['commit']['tree']['sha']}?recursive=1")
    if tree.get("truncated"):
        raise orch.GhError(0, "the factory-metrics tree listing is truncated")
    return commit["commit"]["tree"]["sha"], {t["path"]: t["sha"] for t in tree.get("tree", [])
                                            if t["type"] == "blob" and t["path"].startswith("metrics/daily/")}


def read_blob(gh, sha):
    blob, _ = gh.get(f"repos/{OWNER}/{DATA_REPO}/git/blobs/{sha}")
    return json.loads(base64.b64decode(blob["content"]).decode("utf-8"))


def commit_files(gh, head, tree_sha, files, message):
    """Three writes: a tree on the old tree, a commit on head, and a fast-forward of the ref."""
    tree = gh.write("POST", f"repos/{OWNER}/{DATA_REPO}/git/trees", {
        "base_tree": tree_sha,
        "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in sorted(files.items())]})
    commit = gh.write("POST", f"repos/{OWNER}/{DATA_REPO}/git/commits",
                      {"message": message, "tree": tree["sha"], "parents": [head]})
    gh.write("PATCH", f"repos/{OWNER}/{DATA_REPO}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": False})
    return commit["sha"]


# ---------------------------------------------------------------- commands

class Ctx:
    def __init__(self, environ):
        self.paths = orch.Paths(environ)
        self.cfg = orch.load_config(self.paths, environ)
        self.environ = environ  # the alert webhook URL and key are read from here only, never the env file
        file_cfg = orch.read_env_file(self.paths.env_file)
        for k, v in DEFAULTS.items():
            self.cfg[k] = environ.get(k, file_cfg.get(k, v))
        if self.cfg["METRICS_MODE"] not in ("dry-run", "apply"):
            raise ValueError("METRICS_MODE must be dry-run or apply")
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", self.cfg["METRICS_TIME"]):
            raise ValueError("METRICS_TIME must be HH:MM")
        for k in ("METRICS_BACKFILL_DAYS", "METRICS_MAX_GETS"):
            if not re.fullmatch(r"[1-9][0-9]*", self.cfg[k]):
                raise ValueError(f"{k} must be a positive integer")
        if int(self.cfg["METRICS_BACKFILL_DAYS"]) > STATE_KEEP_DAYS:
            # metrics-state.json keeps STATE_KEEP_DAYS done days; a longer window would never finish.
            raise ValueError(f"METRICS_BACKFILL_DAYS must be at most {STATE_KEEP_DAYS}")
        st = self.paths.state
        self.state_file = os.path.join(st, "metrics-state.json")
        self.cache = os.path.join(st, "metrics-etag-cache.json")
        self.lock = os.path.join(st, "metrics.lock")
        self.out_dir = os.path.join(st, "metrics")
        self.token_dir = environ.get("METRICS_TOKEN_DIR") or file_cfg.get("METRICS_TOKEN_DIR") or \
            os.path.join(st, "metrics-input", "tokens")
        self.log_paths = type("P", (), {"log": os.path.join(st, "log", "metrics.jsonl"),
                                        "log_dir": os.path.join(st, "log")})()


def release():
    m = re.search(r"/releases/([0-9a-f]{7,40})/", os.path.realpath(HERE) + "/")
    return m.group(1) if m else "dev"


def write_local(ctx, mode, day, doc_text):
    d = os.path.join(ctx.out_dir, mode, "daily")
    orch.ensure_dir(d)
    path = os.path.join(d, f"{day}.json")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc_text)
    os.replace(path + ".tmp", path)
    return path


def local_docs(ctx, mode):
    out = []
    for f in sorted(glob.glob(os.path.join(ctx.out_dir, mode, "daily", "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (ValueError, OSError):
            continue
    return out


def run_day(ctx, gh, log, args, day, now, recompute=False):
    """Compute one day; publish it in apply mode. Returns an exit status."""
    cfg = ctx.cfg
    mode = "dry-run" if args.fixture else cfg["METRICS_MODE"]  # a fixture never publishes
    state = orch.read_json(ctx.state_file, {})
    snap = day == (kst_date(now) - dt.timedelta(days=1)).isoformat() or bool(args.snapshot)
    if args.fixture:
        raw = orch.read_json(args.fixture, None)
    else:
        repos = orch.load_repos(args.repos, cfg["ORCHESTRATE_EXTRA_REPOS"])
        raw = collect(gh, repos, day, cfg, snapshot=snap)
    if args.dump_raw:
        orch.write_json(args.dump_raw, raw)
    start, end = kst_day_window(day)
    recs, covered = read_orchestrator_log(args.orchestrator_log_dir or ctx.paths.log_dir, start, end)
    exclusions = orch.load_exclusions(os.path.join(HERE, "..", "orchestrate", "exclusions.tsv"))
    usage = os.path.join(ctx.token_dir, f"{day}.json")
    if not args.fixture:
        # The daily usage writer (vws-deploy#410): lead_wakes from GitHub, once the day is over. A fixture
        # never writes it, and a day still running would get a partial count.
        lead = lead_wakes_from_github(raw, cfg) if now >= end else None
        if lead is None:
            log("metrics_usage_skipped", day=day, reason="day not over" if now < end else "ORCHESTRATE_WAKE_THREAD unset "
                "or vws-deploy not collected: lead_wakes stays null")
        else:
            changed = write_usage(usage, lead)
            event = {True: "metrics_usage_written", False: "metrics_usage_unchanged"}.get(changed, "metrics_usage_refused")
            log(event, day=day, path=usage, lead_wakes=lead)
    doc = compute(raw, recs, covered, cfg, exclusions, snapshot_at=orch.iso(now) if snap else None,
                  tokens=read_tokens(usage), lead_wakes=read_lead_wakes(usage))
    # History: the published files in apply mode, the local dry-run copies otherwise.
    path = f"metrics/daily/{day}.json"
    cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=SUMMARY_DAYS)).isoformat()
    if mode == "apply":
        head = branch_head(gh)
        if head is None:
            log("metrics_no_branch", reason=f"{BRANCH} does not exist; run --init (approved once) first")
            return FAIL_EXIT
        problem = check_history(gh, head, state.get("verified_head", ""))
        if problem:
            log("metrics_foreign_commit", head=head, problem=problem)
            return REFUSE_EXIT
        tree_sha, files = branch_files(gh, head)
        history = {d["day"]: d for d in (read_blob(gh, sha) for p, sha in sorted(files.items())
                                         if p != path and p.endswith(".json") and p[len("metrics/daily/"):-5] >= cutoff)}
    else:
        history = {d["day"]: d for d in local_docs(ctx, mode) if d.get("day", "") >= cutoff and d.get("day") != day}
    doc["alerts"] = anomalies(doc, list(history.values()))
    doc["generated_at"], doc["release"] = orch.iso(now), release()
    text = dumps(doc)
    if args.print:
        sys.stdout.write(text)
    body = alert_body(doc, f"{path} on branch {BRANCH}") if doc["alerts"] else ""
    if not ascii_only(text, body):
        log("metrics_non_ascii", day=day)
        return REFUSE_EXIT
    local = write_local(ctx, mode, day, text)
    log("metrics_computed", day=day, path=local, content_sha256=doc["content_sha256"], alerts=[a["kind"] for a in doc["alerts"]],
        gh=gh.stats if gh else {})
    if mode != "apply":
        if body:
            adir = os.path.join(ctx.out_dir, mode, "alerts")
            orch.ensure_dir(adir)
            with open(os.path.join(adir, f"{day}.md"), "w", encoding="utf-8") as f:
                f.write(body + "\n")
            log("metrics_alert_planned", day=day, kinds=[a["kind"] for a in doc["alerts"]])
        state.setdefault("done", {}).setdefault("dry-run", [])
        state["done"]["dry-run"] = sorted(set(state["done"]["dry-run"]) | {day})[-STATE_KEEP_DAYS:]
        orch.write_json(ctx.state_file, state)
        return 0
    # The alert is journaled before any branch write: if the POST fails, or the process dies after the
    # ref update, the next run posts this same alert even when a fresh computation no longer sees the
    # anomaly (a stalled item got an update, the snapshot is gone, it is another KST day).
    pending = state.setdefault("pending_alerts", {})
    if doc["alerts"] and day not in pending and alert_key(doc) not in state.get("alerted", []):
        pending[day] = {"day": day, "key": alert_key(doc), "kinds": [a["kind"] for a in doc["alerts"]], "body": body}
        orch.write_json(ctx.state_file, state)
    new = head
    if path in files and not recompute:
        old = read_blob(gh, files[path])
        if old.get("content_sha256") == doc["content_sha256"]:
            log("metrics_unchanged", day=day)
        else:
            log("metrics_drift", day=day, published=old.get("content_sha256"), now=doc["content_sha256"])
    elif path in files and read_blob(gh, files[path]).get("content_sha256") == doc["content_sha256"]:
        log("metrics_unchanged", day=day)
    else:
        docs = dict(history)
        docs[day] = doc
        summary, md = summarize(list(docs.values()), max(docs))
        out = {path: text, "metrics/summary.json": dumps(summary), "metrics/SUMMARY.md": md}
        if not ascii_only(*out.values()):
            log("metrics_non_ascii", day=day)
            return REFUSE_EXIT
        msg = (f"metrics: daily {day}\n\nGenerated by .github/metrics/metrics.py (release {release()}) for the KST day "
               f"{day}.\n\n{GENERATOR_TRAILER}\n")
        new = commit_files(gh, head, tree_sha, out, msg)
        log("metrics_published", day=day, commit=new, content_sha256=doc["content_sha256"])
    # Silent on a normal day. The alert goes out after the data is on the branch, so it points at it.
    silence = orch.wake_silence(ctx.cfg)
    if pending and silence:
        # vws-deploy#521: STOP is full status-bus silence. The alert stays journaled and posts on the first
        # run after RESUME; the log counts what would have posted.
        state["verified_head"] = new
        orch.write_json(ctx.state_file, state)
        log("metrics_suppressed", day=day, reason=silence, alerts=len(pending), days=sorted(pending))
    elif pending:
        state["verified_head"] = new
        orch.write_json(ctx.state_file, state)
        # This day's alert, and any held back during a STOP window (vws-deploy#521), oldest first.
        for d in sorted(pending):
            post_alert(gh, ctx, log, pending[d], state)
            del pending[d]
            orch.write_json(ctx.state_file, state)
    return mark_published(ctx, state, day, new)


def mark_published(ctx, state, day, head):
    state.setdefault("done", {}).setdefault("apply", [])
    state["done"]["apply"] = sorted(set(state["done"]["apply"]) | {day})[-STATE_KEEP_DAYS:]
    state["verified_head"] = head
    orch.write_json(ctx.state_file, state)
    return 0


def init_branch(ctx, gh, log):
    if ctx.cfg["METRICS_MODE"] != "apply":
        log("metrics_init_skipped", reason="METRICS_MODE is not apply")
        print("--init needs METRICS_MODE=apply", file=sys.stderr)
        return 2
    if branch_head(gh) is not None:
        log("metrics_init_skipped", reason=f"{BRANCH} already exists")
        return 0
    readme = ("# factory-metrics\n\nDaily factory metrics for the vws-* repos, written only by "
              ".github/metrics/metrics.py on the shared box (vws-deploy#345). Never merged into main.\n")
    tree = gh.write("POST", f"repos/{OWNER}/{DATA_REPO}/git/trees",
                    {"tree": [{"path": "metrics/README.md", "mode": "100644", "type": "blob", "content": readme}]})
    commit = gh.write("POST", f"repos/{OWNER}/{DATA_REPO}/git/commits",
                      {"message": f"{INIT_SUBJECT}\n\n{GENERATOR_TRAILER}\n", "tree": tree["sha"], "parents": []})
    gh.write("POST", f"repos/{OWNER}/{DATA_REPO}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    log("metrics_init", commit=commit["sha"])
    return 0


def main(argv=None, environ=None, now=None, gh=None):
    environ = os.environ if environ is None else environ
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--if-due", action="store_true")
    g.add_argument("--date")
    g.add_argument("--recompute")
    g.add_argument("--init", action="store_true")
    p.add_argument("--repos", default=os.path.join(HERE, "..", "drift", "repos.tsv"))
    p.add_argument("--fixture", help="compute from a saved raw JSON (--dump-raw); no network, never publishes")
    p.add_argument("--dump-raw", help="write the collected raw JSON here")
    p.add_argument("--orchestrator-log-dir", help="read the orchestrator log here (default: the state log dir)")
    p.add_argument("--snapshot", action="store_true", help="include the live queue-depth snapshot for an older day")
    p.add_argument("--print", action="store_true", help="print the daily JSON")
    args = p.parse_args(argv)
    day = args.date or args.recompute
    if day and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        print("a day is YYYY-MM-DD", file=sys.stderr)
        return 2
    if args.fixture and (args.init or args.if_due):
        print("--fixture only works with --date or --recompute (it never publishes)", file=sys.stderr)
        return 2
    try:
        ctx = Ctx(environ)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    now = now or time.time()
    log = orch.Log(ctx.log_paths, ctx.cfg, orch.run_id_now(now), ctx.cfg["METRICS_MODE"], echo=args.print is False and not args.if_due)
    orch.ensure_dir(ctx.paths.state)
    if os.path.exists(ctx.paths.stop):
        log("metrics_stopped", reason="STOP file present")
        return 0
    if args.if_due:
        state = orch.read_json(ctx.state_file, {})
        days = due_days(now, ctx.cfg, state.get("done", {}).get(ctx.cfg["METRICS_MODE"], []))
        if not days:
            return 0  # the common case: no lock, no log line, no gh call
        day = days[0]
    lock_fd = os.open(ctx.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("metrics_skipped", reason="another run holds metrics.lock")
        return 0
    try:
        log.rotate()
        if args.fixture:
            return run_day(ctx, None, log, args, day, now)
        gh = gh or MetricsGh(ctx.cache, max_gets=int(ctx.cfg["METRICS_MAX_GETS"]), init=args.init,
                             rate_floor=orch.num(ctx.cfg, "ORCHESTRATE_RATE_FLOOR"),
                             status_issue=ctx.cfg["ORCHESTRATE_STATUS_ISSUE"])
        try:
            if args.init:
                return init_branch(ctx, gh, log)
            return run_day(ctx, gh, log, args, day, now, recompute=bool(args.recompute))
        except RateLow as e:
            log("metrics_rate_low", day=day or "", remaining=int(str(e)))
            return 0  # due again on the next tick
        except (orch.GhError, PermissionError, Budget, KeyError, ValueError) as e:
            log("metrics_failed", day=day or "", error=str(e), http_status=getattr(e, "status", 0))
            return FAIL_EXIT
        finally:
            gh.save_cache()
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
