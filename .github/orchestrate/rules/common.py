"""Shared helpers and constants of the orchestrator rules (vws-deploy#501). Python 3 stdlib only.

Everything two or more rules (or a rule and orchestrate.py's collect/apply/status code) use lives here, with
one definition each; a helper only one rule uses lives in that rule's module. Nothing here writes: no gh
call, no file write, no clock read. orchestrate.py imports what it needs from here and re-exports the rest,
so tests, .github/metrics and .github/verify keep reading every name as orch.X.
"""
import datetime as dt
import json
import re

import trusted_read as tr
from trusted_read import (
    OWNER,
)


# ---------------------------------------------------------------- the rule protocol (vws-deploy#501)
# The dispatcher passes cfg with the exclusions (load_exclusions) under this key.
EXCLUSIONS_KEY = "_exclusions"


def decision(actions=(), notes=(), state=None, **extra):
    """A rule's Decision: its actions and notes in order, its state updates (only keys in its STATE_KEYS)
    and extras (e.g. pending_count) that become top-level keys of the tick result."""
    return {"actions": list(actions), "notes": list(notes), "state": dict(state or {}), "extra": extra}


def active_repos(snapshot):
    """Names of the repos that are not archived."""
    return {r["name"] for r in snapshot["repos"] if not r.get("archived")}


def owned_labels(state, items):
    """state["owned_labels"] with every entry whose label is not on the item dropped (a copy; state is not
    changed). Ownership is journaled before the write, so such an entry means the write never happened or a
    human removed the label."""
    owned = json.loads(json.dumps(state.get("owned_labels", {name: [] for name in MANAGED_LABELS})))
    for name in MANAGED_LABELS:
        owned[name] = [k for k in owned.get(name, []) if k in items and name in items[k]["labels"]]
    return owned



STATUS_REPO = "vws-deploy"  # the status issue and the verify-wake thread live here; config picks the numbers
DEEPSEEK = "@deepseek-dev"
VERIFY_LOGIN = "vws-verify[bot]"
# CI-failure marker (vws-deploy#412): a PR comment that #420's ci-fail workflow posts with the caller's
# GITHUB_TOKEN. Line 1 is the marker and a fenced json block is the record; the comment counts only as
# #420 parse_comment accepts it (.github/ci-fail/ci_fail.py, ported below). github-actions[bot] is
# trusted, and so is this orchestrator's own login when it posts the second startup_failure marker
# (vws-deploy#446: workflow_run does not fire for that conclusion). Anyone else is ignored. n/wake are
# recomputed from the comments (the marker can only tighten them).
CI_FAIL_LOGIN = "github-actions[bot]"
# Exactly #420 JSON_BLOCK_RE and marker_line's field checks (MARKER_RE is trusted_read.CI_FAIL_RE).
CI_FAIL_JSON_RE = re.compile(r"^```json\n(.*?)^```$", re.M | re.S)
CI_FAIL_SHA_RE = re.compile(r"[0-9a-f]{40}")
CI_FAIL_REPO_RE = re.compile(r"vws-[a-z0-9-]+")
CI_FAIL_AGENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,38}")
CI_FAIL_OWNERS = {"deepseek", "grok", "none"}
CI_FAIL_CAP = 3  # CI reworks per head chain; the 4th failing head waits for a human
RESET_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}  # who may reset the CI cap
# A verify verdict comment or review body: "# vws-iam#127 @ <40-hex head>" then "Result: FAIL ...".
VERDICT_RE = re.compile(r"\A#\s+(vws-[a-z0-9-]+)#([0-9]+)\s+@\s+([0-9a-f]{40})\s*\n+Result:\s*(PASS|FAIL)\b")
# Conflict-only verdicts are a rebuild for eggbot, not a DeepSeek rework.
CONFLICT_RE = re.compile(r"(?i)conflicts? with main|merge conflict|needs? (?:a )?rebase|rebase onto main")
MANAGED_LABELS = ("blocked", "in-review")
NEEDS_VERIFY = "needs-verify"  # set by verify-queue.yml while every non-verify check is green (vws-deploy#397)
VERIFY_APPROVED = "verify-approved"
# Soft-CAP park: issue or linked open PR with any of these does not count against ORCHESTRATE_GROK_CAP.
PARK_BLOCK_LABELS = frozenset({"blocked", "hold", "do-not-merge", "needs-human"})
PARK_VERIFY_LABELS = frozenset({NEEDS_VERIFY, VERIFY_APPROVED})
GROK_ALLOW_RE = re.compile(r"vws-[a-z0-9-]+#[1-9][0-9]*")
# vws-deploy#506: ORCHESTRATE_GROK_OWNERS entries, repo=bot; the bot is what a grok-wake's Owner line names.
GROK_OWNER_RE = re.compile(r"(vws-[a-z0-9-]+)=([a-z0-9][a-z0-9-]{0,38})")
STATUS_EVENT_KEY_RE = re.compile(r"key `(sha256:[0-9a-f]{64})`")


# Rule D (vws-deploy#346).
VERIFY_CHECK = "vws-verify"
VERIFY_APP_ID = 5313004
ACTIONS_APP_ID = 15368  # GitHub Actions
CHECK_OK = {"success", "neutral", "skipped"}
# blocked: a human or the dependency rule holds the PR (vws-deploy#393); it never merges while labeled.
OPT_OUT_LABELS = ("do-not-merge", "hold", "blocked")
# repo-guard / verify-gate must pass on the head by name, even when main's protection does not list it.
GATE_CHECK = "verify-gate"
# ...and one passing run must come from this workflow file (vws-deploy#402): the GitHub Actions app id alone
# would also accept a verify-gate job that a PR adds in a workflow file of its own.
GATE_WORKFLOW = ".github/workflows/repo-guard.yml"
# GitHub's own verdict (REST mergeable_state = GraphQL mergeStateStatus) must be clean: it already counts
# main's protection and rulesets, which the PAT cannot read (vws-deploy#408). Anything else never merges.
MERGEABLE_OK = {"clean"}
# Rule D reads the same forms pr-hygiene accepts: `Part of #N` and `Part of vws-x#N` at the start
# of a line. The owner/repo form `Part of popododo0720/vws-x#N` is rejected by both (vws-deploy#477).
PART_OF_RE = re.compile(r"(?im)^\s*Part of\s+((?:vws-[a-z0-9-]+)?#[0-9]+)")
# The owner/repo form pr-hygiene rejects. Rule D names it in the reason instead of the generic
# no_part_of, so a PR skipped for it says what to change (#477).
OWNER_PART_OF_RE = re.compile(rf"(?im)^\s*Part of\s+{OWNER}/vws-[a-z0-9-]+#[0-9]+")
# vws-deploy#504: what to write instead, in the part_of_owner_repo merge note and lead wake. pr-hygiene
# (guard-agents.sh pr-body) names the same allowed forms and `guard-agents.sh pr-normalize` rewrites them.
PART_OF_HINT = ("write `Part of #N` (same repo) or `Part of vws-<repo>#N` at the start of a line; the owner/repo "
                "form `Part of <owner>/<repo>#N` is not read (https://github.com/popododo0720/vws-deploy/issues/504)")
# The issue a PR names with Closes/Fixes/Resolves: #N (the PR's repo), vws-x#N, or owner/repo#N (vws-deploy#560).
CLOSING_LINK_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]*"
    r"(?:[A-Za-z0-9_.-]+/)?"
    r"((?:vws-[a-z0-9-]+)?#[0-9]+)")
# Part of owner/repo#N. Rule D skips it, but an open PR that writes it still names the issue.
PART_OF_OWNER_REPO_REF_RE = re.compile(r"(?im)^\s*Part of\s+[A-Za-z0-9_.-]+/(vws-[a-z0-9-]+#[0-9]+)")


# The #412 Agent-line spec: an unindented, case-sensitive "Agent: <name>" line; the first one wins.
AGENT_RE = re.compile(r"^Agent:[ \t]*([a-z0-9][a-z0-9-]{0,38})[ \t]*$", re.M)
INFRA_MAX_RETRIES = 3
TOKEN_RE = re.compile(r"(gh[opsu]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|Bearer\s+\S+)")

DEFAULTS = {
    "ORCHESTRATE_MODE": "dry-run",
    "ORCHESTRATE_SCOPE": "",
    "ORCHESTRATE_MAX_WRITES": "5",
    "ORCHESTRATE_RATE_FLOOR": "1000",
    "ORCHESTRATE_EXTRA_REPOS": "vws-conformance",
    "ORCHESTRATE_COMMENT_WINDOW_DAYS": "14",
    "DEEPSEEK_TIMEOUT_HOURS": "3",
    "DEEPSEEK_MAX_FAILURES": "2",
    "ORCHESTRATE_DEEPSEEK_CAP": "2",
    "ORCHESTRATE_GROK_CAP": "1",
    "ORCHESTRATE_GROK_ALLOWLIST": "",  # repo#N entries; only these are ever assigned to Grok
    "ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN": "120",  # an unclaimed Grok wake frees its slot after this (#411)
    # Soft-CAP: a claimed Grok task with no comment/assignment/PR activity for this many minutes frees its slot
    # (done-unlinked claims); labels stay. Parked blocked/verify free the CAP without waiting for idle.
    "ORCHESTRATE_GROK_IDLE_MIN": "90",
    # vws-deploy#506: the Grok dev bot each repo's grok-wakes name (repo=bot); an unlisted repo names
    # the bot called after the repo (vws-iam -> vws-iam). An issue or PR `Agent:` line wins over both.
    # vws-deploy#511: the console-api Grok dev bot is named vws-api.
    "ORCHESTRATE_GROK_OWNERS": ",".join([f"{r}=vws-platform" for r in
                                         ("vws-deploy", "vws-lib", "vws-proto", "vws-test", "vws-conformance")]
                                        + ["vws-console-api=vws-api"]),
    # vws-deploy#411: GitHub logins the woken Grok bots comment as; only their comments (or a current
    # assignee's) claim a woken issue. The Grok bots use the owner's gh login.
    "ORCHESTRATE_GROK_LOGINS": OWNER,
    # GitHub logins whose [deepseek-dev] Done/Failed comments count: the poller's account (the VM PAT user).
    "ORCHESTRATE_DEEPSEEK_LOGINS": "popododo0720",
    # vws-deploy#433/#434: the login whose verify verdict comments and reviews count (besides an OWNER
    # comment), and optionally its numeric user id, so a re-registered login does not match.
    "ORCHESTRATE_VERIFY_LOGIN": VERIFY_LOGIN,
    "ORCHESTRATE_VERIFY_USER_ID": "",
    "ORCHESTRATE_INFRA_RETRY_DELAY_MIN": "5",
    "VERIFY_WAKE_THRESHOLD": "3",
    "VERIFY_WAKE_MAX_WAIT_MIN": "30",
    "ORCHESTRATE_STATUS_ISSUE": "415",
    "ORCHESTRATE_WAKE_THREAD": "",
    "ORCHESTRATE_FAILURE_ALERT": "apply",
    "ORCHESTRATE_FAILURE_THRESHOLD": "3",
    "ORCHESTRATE_FAILURE_MIN_GAP_HOURS": "1",
    "ORCHESTRATE_LOG_MAX_BYTES": str(5 * 1024 * 1024),
    "ORCHESTRATE_LOG_KEEP": "5",
    "ORCHESTRATE_DISPATCH": "on",
    # vws-deploy#521: off = full wake-thread silence (no wake_verify, lead_wake, grok_wake, grok_rework,
    # rebuild_wake) and no metrics-alert comment. STOP sets it off, RESUME sets it on. DISPATCH=off implies it.
    "ORCHESTRATE_WAKES": "on",
    # vws-deploy#487: needs-verify and verify-approved follow each PR head (decide_verify_labels). off = no label writes.
    "ORCHESTRATE_VERIFY_LABELS": "on",
    "ORCHESTRATE_MERGE": "dry-run",
    "ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS": "2",
    "ORCHESTRATE_MERGE_MAX_ATTEMPTS": "2",
    # vws-deploy#486: tick a merged PR's AC: boxes on its Part of issue (off, dry-run = plan only, apply).
    "ORCHESTRATE_AC_TICK": "apply",
    "ORCHESTRATE_BOARD": "dry-run",
    "ORCHESTRATE_BOARD_PROJECT": "PVT_kwHOBK54jM4BoB_Z",
    "ORCHESTRATE_BOARD_EVERY": "6",
    "ORCHESTRATE_BOARD_WRITES": "50",
    # Classic PAT with only the `project` scope, used for board_sync GraphQL calls and nothing else.
    "ORCHESTRATE_BOARD_TOKEN_FILE": "/root/.config/vws/board-sync-token",
}


# ---------------------------------------------------------------- config, paths and time

def utc(ts):
    """Parse a GitHub timestamp (2026-10-21T05:54:25Z) to epoch seconds."""
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_target(cfg):
    """The status issue in vws-deploy: the orchestrator edits its body; it never comments there."""
    return f"{STATUS_REPO}#{int(cfg['ORCHESTRATE_STATUS_ISSUE'])}"


def wake_target(cfg):
    """The verify-wake thread in vws-deploy ('' = unset: Rule A plans no wake). Never the status issue."""
    n = cfg.get("ORCHESTRATE_WAKE_THREAD", "")
    return f"{STATUS_REPO}#{int(n)}" if n else ""


def num(cfg, key):
    return float(cfg[key])


# ---------------------------------------------------------------- structured log

def scrub(text):
    return TOKEN_RE.sub("[redacted]", str(text))[:300]


# ---------------------------------------------------------------- parsing

DEP_RE = re.compile(r"(?im)^\s*depends on\s+(.+)$")
REF_RE = re.compile(r"^(?:(vws-[a-z0-9-]+))?#([0-9]+)$")
REPORT_RE = re.compile(r"^\[deepseek-dev\]\s+(Done|Failed)\s+for\s+(\S+?)(?::\s+(.*))?$")
PR_RE = re.compile(r"PR:\s*(https://github\.com/[\w.-]+/[\w.-]+/pull/[0-9]+)")
URL_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/([0-9]+)")


def parse_deps(body, repo):
    """'Depends on #N' or 'Depends on vws-<repo>#N', comma separated. Returns (keys, bad tokens)."""
    deps, bad = [], []
    for m in DEP_RE.finditer(body or ""):
        for tok in m.group(1).split(","):
            tok = tok.strip().rstrip(".")
            if not tok:
                continue
            r = REF_RE.match(tok)
            if not r:
                bad.append(tok)
                continue
            key = f"{r.group(1) or repo}#{r.group(2)}"
            if key not in deps:
                deps.append(key)
    return deps, bad


def ci_fail_marker_line(rec):
    """#420 marker_line: line 1 for a record; raises on an invalid field (KeyError/TypeError/ValueError)."""
    if not (CI_FAIL_SHA_RE.fullmatch(rec["head_sha"]) and CI_FAIL_REPO_RE.fullmatch(rec["repo"])
            and rec["owner"] in CI_FAIL_OWNERS and (rec["agent"] == "" or CI_FAIL_AGENT_RE.fullmatch(rec["agent"]))):
        raise ValueError("record has an invalid marker field")
    return (f"<!-- vws-ci-fail sha={rec['head_sha']} v={int(rec['v'])} repo={rec['repo']} pr={int(rec['pr'])} "
            f"owner={rec['owner']} agent={rec['agent'] or '-'} n={int(rec['n'])} "
            f"wake={'true' if rec['wake'] else 'false'} -->")


def ci_fail_record(body):
    """#420 parse_comment: the JSON record of a CI-failure comment, or None when line 1 and the record
    (the last json block) disagree or are absent."""
    body = (body or "").replace("\r\n", "\n")
    m = tr.ci_fail_match(body.split("\n", 1)[0])
    blocks = CI_FAIL_JSON_RE.findall(body)
    if not m or not blocks:
        return None
    try:
        rec = json.loads(blocks[-1])
    except ValueError:
        return None
    try:
        if not isinstance(rec, dict) or ci_fail_marker_line(rec) != m.group(0):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return rec


def classify_comment(body):
    """Return (kind, data) for comments the orchestrator cares about, else None."""
    rec = ci_fail_record(body)
    if rec is not None:
        m = tr.ci_fail_match((body or "").replace("\r\n", "\n").split("\n", 1)[0])
        return "ci_fail", {"sha": m.group(1), "v": int(m.group(2)), "repo": m.group(3), "pr": int(m.group(4)),
                           "owner": m.group(5), "agent": m.group(6), "n": int(m.group(7)),
                           "wake": m.group(8) == "true", "record": rec}
    text = (body or "").lstrip()
    first = text.split("\n", 1)[0].strip()
    if text.startswith(DEEPSEEK):
        return "dispatch", {}
    m = REPORT_RE.match(first)
    if m:
        pr = PR_RE.search(text)
        return "report", {"result": m.group(1).lower(), "for": m.group(2), "reason": m.group(3) or "",
                          "pr": pr.group(1) if pr else ""}
    if text.startswith("/orchestrate reset-failures"):
        return "reset", {}
    m = VERDICT_RE.match(text)
    if m:
        return "verdict", {"target": f"{m.group(1)}#{m.group(2)}", "sha": m.group(3), "result": m.group(4).lower()}
    kind = tr.marker_kind(text)
    if kind:
        return "marker", {"marker": kind}
    return None


def vws_pr_key(url):
    """'repo#n' for a PR URL in popododo0720/vws-*, else '' (the gh allowlist refuses other repos)."""
    m = URL_RE.match(url or "")
    if not m or m.group(1) != OWNER or not re.fullmatch(r"vws-[a-z0-9-]+", m.group(2)):
        return ""
    return f"{m.group(2)}#{m.group(3)}"


def reset_time(item):
    """Latest human reset: a /orchestrate reset-failures comment, or deepseek re-added after grok."""
    t = 0.0
    for c in item.get("comments", []):
        k = classify_comment(c["body"])
        if k and k[0] == "reset":
            t = max(t, utc(c["created_at"]))
    seen_grok = False
    for e in sorted(item.get("events", []), key=lambda e: e["created_at"]):
        label = (e.get("label") or {}).get("name")
        if label == "grok" and e.get("event") in ("labeled", "unlabeled"):
            seen_grok = True
        if label == "deepseek" and e.get("event") == "labeled" and seen_grok:
            t = max(t, utc(e["created_at"]))
    return t


def ready_time(item):
    t = None
    for e in item.get("events", []):
        if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == "ready":
            t = max(t or 0, utc(e["created_at"]))
    return t if t is not None else utc(item.get("created_at", "1970-01-01T00:00:00Z"))


def dep_state(key, snapshot, repos):
    repo = key.split("#")[0]
    if repo not in repos:
        return "outside"
    d = snapshot["items"].get(key) or snapshot.get("deps", {}).get(key)
    if not d or d.get("unreadable"):
        return "unreadable"
    if d.get("merged") or (d.get("state") == "closed" and d.get("state_reason") == "completed"):
        return "met"
    return "open"


def find_cycles(snapshot):
    """Nodes on a dependency cycle. Iterative three-colour DFS: each node and edge is visited once."""
    graph = {k: parse_deps(v.get("body"), v["repo"])[0] for k, v in snapshot["items"].items()}
    colour, cyclic = {}, set()
    for root in sorted(graph):
        if colour.get(root):
            continue
        stack, path = [(root, iter(graph.get(root, [])))], [root]
        colour[root] = 1
        while stack:
            node, edges = stack[-1]
            nxt = next(edges, None)
            if nxt is None:
                colour[node] = 2
                stack.pop()
                path.pop()
            elif colour.get(nxt) == 1:
                cyclic.update(path[path.index(nxt):])
            elif not colour.get(nxt):
                colour[nxt] = 1
                stack.append((nxt, iter(graph.get(nxt, []))))
                path.append(nxt)
    return cyclic


# ---------------------------------------------------------------- rule D: serialized merges (pure)

def run_order(r):
    # A queued run has no started_at yet; it counts as the newest so it holds the gate.
    return (r.get("started_at") or "9999", r.get("id", 0))


def check_key(r):
    """(name, app id, source). An Actions re-run gets a new check suite, so Actions runs are keyed by their
    workflow (path or id, plus event); a suite not mapped to a workflow stays alone (an old failure there keeps blocking).
    Other apps keep (name, app id)."""
    if r.get("app_id") != ACTIONS_APP_ID:
        return (r.get("name"), r.get("app_id"), "")
    if r.get("workflow"):
        return (r.get("name"), r.get("app_id"), f"wf:{r['workflow']}")
    return (r.get("name"), r.get("app_id"), f"suite:{r.get('suite_id')}")


def latest_runs(check_runs):
    """Newest check run (started_at, then id) per check_key."""
    out = {}
    for r in check_runs or []:
        k = check_key(r)
        if k not in out or run_order(r) > run_order(out[k]):
            out[k] = r
    return out


def run_state(r):
    if r.get("status") != "completed":
        return "pending"
    return "ok" if r.get("conclusion") in CHECK_OK else "failed"


def gate_from_repo_guard(r):
    """True when the run's check suite maps to GATE_WORKFLOW (key "<path>@<event>"). An unmapped suite fails closed."""
    return (r.get("workflow") or "").rpartition("@")[0] == GATE_WORKFLOW


def head_check_reasons(info, required):
    """Why the PR head's checks do not allow a merge ([] = they do). Pure.
    info: {"head_sha", "checks": [{"id", "name", "app_id", "suite_id", "status", "conclusion", "head_sha"}],
    "statuses": [{"context", "state"}], "checks_truncated"}; required: [{"context", "app_id"}] or None."""
    reasons = []
    runs = latest_runs([r for r in info.get("checks", []) if r.get("head_sha") == info.get("head_sha")])
    vs = [r for (name, app, _s), r in runs.items() if name == VERIFY_CHECK and app == VERIFY_APP_ID]
    v = max(vs, key=run_order) if vs else None
    if v is None:
        reasons.append("verify_check_missing")
    elif v.get("status") != "completed" or v.get("conclusion") != "success":
        reasons.append(f"verify_check_{v.get('conclusion') or v.get('status')}")
    # Every verify-gate run from GitHub Actions on the head must be success; a success elsewhere cannot hide a failure.
    gates = [r for (name, app, _s), r in runs.items() if name == GATE_CHECK and app == ACTIONS_APP_ID]
    bad_gates = [r for r in gates if r.get("status") != "completed" or r.get("conclusion") != "success"]
    if not gates:
        reasons.append("verify_gate_missing")
    elif not [r for r in gates if gate_from_repo_guard(r)]:
        reasons.append("verify_gate_not_repo_guard")
    if gates and bad_gates:
        g = max(bad_gates, key=run_order)
        reasons.append("verify_gate_pending" if g.get("status") != "completed" else f"verify_gate_{g.get('conclusion')}")
    if info.get("checks_truncated"):
        reasons.append("checks_truncated")
    if info.get("statuses_truncated"):
        reasons.append("statuses_truncated")
    if info.get("statuses") and info.get("status_state") not in (None, "success"):
        reasons.append(f"combined_status_{info.get('status_state')}")
    statuses = {}
    for st in info.get("statuses", []):
        statuses.setdefault(st.get("context"), st)  # the combined status lists the newest per context
    if required is None:
        reasons.append("required_checks_unreadable")
    else:
        for req in sorted(required, key=lambda q: (q["context"], q.get("app_id") or 0)):
            hits = [(app, r) for (name, app, _s), r in runs.items() if name == req["context"]
                    and (req.get("app_id") in (None, -1) or app == req["app_id"])]
            match = [r for _app, r in hits]
            if match:
                # Every distinct check with this name must pass; a success elsewhere cannot hide a failure.
                # A skip passes only next to a success of the same name and app (vws-deploy#505): a workflow on
                # both pull_request and pull_request_target skips its pull_request-only job on the other event.
                # The app is the run's own, so a requirement bound to no app cannot borrow another app's success.
                ok = {app for app, x in hits if x.get("status") == "completed" and x.get("conclusion") == "success"}
                bad = [x for app, x in hits if x.get("status") != "completed"
                       or x.get("conclusion") not in (("success", "skipped") if app in ok else ("success",))]
                r = max(bad, key=run_order) if bad else None
                if r is None:
                    pass
                elif r.get("status") != "completed":
                    reasons.append(f"required_pending:{req['context']}")
                else:
                    reasons.append(f"required_{r.get('conclusion')}:{req['context']}")
            elif req["context"] in statuses and req.get("app_id") in (None, -1):
                # A commit status has no app, so it can only satisfy a requirement not bound to one.
                state = statuses[req["context"]].get("state")
                if state != "success":
                    reasons.append(f"required_{state}:{req['context']}")
            else:
                reasons.append(f"required_missing:{req['context']}")
    # AGENTS.md rule 5: every check on the head is success (skipped and neutral helpers allowed).
    for (name, _app, _s), r in sorted(runs.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0, kv[0][2])):
        st = run_state(r)
        if st != "ok":
            reasons.append(f"check_{st}:{name}")
    for ctx, st in sorted(statuses.items(), key=lambda kv: str(kv[0])):
        if st.get("state") != "success":
            reasons.append(f"status_{st.get('state')}:{ctx}")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# vws-deploy#486: the acceptance-criteria boxes a PR covers, one `AC: <n>` line per box of its `Part of`
# issue (n from 1), or one `AC: none - <reason>`. Same syntax as pr-body in scripts/guard-agents.sh;
# tests/orchestrate/test_ac_parity.py runs one set of vectors through both.
AC_LINE_RE = re.compile(r"^[ \t]*AC:(.*)$", re.M)
AC_BOX_RE = re.compile(r"[ \t]*([1-9][0-9]*)[ \t]*")
AC_NONE_RE = re.compile(r"[ \t]*none[ \t]+-[ \t]+([^ \t\n\r\f\v].*?)[ \t]*")


def visible_text(text):
    """The text a reader sees: the Python port of guard-agents.sh visible_text, the one normalizer for PR,
    comment and commit text (vws-deploy#439 class 2; tests/fixtures/guard-text/corpus.json pins both).
    A CRLF line end is read as LF; HTML comments (also across lines) and fenced code blocks (``` or ~~~,
    indented by spaces and tabs only, closed only by a run of the same character at least as long) are
    removed; an unclosed comment or fence runs to the end."""
    out, incom, fence, fch, flen = [], False, False, "", 0
    for line in (text or "").split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if not incom and not fence:
            m = re.match(r"[ \t]*(`{3,}|~{3,})", line)
            if m:
                fence, fch, flen = True, m.group(1)[0], len(m.group(1))
                continue
        if fence:
            m = re.fullmatch(r"[ \t]*(`{3,}|~{3,})[ \t]*", line)
            if m and m.group(1)[0] == fch and len(m.group(1)) >= flen:
                fence = False
            continue
        seen = ""
        while True:
            if incom:
                i = line.find("-->")
                if i < 0:
                    break
                line, incom = line[i + 3:], False
            else:
                i = line.find("<!--")
                if i < 0:
                    seen += line
                    break
                seen, line, incom = seen + line[:i], line[i + 4:], True
        out.append(seen)
    return "\n".join(out)


def parse_ac(body):
    """The `AC:` lines of a PR body's visible text: {"boxes": sorted box numbers, "none": the reason of
    `AC: none - <reason>`, "error": why the lines are invalid, "" when valid}."""
    boxes, none = set(), ""
    lines = [m.group(0).strip() for m in AC_LINE_RE.finditer(visible_text((body or "").replace("\r", "")))]
    if not lines:
        return {"boxes": [], "none": "", "error": "no 'AC: <n>' or 'AC: none - <reason>' line"}
    for line in lines:
        rest = line[3:]
        b, n = AC_BOX_RE.fullmatch(rest), AC_NONE_RE.fullmatch(rest)
        if b:
            boxes.add(int(b.group(1)))
        elif n:
            none = n.group(1)
        else:
            return {"boxes": [], "none": "", "error": f"'{line}' is not 'AC: <n>' (n from 1) or 'AC: none - <reason>'"}
    if boxes and none:
        return {"boxes": [], "none": "", "error": "'AC: none - <reason>' cannot be combined with 'AC: <n>' lines"}
    return {"boxes": sorted(boxes), "none": none, "error": ""}


AC_HEADING_RE = re.compile(r"^#{1,6}[ \t]+Acceptance criteria[ \t]*$", re.I)
AC_ANY_HEADING_RE = re.compile(r"^#{1,6}[ \t]")
AC_ISSUE_BOX_RE = re.compile(r"^([ \t]*[-*] \[)([ xX])(\] )(.*)$")  # the box lines .github/ac/ac-lint.sh lists
AC_TICK_LABELS = ("ac-partial", "ac-pending")


def ac_boxes(body):
    """The boxes under the issue's `## Acceptance criteria` heading, up to the next heading:
    [{"n": 1-based index, "checked", "text", "line": index into body.split("\n")}]."""
    out, inside = [], False
    for i, raw in enumerate((body or "").split("\n")):
        line = raw[:-1] if raw.endswith("\r") else raw
        if AC_HEADING_RE.match(line):
            inside = True
            continue
        if inside and AC_ANY_HEADING_RE.match(line):
            break
        m = AC_ISSUE_BOX_RE.match(line) if inside else None
        if m:
            out.append({"n": len(out) + 1, "checked": m.group(2) != " ", "text": m.group(4), "line": i})
    return out


def ac_tick_body(body, boxes, pr_url, sha):
    """Pure: tick the listed boxes of an issue body, each with an evidence suffix ac-lint.sh accepts.
    {"body", "ticked": boxes newly ticked, "open": boxes still open after, "error": "" | ac_no_section |
    ac_index_invalid}. Any invalid index means no edit at all."""
    found = ac_boxes(body)
    if not found:
        return {"body": body, "ticked": [], "open": [], "error": "ac_no_section"}
    if any(n > len(found) for n in boxes):
        return {"body": body, "ticked": [], "open": [], "error": "ac_index_invalid"}
    lines = (body or "").split("\n")
    ticked = []
    for b in found:
        if b["n"] in boxes and not b["checked"]:
            raw = lines[b["line"]]
            cr = "\r" if raw.endswith("\r") else ""
            m = AC_ISSUE_BOX_RE.match(raw[:-1] if cr else raw)
            lines[b["line"]] = (f"{m.group(1)}x{m.group(3)}{m.group(4).rstrip()} (evidence: {pr_url} merged {sha}){cr}")
            b["checked"] = True
            ticked.append(b["n"])
    return {"body": "\n".join(lines), "ticked": ticked, "open": [b for b in found if not b["checked"]], "error": ""}


def ac_own_ticks(body, boxes, pr_url, sha):
    """Pure: the listed boxes that are ticked with this PR's own evidence suffix (ac_tick_body's), so a rerun
    after a tick that wrote the body but not the marker still reports them as ticked by this PR."""
    lines, suffix = (body or "").split("\n"), f"(evidence: {pr_url} merged {sha})"
    return [b["n"] for b in ac_boxes(body) if b["n"] in boxes and b["checked"]
            and lines[b["line"]].rstrip("\r").endswith(suffix)]


def ac_recoverable(state, state_reason, body, boxes, pr_url, sha):
    """Pure (vws-deploy#517 class 3, the #495 finding): True when a closed issue is the end state of this PR's
    own tick whose marker comment never landed: closed as completed, every box ticked, and at least one of the
    PR's boxes carries its evidence. The rerun then posts only the missing label removals and the marker. Any
    other closed issue (a human close, not planned, an open box) is left alone."""
    found = ac_boxes(body)
    return (state == "closed" and state_reason == "completed" and bool(found) and all(b["checked"] for b in found)
            and bool(ac_own_ticks(body, boxes, pr_url, sha)))


def agent_of(body):
    """The body's 'Agent:' value: logged on merge, the owner a rebuild wake names (Rule R), and the owner
    a grok-wake names (vws-deploy#506). CRLF line ends count; lines in code fences or HTML comments do not
    (visible_text, the one normalizer). No eligibility rule depends on it."""
    m = AGENT_RE.search(visible_text(body))
    return m.group(1)[:64] if m else ""


def owner_repo_part_of(body):
    """True when Rule D skips the body with part_of_owner_repo: no Part of it reads, but this owner's
    owner/repo form (vws-deploy#477, #504)."""
    body = body or ""
    return not PART_OF_RE.search(body) and bool(OWNER_PART_OF_RE.search(body))


def conflict_only(body):
    """True when every blocking bullet of a FAIL verdict is a merge conflict / rebase request."""
    bullets = [ln.strip()[2:] for ln in (body or "").splitlines() if ln.strip().startswith("- ")]
    blocking = [b for b in bullets if not re.match(r"(?i)(codex|follow-?ups?|non-blocking)\b", b)] or [body or ""]
    return all(CONFLICT_RE.search(b) for b in blocking)


def head_verdict(pr):
    """The newest vws-verify verdict (review or verdict comment) on the PR's current head, or None."""
    k = f"{pr['repo']}#{pr['number']}"
    vs = [v for v in pr.get("reviews", []) if v["sha"] == pr["head_sha"]]
    for c in pr.get("comments", []):
        kind = classify_comment(c["body"])
        if kind and kind[0] == "verdict" and kind[1]["target"] == k and kind[1]["sha"] == pr["head_sha"]:
            vs.append({"id": c["id"], "url": c["url"], "at": c["created_at"], "sha": kind[1]["sha"],
                       "result": kind[1]["result"], "body": c["body"]})
    return max(vs, key=lambda v: (v["at"], v["id"]), default=None)


def rework_prs(k, it, items):
    """Open PRs a ready+deepseek item owns: the item itself when it is a PR, else the open vws PRs its
    DeepSeek reports named, then (vws-deploy#457) open PRs whose `Part of` names it and whose owner
    resolves to deepseek. A PR that is itself a ready+deepseek item is reworked on its own. A PR labeled
    `blocked` or `grok` is not reworked, even when its issue is eligible (vws-deploy#376)."""
    def target(pk):
        p = items.get(pk) or {}
        return (p.get("is_pr") and p.get("state") == "open"
                and not ({"blocked", "grok"} & set(p.get("labels") or [])))

    if not {"ready", "deepseek"} <= set(it["labels"]) or "grok" in it["labels"]:
        return []
    if it["is_pr"]:
        return [k] if target(k) else []
    keys = [vws_pr_key(u) for u, p in sorted(it.get("report_prs", {}).items()) if p.get("state") == "open"]
    out = [pk for pk in keys if pk and target(pk)]
    for pk in sorted(items):
        p = items[pk]
        if (pk not in out and target(pk) and grok_link(p) == k
                and not {"ready", "deepseek"} <= set(p["labels"]) and pr_owner(p, items)[0] == "deepseek"):
            out.append(pk)
    return out


def pr_owner(pr, items):
    """vws-deploy#457: who owns a PR, as (owner, via). In order: the PR body `Agent:` line (via "agent"),
    the PR's grok or deepseek label ("label"), then the grok or deepseek label of the item its `Part of`
    names ("part_of:<repo#N>"). grok wins over deepseek, as in rework_prs. ("", "") when none resolves."""
    agent = agent_of(pr.get("body"))
    if agent:
        return agent, "agent"
    for name in ("grok", "deepseek"):
        if name in pr["labels"]:
            return name, "label"
    link = grok_link(pr)
    for name in ("grok", "deepseek"):
        if link and name in items.get(link, {}).get("labels", []):
            return name, f"part_of:{link}"
    return "", ""


def ci_fail_author_ok(c):
    """True for github-actions[bot], or for the orchestrator's own login (the #446 second-failure marker).
    A comment with no login, or any other login, is not trusted."""
    return c.get("author") == CI_FAIL_LOGIN or tr.own_comment(c)


def ci_fails(pr):
    """CI-failure markers on a PR, oldest first: (accepted, untrusted). Accepted means posted by
    github-actions[bot] or by this orchestrator's login, v=1, and naming this repo#N."""
    ok, bad = [], []
    for c in sorted(pr.get("comments", []), key=lambda c: (c["created_at"], c["id"])):
        kind = classify_comment(c["body"])
        if not kind or kind[0] != "ci_fail":
            continue
        d = kind[1]
        if not ci_fail_author_ok(c):
            bad.append(c)
        elif d["v"] == 1 and d["repo"] == pr["repo"] and d["pr"] == pr["number"]:
            ok.append(dict(d, id=c["id"], url=c["url"], at=c["created_at"], body=c["body"]))
    return ok, bad


def authorized_reset(c):
    """A `/orchestrate reset-failures` comment by an OWNER, MEMBER or COLLABORATOR (author_association as
    recorded by comment_record). Only the CI cap uses it so far; vws-deploy#419 can reuse it."""
    kind = classify_comment(c.get("body"))
    return bool(kind and kind[0] == "reset" and c.get("assoc") in RESET_ASSOC)


def ci_reset_time(pr, owner):
    """Latest CI-cap reset: /orchestrate reset-failures by an OWNER, MEMBER or COLLABORATOR on the PR or
    its owner item, or needs-human removed from the PR."""
    t = 0.0
    for c in pr.get("comments", []) + (owner.get("comments", []) if owner is not pr else []):
        if authorized_reset(c):
            t = max(t, utc(c["created_at"]))
    for e in pr.get("events", []):
        if e.get("event") == "unlabeled" and (e.get("label") or {}).get("name") == "needs-human":
            t = max(t, utc(e["created_at"]))
    return t


def ci_fail_head(pr, owner):
    """The CI failure on the PR's current head and why it is not reworked ('' = rework it), or (None, '').
    n is recomputed: the distinct failing shas on this PR since the last CI reset, this head included.
    A marker posted after that reset can only raise n (it also counts rebuilt predecessors)."""
    ok, _ = ci_fails(pr)
    m = next((x for x in ok if x["sha"] == pr["head_sha"]), None)
    if not m:
        return None, ""
    since = ci_reset_time(pr, owner)
    shas = []
    for x in ok:
        if utc(x["at"]) > since and x["sha"] not in shas:
            shas.append(x["sha"])
    n = shas.index(m["sha"]) + 1 if m["sha"] in shas else len(shas) + 1
    if utc(m["at"]) > since:
        n = max(n, m["n"])
    m = dict(m, n=n)
    labels = set(pr["labels"]) | set(owner["labels"])
    if m["owner"] != "deepseek":
        return m, f"owner_{m['owner']}"
    for reason, bad in (("wake_false", not m["wake"]),
                        ("needs-human", "needs-human" in labels), ("blocked", "blocked" in labels),
                        ("cap", n > CI_FAIL_CAP)):
        if bad:
            return m, reason
    return m, ""


def quote(text):
    return "\n".join("> " + ln for ln in text[:3000].splitlines())


def grok_allowlist(text):
    """ORCHESTRATE_GROK_ALLOWLIST: repo#N entries split on commas or whitespace. Returns (keys, bad)."""
    toks = [t for t in re.split(r"[,\s]+", text or "") if t]
    return {t for t in toks if GROK_ALLOW_RE.fullmatch(t)}, [t for t in toks if not GROK_ALLOW_RE.fullmatch(t)]


def grok_owners(cfg):
    """ORCHESTRATE_GROK_OWNERS as {repo: bot}; entries split on commas or whitespace. Raises ValueError on
    an entry that is not repo=bot (load_config checks it once, so decide never sees a bad value)."""
    out = {}
    for t in re.split(r"[,\s]+", cfg.get("ORCHESTRATE_GROK_OWNERS", DEFAULTS["ORCHESTRATE_GROK_OWNERS"])):
        m = GROK_OWNER_RE.fullmatch(t)
        if t and not m:
            raise ValueError(f"ORCHESTRATE_GROK_OWNERS entries must be vws-<repo>=<bot>, got {t[:64]!r}")
        if m:
            out[m.group(1)] = m.group(2)
    return out


def grok_bot(it, cfg):
    """vws-deploy#506: the Grok dev bot that owns issue or PR it, as (bot, via): its body's `Agent:` line
    ("agent"), else ORCHESTRATE_GROK_OWNERS for its repo, else the bot named after its repo ("repo:<repo>")."""
    agent = agent_of(it.get("body"))
    if agent:
        return agent, "agent"
    return grok_owners(cfg).get(it["repo"], it["repo"]), f"repo:{it['repo']}"


def grok_link(it):
    """The issue a PR's `Part of #N` / `Part of repo#N` line names, as repo#N, or ""."""
    m = PART_OF_RE.search(it.get("body") or "") if it.get("is_pr") else None
    return "" if not m else (it["repo"] + m.group(1) if m.group(1).startswith("#") else m.group(1))


def _issue_ref(repo, ref):
    """#N is this PR's repo; vws-x#N is that repo."""
    return repo + ref if ref.startswith("#") else ref


def pr_linked_issues(it):
    """Issue keys (repo#N) an open PR names with Part of, Closes, Fixes or Resolves.
    Empty when it is not an open PR. A keyword inside a code fence or HTML comment does not count
    (visible_text). A draft still counts: it is an open PR (vws-deploy#560)."""
    if not it.get("is_pr") or it.get("state") != "open":
        return set()
    keys = set()
    repo = it["repo"]
    for raw in (it.get("body") or "", it.get("title") or ""):
        text = visible_text(raw)
        for m in PART_OF_RE.finditer(text):
            keys.add(_issue_ref(repo, m.group(1)))
        for m in PART_OF_OWNER_REPO_REF_RE.finditer(text):
            keys.add(m.group(1))
        for m in CLOSING_LINK_RE.finditer(text):
            keys.add(_issue_ref(repo, m.group(1)))
    return keys


def open_pr_links(items):
    """Issue keys an open PR names with Part of, Closes, Fixes or Resolves."""
    linked = set()
    for it in items.values():
        linked |= pr_linked_issues(it)
    return linked


def ac_partial_idle(it, items):
    """vws-deploy#545: rule T left this issue labeled ac-partial (some acceptance-criteria boxes ticked,
    some still open) and no open PR names it. The assignee is not working a PR, so the issue does not
    hold the Grok slot; decide_grok posts one wake for the remaining boxes instead."""
    return ac_partial_slice(it) and not grok_open_pr(f"{it['repo']}#{it['number']}", items)


def ac_partial_slice(it):
    """An open issue labeled ac-partial with some acceptance-criteria boxes ticked and some still open."""
    if it.get("is_pr") or it.get("state") != "open" or "ac-partial" not in it.get("labels", []):
        return False
    boxes = ac_boxes(it.get("body") or "")
    return any(b["checked"] for b in boxes) and not all(b["checked"] for b in boxes)


def grok_open_pr(k, items):
    """An open PR whose `Part of` names issue k."""
    return any(p.get("is_pr") and p.get("state") == "open" and grok_link(p) == k for p in items.values())


def linked_open_prs(k, items):
    """Open PRs whose `Part of` names issue k."""
    return [p for p in items.values()
            if p.get("is_pr") and p.get("state") == "open" and grok_link(p) == k]


def grok_parked_blocked(k, items):
    """Soft-CAP: the issue or any linked open PR has blocked|hold|do-not-merge|needs-human.
    Does not count against the Grok CAP; labels are left as-is (no strip)."""
    it = items.get(k)
    if it and set(it.get("labels") or []) & PARK_BLOCK_LABELS:
        return True
    return any(set(p.get("labels") or []) & PARK_BLOCK_LABELS for p in linked_open_prs(k, items))


def grok_parked_verify(k, items):
    """Soft-CAP: every linked open PR is non-draft and has needs-verify|verify-approved.
    Does not count against the Grok CAP; keeps the repo in grok_busy; keeps `grok` on the issue
    (B4 rework routing needs it)."""
    prs = linked_open_prs(k, items)
    if not prs:
        return False
    for p in prs:
        if p.get("draft"):
            return False
        if not (set(p.get("labels") or []) & PARK_VERIFY_LABELS):
            return False
    return True


def grok_cap_parked(k, items):
    """True when soft-CAP parks k (blocked/human hold, or verify-owned linked PRs)."""
    return grok_parked_blocked(k, items) or grok_parked_verify(k, items)


def grok_held(items):
    """Grok work in flight without a wake key: an open grok item with an assignee, or the issue an open
    grok PR (draft too) names with `Part of`. An ac-partial issue with boxes still open and no open PR
    is not in flight (vws-deploy#545). Soft-CAP parks (blocked/hold/do-not-merge/needs-human on the issue
    or a linked open PR, or every linked open PR verify-owned) do not hold the CAP. Returns the set of
    held repo#N (the linked issue for a PR)."""
    held = set()
    for k, it in items.items():
        if it["state"] != "open" or "grok" not in it["labels"]:
            continue
        if it["is_pr"]:
            link = grok_link(it)
            if link:
                if not grok_cap_parked(link, items):
                    held.add(link)
            elif it.get("assignees"):
                held.add(k)  # assigned grok PR without Part of still holds the CAP
        elif it.get("assignees") and not ac_partial_idle(it, items) and not grok_cap_parked(k, items):
            held.add(k)
    return held


def grok_busy(items, held):
    """Repos with Grok work in flight: the repo of each held task, plus the repo of each open grok PR
    that names an issue (a PR in vws-proto with `Part of vws-iam#1` keeps both repos busy). Soft-CAP
    verify-parked issues stay busy (one-per-repo) even though they do not hold the CAP; blocked parks do not."""
    busy = {k.split("#")[0] for k in held} | {
        it["repo"] for it in items.values()
        if it["state"] == "open" and "grok" in it["labels"] and it["is_pr"] and grok_link(it)}
    for k, it in items.items():
        if it["state"] != "open" or "grok" not in it.get("labels", []) or it.get("is_pr"):
            continue
        if grok_parked_verify(k, items):
            busy.add(it["repo"])
            for p in linked_open_prs(k, items):
                busy.add(p["repo"])
    return busy


def status_keys(snapshot, state):
    """Keys of merge and infra entries already reported: in the status issue body, in state, or (before
    vws-deploy#416) as comments on the verify-wake thread."""
    keys = set(snapshot.get("status", {}).get("keys", []))
    keys |= set(STATUS_EVENT_KEY_RE.findall(snapshot.get("status_issue", {}).get("body", "")))
    return keys | {e["key"] for e in state.get("status_events", [])}
