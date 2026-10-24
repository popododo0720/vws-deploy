#!/usr/bin/env python3
"""Deterministic factory orchestrator for the vws-* repos (vws-deploy#333). Python 3 stdlib only.

One tick: collect (gh API reads with an on-disk ETag cache) -> decide (pure) -> apply (writes only
when ORCHESTRATE_MODE=apply). It never calls an LLM. It runs on the shared box under
box/orchestrate-loop.sh with the box's gh login; GH_TOKEN is always removed from the gh environment.
The one exception is board_sync: its GraphQL calls run with GH_TOKEN set to the project-scope classic
PAT read from ORCHESTRATE_BOARD_TOKEN_FILE (no file: board_sync is skipped for that tick).

Rules (see docs/factory-orchestrator.md):
  A  wake vws-verify with one marker comment on the verify-wake thread (ORCHESTRATE_WAKE_THREAD; never
     the status issue) when needs-verify PRs wait (labeled by verify-queue.yml): the oldest for
     VERIFY_WAKE_MAX_WAIT_MIN or at least VERIFY_WAKE_THRESHOLD of them; one comment per pending set
  B  hand the next ready+deepseek tasks to DeepSeek, at most ORCHESTRATE_DEEPSEEK_CAP (2) in flight
     and one per repo, with an @deepseek-dev comment; a task whose Depends on target is still open is
     never dispatched
  C  count DeepSeek failures (Failed report, 3h timeout, Done PR closed unmerged); at 2, swap to grok.
     A Failed report that shows a provider error (INFRA_ERROR_RE) is an infra failure instead: never
     counted, re-mentioned ORCHESTRATE_INFRA_RETRY_DELAY_MIN after the report, at most
     INFRA_MAX_RETRIES times; then one entry in the status issue body and the item gets blocked
  G  wake the Grok dev bot with one marker comment on the verify-wake thread (ORCHESTRATE_WAKE_THREAD;
     unset: no Grok wake) for the top ready+grok issue on ORCHESTRATE_GROK_ALLOWLIST (empty: nothing is
     assigned), at most ORCHESTRATE_GROK_CAP (1) at a time and one per repo; a woken issue holds its slot
     while it stays open with grok
  D  squash-merge PRs that passed vws-verify, at most one per repo per tick, only while main's push CI
     is green for the current main head; after a merge the repo is locked until the push CI for the
     new main head is green (red stops merges there and adds one entry to the status issue body)

Status (vws-deploy#416): each apply tick rewrites the BODY of the status issue (ORCHESTRATE_STATUS_ISSUE)
in place, and only when its content changed; the orchestrator posts no status comments. A failed tick
streak is reported in that body too (--notify-failure, called by box/orchestrate-loop.sh).

Exit status: 0 tick done or skipped on purpose, 2 usage error, 4 tick failed (auth, API or
config error), 1 is an uncaught Python error.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER = "popododo0720"
STATUS_REPO = "vws-deploy"  # the status issue and the verify-wake thread live here; config picks the numbers
FAIL_EXIT = 4
DEEPSEEK = "@deepseek-dev"
DISPATCH_TEXT = (
    "@deepseek-dev please take this issue: one small PR per the acceptance criteria above. "
    "Small diff, no unrelated changes, no closing keywords."
)
# Rework (vws-deploy#373): a verify FAIL on the current head of a DeepSeek PR is re-dispatched on the
# same branch with the verdict quoted. The marker keys it to the head, so one head is reworked once.
REWORK_TEXT = (
    "@deepseek-dev please rework this PR: push new commits to this branch (no force-push, no new PR) "
    "that fix every blocking point of the verify verdict below. Small diff, no unrelated changes, no closing keywords."
)
REWORK_MARKER = "<!-- orchestrate:rework head="
VERIFY_LOGIN = "vws-verify[bot]"
# CI-failure marker (vws-deploy#412): a PR comment that #420's ci-fail workflow posts with the caller's
# GITHUB_TOKEN. Line 1 is the marker and a fenced json block is the record; the comment counts only as
# #420 parse_comment accepts it (.github/ci-fail/ci_fail.py, ported below). Only github-actions[bot]
# is trusted, and n/wake are recomputed from the comments (the marker can only tighten them).
CI_FAIL_LOGIN = "github-actions[bot]"
# Exactly #420 MARKER_RE, JSON_BLOCK_RE and marker_line's field checks.
CI_FAIL_RE = re.compile(r"<!-- vws-ci-fail sha=([0-9a-f]{40}) v=(\d+) repo=(vws-[a-z0-9-]+) pr=(\d+) "
                        r"owner=(deepseek|grok|none) agent=([a-z0-9][a-z0-9-]{0,38}|-) n=(\d+) wake=(true|false) -->")
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
# These checks fail by design until vws-verify approves, so they never make a PR "CI failing".
VERIFY_CHECKS = {"verify-gate", "vws-verify"}
CI_BAD_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
# The first line of a status comment is exactly one of these; vws-verify and vws-platform routines
# match on it.
WAKE_MARKER = "<!-- vws-orchestrate:verify-wake -->"
NEEDS_VERIFY = "needs-verify"  # set by verify-queue.yml while every non-verify check is green (vws-deploy#397)
FAILURE_MARKER = "<!-- vws-orchestrate:failure -->"
# Rule G (vws-deploy#378): the Grok dev bot's GitHub-trigger routine on the verify-wake thread matches this.
GROK_WAKE_MARKER = "<!-- vws-orchestrate:grok-wake -->"
# Lead wake (vws-deploy#397): dr eggbot's GitHub-trigger routine on the wake thread matches this first line.
LEAD_WAKE_MARKER = "<!-- vws-orchestrate:lead-wake -->"
# Rule D serial set (vws-deploy#397): PRs touching these merge one at a time across all repos, oldest approval first.
SERIAL_MERGE_RE = re.compile(r"^(?:\.github/orchestrate/orchestrate\.py|\.github/workflows/repo-guard[^/]*\.ya?ml|AGENTS\.md)$")
# The last line, so classify_comment() (and metrics, vws-deploy#400) sees a "grok-wake" marker comment.
GROK_ASSIGN_MARKER = "<!-- orchestrate:grok-wake item="
# vws-deploy#460: a PR to rebuild (labeled needs-rebuild, or conflicting with main) wakes its owner on the
# verify-wake thread, once per PR head.
REBUILD_WAKE_MARKER = "<!-- vws-orchestrate:rebuild-wake -->"
NEEDS_REBUILD = "needs-rebuild"
GROK_ALLOW_RE = re.compile(r"vws-[a-z0-9-]+#[1-9][0-9]*")
# The status issue (vws-deploy#416): the orchestrator owns only the block from STATUS_MARKER to
# STATUS_END_MARKER in its body and rewrites that block in place; text outside it is kept byte for byte,
# and a body without exactly one block is never written. Its comments are metrics alerts only
# (.github/metrics posts them); the orchestrator never comments there.
STATUS_MARKER = "<!-- vws-orchestrate:status -->"
STATUS_END_MARKER = "<!-- /vws-orchestrate:status -->"
METRICS_ALERT_MARKER = "<!-- vws-orchestrate:metrics-alert -->"
STATUS_UPDATED_RE = re.compile(r"^_Updated [^\n]*_$", re.M)  # the one volatile line; ignored when comparing
STATUS_EVENT_KEY_RE = re.compile(r"key `(sha256:[0-9a-f]{64})`")
STATUS_EVENTS_KEEP = 20
# Rule D (vws-deploy#346).
VERIFY_CHECK = "vws-verify"
VERIFY_APP_ID = 5313004
ACTIONS_APP_ID = 15368  # GitHub Actions
CHECK_OK = {"success", "neutral", "skipped"}
# blocked: a human or the dependency rule holds the PR (vws-deploy#393); it never merges while labeled.
OPT_OUT_LABELS = ("do-not-merge", "hold", "blocked")
# repo-guard / verify-gate must pass on the head by name, even when main's protection does not list it.
GATE_CHECK = "verify-gate"
OPT_OUT_MARKER = "<!-- vws-orchestrate:no-merge -->"
# GitHub's own verdict (REST mergeable_state = GraphQL mergeStateStatus) must be clean: it already counts
# main's protection and rulesets, which the PAT cannot read (vws-deploy#408). Anything else never merges.
MERGEABLE_OK = {"clean"}
# Rule D reads the same forms pr-hygiene accepts: `Part of #N` and `Part of vws-x#N` at the start
# of a line. The owner/repo form `Part of popododo0720/vws-x#N` is rejected by both (vws-deploy#477).
PART_OF_RE = re.compile(r"(?im)^\s*Part of\s+((?:vws-[a-z0-9-]+)?#[0-9]+)")
# The owner/repo form pr-hygiene rejects. Rule D names it in the reason instead of the generic
# no_part_of, so a PR skipped for it says what to change (#477).
OWNER_PART_OF_RE = re.compile(rf"(?im)^\s*Part of\s+{OWNER}/vws-[a-z0-9-]+#[0-9]+")
CLOSING_RE = re.compile(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b:?\s*([\w.-]+/[\w.-]+)?#[0-9]+")
# The #412 Agent-line spec: an unindented, case-sensitive "Agent: <name>" line; the first one wins.
AGENT_RE = re.compile(r"^Agent:[ \t]*([a-z0-9][a-z0-9-]{0,38})[ \t]*$", re.M)
# vws-deploy#419: who may write the comments the orchestrator acts on (the repos are public).
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
MERGE_PAYLOAD_KEYS = {"merge_method", "sha", "commit_title"}
KEY_RE = re.compile(r"^Idempotency key: `(sha256:[0-9a-f]{64})`$", re.M)
# A [deepseek-dev] Failed report is a provider (infra) failure, not a task failure, when its body has
# the last dsh line of the "Last output" block, `dsh: SERVER: 503: {...}` or `dsh: RATE_LIMIT: 429: ...`
# (the real reports on vws-proto#51, comments 6310291529 and 6311725351; the poller's format per the VM
# owner), or the first-line form `Failed for <url>: provider 503 (SERVER)`. A plain "dsh exited with
# code 1" without that line is a task failure. Bare HTTP statuses are never matched: a task's own test
# output can print them.
INFRA_ERROR_RE = re.compile(
    r"^dsh: (?:RATE_LIMIT|SERVER): \d{3}:"
    r"|^\[deepseek-dev\] Failed for \S+: provider \d{3} \((?:RATE_LIMIT|SERVER)\)",
    re.M)
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
    # vws-deploy#487: needs-verify and verify-approved follow each PR head (decide_verify_labels). off = no label writes.
    "ORCHESTRATE_VERIFY_LABELS": "on",
    "ORCHESTRATE_MERGE": "dry-run",
    "ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS": "2",
    "ORCHESTRATE_MERGE_MAX_ATTEMPTS": "2",
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


def read_env_file(path):
    """KEY=VALUE lines; no shell evaluation. A missing file is an empty config."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


class Paths:
    def __init__(self, environ):
        home = os.path.expanduser("~")
        self.config = environ.get("ORCHESTRATE_CONFIG_DIR") or os.path.join(home, ".config", "vws-orchestrate")
        self.state = environ.get("ORCHESTRATE_STATE_DIR") or os.path.join(home, ".local", "state", "vws-orchestrate")
        self.env_file = os.path.join(self.config, "env")
        self.stop = os.path.join(self.config, "STOP")
        self.state_file = os.path.join(self.state, "state.json")
        self.failure_file = os.path.join(self.state, "failure.json")
        self.health = os.path.join(self.state, "health.json")
        self.cache = os.path.join(self.state, "etag-cache.json")
        self.board_file = os.path.join(self.state, "board.json")
        self.lock = os.path.join(self.state, "run.lock")
        self.log_dir = os.path.join(self.state, "log")
        self.log = os.path.join(self.log_dir, "orchestrate.jsonl")


def load_config(paths, environ):
    cfg = dict(DEFAULTS)
    cfg.update(read_env_file(paths.env_file))
    for k in DEFAULTS:
        if k in environ:
            cfg[k] = environ[k]
    if cfg["ORCHESTRATE_MODE"] not in ("dry-run", "apply"):
        raise ValueError(f"ORCHESTRATE_MODE must be dry-run or apply, got {cfg['ORCHESTRATE_MODE']!r}")
    if cfg["ORCHESTRATE_SCOPE"] and not re.fullmatch(r"vws-[a-z0-9-]+#[0-9]+", cfg["ORCHESTRATE_SCOPE"]):
        raise ValueError("ORCHESTRATE_SCOPE must look like vws-deploy#123")
    if not re.fullmatch(r"[1-9][0-9]*", cfg["ORCHESTRATE_STATUS_ISSUE"]):
        raise ValueError(f"ORCHESTRATE_STATUS_ISSUE must be a positive integer, got {cfg['ORCHESTRATE_STATUS_ISSUE']!r}")
    if cfg["ORCHESTRATE_WAKE_THREAD"] and not re.fullmatch(r"[1-9][0-9]*", cfg["ORCHESTRATE_WAKE_THREAD"]):
        raise ValueError("ORCHESTRATE_WAKE_THREAD must be empty or a positive integer, "
                         f"got {cfg['ORCHESTRATE_WAKE_THREAD']!r}")
    if cfg["ORCHESTRATE_WAKE_THREAD"] == cfg["ORCHESTRATE_STATUS_ISSUE"]:
        raise ValueError("ORCHESTRATE_WAKE_THREAD must not be the status issue: verify-wake never posts there")
    if not re.fullmatch(r"[1-9][0-9]*", cfg["ORCHESTRATE_DEEPSEEK_CAP"]):
        raise ValueError(f"ORCHESTRATE_DEEPSEEK_CAP must be a positive integer, got {cfg['ORCHESTRATE_DEEPSEEK_CAP']!r}")
    logins = [x.strip() for x in cfg["ORCHESTRATE_DEEPSEEK_LOGINS"].split(",")]
    if not all(LOGIN_RE.fullmatch(x) for x in logins):
        raise ValueError("ORCHESTRATE_DEEPSEEK_LOGINS must be a comma-separated list of GitHub logins, "
                         f"got {cfg['ORCHESTRATE_DEEPSEEK_LOGINS']!r}")
    if not LOGIN_RE.fullmatch(cfg["ORCHESTRATE_VERIFY_LOGIN"]):
        raise ValueError(f"ORCHESTRATE_VERIFY_LOGIN must be one GitHub login, got {cfg['ORCHESTRATE_VERIFY_LOGIN']!r}")
    if not re.fullmatch(r"(?:[1-9][0-9]*)?", cfg["ORCHESTRATE_VERIFY_USER_ID"]):
        raise ValueError("ORCHESTRATE_VERIFY_USER_ID must be empty or a positive integer, "
                         f"got {cfg['ORCHESTRATE_VERIFY_USER_ID']!r}")
    if not re.fullmatch(r"[1-9][0-9]*", cfg["ORCHESTRATE_INFRA_RETRY_DELAY_MIN"]):
        raise ValueError("ORCHESTRATE_INFRA_RETRY_DELAY_MIN must be a positive integer (minutes), "
                         f"got {cfg['ORCHESTRATE_INFRA_RETRY_DELAY_MIN']!r}")
    if cfg["ORCHESTRATE_FAILURE_ALERT"] not in ("apply", "always", "never"):
        raise ValueError("ORCHESTRATE_FAILURE_ALERT must be apply, always or never")
    if cfg["ORCHESTRATE_DISPATCH"] not in ("on", "off"):
        raise ValueError("ORCHESTRATE_DISPATCH must be on or off")
    if cfg["ORCHESTRATE_VERIFY_LABELS"] not in ("on", "off"):
        raise ValueError("ORCHESTRATE_VERIFY_LABELS must be on or off")
    if cfg["ORCHESTRATE_MERGE"] not in ("off", "dry-run", "apply"):
        raise ValueError("ORCHESTRATE_MERGE must be off, dry-run or apply")
    if cfg["ORCHESTRATE_BOARD"] not in ("off", "dry-run", "apply"):
        raise ValueError("ORCHESTRATE_BOARD must be off, dry-run or apply")
    if not re.fullmatch(r"PVT_[A-Za-z0-9_-]+", cfg["ORCHESTRATE_BOARD_PROJECT"]):
        raise ValueError("ORCHESTRATE_BOARD_PROJECT must be a ProjectV2 node id (PVT_...)")
    for k in ("ORCHESTRATE_BOARD_EVERY", "ORCHESTRATE_BOARD_WRITES", "ORCHESTRATE_GROK_CAP"):
        if not re.fullmatch(r"[1-9][0-9]*", cfg[k]):
            raise ValueError(f"{k} must be a positive integer, got {cfg[k]!r}")
    tf = cfg["ORCHESTRATE_BOARD_TOKEN_FILE"]
    if not os.path.isabs(tf) or re.search(r"[\x00-\x1f]", tf):
        raise ValueError(f"ORCHESTRATE_BOARD_TOKEN_FILE must be an absolute path, got {tf!r}")
    text_keys = ("ORCHESTRATE_MODE", "ORCHESTRATE_SCOPE", "ORCHESTRATE_EXTRA_REPOS", "ORCHESTRATE_STATUS_ISSUE",
                 "ORCHESTRATE_WAKE_THREAD",
                 "ORCHESTRATE_FAILURE_ALERT", "ORCHESTRATE_DISPATCH", "ORCHESTRATE_VERIFY_LABELS", "ORCHESTRATE_MERGE", "ORCHESTRATE_BOARD", "ORCHESTRATE_BOARD_PROJECT", "ORCHESTRATE_GROK_ALLOWLIST",
                 "ORCHESTRATE_BOARD_TOKEN_FILE",
                 "ORCHESTRATE_DEEPSEEK_LOGINS", "ORCHESTRATE_VERIFY_LOGIN", "ORCHESTRATE_VERIFY_USER_ID")
    for k in DEFAULTS:
        if k not in text_keys:
            float(cfg[k])  # raises ValueError on a bad number
    # Other keys in the shared env file (METRICS_* for .github/metrics) belong to other scripts,
    # which validate them; they must not stop a tick.
    return cfg


def status_target(cfg):
    """The status issue in vws-deploy: the orchestrator edits its body; it never comments there."""
    return f"{STATUS_REPO}#{int(cfg['ORCHESTRATE_STATUS_ISSUE'])}"


def wake_target(cfg):
    """The verify-wake thread in vws-deploy ('' = unset: Rule A plans no wake). Never the status issue."""
    n = cfg.get("ORCHESTRATE_WAKE_THREAD", "")
    return f"{STATUS_REPO}#{int(n)}" if n else ""


def num(cfg, key):
    return float(cfg[key])


def ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def write_json(path, data):
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- structured log

def scrub(text):
    return TOKEN_RE.sub("[redacted]", str(text))[:300]


class Log:
    def __init__(self, paths, cfg, run_id, mode, echo=False):
        self.path, self.run_id, self.mode, self.echo = paths.log, run_id, mode, echo
        self.max_bytes = int(num(cfg, "ORCHESTRATE_LOG_MAX_BYTES"))
        self.keep = int(num(cfg, "ORCHESTRATE_LOG_KEEP"))
        ensure_dir(paths.log_dir)

    def rotate(self):
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except FileNotFoundError:
            return
        for i in range(self.keep - 1, 0, -1):
            src = f"{self.path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{self.path}.{i + 1}")
        os.replace(self.path, f"{self.path}.1")

    def __call__(self, event, **fields):
        rec = {"ts": iso(time.time()), "run_id": self.run_id, "mode": self.mode, "event": event}
        for k, v in fields.items():
            rec[k] = scrub(v) if isinstance(v, str) else v
        line = json.dumps(rec, sort_keys=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.echo:
            print(line, flush=True)


# ---------------------------------------------------------------- gh wrapper (allowlist + ETag cache)

_REPO = r"repos/" + OWNER + r"/vws-[a-z0-9-]+"
ALLOWED = {
    "GET": [
        re.compile(rf"^{_REPO}(\?.*)?$"),
        re.compile(rf"^{_REPO}/(issues|pulls|labels|issues/comments)(\?.*)?$"),
        re.compile(rf"^{_REPO}/(issues|pulls)/[0-9]+(\?.*)?$"),
        re.compile(rf"^{_REPO}/pulls/[0-9]+/reviews(\?.*)?$"),
        re.compile(rf"^{_REPO}/pulls/[0-9]+/files(\?.*)?$"),  # Rule D serial set (vws-deploy#397)
        re.compile(rf"^{_REPO}/issues/[0-9]+/(events|comments)(\?.*)?$"),
        re.compile(rf"^{_REPO}/commits/[0-9a-f]{{40}}/(check-runs|status)(\?.*)?$"),
        re.compile(rf"^{_REPO}/branches/main$"),
        re.compile(rf"^{_REPO}/compare/[0-9a-f]{{40}}\.\.\.[0-9a-f]{{40}}\?per_page=1$"),
        re.compile(rf"^{_REPO}/rules/branches/main(\?.*)?$"),
        re.compile(rf"^{_REPO}/actions/runs\?event=push&branch=main&head_sha=[0-9a-f]{{40}}&per_page=100$"),
        re.compile(rf"^{_REPO}/actions/runs\?head_sha=[0-9a-f]{{40}}&per_page=100$"),
        re.compile(r"^rate_limit$"),
    ],
    "POST": [re.compile(rf"^{_REPO}/issues/[0-9]+/(comments|labels)$")],
    "DELETE": [re.compile(rf"^{_REPO}/issues/[0-9]+/labels/(blocked|in-review|deepseek|needs-verify|verify-approved)$")],
    # Rule D only: the REST squash merge pinned to a head sha. update-branch is never allowed.
    "PUT": [re.compile(rf"^{_REPO}/pulls/[0-9]+/merge$")],
    # The status issue body only (vws-deploy#416); Gh.write also pins the number and the payload.
    "PATCH": [re.compile(rf"^repos/{OWNER}/{STATUS_REPO}/issues/[0-9]+$")],
}


class GhError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def label_absent(method, path, err):
    """A label DELETE that GitHub answers 404 "Label does not exist": the label is already off the item
    (removed by someone else after collect), so the removal is done."""
    return (method == "DELETE" and "/labels/" in path and isinstance(err, GhError) and err.status == 404
            and "Label does not exist" in str(err))


def allowed(method, path):
    return any(p.match(path) for p in ALLOWED.get(method, []))


def _parse_include(out):
    """gh api -i output: status line, headers, blank line, body."""
    out = out.replace("\r\n", "\n")
    head, _, body = out.partition("\n\n")
    lines = head.split("\n")
    m = re.match(r"HTTP/\S+\s+(\d+)", lines[0] if lines else "")
    status = int(m.group(1)) if m else 0
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def get_class(path):
    """Endpoint class of a GET path, for the per-tick request breakdown in the tick_end log."""
    p = path.split("?", 1)[0]
    if p == "graphql":
        return "graphql"
    m = re.match(r"^repos/[^/]+/[^/]+(/.*)?$", p)
    rest = (m.group(1) or "") if m else None
    if rest is None:
        return "other"
    if re.match(r"^/issues/[0-9]+/comments$|^/issues/comments$", rest):
        return "comments"
    if re.match(r"^/issues/[0-9]+/events$", rest):
        return "events"
    for name in ("issues", "pulls", "labels"):
        if rest == f"/{name}" or rest.startswith(f"/{name}/"):
            return name
    if re.match(r"^/commits/[0-9a-f]+/(check-runs|status)$|^/actions/runs$", rest):
        return "checks"
    return "repo"


class Gh:
    """Every GitHub call goes through here. Reads use If-None-Match; 304 answers come from the cache
    and do not count against the primary rate limit."""

    def __init__(self, cache_path=None, runner=subprocess.run, sleep=time.sleep, timeout=30, tries=3, allow=None,
                 status_issue=None):
        self.cache_path = cache_path
        # The only issue whose body may be PATCHed (the configured status issue); None refuses every edit.
        self.status_issue = int(status_issue) if status_issue else None
        # allow(method, path) -> bool. Another box script (.github/metrics) passes its own, narrower list.
        self.allow = allow or allowed
        self.cache = read_json(cache_path, {}) if cache_path else {}
        self.used = {}
        self.runner, self.sleep, self.timeout, self.tries = runner, sleep, timeout, tries
        self.stats = {"get": 0, "not_modified": 0, "write": 0}
        # GETs per endpoint class and status ("200", "304", or the error status), for the tick_end log.
        self.by_class = {}
        # Set by board_sync for the duration of one sync; only graphql() ever passes it to gh.
        self.board_token = None

    def _count_get(self, path, status):
        c = self.by_class.setdefault(get_class(path), {})
        c[str(status)] = c.get(str(status), 0) + 1

    def _env(self, token=None):
        env = dict(os.environ)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        if token:
            env["GH_TOKEN"] = token
        return env

    def _run(self, args, stdin=None, tries=None, token=None):
        last = None
        tries = tries or self.tries
        for attempt in range(tries):
            try:
                r = self.runner(args, input=stdin, capture_output=True, text=True, timeout=self.timeout, env=self._env(token))
            except subprocess.TimeoutExpired:
                last = GhError(0, "timeout")
            else:
                status, headers, body = _parse_include(r.stdout or "")
                if status and (status < 500 and status != 429):
                    return status, headers, body
                last = GhError(status, scrub((r.stderr or "").strip() or "no status"))
            if attempt + 1 < tries:
                self.sleep(2 ** attempt)
        raise last

    def get(self, path):
        if not self.allow("GET", path):
            raise PermissionError(f"gh call not allowed: GET {path}")
        args = ["gh", "api", "-i", "--method", "GET", path]
        cached = self.cache.get(path)
        if cached and cached.get("etag"):
            args += ["-H", f"If-None-Match: {cached['etag']}"]
        status, headers, body = self._run(args)
        self.stats["get"] += 1
        self._count_get(path, status)
        if status == 304 and cached:
            self.stats["not_modified"] += 1
            self.used[path] = cached
            return cached["data"], cached.get("next", False)
        if status != 200:
            raise GhError(status, scrub(body[:200]))
        data = json.loads(body) if body.strip() else None
        nxt = 'rel="next"' in headers.get("link", "")
        entry = {"etag": headers.get("etag", ""), "data": data, "next": nxt}
        self.used[path] = entry
        return data, nxt

    def get_all(self, path, limit=20):
        sep = "&" if "?" in path else "?"
        items, page = [], 1
        while True:
            data, nxt = self.get(f"{path}{sep}page={page}" if page > 1 else path)
            items.extend(data or [])
            if not nxt:
                return items
            if page >= limit:
                # Truncated history could hide markers or dispatches, so fail the tick instead.
                raise GhError(0, f"more than {limit} pages at {path}")
            page += 1

    def write(self, method, path, payload=None):
        if not self.allow(method, path):
            raise PermissionError(f"gh call not allowed: {method} {path}")
        if method == "PUT":
            # The only PUT is a squash merge pinned to the verified head (GitHub answers 409 if it moved).
            p = payload or {}
            if (set(p) - MERGE_PAYLOAD_KEYS or p.get("merge_method") != "squash"
                    or not re.fullmatch(r"[0-9a-f]{40}", str(p.get("sha", "")))):
                raise PermissionError("merge refused: squash with a 40-hex head sha only")
        if method == "PATCH" and re.fullmatch(rf"repos/{OWNER}/[^/]+/issues/[0-9]+", path):
            # The only issue edit: the status issue's body, with exactly one managed status block.
            body = payload.get("body") if isinstance(payload, dict) and set(payload) == {"body"} else None
            if (not self.status_issue or path != f"repos/{OWNER}/{STATUS_REPO}/issues/{self.status_issue}"
                    or not isinstance(body, str) or status_unowned(body)):
                raise PermissionError("issue edit refused: only the status issue body, with one status block")
        args = ["gh", "api", "-i", "--method", method, path]
        stdin = None
        if payload is not None:
            args += ["--input", "-"]
            stdin = json.dumps(payload)
        # One try: a timed-out POST may have landed, and a retry could post a duplicate comment.
        status, _headers, body = self._run(args, stdin, tries=1)
        self.stats["write"] += 1
        if status not in (200, 201, 204):
            raise GhError(status, scrub(body[:200]))
        return json.loads(body) if body.strip() else None

    def graphql(self, query, variables, write=False):
        """Projects v2 (board_sync) only. A write must be a batch of the three item mutations, built from
        the BOARD_* templates with every value in variables; anything else is refused before the call."""
        if write:
            if not board_write_ok(query):
                raise PermissionError("graphql mutation not allowed")
        elif query != BOARD_QUERY:
            raise PermissionError("graphql query not allowed")
        if not self.board_token:
            # Never fall back to the box login: the board has its own project-scope token.
            raise PermissionError("graphql needs the board token")
        args = ["gh", "api", "-i", "graphql", "--input", "-"]
        stdin = json.dumps({"query": query, "variables": variables})
        status, _headers, body = self._run(args, stdin, tries=1 if write else None, token=self.board_token)
        self.stats["write" if write else "get"] += 1
        if not write:
            self._count_get("graphql", status)
        out = json.loads(body) if body.strip() else {}
        if status != 200 or out.get("errors") or "data" not in out:
            raise GhError(status, scrub(json.dumps(out.get("errors") or body[:200])[:300]))
        return out["data"]

    def save_cache(self):
        if self.cache_path:
            write_json(self.cache_path, self.used)


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
    m = CI_FAIL_RE.fullmatch(body.split("\n", 1)[0])
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
        m = CI_FAIL_RE.fullmatch((body or "").replace("\r\n", "\n").split("\n", 1)[0])
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
    m = re.search(r"<!-- orchestrate:([a-z-]+)", text)
    if m:
        return "marker", {"marker": m.group(1)}
    return None


def is_trusted_reset_author(login, association):
    """vws-deploy#419, same rule as .github/ci-fail (#412): the repo owner, or an OWNER/MEMBER/COLLABORATOR
    association. A missing login or association fails closed."""
    if not login or not association:
        return False
    return login.lower() == OWNER.lower() or association in TRUSTED_ASSOCIATIONS


def is_verifier(login, user_id, cfg):
    """vws-deploy#433: the configured verify login, and its user id when ORCHESTRATE_VERIFY_USER_ID pins one."""
    want_login = cfg.get("ORCHESTRATE_VERIFY_LOGIN", DEFAULTS["ORCHESTRATE_VERIFY_LOGIN"])
    want_id = cfg.get("ORCHESTRATE_VERIFY_USER_ID", DEFAULTS["ORCHESTRATE_VERIFY_USER_ID"])
    if not login or login.lower() != want_login.lower():
        return False
    return not want_id or (type(user_id) is int and user_id == int(want_id))


def comment_distrust(c, cfg):
    """'' when the orchestrator may act on comment record c ({"body", "author", "author_id", "assoc"}),
    else why not. Resets and dispatches need a trusted author; [deepseek-dev] reports need a DeepSeek poller
    login; verdicts and markers need the verify login or an OWNER association (#433/#434)."""
    kind = classify_comment(c.get("body"))
    # CI-failure markers have their own author check (ci_fail_markers, #412).
    if not kind or kind[0] == "ci_fail":
        return ""
    login, assoc = c.get("author") or "", c.get("assoc") or ""
    if not login or not assoc:
        return "author_missing"
    if kind[0] in ("verdict", "marker"):
        trusted = assoc == "OWNER" or is_verifier(login, c.get("author_id"), cfg)
        return "" if trusted else f"{kind[0]}_author_not_trusted"
    if kind[0] == "report":
        logins = cfg.get("ORCHESTRATE_DEEPSEEK_LOGINS", DEFAULTS["ORCHESTRATE_DEEPSEEK_LOGINS"])
        allowed = {x.strip().lower() for x in logins.split(",")}
        return "" if login.lower() in allowed else "report_author_not_deepseek_poller"
    return "" if is_trusted_reset_author(login, assoc) else f"{kind[0]}_author_not_trusted"


def pr_key(url):
    m = URL_RE.match(url or "")
    return f"{m.group(2)}#{m.group(3)}" if m and m.group(1) == OWNER else ""


def vws_pr_key(url):
    """'repo#n' for a PR URL in popododo0720/vws-*, else '' (the gh allowlist refuses other repos)."""
    m = URL_RE.match(url or "")
    if not m or m.group(1) != OWNER or not re.fullmatch(r"vws-[a-z0-9-]+", m.group(2)):
        return ""
    return f"{m.group(2)}#{m.group(3)}"


def load_exclusions(path):
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    out.add(line.split("\t", 1)[0].strip())
    except FileNotFoundError:
        pass
    return out


def load_repos(tsv, extra):
    names = []
    with open(tsv, encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                names.append(line.split("\t", 1)[0].strip())
    for e in extra.split(","):
        if e.strip() and e.strip() not in names:
            names.append(e.strip())
    return names


# ---------------------------------------------------------------- decide (pure)

def wake_body(pending, key, run_id, threshold, max_wait, repos):
    lines = [WAKE_MARKER,
             f"vws-orchestrate: {len(pending)} PRs wait for verify (wake at {threshold} or after {max_wait} min).", ""]
    lines += [f"- {p['repo']}#{p['number']}@{p['head_sha']} {p['url']} (CI pre-check: green, waited {p['waited'] // 60}m)"
              for p in pending]
    lines += ["", f"Repos scanned: {', '.join(repos)}", f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def status_wakes(comments):
    """Verify-wake comments already on the verify-wake thread: [{"key", "at"}]."""
    out = []
    for c in comments:
        body = c.get("body") or ""
        if body.split("\n", 1)[0].strip() == WAKE_MARKER:
            m = KEY_RE.search(body)
            out.append({"key": m.group(1) if m else "", "at": utc(c["created_at"])})
    return out


def pending_key(prs):
    lines = sorted(f"{p['repo']}#{p['number']}@{p['head_sha']}" for p in prs)
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


def infra_error(body):
    """The provider error a Failed report shows, as one redacted line ('' = not an infra failure)."""
    text = body or ""
    if not INFRA_ERROR_RE.search(text):
        return ""
    for line in reversed(text.splitlines()):
        if INFRA_ERROR_RE.search(line):
            return scrub(line.strip())[:200]
    return ""


def dispatch_history(item, now, cfg, exclusions):
    """Per dispatch on one item: outcome in in_flight | done | review | failed | infra | excluded.
    infra: a Failed report that shows a provider error; it never counts toward the grok swap."""
    timeout = num(cfg, "DEEPSEEK_TIMEOUT_HOURS") * 3600
    comments = sorted(item.get("comments", []), key=lambda c: (c["created_at"], c["id"]))
    dispatches, reports = [], {}
    for c in comments:
        kind = classify_comment(c["body"])
        if not kind:
            continue
        if kind[0] == "dispatch":
            dispatches.append(c)
        elif kind[0] == "report":
            reports.setdefault(kind[1]["for"], (c, kind[1]))
    out = []
    for d in dispatches:
        rep = reports.get(d["url"])
        h = {"url": d["url"], "at": utc(d["created_at"]), "report": rep[0]["url"] if rep else ""}
        if rep is None:
            h["outcome"] = "in_flight" if now - h["at"] < timeout else "failed"
            h["kind"] = "timeout"
        elif rep[1]["result"] == "failed":
            h["outcome"], h["kind"] = "failed", "failed_report"
            err = infra_error(rep[0]["body"])
            if err and not (h["url"] in exclusions or h["report"] in exclusions):
                # An exclusions.tsv entry still wins: that attempt is ignored, not retried with a delay.
                h["outcome"], h["kind"], h["error"] = "infra", "provider_error", err
                h["report_at"] = utc(rep[0]["created_at"])
        elif rep[1]["pr"] and not vws_pr_key(rep[1]["pr"]):
            # The PR cannot be read or checked (outside popododo0720/vws-*): one failed attempt, so the
            # task is retried or moved to grok instead of waiting for review forever.
            h["outcome"], h["kind"], h["pr"] = "failed", "pr_outside_vws", rep[1]["pr"]
        else:
            pr = item.get("report_prs", {}).get(rep[1]["pr"], {})
            if pr.get("merged"):
                h["outcome"], h["kind"] = "done", "merged"
            elif pr.get("state") == "closed":
                h["outcome"], h["kind"] = "failed", "closed_unmerged"
            elif rep[1]["pr"]:
                h["outcome"], h["kind"], h["pr"] = "review", "pr_open", rep[1]["pr"]
            else:
                h["outcome"], h["kind"] = "done", "no_pr"
        if h["outcome"] == "failed" and (h["url"] in exclusions or h["report"] in exclusions):
            h["outcome"] = "excluded"
        out.append(h)
    return out


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
    elif bad_gates:
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
            match = [r for (name, app, _s), r in runs.items() if name == req["context"]
                     and (req.get("app_id") in (None, -1) or app == req["app_id"])]
            if match:
                # Every distinct check with this name must pass; a success elsewhere cannot hide a failure.
                bad = [x for x in match if x.get("status") != "completed" or x.get("conclusion") != "success"]
                r = max(bad or match, key=run_order)
                if r.get("status") != "completed":
                    reasons.append(f"required_pending:{req['context']}")
                elif r.get("conclusion") != "success":
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


def main_ci_verdict(runs):
    """Push CI on one main commit, from the Actions runs for it: green | pending | red | none."""
    latest = {}
    for r in runs or []:
        k = r.get("workflow_id")
        if k not in latest or r.get("id", 0) > latest[k].get("id", 0):
            latest[k] = r
    if not latest:
        return "none"
    states = {run_state(r) for r in latest.values()}
    if "failed" in states:
        return "red"
    return "pending" if "pending" in states else "green"


# vws-deploy#486: the acceptance-criteria boxes a PR covers, one `AC: <n>` line per box of its `Part of`
# issue (n from 1), or one `AC: none - <reason>`. Same syntax as pr-body in scripts/guard-agents.sh;
# tests/orchestrate/test_ac_parity.py runs one set of vectors through both.
AC_LINE_RE = re.compile(r"^[ \t]*AC:(.*)$", re.M)
AC_BOX_RE = re.compile(r"[ \t]*([1-9][0-9]*)[ \t]*")
AC_NONE_RE = re.compile(r"[ \t]*none[ \t]+-[ \t]+([^ \t\n\r\f\v].*?)[ \t]*")


def visible_text(text):
    """The text a reader sees, as guard-agents.sh visible_text: HTML comments (also across lines) and fenced
    code blocks (``` or ~~~; closed only by a run of the same character at least as long) are removed."""
    out, incom, fence, fch, flen = [], False, False, "", 0
    for line in (text or "").split("\n"):
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


def agent_of(body):
    """The PR body's 'Agent:' value: logged on merge, and the owner a rebuild wake names (Rule R).
    No eligibility rule depends on it."""
    m = AGENT_RE.search(body or "")
    return m.group(1)[:64] if m else ""


def merge_reasons(it, ctx, snapshot, repos, cfg, attempts):
    """Every reason PR item `it` cannot be merged this tick ([] = eligible). Pure.
    ctx is the repo's merge context from collect (main head, required checks, lock inputs)."""
    k = f"{it['repo']}#{it['number']}"
    labels = set(it["labels"])
    info = it.get("merge_info")
    reasons = []
    if it.get("draft"):
        reasons.append("draft")
    if it.get("base") != "main":
        reasons.append("base_not_main")
    if "verify-approved" not in labels:
        reasons.append("no_verify_approved")
    if labels & set(OPT_OUT_LABELS) or OPT_OUT_MARKER in (it.get("body") or "") or k in (status_target(cfg), wake_target(cfg)):
        reasons.append("opted_out")
    if reasons:
        return reasons  # the cheap rules decide; nothing else was read for this PR
    if not info:
        return ["merge_info_unreadable"]
    if info.get("head_sha") != it.get("head_sha") or not re.fullmatch(r"[0-9a-f]{40}", info.get("head_sha") or ""):
        reasons.append("head_moved_during_collect")
    if info.get("state") != "open" or info.get("merged"):
        reasons.append("not_open")
    if info.get("draft") or info.get("base") != "main":
        reasons.append("draft_or_base_changed")
    if info.get("mergeable") is None:
        reasons.append("mergeable_unknown")
    elif info.get("mergeable") is False:
        reasons.append("conflicts")
    elif info.get("mergeable_state") not in MERGEABLE_OK:
        reasons.append(f"mergeable_state:{info.get('mergeable_state')}")
    body = it.get("body") or ""
    if not PART_OF_RE.search(body):
        reasons.append("part_of_owner_repo" if OWNER_PART_OF_RE.search(body) else "no_part_of")
    if CLOSING_RE.search(info.get("title") or ""):
        reasons.append("closing_keyword_in_title")
    deps, bad = parse_deps(it.get("body"), it["repo"])
    reasons += [f"dep_unmet:{d}" for d in deps if dep_state(d, snapshot, repos) != "met"]
    reasons += [f"dep_unparsed:{b}" for b in bad]
    reasons += head_check_reasons(info, ctx.get("required"))
    if attempts.get(f"{k}@{it.get('head_sha')}", 0) >= int(num(cfg, "ORCHESTRATE_MERGE_MAX_ATTEMPTS")):
        reasons.append("merge_attempts_exhausted")
    return reasons


def merge_status_body(kind, repo, sha, detail, key, run_id):
    lines = [FAILURE_MARKER,
             f"vws-orchestrate: {kind} in {repo}; merges in {repo} are stopped.", "",
             f"Kind: merge-{kind.replace(' ', '-')}", f"Repo: {repo}", f"Main commit: `{sha}`", f"Detail: {detail}", "",
             "Merges in this repo resume by themselves once a newer main head has a green push CI run.",
             f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def infra_status_body(target, hist, key, run_id):
    last = hist[-1]
    lines = [FAILURE_MARKER,
             f"vws-orchestrate: {target} failed {len(hist)} times on provider errors; "
             f"{INFRA_MAX_RETRIES} infra retries are used up. The item is now `blocked` for a human.", "",
             "Kind: infra-retries-exhausted", f"Item: {target}", f"Last report: {last['report']}",
             f"Last error (redacted): {scrub(last.get('error', '')).replace(chr(10), ' ')}", "",
             "These failures did not count toward the grok swap. To retry, fix the provider and comment "
             "`/orchestrate reset-failures` on the item (the orchestrator then removes its `blocked`).",
             f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def infra_key(target, report_url):
    return "sha256:" + hashlib.sha256(f"infra-exhausted:{target}:{report_url}".encode()).hexdigest()


def status_key(kind, repo, sha):
    return "sha256:" + hashlib.sha256(f"merge-{kind}:{repo}:{sha}".encode()).hexdigest()


def serial_merge_pr(info):
    """True when the PR touches a serial-merge path (SERIAL_MERGE_RE). Unread files count as serial."""
    files = (info or {}).get("files")
    return files is None or any(SERIAL_MERGE_RE.match(f or "") for f in files)


def verify_approved_at(info):
    """When vws-verify approved the PR head: the newest successful vws-verify check run on it ('' = unknown)."""
    runs = [r for r in (info or {}).get("checks", []) if r.get("name") == VERIFY_CHECK and r.get("app_id") == VERIFY_APP_ID
            and r.get("head_sha") == (info or {}).get("head_sha") and r.get("conclusion") == "success"]
    return max((r.get("completed_at") or r.get("started_at") or "" for r in runs), default="")


def decide_merges(snapshot, new_state, now, cfg, repos):
    """Rule D. Updates new_state["merge_locks"] / ["merge_attempts"]; returns (actions, notes). Pure."""
    actions, notes = [], []
    if cfg.get("ORCHESTRATE_MERGE", "dry-run") == "off":
        return actions, notes
    items = snapshot["items"]
    merge = snapshot.get("merge", {})
    posted = status_keys(snapshot, new_state)
    locks = new_state.setdefault("merge_locks", {})
    attempts = new_state.setdefault("merge_attempts", {})
    open_heads = {f"{k}@{it.get('head_sha')}" for k, it in items.items() if it["is_pr"] and it["state"] == "open"}
    for a in list(attempts):
        if a not in open_heads:
            del attempts[a]  # the PR merged, closed or moved: its attempts no longer matter
    timeout = num(cfg, "ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS") * 3600
    rows = []  # (repo, PR item, reasons, main) for every PR in a readable repo; chosen after the loop

    def comment(kind, repo, sha, detail):
        key = status_key(kind, repo, sha)
        if key in posted:
            return
        posted.add(key)
        actions.append({"type": "merge_status", "target": status_target(cfg), "key": key, "repo": repo,
                        "reason": f"{kind} in {repo} at {sha[:7]}",
                        "body": merge_status_body(kind, repo, sha, detail, key, snapshot["run_id"])})

    for repo in sorted(repos):
        ctx = merge.get(repo)
        lock = locks.get(repo)
        prs = sorted((it for it in items.values() if it["repo"] == repo and it["is_pr"] and it["state"] == "open"),
                     key=lambda it: it["number"])
        if ctx is None or ctx.get("error"):
            # Not read (no verify-approved candidate and no lock) or unreadable: nothing merges here.
            if ctx is not None:
                notes.append({"kind": "merge_repo_unreadable", "repo": repo, "error": ctx["error"]})
            elif lock:
                notes.append({"kind": "merge_lock", "repo": repo, "status": lock.get("status"), "detail": "repo not read"})
            for it in prs:
                reasons = merge_reasons(it, {}, snapshot, repos, cfg, attempts)
                notes.append({"kind": "merge_check", "target": f"{repo}#{it['number']}", "eligible": False,
                              "reasons": reasons if ctx is None else ["merge_repo_unreadable"] + reasons,
                              "head": (it.get("head_sha") or "")[:7], "main": "not read"})
            continue
        main_sha, main_ci = ctx.get("main_sha", ""), ctx.get("main_ci", "none")
        held = ""
        if lock and lock.get("status") == "merging":
            lp = ctx.get("lock_pr") or {}
            if lp.get("merged") and re.fullmatch(r"[0-9a-f]{40}", lp.get("merge_commit_sha") or ""):
                lock = dict(lock, status="waiting", merge_sha=lp["merge_commit_sha"], merged_at=lock.get("at", now))
                locks[repo] = lock
                notes.append({"kind": "merge_confirmed", "repo": repo, "pr": lock["pr"], "merge_sha": lock["merge_sha"]})
            elif lp.get("state") == "open" and not lp.get("merged"):
                notes.append({"kind": "merge_not_landed", "repo": repo, "pr": lock["pr"]})
                del locks[repo]
                lock = None
            else:
                held = "merge outcome unknown"
        if lock and lock.get("status") == "waiting":
            lci = main_ci if lock["merge_sha"] == main_sha else ctx.get("lock_ci", "none")
            if lci == "green" or (lci != "red" and ctx.get("lock_ahead") is True and main_ci == "green"):
                # Green for the squash commit, or a newer main head that contains it is already green. A
                # different main sha alone is not enough: it may be a stale read of the pre-merge head.
                notes.append({"kind": "merge_lock_released", "repo": repo, "pr": lock["pr"], "merge_sha": lock["merge_sha"],
                              "via": "merge commit" if lci == "green" else f"newer main {main_sha[:7]}"})
                del locks[repo]
                lock = None
            elif lci == "red":
                lock = dict(lock, status="red", red_sha=lock["merge_sha"])
                locks[repo] = lock
                comment("main CI red", repo, lock["merge_sha"], f"push CI failed on the squash commit of {lock['pr']}")
            else:
                held = f"push CI {lci} for {lock['merge_sha'][:7]}"
                if now - lock.get("merged_at", now) >= timeout:
                    comment("main CI wait timeout", repo, lock["merge_sha"],
                            f"no green push CI {int(timeout // 60)} min after merging {lock['pr']} (now {lci})")
        if lock and lock.get("status") == "red":
            if ctx.get("red_ahead") is True and main_ci == "green":
                notes.append({"kind": "merge_red_cleared", "repo": repo, "red_sha": lock["red_sha"], "main_sha": main_sha})
                del locks[repo]
                lock = None
            else:
                held = f"main CI red at {lock['red_sha'][:7]}"
        gate = []
        if lock:
            gate.append(f"repo_locked:{held or lock.get('status')}")
            notes.append({"kind": "merge_lock", "repo": repo, "status": lock.get("status"), "pr": lock.get("pr"),
                          "detail": held})
        elif main_ci != "green":
            gate.append(f"main_ci_{main_ci}")
            if main_ci == "red":
                comment("main CI red", repo, main_sha, "the push CI run for the current main head failed")
        for it in prs:
            reasons = merge_reasons(it, ctx, snapshot, repos, cfg, attempts)
            rows.append((repo, it, reasons or list(gate), f"{main_sha[:7]} {main_ci}"))
    # The serial set (vws-deploy#397): at most one merge per tick among PRs that touch orchestrate.py,
    # repo-guard*.yml or AGENTS.md, in any repo; the oldest verify approval goes first.
    serial = sorted((verify_approved_at(it.get("merge_info")) or "~", repo, it["number"])
                    for repo, it, reasons, _ in rows if not reasons and serial_merge_pr(it.get("merge_info")))
    first = f"{serial[0][1]}#{serial[0][2]}" if serial else ""
    chosen = {}
    for repo, it, reasons, main in sorted(rows, key=lambda r: (r[0], f"{r[0]}#{r[1]['number']}" != first, r[1]["number"])):
        k = f"{repo}#{it['number']}"
        if not reasons and k != first and serial_merge_pr(it.get("merge_info")):
            reasons = [f"serial_after:{first}"]
        if not reasons and repo in chosen:
            reasons = [f"one_merge_per_repo:{chosen[repo]}"]
        eligible = not reasons
        if eligible:
            chosen[repo] = k
            head = it["head_sha"]
            actions.append({"type": "merge", "target": k, "repo": repo, "head": head, "reason": "verify PASS for the head, checks green, main green",
                            "agent": agent_of(it.get("body")),
                            "payload": {"merge_method": "squash", "sha": head,
                                        "commit_title": f"{it['merge_info']['title']} (#{it['number']})"}})
        note = {"kind": "merge_check", "target": k, "eligible": eligible, "reasons": reasons,
                "head": (it.get("head_sha") or "")[:7], "main": main}
        unmapped = (it.get("merge_info") or {}).get("unmapped")
        if unmapped:
            note["unmapped_suite_checks"] = unmapped  # keyed per suite (conservative)
        notes.append(note)
    return actions, notes


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


def verify_labels_wanted(it):
    """(needs-verify, verify-approved, why) for one PR collect read (vws-deploy#487 table). Pure.
    verify-approved True means "may stay"; decide_verify_labels never adds it (see there)."""
    if it["verify_ci"] == "ineligible":
        return False, False, "draft or not into main"
    if it.get("verify_check_failed"):
        # The newest vws-verify check on this head failed: it beats an earlier PASS review on the same head.
        return False, False, f"vws-verify check failed on head {it['head_sha'][:7]}"
    v = head_verdict(it)
    if v and v["result"] == "pass":
        return False, True, f"PASS on head {it['head_sha'][:7]} {v['url']}"
    if v:
        return False, False, f"FAIL on head {it['head_sha'][:7]} {v['url']}"
    if it["verify_ci"] == "green":
        return True, False, f"checks green on head {it['head_sha'][:7]}, no verdict yet"
    return False, False, f"checks {it['verify_ci']} on head {it['head_sha'][:7]}"


def decide_verify_labels(items, cfg, notes):
    """vws-deploy#487: needs-verify and verify-approved as one rule over every vws-* repo (verify-queue.yml
    is retired). Only PRs collect read (verify_ci set) are touched; an unreadable state writes nothing. A
    blocked, held or do-not-merge PR gets removals only. verify-approved is only ever removed: verify-gate
    rejects one added by the PR author or pusher, and this rule writes as the repo owner (vws-verify adds it,
    review on vws-deploy#488). Longest-waiting PRs first, so a write cap that
    stops this tick short serves them first; the rest converge on later ticks. Pure."""
    if cfg.get("ORCHESTRATE_VERIFY_LABELS", "on") != "on":
        return []
    out = []
    for k, it in sorted(items.items(), key=lambda kv: (kv[1].get("created_at") or "", kv[0])):
        if not it.get("is_pr") or it.get("verify_ci") not in ("green", "pending", "red", "ineligible"):
            continue
        labels = set(it["labels"])
        want_nv, want_va, why = verify_labels_wanted(it)
        parked = sorted(labels & set(OPT_OUT_LABELS))
        if parked:
            want_nv = False  # a parked PR does not wait for verify (AC7)
        for label, want in ((NEEDS_VERIFY, want_nv), ("verify-approved", want_va)):
            if want and label not in labels:
                if label == "verify-approved":
                    continue  # vws-verify adds it; an orchestrator-added one would fail verify-gate
                if parked:
                    notes.append({"kind": "verify_label_parked", "target": k, "label": label, "parked": parked})
                    continue
                out.append({"type": "add_label", "target": k, "label": label, "reason": f"verify labels: {why}"})
            elif not want and label in labels:
                extra = f" ({', '.join(parked)})" if parked and label == NEEDS_VERIFY else ""
                out.append({"type": "remove_label", "target": k, "label": label, "reason": f"verify labels: {why}{extra}"})
    return out


def rework_prs(k, it, items):
    """Open PRs a ready+deepseek item owns: the item itself when it is a PR, else the open vws PRs its
    DeepSeek reports named, then (vws-deploy#457) open PRs whose `Part of` names it and whose owner
    resolves to deepseek. A PR that is itself a ready+deepseek item is reworked on its own."""
    if not {"ready", "deepseek"} <= set(it["labels"]) or "grok" in it["labels"]:
        return []
    if it["is_pr"]:
        return [k]
    keys = [vws_pr_key(u) for u, p in sorted(it.get("report_prs", {}).items()) if p.get("state") == "open"]
    out = [pk for pk in keys if pk and items.get(pk, {}).get("is_pr") and items[pk]["state"] == "open"]
    for pk in sorted(items):
        p = items[pk]
        if (pk not in out and p["is_pr"] and p["state"] == "open" and grok_link(p) == k
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


def verify_fail_unrouted(items, prev, notes, routed=()):
    """vws-deploy#457: open non-draft PRs into main whose head has a vws-verify FAIL that no rule picks up
    (rule B2 reworks only PRs a ready+deepseek item owns; a conflict-only FAIL is Rule R's; routed are
    the PRs the grok rework wake, vws-deploy#398, covers this tick). Adds one verify_fail_unrouted note
    per PR head not in prev ({repo#N: head}); returns the new {repo#N: head}."""
    reworked = {pk for k, it in items.items() if it["state"] == "open" for pk in rework_prs(k, it, items)}
    seen = {}
    for k in sorted(items):
        it = items[k]
        if not (it["is_pr"] and it["state"] == "open" and not it.get("draft") and it.get("base") == "main"):
            continue
        v = head_verdict(it)
        if not v or v["result"] != "fail" or conflict_only(v["body"]) or k in reworked or k in routed:
            continue
        owner, _ = pr_owner(it, items)
        reason = ("no_owner" if not owner else "owner_grok" if owner == "grok"
                  else "owner_deepseek_not_ready" if owner == "deepseek" else "owner_agent")
        seen[k] = it["head_sha"]
        if prev.get(k) != it["head_sha"]:
            notes.append({"kind": "verify_fail_unrouted", "target": k, "head": it["head_sha"], "verdict": v["url"],
                          "owner": owner, "reason": reason})
    return seen


def grok_rework_key(k, head_sha):
    """One grok rework wake per PR head: a new push re-arms it."""
    return "sha256:" + hashlib.sha256(f"grok-rework\n{k}\n{head_sha}".encode()).hexdigest()


def grok_rework_waiting(items):
    """vws-deploy#398(a): open non-draft PRs into main whose head has a vws-verify FAIL that is not
    conflict-only, that rule B2 does not rework, that are not parked (OPT_OUT_LABELS), and whose owner
    (pr_owner) resolves to someone other than deepseek. Sorted by key."""
    reworked = {pk for k, it in items.items() if it["state"] == "open" for pk in rework_prs(k, it, items)}
    out = []
    for k in sorted(items):
        it = items[k]
        if not (it["is_pr"] and it["state"] == "open" and not it.get("draft") and it.get("base") == "main"):
            continue
        if k in reworked or set(it["labels"]) & set(OPT_OUT_LABELS):
            continue
        v = head_verdict(it)
        if not v or v["result"] != "fail" or conflict_only(v["body"]):
            continue
        owner, via = pr_owner(it, items)
        if owner and owner != "deepseek":
            out.append({"target": k, "url": it["url"], "head_sha": it["head_sha"], "owner": owner, "via": via,
                        "verdict": v})
    return out


def grok_rework_body(p, key, run_id):
    v = p["verdict"]
    return "\n".join([GROK_WAKE_MARKER, f"vws-orchestrate: Grok rework {p['target']} {p['url']}", "",
                      f"Head `{p['head_sha']}` has a vws-verify FAIL: {v['url']}", "",
                      quote(scrub(v["body"] or "")).replace("<!--", "&lt;!--"), "",
                      f"Owner `{p['owner']}` ({p['via']}): please fix the findings on the same branch with a new "
                      "commit (no force-push, no update-branch, no merge commits), then let verify run on the new head.",
                      "", f"Idempotency key: `{key}`", f"Run id: `{run_id}`",
                      f"{REWORK_MARKER}{p['head_sha']} verdict={v['id']} item={p['target']} -->"])


def decide_grok_rework(snapshot, cfg, posted, notes):
    """Rule B4 (vws-deploy#398(a)). Returns (grok_rework actions, the PRs the wake covers). Pure."""
    waiting = grok_rework_waiting(snapshot["items"])
    if not waiting:
        return [], set()
    if not wake_target(cfg):
        notes.append({"kind": "grok_rework_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "waiting": len(waiting)})
        return [], set()
    if cfg.get("ORCHESTRATE_DISPATCH", "on") == "off":
        notes.append({"kind": "grok_rework_paused", "reason": "ORCHESTRATE_DISPATCH=off", "waiting": len(waiting)})
        return [], set()
    actions = []
    for p in waiting:
        key = grok_rework_key(p["target"], p["head_sha"])
        if key in posted:
            continue
        actions.append({"type": "grok_rework", "target": wake_target(cfg), "item": p["target"], "head": p["head_sha"],
                        "owner": p["owner"], "key": key,
                        "reason": f"{p['target']}: verify FAIL on head {p['head_sha'][:7]}, owner {p['owner']}",
                        "body": grok_rework_body(p, key, snapshot["run_id"])})
    return actions, {p["target"] for p in waiting}


def ci_fails(pr):
    """CI-failure markers on a PR, oldest first: (accepted, untrusted). Accepted means posted by
    github-actions[bot], v=1, and naming this repo#N. A comment with no recorded author is untrusted."""
    ok, bad = [], []
    for c in sorted(pr.get("comments", []), key=lambda c: (c["created_at"], c["id"])):
        kind = classify_comment(c["body"])
        if not kind or kind[0] != "ci_fail":
            continue
        d = kind[1]
        if c.get("author") != CI_FAIL_LOGIN:
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


def rework_kind(body):
    tail = body.split(REWORK_MARKER, 1)[1].split("-->", 1)[0]
    return "ci_fail_rework" if " ci=" in tail and " verdict=" not in tail else "verify_fail_rework"


def rework_plan(k, it, items, since):
    """Rework state of the item's DeepSeek PR (vws-deploy#373, #412). counted: the reworks already
    dispatched since the last reset plus a pending FAIL, each one failure toward the grok swap. A head
    with a verify FAIL and a CI failure is one rework and one count. state: pending | done (this head
    was reworked already) | conflict_only (left to eggbot) | None (no FAIL on the head)."""
    plan = {"target": "", "verdict": None, "ci": None, "state": None, "counted": [], "ci_skipped": []}
    for pk in rework_prs(k, it, items):
        pr = items[pk]
        dispatches = [c for c in pr.get("comments", []) if (classify_comment(c["body"]) or ("",))[0] == "dispatch"]
        plan["counted"] += [{"url": c["url"], "kind": rework_kind(c["body"]), "report": ""} for c in dispatches
                            if REWORK_MARKER in c["body"] and utc(c["created_at"]) > since]
        if plan["state"]:
            continue
        v = head_verdict(pr)
        if v and v["result"] != "fail":
            v = None
        cf, why = ci_fail_head(pr, it)
        if cf and why:
            plan["ci_skipped"].append({"kind": "ci_fail_skipped", "target": pk, "comment": cf["url"],
                                       "reason": why, "n": cf["n"]})
            cf = None
        if v and conflict_only(v["body"]):
            if not cf:
                plan["target"], plan["verdict"], plan["state"] = pk, v, "conflict_only"
                continue
            v = None  # the CI failure is still reworked; the conflict stays with eggbot
        if not v and not cf:
            continue
        plan["target"], plan["verdict"], plan["ci"] = pk, v, cf
        first = min(x["at"] for x in (v, cf) if x)
        if any(f"{REWORK_MARKER}{pr['head_sha']} " in c["body"] or c["created_at"] >= first for c in dispatches):
            plan["state"] = "done"
        else:
            plan["state"] = "pending"
            plan["counted"].append({"url": (v or cf)["url"], "kind": "verify_fail" if v else "ci_fail", "report": ""})
    return plan


def ci_fail_text(body):
    """The human part of a CI-failure comment (failed jobs, log tails): no marker line, no JSON record,
    and no HTML comment opener, so a log line cannot add an orchestrator marker."""
    lines = (body or "").replace("\r\n", "\n").split("\n")[1:]
    end = next((i for i, ln in enumerate(lines) if ln.strip() == "```json"), len(lines))
    return scrub("\n".join(lines[:end]).strip()).replace("<!--", "&lt;!--")


def quote(text):
    return "\n".join("> " + ln for ln in text[:3000].splitlines())


def rework_body(pr, v, cf=None):
    parts, keys = [REWORK_TEXT], ""
    if v:
        parts.append(f"Verify verdict: {v['url']} (head `{pr['head_sha'][:7]}`)\n\n{quote(scrub(v['body']))}")
        keys += f" verdict={v['id']}"
    if cf:
        parts.append(f"CI failure: {cf['url']} (head `{pr['head_sha'][:7]}`, failing head {cf['n']} of {CI_FAIL_CAP})"
                     f"\n\n{quote(ci_fail_text(cf['body']))}")
        keys += f" ci={cf['id']}"
    parts.append(f"{REWORK_MARKER}{pr['head_sha']}{keys} -->")
    return "\n\n".join(parts)


def grok_allowlist(text):
    """ORCHESTRATE_GROK_ALLOWLIST: repo#N entries split on commas or whitespace. Returns (keys, bad)."""
    toks = [t for t in re.split(r"[,\s]+", text or "") if t]
    return {t for t in toks if GROK_ALLOW_RE.fullmatch(t)}, [t for t in toks if not GROK_ALLOW_RE.fullmatch(t)]


def grok_key(k, since):
    """One wake per issue per failure reset (/orchestrate reset-failures re-arms it)."""
    return "sha256:" + hashlib.sha256(f"grok-wake\n{k}\n{int(since)}".encode()).hexdigest()


def grok_wake_body(it, k, key, slot, cap, run_id):
    return "\n".join([GROK_WAKE_MARKER, f"vws-orchestrate: Grok task {k} {it['url']}", "",
                      "Owning Grok dev bot: please take this issue: one small PR per its acceptance criteria, "
                      f"`Part of #{it['number']}` in commits and the PR body, no closing keywords.",
                      f"Grok slot {slot} of {cap}; this issue holds the slot while it is open with `grok`.",
                      "", f"Idempotency key: `{key}`", f"Run id: `{run_id}`", f"{GROK_ASSIGN_MARKER}{k} -->"])


def grok_link(it):
    """The issue a PR's `Part of #N` / `Part of repo#N` line names, as repo#N, or ""."""
    m = PART_OF_RE.search(it.get("body") or "") if it.get("is_pr") else None
    return "" if not m else (it["repo"] + m.group(1) if m.group(1).startswith("#") else m.group(1))


def grok_held(items):
    """Grok work in flight without a wake key: an open grok item with an assignee, or the issue an open
    grok PR (draft too) names with `Part of`. Returns the set of held repo#N (the linked issue for a PR)."""
    held = set()
    for k, it in items.items():
        if it["state"] != "open" or "grok" not in it["labels"]:
            continue
        if it["is_pr"] and grok_link(it):
            held.add(grok_link(it))
        elif it.get("assignees"):
            held.add(k)
    return held


def grok_busy(items, held):
    """Repos with Grok work in flight: the repo of each held task, plus the repo of each open grok PR
    that names an issue (a PR in vws-proto with `Part of vws-iam#1` keeps both repos busy)."""
    return {k.split("#")[0] for k in held} | {
        it["repo"] for it in items.values()
        if it["state"] == "open" and "grok" in it["labels"] and it["is_pr"] and grok_link(it)}


def decide_grok(snapshot, cfg, posted, repos, cycles, notes):
    """Rule G (vws-deploy#378). Returns the grok_wake actions. Pure."""
    items = snapshot["items"]
    allow, bad = grok_allowlist(cfg.get("ORCHESTRATE_GROK_ALLOWLIST", ""))
    notes += [{"kind": "grok_allowlist_invalid", "token": b} for b in bad]
    held, queue, woken = grok_held(items), [], set()
    for k in sorted(items):
        it = items[k]
        labels = set(it["labels"])
        if it["state"] != "open" or "grok" not in labels:
            continue
        unit = grok_link(it) or k  # a PR and the issue it is part of are one Grok task
        if grok_key(k, reset_time(it)) in posted:
            held.add(unit)  # woken and still open with grok: the slot stays taken
            woken.add(k)
            continue
        if unit in held or k in held:
            continue
        if (k not in allow or "ready" not in labels or labels & ({"deepseek"} | set(OPT_OUT_LABELS))
                or (it["is_pr"] and it.get("draft"))):
            continue
        deps, bad_deps = parse_deps(it.get("body"), it["repo"])
        unmet = [d for d in deps if dep_state(d, snapshot, repos) != "met"]
        notes += [{"kind": "dep_unmet", "target": k, "dep": d, "state": dep_state(d, snapshot, repos)} for d in unmet]
        if unmet or bad_deps or k in cycles:
            continue
        security = "security" in labels or re.search(r"(?i)\bsecurity\b", it.get("title") or "")
        prio = 0 if "priority/blocker" in labels else 1 if security else 2
        queue.append((prio, ready_time(it), it["repo"], it["number"], k))
    running = sorted(held)
    if running:
        notes.append({"kind": "grok_in_flight", "targets": running})
    cap = int(num(cfg, "ORCHESTRATE_GROK_CAP"))
    free = 0 if cfg.get("ORCHESTRATE_DISPATCH", "on") == "off" else max(0, cap - len(running))
    busy = grok_busy(items, held)
    actions = []
    if queue and free and not wake_target(cfg):
        # Grok wakes go to the verify-wake thread (vws-deploy#416); never to the status issue.
        notes.append({"kind": "grok_wake_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "queued": len(queue)})
        return actions
    for q in sorted(queue):
        if len(actions) >= free:
            break
        if q[2] in busy:
            notes.append({"kind": "repo_busy", "target": q[4], "repo": q[2]})
            continue
        busy.add(q[2])
        it, key = items[q[4]], grok_key(q[4], reset_time(items[q[4]]))
        slot = len(running) + len(actions) + 1
        # woken: the tasks this tick saw as woken, so the apply-time re-check still counts them (it
        # cannot rebuild their reset time from the bare issues list).
        actions.append({"type": "grok_wake", "target": wake_target(cfg), "item": q[4], "key": key,
                        "woken": sorted(woken), "reason": f"{q[4]}: allowlisted ready+grok, Grok slot {slot} of {cap}",
                        "body": grok_wake_body(it, q[4], key, slot, cap, snapshot["run_id"])})
    return actions


def rebuild_key(k, head_sha):
    """One rebuild wake per PR head: a push (the rebuild itself, or any new head) re-arms it."""
    return "sha256:" + hashlib.sha256(f"rebuild-wake\n{k}\n{head_sha}".encode()).hexdigest()


def rebuild_waiting(items):
    """Open non-draft PRs into main labeled needs-rebuild or with mergeable_state dirty, sorted by key.
    mergeable_state comes from GET pulls/N; GitHub computes it lazily, so "unknown" is not a conflict."""
    out = []
    for k in sorted(items):
        it = items[k]
        if not (it["is_pr"] and it["state"] == "open" and not it.get("draft") and it.get("base") == "main"):
            continue
        why = ("labeled needs-rebuild" if NEEDS_REBUILD in it["labels"]
               else "conflicts with main" if it.get("mergeable_state") == "dirty" else "")
        if why:
            out.append({"target": k, "url": it["url"], "head_sha": it["head_sha"], "owner": agent_of(it.get("body")),
                        "why": why})
    return out


def rebuild_wake_body(p, key, run_id):
    who = (f"Owner `{p['owner']}` (the PR's `Agent:` line)" if p["owner"]
           else "The PR has no `Agent:` line, so the lead (dr eggbot)")
    return "\n".join([REBUILD_WAKE_MARKER, f"vws-orchestrate: rebuild {p['target']} {p['url']}", "",
                      f"Head `{p['head_sha']}` {p['why']}.",
                      f"{who}: please rebuild it on current main with `vws-rebuild-pr` as a new `-vN` branch and PR "
                      "(no force-push, no update-branch, no merge commits).",
                      "", f"Idempotency key: `{key}`", f"Run id: `{run_id}`"])


def decide_rebuild(snapshot, cfg, posted, notes):
    """Rule R (vws-deploy#460). Returns (rebuild_wake actions, the PRs waiting for a rebuild). Pure."""
    waiting = rebuild_waiting(snapshot["items"])
    if waiting and not wake_target(cfg):
        notes.append({"kind": "rebuild_wake_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "waiting": len(waiting)})
        return [], waiting
    actions = []
    for p in waiting:
        key = rebuild_key(p["target"], p["head_sha"])
        if key in posted:
            continue
        actions.append({"type": "rebuild_wake", "target": wake_target(cfg), "item": p["target"], "head": p["head_sha"],
                        "owner": p["owner"], "key": key, "reason": f"{p['target']} {p['why']}",
                        "body": rebuild_wake_body(p, key, snapshot["run_id"])})
    return actions, waiting


def lead_key(k, sha):
    """One lead wake per PR head (vws-deploy#397)."""
    return "sha256:" + hashlib.sha256(f"lead-wake\n{k}@{sha}".encode()).hexdigest()


def lead_wake_body(it, k, reason, key, run_id):
    return "\n".join([LEAD_WAKE_MARKER, f"vws-orchestrate: lead wake for {k} at `{it['head_sha']}` {it['url']}", "",
                      f"Reason: {reason}.",
                      "No rule moves this PR head on its own: dr eggbot rebuilds it onto main as a new -vN PR "
                      "(no force-push) or routes the FAIL to its owner.", "",
                      f"Idempotency key: `{key}`", f"Run id: `{run_id}`"])


def decide_lead(snapshot, cfg, notes):
    """Lead wake (vws-deploy#397). Returns lead_wake actions: one per PR head that is verify-approved but
    conflicts with main, or has a vws-verify FAIL that no DeepSeek rework picks up (a Grok PR, a PR with no
    DeepSeek owner, or a conflict-only verdict). Pure."""
    items = snapshot["items"]
    posted = set(snapshot.get("status", {}).get("keys", []))
    reworked = {pk for k, it in items.items() if it["state"] == "open" for pk in rework_prs(k, it, items)}
    due = []
    for k in sorted(items):
        it = items[k]
        if not (it["is_pr"] and it["state"] == "open" and not it.get("draft") and it.get("base") == "main"):
            continue
        if k in (status_target(cfg), wake_target(cfg)):
            continue
        info = it.get("merge_info") or {}
        reason = ""
        if ("verify-approved" in it["labels"] and info.get("head_sha") == it["head_sha"]
                and info.get("mergeable") is False):
            reason = "verify-approved but conflicts with main"
        else:
            v = head_verdict(it)
            if v and v["result"] == "fail":
                owner = items.get(grok_link(it), {})
                if conflict_only(v["body"]):
                    reason = f"verify FAIL is conflict-only ({v['url']})"
                elif "grok" in it["labels"] or "grok" in owner.get("labels", []):
                    reason = f"verify FAIL on a Grok PR; Grok rework is not automatic ({v['url']})"
                elif k not in reworked:
                    reason = f"verify FAIL on a PR with no DeepSeek owner ({v['url']})"
        if reason:
            due.append((k, it, reason))
    if due and not wake_target(cfg):
        notes.append({"kind": "lead_wake_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "due": len(due)})
        return []
    actions = []
    for k, it, reason in due:
        key = lead_key(k, it["head_sha"])
        if key in posted:
            continue
        actions.append({"type": "lead_wake", "target": wake_target(cfg), "item": k, "key": key,
                        "reason": f"{k}: {reason}", "body": lead_wake_body(it, k, reason, key, snapshot["run_id"])})
    return actions


def status_keys(snapshot, state):
    """Keys of merge and infra entries already reported: in the status issue body, in state, or (before
    vws-deploy#416) as comments on the verify-wake thread."""
    keys = set(snapshot.get("status", {}).get("keys", []))
    keys |= set(STATUS_EVENT_KEY_RE.findall(snapshot.get("status_issue", {}).get("body", "")))
    return keys | {e["key"] for e in state.get("status_events", [])}


def decide(snapshot, state, now, cfg, exclusions):
    """Pure: the same inputs give the same output. Returns {"actions", "notes", "state"}."""
    actions, notes = [], []
    new_state = json.loads(json.dumps(state))
    repos = {r["name"] for r in snapshot["repos"] if not r.get("archived")}
    items = snapshot["items"]
    owned = new_state.setdefault("owned_labels", {name: [] for name in MANAGED_LABELS})
    for name in MANAGED_LABELS:
        # Ownership is journaled before the write; drop entries whose label is not on the item
        # (the write never happened, or a human removed it).
        owned[name] = [k for k in owned.get(name, []) if k in items and name in items[k]["labels"]]
    max_fail = int(num(cfg, "DEEPSEEK_MAX_FAILURES"))
    cap = int(num(cfg, "ORCHESTRATE_DEEPSEEK_CAP"))
    infra_delay = int(num(cfg, "ORCHESTRATE_INFRA_RETRY_DELAY_MIN")) * 60
    posted = status_keys(snapshot, state)
    cycles = find_cycles(snapshot)

    # Rule A: needs-verify PRs and the verify wake (vws-deploy#397)
    pending = []
    for key in sorted(items):
        it = items[key]
        if (it["is_pr"] and it["state"] == "open" and not it.get("draft") and it.get("base") == "main"
                and NEEDS_VERIFY in it["labels"] and "verify-approved" not in it["labels"] and it.get("ci") != "failing"):
            # A stale label on a head vws-verify already failed is not a wait: verify will not run it again
            # until a push, so it must not wake verify or hold the pending set (vws-deploy#430, console-api#362).
            v = head_verdict(it)
            if v and v["result"] == "fail":
                notes.append({"kind": "needs_verify_failed_head", "target": key, "verdict": v["url"]})
                continue
            parked = sorted(set(it["labels"]) & set(OPT_OUT_LABELS))
            if parked:
                # vws-deploy#487 AC7: a parked PR never wakes verify; rule V removes its label this tick.
                notes.append({"kind": "needs_verify_parked", "target": key, "labels": parked})
                continue
            # Waited since the latest needs-verify label (a push removes it); unknown counts as just labeled.
            since = max((utc(e["created_at"]) for e in it.get("events", [])
                         if e["event"] == "labeled" and e["label"]["name"] == NEEDS_VERIFY), default=now)
            pending.append({"repo": it["repo"], "number": it["number"], "url": it["url"], "head_sha": it["head_sha"],
                            "waited": int(max(0, now - since))})
    threshold = int(num(cfg, "VERIFY_WAKE_THRESHOLD"))
    max_wait = int(num(cfg, "VERIFY_WAKE_MAX_WAIT_MIN"))
    key = pending_key(pending)
    wakes = snapshot.get("status", {}).get("wakes", [])
    oldest = max((p["waited"] for p in pending), default=0)
    due = pending and (len(pending) >= threshold or oldest >= max_wait * 60)
    if not wake_target(cfg):
        if due:
            notes.append({"kind": "wake_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "pending": len(pending)})
    elif due and key not in {w["key"] for w in wakes}:
        actions.append({"type": "wake_verify", "target": wake_target(cfg),
                        "reason": f"{len(pending)} PRs need verify, oldest waited {oldest // 60}m", "key": key,
                        "body": wake_body(pending, key, snapshot["run_id"], threshold, max_wait, sorted(repos))})
    new_state["prev_pending_count"] = len(pending)

    # CI-failure markers (vws-deploy#412): untrusted authors are ignored; owner=grok waits for #398(a).
    for k in sorted(items):
        it = items[k]
        if not (it["is_pr"] and it["state"] == "open"):
            continue
        ok, bad = ci_fails(it)
        for c in bad:
            notes.append({"kind": "ci_fail_untrusted", "target": k, "comment": c["url"], "author": c.get("author", "")})
        for m in ok:
            if m["sha"] == it["head_sha"] and m["owner"] == "grok":
                notes.append({"kind": "ci_fail_grok_pending", "target": k, "comment": m["url"], "sha": m["sha"],
                              "agent": m["agent"]})
                break

    # Rules B and C
    in_flight, queue, slots_used = [], [], 0
    for k in sorted(items):
        it = items[k]
        if it["state"] != "open":
            continue
        hist = dispatch_history(it, now, cfg, exclusions)
        for h in hist:
            if h["kind"] == "pr_outside_vws":
                notes.append({"kind": "report_pr_outside_vws", "target": k, "pr": h["pr"]})
        running = sum(1 for h in hist if h["outcome"] == "in_flight")
        if running:
            in_flight.append(k)
            slots_used += running
        labels = set(it["labels"])
        if "grok" in labels and "deepseek" not in labels and "in-review" in labels and k in owned["in-review"]:
            # Moved to grok earlier (or a swap stopped before this label call): DeepSeek review is over.
            actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": "task moved to grok"})
        if "deepseek" not in labels or "ready" not in labels:
            continue
        since = reset_time(it)
        rw = rework_plan(k, it, items, since)
        notes += rw["ci_skipped"]
        counted = [h for h in hist if h["outcome"] == "failed" and h["at"] > since] + rw["counted"]
        latest = hist[-1] if hist else None
        if len(counted) >= max_fail:
            marker = any((classify_comment(c["body"]) or ("",))[0] == "marker"
                         and "orchestrate:swap" in c["body"] and utc(c["created_at"]) > since
                         for c in it.get("comments", []))
            lines = "\n".join(f"- {h['url']} ({h['kind']}{', report ' + h['report'] if h['report'] else ''})" for h in counted)
            # "grok" may already be there when an earlier swap stopped between its two label calls.
            actions.append({"type": "swap_label", "target": k, "reason": f"{len(counted)} counted DeepSeek failures",
                            "remove": "deepseek", "add": "" if "grok" in labels else "grok", "comment": not marker,
                            "body": f"Moving this task from DeepSeek to grok after {len(counted)} failed attempts:\n"
                                    f"{lines}\n\n<!-- orchestrate:swap -->"})
            if "in-review" in labels and k in owned["in-review"]:
                actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": "task moved to grok"})
            continue
        if "grok" in labels:
            continue
        # Infra failures (provider 429/503) since the last reset: never counted above. While the latest
        # attempt is one, re-mention it after the delay, at most INFRA_MAX_RETRIES times; the count
        # comes from the comments, so a restart or rerun sees the same number.
        infra = [h for h in hist if h["outcome"] == "infra" and h["at"] > since]
        retry, exhausted = 0, False
        if latest and latest["outcome"] == "infra" and latest["at"] > since:
            if len(infra) > INFRA_MAX_RETRIES:
                exhausted = True
                notes.append({"kind": "infra_exhausted", "target": k, "failures": len(infra), "report": latest["report"]})
                key = infra_key(k, latest["report"])
                if key not in posted:
                    posted.add(key)
                    actions.append({"type": "infra_status", "target": status_target(cfg), "key": key, "item": k,
                                    "reason": f"{k}: infra retries exhausted",
                                    "body": infra_status_body(k, infra, key, snapshot["run_id"])})
            else:
                due = latest["report_at"] + infra_delay
                if now < due:
                    notes.append({"kind": "infra_wait", "target": k, "retry": len(infra), "due": iso(due),
                                  "error": latest.get("error", "")})
                else:
                    retry = len(infra)
        deps, bad = parse_deps(it.get("body"), it["repo"])
        unmet = [d for d in deps if dep_state(d, snapshot, repos) != "met"]
        for d in unmet:
            notes.append({"kind": "dep_unmet", "target": k, "dep": d, "state": dep_state(d, snapshot, repos)})
        for b in bad:
            notes.append({"kind": "dep_unparsed", "target": k, "token": b})
        if k in cycles:
            notes.append({"kind": "dep_cycle", "target": k})
        blocked = bool(unmet or bad or k in cycles or exhausted)
        if blocked and "blocked" not in labels:
            actions.append({"type": "add_label", "target": k, "label": "blocked",
                            "reason": "infra retries exhausted" if exhausted else "unmet dependency"})
        if not blocked and "blocked" in labels and k in owned["blocked"]:
            actions.append({"type": "remove_label", "target": k, "label": "blocked", "reason": "dependencies met"})
        if latest and latest["outcome"] == "review" and "in-review" not in labels:
            actions.append({"type": "add_label", "target": k, "label": "in-review", "reason": f"DeepSeek PR {latest.get('pr')} open"})
        if (latest and latest["outcome"] in ("done", "failed", "infra") and "in-review" in labels and k in owned["in-review"]):
            actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": f"DeepSeek PR {latest['kind']}"})
        if rw["state"]:
            # A FAIL on the PR head replaces the issue-style dispatch with a rework of that PR.
            pr = items[rw["target"]]
            pr_running = any(h["outcome"] == "in_flight" for h in dispatch_history(pr, now, cfg, exclusions))
            if rw["state"] == "pending" and not (running or pr_running or blocked or "blocked" in labels or pr.get("draft")):
                queue.append((1, ready_time(it), pr["repo"], pr["number"], rw["target"], 0, k))
            else:
                notes.append({"kind": "rework_skipped", "target": rw["target"], "verdict": (rw["verdict"] or rw["ci"])["url"],
                              "reason": "in flight or blocked" if rw["state"] == "pending" else rw["state"]})
            continue
        # A running target is never dispatched again, even after a failure reset.
        eligible = (not running and not blocked and "blocked" not in labels and not (it["is_pr"] and it.get("draft"))
                    and (latest is None or latest["outcome"] in ("failed", "excluded") or latest["at"] <= since
                         or retry > 0))
        if eligible:
            prio = 0 if "priority/blocker" in labels else 2
            queue.append((prio, ready_time(it), it["repo"], it["number"], k, retry, k))
    grok_rework_actions, routed = decide_grok_rework(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), notes)
    new_state["verify_fail_unrouted"] = verify_fail_unrouted(items, state.get("verify_fail_unrouted", {}), notes, routed)
    scope = cfg.get("ORCHESTRATE_SCOPE", "")
    if scope:
        queue = [q for q in queue if q[4] == scope]
    if in_flight:
        notes.append({"kind": "in_flight", "targets": in_flight})
    # Dependent tasks never run together without an extra guard: a queued task has every Depends on
    # target met (closed as completed or merged), while an in-flight or same-tick target is an open item,
    # so its dependents are blocked above.
    # dsh runs one task per repo at a time and queues the rest, and a queued task could hit the 3h
    # timeout and count as a failure. So one DeepSeek task per repo: a repo with a task in flight or
    # picked in this tick is skipped, and the slot goes to the next task from another repo.
    busy = {items[k]["repo"] for k in in_flight}
    free = max(0, cap - slots_used)
    if cfg.get("ORCHESTRATE_DISPATCH", "on") == "off":
        free = 0  # pause switch: no new dispatches; in-flight, verify-wake and merges carry on
    picked, seen = 0, set()
    for q in sorted(queue):
        if picked >= free:
            break
        if q[4] in seen:
            continue  # a PR queued by both itself and its issue
        seen.add(q[4])
        if q[2] in busy:
            notes.append({"kind": "repo_busy", "target": q[4], "repo": q[2]})
            continue
        busy.add(q[2])
        picked += 1
        if q[0] == 1:
            pr = items[q[4]]
            plan = rework_plan(q[6], items[q[6]], items, reset_time(items[q[6]]))
            v, cf = plan["verdict"], plan["ci"]
            why = f"verify FAIL {v['url']}" if v else f"CI failure {cf['url']}"
            actions.append({"type": "dispatch", "target": q[4], "owner": q[6], "head": pr["head_sha"],
                            "reason": f"rework after {why}, DeepSeek slot {slots_used + picked} of {cap}",
                            "body": rework_body(pr, v, cf)})
            continue
        retry_line = f"Retry {q[5]}/{INFRA_MAX_RETRIES} after provider error.\n\n" if q[5] else ""
        actions.append({"type": "dispatch", "target": q[4],
                        "reason": (f"infra retry {q[5]}/{INFRA_MAX_RETRIES}, " if q[5] else "next in queue, ")
                        + f"DeepSeek slot {slots_used + picked} of {cap}",
                        "body": f"{DISPATCH_TEXT}\n\n{retry_line}<!-- orchestrate:dispatch -->"})
    actions += decide_grok(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), repos, cycles, notes)
    rebuild_actions, rebuild = decide_rebuild(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), notes)
    actions += rebuild_actions
    actions += grok_rework_actions
    actions += decide_lead(snapshot, cfg, notes)
    merge_actions, merge_notes = decide_merges(snapshot, new_state, now, cfg, repos)
    actions += merge_actions
    notes += merge_notes
    # Last: label repairs take the writes left under the cap, and a PR missed this tick converges on the next.
    actions += decide_verify_labels(items, cfg, notes)
    return {"actions": actions, "notes": notes, "state": new_state, "pending_count": len(pending), "rebuild": rebuild}


# ---------------------------------------------------------------- collect

def ci_verdict(runs, status):
    """'failing' when the newest run of any non-verify check failed or a commit status failed."""
    latest = {}
    for r in (runs or {}).get("check_runs", []):
        if r.get("name") in VERIFY_CHECKS:
            continue
        if r.get("name") not in latest or r.get("id", 0) > latest[r["name"]].get("id", 0):
            latest[r["name"]] = r
    if any(r.get("conclusion") in CI_BAD_CONCLUSIONS for r in latest.values()):
        return "failing"
    for st in (status or {}).get("statuses", []):
        if st.get("context") not in VERIFY_CHECKS and st.get("state") in ("failure", "error"):
            return "failing"
    return "ok"


def ci_reads(gh, repo, sha):
    runs, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/check-runs?per_page=100")
    status, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/status")
    return runs, status


# Checks that wait for the verdict, as verify-queue.yml skips them: the gate, its called form (#402), the
# verifier's own check and verify-queue's job.
WAIT_CHECKS = VERIFY_CHECKS | {"gate / verify-gate", "needs-verify"}


def pr_ci(runs, status):
    """(green | pending | red, vws-verify check failed) for one PR head, as verify-queue.yml decides it: the
    newest run per check name, verify checks excluded; skipped and neutral count as green; no checks at all,
    or a truncated page, is pending (never green). Pure."""
    runs = runs if isinstance(runs, dict) else {}
    latest = {}
    for r in runs.get("check_runs", []):
        if r.get("name") not in latest or r.get("id", 0) > latest[r["name"]].get("id", 0):
            latest[r["name"]] = r
    v = latest.get(VERIFY_CHECK)
    verify_failed = bool(v and v.get("status") == "completed" and v.get("conclusion") == "failure")
    states = {run_state(r) for name, r in latest.items() if name not in WAIT_CHECKS}
    for st in (status if isinstance(status, dict) else {}).get("statuses", []):
        if st.get("context") in WAIT_CHECKS:
            continue
        states.add("failed" if st.get("state") in ("failure", "error") else "ok" if st.get("state") == "success" else "pending")
    if "failed" in states:
        return "red", verify_failed
    if "pending" in states or not states or runs.get("total_count", 0) > len(runs.get("check_runs", [])):
        return "pending", verify_failed
    return "green", verify_failed


# Candidate history (comments and label events) is re-read only when the issue's updated_at moved
# since the copy in history-cache.json (a new comment or label change bumps it), so a quiet tick costs
# no per-item reads. A full re-read every HISTORY_REFRESH_S (plus up to an hour of per-item jitter,
# so entries do not all expire on one tick) catches edits and deletes, which do not bump updated_at.
HISTORY_VERSION = 4  # 2: verdict comments are kept (vws-deploy#373); 3: author and association (#412, #419); 4: author_id (#433)
HISTORY_REFRESH_S = 6 * 3600


def history_path(paths):
    return os.path.join(paths.state, "history-cache.json")


def history_fresh(entry, issue, now):
    if not entry or not issue.get("updated_at") or entry.get("updated_at") != issue["updated_at"]:
        return False
    return now - entry.get("fetched_at", 0) < HISTORY_REFRESH_S + (issue["number"] % 60) * 60


def collect(gh, repo_names, now, cfg, run_id, state=None, history=None):
    # history: the history-cache.json dict, updated in place. None or empty means a full read.
    if history is not None and history.get("version") != HISTORY_VERSION:
        history.clear()
    hist_items = history.setdefault("items", {}) if history is not None else {}
    updated = {}
    window = int(num(cfg, "ORCHESTRATE_COMMENT_WINDOW_DAYS"))
    # A since value floored to the UTC day keeps the URL stable for the day, so ETags can match.
    since = iso((int(now) // 86400 - window) * 86400)
    snap = {"run_id": run_id, "repos": [], "items": {}, "deps": {}, "labels": {}, "ignored_comments": []}
    snap["status"] = {"wakes": [], "keys": []}
    if wake_target(cfg):
        wrepo, wnum = wake_target(cfg).split("#")
        wake_comments = gh.get_all(f"repos/{OWNER}/{wrepo}/issues/{wnum}/comments?per_page=100")
        snap["status"] = {"wakes": status_wakes(wake_comments),
                          "keys": sorted({m.group(1) for c in wake_comments for m in [KEY_RE.search(c.get("body") or "")] if m})}
    # The status issue body (ETag-cached: an unchanged issue costs a 304), compared before any edit.
    srepo, snum = status_target(cfg).split("#")
    issue, _ = gh.get(f"repos/{OWNER}/{srepo}/issues/{snum}")
    snap["status_issue"] = {"body": (issue.get("body") or "") if isinstance(issue, dict) else ""}
    for name in repo_names:
        meta, _ = gh.get(f"repos/{OWNER}/{name}")
        snap["repos"].append({"name": name, "archived": bool(meta.get("archived"))})
        if meta.get("archived"):
            continue
        snap["labels"][name] = sorted(lb["name"] for lb in gh.get_all(f"repos/{OWNER}/{name}/labels?per_page=100"))
        pulls = {p["number"]: p for p in gh.get_all(f"repos/{OWNER}/{name}/pulls?state=open&per_page=100")}
        for i in gh.get_all(f"repos/{OWNER}/{name}/issues?state=open&per_page=100"):
            p = pulls.get(i["number"])
            updated[f"{name}#{i['number']}"] = {"number": i["number"], "updated_at": i.get("updated_at") or ""}
            snap["items"][f"{name}#{i['number']}"] = {
                "repo": name, "number": i["number"], "url": i["html_url"], "is_pr": "pull_request" in i,
                "state": i["state"], "state_reason": i.get("state_reason"), "created_at": i["created_at"],
                "labels": sorted(lb["name"] for lb in i.get("labels", [])), "body": i.get("body") or "",
                "title": i.get("title") or "", "assignees": sorted(a["login"] for a in i.get("assignees") or []),
                "node_id": i.get("node_id", ""),
                "draft": bool(p and p.get("draft")), "base": p["base"]["ref"] if p else "",
                "head_sha": p["head"]["sha"] if p else "", "comments": [], "events": [], "report_prs": {},
            }
        for c in gh.get_all(f"repos/{OWNER}/{name}/issues/comments?since={since}&sort=created&direction=asc&per_page=100", limit=50):
            k = f"{name}#{c['issue_url'].rsplit('/', 1)[1]}"
            if k in snap["items"] and classify_comment(c.get("body")):
                snap["items"][k]["comments"].append(comment_record(c))
    for k, it in sorted(snap["items"].items()):
        labels = set(it["labels"])
        if labels & {"ready", "deepseek", "grok"}:
            # Candidates get their full comment history (the repo-wide list only covers the window),
            # so old dispatches and failures are never forgotten. Unchanged issues reuse the cached copy.
            cached = hist_items.get(k)
            if history_fresh(cached, updated.get(k, {}), now):
                it["comments"], it["events"] = cached["comments"], cached["events"]
            else:
                it["comments"], it["events"] = candidate_history(gh, it)
                seen = updated.get(k, {}).get("updated_at", "")
                if seen and now - utc(seen) < 120:
                    seen = ""  # GitHub stamps whole seconds: a same-second change could hide; re-read next tick
                hist_items[k] = {"updated_at": seen, "fetched_at": now,
                                 "comments": it["comments"], "events": it["events"]}
            for dep in parse_deps(it["body"], it["repo"])[0]:
                if dep in snap["items"] or dep in snap["deps"]:
                    continue
                drepo, dnum = dep.split("#")
                if drepo not in repo_names:
                    continue
                try:
                    d, _ = gh.get(f"repos/{OWNER}/{drepo}/issues/{dnum}")
                    snap["deps"][dep] = {"state": d["state"], "state_reason": d.get("state_reason"),
                                         "merged": bool((d.get("pull_request") or {}).get("merged_at"))}
                except GhError:
                    snap["deps"][dep] = {"unreadable": True}
        # vws-deploy#419/#433: untrusted resets, dispatches, results, verdicts and markers are dropped here,
        # so decide never sees them. The cache keeps every comment, so a config change is applied on the next tick.
        kept = []
        for c in it["comments"]:
            why = comment_distrust(c, cfg)
            if why:
                snap["ignored_comments"].append({"target": k, "comment": c["url"], "kind": classify_comment(c["body"])[0],
                                                 "author": c.get("author") or "", "association": c.get("assoc") or "",
                                                 "reason": why})
            else:
                kept.append(c)
        it["comments"] = kept
        for c in it["comments"]:
            kind = classify_comment(c["body"])
            if kind and kind[0] == "report" and kind[1]["pr"] and kind[1]["pr"] not in it["report_prs"]:
                pk = vws_pr_key(kind[1]["pr"])  # other repos are never read; decide counts them as failures
                if pk:
                    prepo, pnum = pk.split("#")
                    try:
                        pr, _ = gh.get(f"repos/{OWNER}/{prepo}/pulls/{pnum}")
                        it["report_prs"][kind[1]["pr"]] = {"state": pr["state"], "merged": bool(pr.get("merged_at"))}
                    except (GhError, PermissionError):
                        pass  # unreadable: the attempt stays in review until the PR can be read
        if it["is_pr"] and it["state"] == "open" and not it["draft"] and it["base"] == "main":
            # Rule R (vws-deploy#460): the issues and pulls lists carry no mergeable_state. ETag-cached.
            # A read error is "unknown" (no wake this tick), never a failed tick: Rule R only notifies.
            try:
                pull, _ = gh.get(f"repos/{OWNER}/{it['repo']}/pulls/{it['number']}")
            except (GhError, PermissionError):
                pull = None
            it["mergeable_state"] = (pull.get("mergeable_state") or "") if isinstance(pull, dict) else ""
        if it["is_pr"] and it["state"] == "open" and not it["draft"] and it["base"] == "main":
            # Every open PR into main: the verify labels follow its head (vws-deploy#487). ETag-cached reads.
            # Reads Rule A needed before #487 still fail the tick on an error; the new ones only skip the
            # PR in the label rule (verify_ci "unknown"), and Rule D re-reads a verify-approved PR itself.
            waiting = "verify-approved" not in labels
            try:
                runs, status = ci_reads(gh, it["repo"], it["head_sha"])
            except (GhError, PermissionError):
                if waiting:
                    raise
                runs = status = None
            if waiting:
                it["ci"] = ci_verdict(runs, status)
                if NEEDS_VERIFY in labels and not it["events"]:
                    it["events"] = label_events(gh, it)  # Rule A's wait time; ETag-cached
            try:
                if "reviews" not in it:
                    it["reviews"] = verify_reviews(gh, it, cfg)  # the head's verdict; Rule A skips a failed head
            except (GhError, PermissionError):
                if waiting and NEEDS_VERIFY in labels:
                    raise
            if runs is None or "reviews" not in it:
                it["verify_ci"] = "unknown"
            else:
                it["verify_ci"], it["verify_check_failed"] = pr_ci(runs, status)
        elif it["is_pr"]:
            it["verify_ci"] = "ineligible"  # a draft or another base waits for nothing: both labels come off
    for k, it in sorted(snap["items"].items()):
        for pk in rework_prs(k, it, snap["items"]):
            if "reviews" not in snap["items"][pk]:
                snap["items"][pk]["reviews"] = verify_reviews(gh, snap["items"][pk], cfg)
    for k, it in sorted(snap["items"].items()):
        # vws-deploy#457: a FAIL on any open PR's head is routed or logged, so every such PR needs its reviews.
        # ETag-cached; a read error leaves the PR's verdict comments only, never a failed tick.
        if it["is_pr"] and it["state"] == "open" and not it["draft"] and it["base"] == "main" and "reviews" not in it:
            try:
                it["reviews"] = verify_reviews(gh, it, cfg)
            except (GhError, PermissionError):
                it["reviews"] = []
    if history is not None:
        live = {k for k, it in snap["items"].items() if set(it["labels"]) & {"ready", "deepseek", "grok"}}
        for k in list(hist_items):
            if k not in live:
                del hist_items[k]  # closed or unlabeled: re-read in full if it comes back
        history.update(version=HISTORY_VERSION, last_tick=iso(now))
    if cfg.get("ORCHESTRATE_MERGE", "dry-run") != "off":
        collect_merge(gh, snap, repo_names, state)
    return snap


def comment_record(c):
    """A kept comment. author and assoc let decide trust CI-failure markers and CI-cap resets (#412), and
    every comment the orchestrator acts on (#419); author_id pins the verify account (#433/#434)."""
    return {"id": c["id"], "url": c["html_url"], "created_at": c["created_at"], "body": c.get("body") or "",
            "author": (c.get("user") or {}).get("login") or "", "author_id": (c.get("user") or {}).get("id") or 0,
            "assoc": c.get("author_association") or ""}


def candidate_history(gh, it):
    comments = [comment_record(c)
                for c in gh.get_all(f"repos/{OWNER}/{it['repo']}/issues/{it['number']}/comments?per_page=100")
                if classify_comment(c.get("body"))]
    return comments, label_events(gh, it)


def label_events(gh, it):
    return [{"event": e["event"], "label": {"name": (e.get("label") or {}).get("name")}, "created_at": e["created_at"]}
            for e in gh.get_all(f"repos/{OWNER}/{it['repo']}/issues/{it['number']}/events?per_page=100")
            if e.get("event") in ("labeled", "unlabeled")]


def verify_reviews(gh, pr, cfg=DEFAULTS):
    """vws-verify verdict reviews on one PR (ETag-cached), for the rework rule."""
    out = []
    for r in gh.get_all(f"repos/{OWNER}/{pr['repo']}/pulls/{pr['number']}/reviews?per_page=100"):
        user = r.get("user") or {}
        if (is_verifier(user.get("login"), user.get("id"), cfg) and r.get("submitted_at")
                and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")):
            out.append({"id": r["id"], "url": r.get("html_url") or pr["url"], "at": r["submitted_at"],
                        "sha": r.get("commit_id") or "", "result": "fail" if r["state"] == "CHANGES_REQUESTED" else "pass",
                        "body": r.get("body") or ""})
    return out


def actions_runs(gh, repo, sha):
    data, _ = gh.get(f"repos/{OWNER}/{repo}/actions/runs?event=push&branch=main&head_sha={sha}&per_page=100")
    runs = [{"id": r.get("id", 0), "workflow_id": r.get("workflow_id"), "name": r.get("name"),
             "status": r.get("status"), "conclusion": r.get("conclusion")} for r in (data or {}).get("workflow_runs", [])]
    if (data or {}).get("total_count", 0) > len(runs):
        runs.append({"id": 0, "workflow_id": "truncated", "status": "in_progress", "conclusion": None})
    return runs


def main_ahead_of(gh, repo, old, new):
    """True when main head `new` strictly contains commit `old` (compare status "ahead")."""
    data, _ = gh.get(f"repos/{OWNER}/{repo}/compare/{old}...{new}?per_page=1")
    return (data or {}).get("status") == "ahead"


def _app_id(v):
    if v is not None and (type(v) is not int):
        raise ValueError(f"required check app id is not an integer: {v!r}")
    return v


def classic_required(branch):
    """Required checks of main's classic branch protection, from the GET branches/main body that
    collect already read (its `protection` field needs no Administration read, unlike the protection
    API, #408). [] when classic protection is off. Anything unexpected raises ValueError: fail closed."""
    if not isinstance(branch, dict) or type(branch.get("protected")) is not bool:
        raise ValueError("branches/main: no boolean protected field")
    prot = branch.get("protection")
    if not isinstance(prot, dict) or type(prot.get("enabled")) is not bool:
        raise ValueError("branches/main: protection details not visible")
    if not branch["protected"] or not prot["enabled"]:
        return []  # no classic protection (the protection API would answer 404 Branch not protected)
    rsc = prot.get("required_status_checks")
    if (not isinstance(rsc, dict) or not isinstance(rsc.get("checks"), list)
            or not isinstance(rsc.get("contexts"), list)):
        raise ValueError("branches/main: required_status_checks not readable")
    out = []
    for c in rsc["checks"]:
        if not isinstance(c, dict) or not isinstance(c.get("context"), str) or "app_id" not in c:
            raise ValueError(f"branches/main: malformed required check {c!r}")
        out.append({"context": c["context"], "app_id": _app_id(c["app_id"])})
    for ctx in rsc["contexts"]:
        if not isinstance(ctx, str):
            raise ValueError(f"branches/main: malformed required context {ctx!r}")
        if not any(c["context"] == ctx for c in out):
            # A context with no checks entry has no app binding we can read: fail closed rather
            # than accept the check from any app.
            raise ValueError(f"branches/main: required context {ctx!r} has no checks entry")
    return out


def required_checks(gh, repo, branch):
    """Required checks on main: the effective rulesets plus classic branch protection (from the
    branches/main body), deduped by (context, app id). head_check_reasons then needs each one to be
    success, so a skipped or neutral required check never merges (AGENTS.md rule 5)."""
    out = classic_required(branch)
    rules, _ = gh.get(f"repos/{OWNER}/{repo}/rules/branches/main?per_page=100")
    for rule in rules or []:
        if rule.get("type") == "required_status_checks":
            for c in (rule.get("parameters") or {}).get("required_status_checks", []):
                out.append({"context": c["context"], "app_id": c.get("integration_id")})
    uniq = {(c["context"], c["app_id"]): c for c in out}
    return [uniq[k] for k in sorted(uniq, key=lambda k: (k[0], k[1] or 0))]


def merge_info(gh, repo, number):
    pr, _ = gh.get(f"repos/{OWNER}/{repo}/pulls/{number}")
    sha = pr["head"]["sha"]
    runs, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/check-runs?per_page=100")
    status, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/status")
    # One GET maps each Actions check suite to its workflow and event (a re-run gets a new suite, same both).
    wruns, _ = gh.get(f"repos/{OWNER}/{repo}/actions/runs?head_sha={sha}&per_page=100")
    workflow = {w["check_suite_id"]: f'{w.get("path") or w.get("workflow_id")}@{w.get("event")}'
                for w in (wruns or {}).get("workflow_runs", []) if w.get("check_suite_id")}
    checks = [{"id": r.get("id", 0), "name": r.get("name"), "app_id": (r.get("app") or {}).get("id"),
               "status": r.get("status"), "conclusion": r.get("conclusion"), "head_sha": r.get("head_sha"),
               "started_at": r.get("started_at"), "completed_at": r.get("completed_at"), "suite_id": (r.get("check_suite") or {}).get("id"),
               "workflow": workflow.get((r.get("check_suite") or {}).get("id"))}
              for r in (runs or {}).get("check_runs", [])]
    unmapped = sorted({c["name"] for c in checks if c["app_id"] == ACTIONS_APP_ID and not c["workflow"]})
    return {"head_sha": sha, "state": pr.get("state"), "merged": bool(pr.get("merged")), "draft": bool(pr.get("draft")),
            "base": pr["base"]["ref"], "mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"),
            "title": pr.get("title") or "", "checks": checks, "unmapped": unmapped,
            "checks_truncated": (runs or {}).get("total_count", 0) > len(checks),
            "statuses": [{"context": st.get("context"), "state": st.get("state")} for st in (status or {}).get("statuses", [])],
            # The combined status lists only its first page; more contexts than shown fail closed.
            "statuses_truncated": (status or {}).get("total_count", 0) > len((status or {}).get("statuses", [])),
            "status_state": (status or {}).get("state")}


def pr_files(gh, repo, number):
    """The paths a PR changes, for Rule D's serial set (GitHub lists at most 3000; 30 pages of 100)."""
    return [f.get("filename") for f in gh.get_all(f"repos/{OWNER}/{repo}/pulls/{number}/files?per_page=100", limit=30)]


def collect_merge(gh, snap, repo_names, state):
    """Rule D inputs, read only for repos with a verify-approved candidate or a merge lock. A read error
    makes that repo's context an error (no merge there this tick) instead of failing the tick."""
    locks = (state or {}).get("merge_locks", {})
    snap["merge"] = {}
    for name in repo_names:
        if not any(r["name"] == name and not r.get("archived") for r in snap["repos"]):
            continue
        cands = [it for it in snap["items"].values() if it["repo"] == name and it["is_pr"] and it["state"] == "open"
                 and not it["draft"] and it["base"] == "main" and "verify-approved" in it["labels"]]
        lock = locks.get(name)
        if not cands and not lock:
            continue
        ctx = {}
        try:
            branch, _ = gh.get(f"repos/{OWNER}/{name}/branches/main")
            ctx["main_sha"] = branch["commit"]["sha"]
            ctx["main_ci"] = main_ci_verdict(actions_runs(gh, name, ctx["main_sha"]))
            if lock and lock.get("status") == "merging":
                _, num_ = lock["pr"].split("#")
                pr, _ = gh.get(f"repos/{OWNER}/{name}/pulls/{num_}")
                ctx["lock_pr"] = {"state": pr.get("state"), "merged": bool(pr.get("merged")),
                                  "merge_commit_sha": pr.get("merge_commit_sha") or ""}
            if lock and lock.get("status") == "waiting" and lock.get("merge_sha") != ctx["main_sha"]:
                ctx["lock_ci"] = main_ci_verdict(actions_runs(gh, name, lock["merge_sha"]))
                ctx["lock_ahead"] = main_ahead_of(gh, name, lock["merge_sha"], ctx["main_sha"])
            if lock and lock.get("status") == "red" and lock.get("red_sha") != ctx["main_sha"]:
                ctx["red_ahead"] = main_ahead_of(gh, name, lock["red_sha"], ctx["main_sha"])
            if cands:
                ctx["required"] = required_checks(gh, name, branch)
            for it in cands:
                it["merge_info"] = merge_info(gh, name, it["number"])
                it["merge_info"]["files"] = pr_files(gh, name, it["number"])
                for dep in parse_deps(it["body"], name)[0]:
                    drepo, dnum = dep.split("#")
                    if dep in snap["items"] or dep in snap["deps"] or drepo not in repo_names:
                        continue
                    try:
                        d, _ = gh.get(f"repos/{OWNER}/{drepo}/issues/{dnum}")
                        snap["deps"][dep] = {"state": d["state"], "state_reason": d.get("state_reason"),
                                             "merged": bool((d.get("pull_request") or {}).get("merged_at"))}
                    except GhError:
                        snap["deps"][dep] = {"unreadable": True}
        except (GhError, PermissionError, KeyError, TypeError, ValueError) as e:
            ctx = {"error": scrub(e)}
            for it in cands:
                it.pop("merge_info", None)
        snap["merge"][name] = ctx


# ---------------------------------------------------------------- apply

def apply(result, snapshot, gh, cfg, paths, log, state, now=None):
    """Run the planned actions. Returns the state to save (dry-run never calls this)."""
    now = now or time.time()
    max_writes = int(num(cfg, "ORCHESTRATE_MAX_WRITES"))
    scope = cfg["ORCHESTRATE_SCOPE"]
    writes, errors = 0, 0
    new_state = result["state"]
    grok_repos = [r["name"] for r in snapshot.get("repos", []) if not r.get("archived")]
    grok_posted = []  # Grok wakes this apply already sent (or tried to): they count toward the cap
    for a in result["actions"]:
        if a["type"] in ("merge", "merge_status") and cfg["ORCHESTRATE_MERGE"] != "apply":
            log("skipped", action=a["type"], target=a["target"], reason=f"ORCHESTRATE_MERGE={cfg['ORCHESTRATE_MERGE']}")
            continue
        if a["type"] in ("wake_verify", "merge_status", "infra_status", "grok_wake", "rebuild_wake", "grok_rework", "lead_wake") and scope:
            log("skipped", action=a["type"], reason="ORCHESTRATE_SCOPE is set")
            continue
        if scope and a["target"] != scope:
            log("skipped", action=a["type"], target=a["target"], reason="outside ORCHESTRATE_SCOPE")
            continue
        if a["type"] in ("merge_status", "infra_status"):
            # An entry in the status issue body (vws-deploy#416), not a comment: no gh write here.
            add_status_event(new_state, a, now)
            log("status_event", action=a["type"], key=a["key"], reason=a["reason"])
            continue
        repo, number = a["target"].split("#")
        base = f"repos/{OWNER}/{repo}/issues/{number}"
        calls = []
        if a["type"] in ("dispatch", "wake_verify", "grok_wake", "rebuild_wake", "grok_rework", "lead_wake"):
            calls.append(("POST", f"{base}/comments", {"body": a["body"]}))
        elif a["type"] == "swap_label":
            if a.get("comment"):
                calls.append(("POST", f"{base}/comments", {"body": a["body"]}))
            if a["add"]:
                calls.append(("POST", f"{base}/labels", {"labels": [a["add"]]}))
            calls.append(("DELETE", f"{base}/labels/{a['remove']}", None))
        elif a["type"] == "add_label":
            calls.append(("POST", f"{base}/labels", {"labels": [a["label"]]}))
        elif a["type"] == "remove_label":
            calls.append(("DELETE", f"{base}/labels/{a['label']}", None))
        elif a["type"] == "merge":
            calls.append(("PUT", f"repos/{OWNER}/{repo}/pulls/{number}/merge", a["payload"]))
        label = a.get("add") or a.get("label")
        if label and label not in snapshot["labels"].get(repo, []):
            log("skipped", action=a["type"], target=a["target"], reason=f"label {label} does not exist on {repo}")
            continue
        if writes + len(calls) > max_writes:
            log("skipped", action=a["type"], target=a["target"], reason=f"write cap {max_writes} reached")
            continue
        if a["type"] == "wake_verify" and not recheck_wake(gh, repo, number, a["key"]):
            log("skipped", action="wake_verify", reason="a comment with this key already exists")
            continue
        if a["type"] == "dispatch" and not recheck_dispatch(gh, repo, number, snapshot["items"][a["target"]],
                                                            a.get("owner"), a.get("head")):
            log("skipped", action="dispatch", target=a["target"], reason="target changed since collect")
            continue
        if a["type"] == "lead_wake" and not recheck_key(gh, repo, number, a["key"]):
            log("skipped", action="lead_wake", target=a["target"], item=a["item"],
                reason="a comment with this key already exists")
            continue
        if a["type"] == "grok_wake":
            if not recheck_key(gh, repo, number, a["key"]):
                log("skipped", action="grok_wake", target=a["target"], item=a["item"],
                    reason="a comment with this key already exists")
                continue
            why = recheck_grok(gh, a["item"], grok_repos, int(num(cfg, "ORCHESTRATE_GROK_CAP")),
                               a.get("woken", ()), grok_posted)
            if why:
                log("skipped", action="grok_wake", target=a["target"], item=a["item"], reason=why)
                continue
            grok_posted.append(a["item"])  # before the POST: a failed POST may still have landed
        if a["type"] in ("rebuild_wake", "grok_rework"):
            if not recheck_key(gh, repo, number, a["key"]):
                log("skipped", action=a["type"], target=a["target"], item=a["item"],
                    reason="a comment with this key already exists")
                continue
            if not recheck_rebuild(gh, a["item"], a["head"]):
                log("skipped", action=a["type"], target=a["target"], item=a["item"], reason="PR changed since collect")
                continue
        if a["type"] == "merge":
            why = recheck_merge(gh, repo, number, a["head"], new_state, snapshot.get("merge", {}).get(repo, {}))
            if why:
                log("skipped", action="merge", target=a["target"], reason=f"target changed since collect: {why}")
                continue
            # Journal the lock and the attempt before the PUT: a crash after a merge that landed still
            # locks the repo, and the next tick resolves "merging" from the PR's merged state.
            new_state.setdefault("merge_locks", {})[repo] = {"status": "merging", "pr": a["target"], "head": a["head"], "at": now}
            att = new_state.setdefault("merge_attempts", {})
            att[f"{a['target']}@{a['head']}"] = att.get(f"{a['target']}@{a['head']}", 0) + 1
            write_json(paths.state_file, new_state)
        owned = new_state.setdefault("owned_labels", {})
        if a["type"] == "add_label" and a["label"] in MANAGED_LABELS:
            # Journal ownership before the write, so a crash right after it cannot orphan the label.
            owned.setdefault(a["label"], [])
            if a["target"] not in owned[a["label"]]:
                owned[a["label"]].append(a["target"])
            write_json(paths.state_file, new_state)
        absent = []
        try:
            for method, path, payload in calls:
                writes += 1  # counted before the call: a failed POST may still have landed
                try:
                    resp = gh.write(method, path, payload)
                except GhError as e:
                    if not label_absent(method, path, e):
                        raise
                    absent.append(path.rsplit("/", 1)[1])
        except (GhError, PermissionError) as e:
            errors += 1
            log("apply_failed", action=a["type"], target=a["target"], error=str(e))
            continue
        if a["type"] == "merge":
            msha = (resp or {}).get("sha") or ""
            if (resp or {}).get("merged") and re.fullmatch(r"[0-9a-f]{40}", msha):
                new_state["merge_locks"][repo] = {"status": "waiting", "pr": a["target"], "head": a["head"],
                                                  "merge_sha": msha, "at": now, "merged_at": now}
            write_json(paths.state_file, new_state)
            # The Agent: line is logged as written in the PR body (empty when absent); no rule reads it.
            log("merged", target=a["target"], head=a["head"], merge_sha=msha, agent=a.get("agent", ""),
                note="Agent: is informational; eligibility never depends on it", reason=a["reason"])
        if a["type"] == "remove_label" and a["target"] in owned.get(a["label"], []):
            owned[a["label"]].remove(a["target"])
        if absent:
            # Idempotent: the label was already gone, which is the state this action wanted.
            log("skipped", action=a["type"], target=a["target"],
                reason=f"label {', '.join(absent)} already absent (404 Label does not exist)")
            if a["type"] == "remove_label":
                continue
        if a["type"] == "wake_verify":
            log("wake_posted", target=a["target"], key=a["key"])
        if a["type"] == "rebuild_wake":
            log("rebuild_wake_posted", target=a["target"], item=a["item"], key=a["key"])
        if a["type"] == "grok_rework":
            log("grok_rework_posted", target=a["target"], item=a["item"], key=a["key"])
        if a["type"] == "lead_wake":
            log("lead_wake_posted", target=a["target"], item=a["item"], key=a["key"])
        if a["type"] == "grok_wake":
            log("grok_wake_posted", target=a["target"], item=a["item"], key=a["key"])
        log("applied", action=a["type"], target=a["target"], reason=a["reason"])
    return new_state, errors


def recheck_key(gh, repo, number, key):
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    return not any(f"Idempotency key: `{key}`" in (c.get("body") or "") for c in comments)


def recheck_rebuild(gh, k, head):
    """True while the PR is still open at the head the wake names (a new head gets its own wake)."""
    repo, number = k.split("#")
    try:
        pr, _ = gh.get(f"repos/{OWNER}/{repo}/pulls/{number}")
    except (GhError, PermissionError):
        return False
    return isinstance(pr, dict) and pr.get("state") == "open" and (pr.get("head") or {}).get("sha") == head


def recheck_wake(gh, repo, number, key):
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    return key not in {w["key"] for w in status_wakes(comments)}


def add_status_event(state, a, now):
    """Journal one merge or infra entry for the status issue body; one per key, the newest kept."""
    events = [e for e in state.get("status_events", []) if e["key"] != a["key"]]
    kind = "merge" if a["type"] == "merge_status" else "infra"
    events.append({"key": a["key"], "at": now, "kind": kind, "text": scrub(a["reason"])})
    state["status_events"] = events[-STATUS_EVENTS_KEEP:]


# ---------------------------------------------------------------- the status issue body (vws-deploy#416)

def status_body(snapshot, result, cfg, state, mode, now, failure=""):
    """Pure: the status issue body. Only the last line (_Updated ..._) changes from tick to tick without a
    real change, and status_same ignores it, so a quiet tick edits nothing."""
    notes = result.get("notes", [])
    in_flight = next((n["targets"] for n in notes if n.get("kind") == "in_flight"), [])
    wake = wake_target(cfg)
    lines = [STATUS_MARKER,
             "Factory status. The orchestrator rewrites this block (down to the end marker) in place each tick; "
             "text outside it is yours and is kept. It does not post status comments.",
             "",
             f"Comments here are metrics alerts only (marker `{METRICS_ALERT_MARKER}`). verify-wake does not post here.",
             "", "## Now", "",
             f"- Mode: {mode}; dispatch {cfg['ORCHESTRATE_DISPATCH']}, merge {cfg['ORCHESTRATE_MERGE']}, "
             f"board {cfg['ORCHESTRATE_BOARD']}",
             f"- PRs waiting for verify: {result.get('pending_count', 0)} (wake threshold {int(num(cfg, 'VERIFY_WAKE_THRESHOLD'))}; "
             + (f"verify wakes go to {wake})" if wake else "verify wake is off: ORCHESTRATE_WAKE_THREAD is unset)"),
             f"- DeepSeek in flight: {len(in_flight)} of {int(num(cfg, 'ORCHESTRATE_DEEPSEEK_CAP'))}"
             + (": " + ", ".join(in_flight) if in_flight else "")]
    rebuild = result.get("rebuild", [])
    lines.append(f"- PRs waiting for a rebuild: {len(rebuild)}" + (": " + ", ".join(
        f"{p['target']} ({p['owner'] or 'no Agent: line'}, {p['why']})" for p in rebuild) if rebuild else ""))
    locks = state.get("merge_locks", {})
    lines.append("- Merge locks: " + ("; ".join(
        f"{repo} {lk.get('status', '?')} ({lk.get('pr', '?')}"
        + (f", main `{lk['merge_sha'][:7]}`" if lk.get("merge_sha") else "") + ")"
        for repo, lk in sorted(locks.items())) if locks else "none"))
    lines += ["", "## Recent entries", ""]
    events = sorted(state.get("status_events", []), key=lambda e: (e["at"], e["key"]), reverse=True)
    lines += [f"- {iso(e['at'])} {e['kind']}: {e['text']} (key `{e['key']}`)" for e in events] or ["- none"]
    if failure:
        lines += ["", failure.rstrip("\n")]
    lines += ["", f"_Updated {iso(now)} by run `{snapshot.get('run_id', '')}`._", STATUS_END_MARKER]
    return "\n".join(lines) + "\n"


def status_unowned(body):
    """'' when body has exactly one status block (STATUS_MARKER, later STATUS_END_MARKER), else why not.
    The orchestrator writes only inside that block and never writes a body that lacks one."""
    body = body or ""
    starts, ends = body.count(STATUS_MARKER), body.count(STATUS_END_MARKER)
    if starts != 1 or ends != 1:
        return (f"the body needs exactly one {STATUS_MARKER} ... {STATUS_END_MARKER} block; "
                f"found {starts} start and {ends} end markers")
    if body.index(STATUS_END_MARKER) < body.index(STATUS_MARKER):
        return "the end marker comes before the start marker"
    return ""


def _status_span(body):
    """(start, end) of the status block, end just past STATUS_END_MARKER; body must be owned."""
    return body.index(STATUS_MARKER), body.index(STATUS_END_MARKER) + len(STATUS_END_MARKER)


def status_block(body):
    """The status block of body, from the start marker through the end marker ('' when unowned)."""
    if status_unowned(body):
        return ""
    s, e = _status_span(body)
    return body[s:e]


def splice_status(current, block):
    """current with its status block replaced by the block in block; everything outside is kept byte for
    byte. None when either has no single status block (the caller writes nothing)."""
    if status_unowned(current) or status_unowned(block):
        return None
    s, e = _status_span(current)
    bs, be = _status_span(block)
    return current[:s] + block[bs:be] + current[e:]


def _status_norm(body):
    text = STATUS_UPDATED_RE.sub("", (body or "").replace("\r\n", "\n"))
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))


def status_same(old, new):
    return _status_norm(old) == _status_norm(new)


FAILURE_SECTION_RE = re.compile(rf"^## Failed ticks\n.*?(?=^## |^_Updated |^{re.escape(STATUS_END_MARKER)}|\Z)",
                                re.M | re.S)


def failure_section(body):
    """The '## Failed ticks' section inside the status block ('' when there is none or no block)."""
    m = FAILURE_SECTION_RE.search(status_block(body))
    return m.group(0).rstrip("\n") if m else ""


def set_failure_section(body, section):
    """body with the failure section inside its status block replaced by section; text outside the block
    is kept byte for byte. None when body has no single status block (the caller writes nothing)."""
    if status_unowned(body):
        return None
    s, e = _status_span(body)
    inner = body[s:e - len(STATUS_END_MARKER)]
    old = failure_section(body)
    if old:
        inner = inner.replace(old, section.rstrip("\n"), 1)
    else:
        m = STATUS_UPDATED_RE.search(inner)
        at = m.start() if m else len(inner)
        inner = inner[:at].rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n\n" + inner[at:].lstrip("\n")
    return body[:s] + inner.rstrip("\n") + "\n" + STATUS_END_MARKER + body[e:]


def publish_status(gh, cfg, snapshot, body, log):
    """Replace the status block of the status issue body when its content changed; text outside the block
    is kept. A body without a single block is not written (fail closed). Returns failed writes (0 or 1)."""
    repo, number = status_target(cfg).split("#")
    current = snapshot.get("status_issue", {}).get("body", "")
    reason = status_unowned(current)
    if reason:
        log("status_body_unowned", target=status_target(cfg), reason=reason)
        return 1
    if status_same(status_block(current), body):
        log("status_unchanged", target=status_target(cfg))
        return 0
    body = splice_status(current, body)
    try:
        gh.write("PATCH", f"repos/{OWNER}/{repo}/issues/{number}", {"body": body})
    except (GhError, PermissionError) as e:
        log("apply_failed", action="status_body", target=status_target(cfg), error=str(e))
        return 1
    log("status_updated", target=status_target(cfg), chars=len(body))
    return 0


def recheck_grok(gh, k, repos, cap, woken=(), posted=()):
    """Rule G, right before a wake is posted. Returns '' when it may go ahead, else why not.
    The issue must still be open, ready+grok, unassigned, without `deepseek` or an opt-out label.
    Then occupancy is rebuilt from a fresh read of the open grok items of every scoped repo: Grok work
    held now (grok_held: assigned, or named by an open grok PR), the tasks the tick saw as woken that
    are still open with grok, and the wakes already posted this tick all count toward cap; the issue's
    own repo must not be busy (grok_busy). A failed read skips the wake (fails closed)."""
    repo, number = k.split("#")
    try:
        issue, _ = gh.get(f"repos/{OWNER}/{repo}/issues/{number}")
        names = {lb["name"] for lb in issue.get("labels", [])}
        if not (issue.get("state") == "open" and {"ready", "grok"} <= names and not issue.get("assignees")
                and not names & ({"deepseek"} | set(OPT_OUT_LABELS))):
            return "the issue changed since collect"
        fresh = {}
        for name in repos:
            for i in gh.get_all(f"repos/{OWNER}/{name}/issues?state=open&labels=grok&per_page=100"):
                fresh[f"{name}#{i['number']}"] = {
                    "repo": name, "number": i["number"], "is_pr": "pull_request" in i,
                    "state": i.get("state") or "open", "body": i.get("body") or "",
                    "labels": sorted(lb["name"] for lb in i.get("labels") or []),
                    "assignees": sorted(a["login"] for a in i.get("assignees") or [])}
    except (GhError, PermissionError, KeyError, TypeError) as e:
        return f"re-read failed: {scrub(e)}"
    held = grok_held(fresh)
    held |= {grok_link(fresh[w]) or w for w in woken if w in fresh and "grok" in fresh[w]["labels"]}
    held |= set(posted)
    unit = (grok_link(fresh[k]) if k in fresh else "") or k
    if unit in held or k in held:
        return "Grok work on this issue is already in flight"
    if len(held) >= cap:
        return f"Grok cap {cap} reached: " + ", ".join(sorted(held))
    if repo in grok_busy(fresh, held):
        return f"repo {repo} has Grok work in flight"
    return ""


def recheck_merge(gh, repo, number, head, new_state, ctx):
    """Re-read the PR, its head checks and main right before the merge. Returns '' when it may go
    ahead, else why not. The pinned sha protects only the head, so the CI verdicts are re-read too."""
    if repo in new_state.get("merge_locks", {}):
        return "repo is locked"
    try:
        branch, _ = gh.get(f"repos/{OWNER}/{repo}/branches/main")
        if branch["commit"]["sha"] != ctx.get("main_sha"):
            return "main moved"
        if main_ci_verdict(actions_runs(gh, repo, ctx["main_sha"])) != "green":
            return "main push CI is no longer green"
        info = merge_info(gh, repo, number)
    except (GhError, PermissionError, KeyError, TypeError) as e:
        return f"re-read failed: {scrub(e)}"
    if info["head_sha"] != head:
        return "head moved"
    if info.get("mergeable") is not True or info.get("mergeable_state") not in MERGEABLE_OK:
        return f"merge state changed: mergeable={info.get('mergeable')} state={info.get('mergeable_state')}"
    bad = head_check_reasons(info, ctx.get("required"))
    if bad:
        return "head checks changed: " + ", ".join(bad[:3])
    try:
        pr, _ = gh.get(f"repos/{OWNER}/{repo}/pulls/{number}")
    except (GhError, PermissionError) as e:
        return f"re-read failed: {scrub(e)}"
    names = {lb["name"] for lb in pr.get("labels", [])}
    if pr.get("state") != "open" or pr.get("merged"):
        return "not open"
    if pr["head"]["sha"] != head:
        return "head moved"
    if pr.get("draft") or pr["base"]["ref"] != "main":
        return "draft or base changed"
    if "verify-approved" not in names or names & set(OPT_OUT_LABELS) or OPT_OUT_MARKER in (pr.get("body") or ""):
        return "labels or opt-out changed"
    return ""


def recheck_dispatch(gh, repo, number, item, owner=None, head=None):
    """Re-read the target right before the dispatch comment: no new dispatch since collect. A rework also
    needs its PR head unchanged and its owner (the PR or its issue) still ready+deepseek."""
    known = {c["id"] for c in item["comments"]}
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    for c in comments:
        kind = classify_comment(c.get("body"))
        if (c["id"] not in known and kind and kind[0] == "dispatch"
                and is_trusted_reset_author((c.get("user") or {}).get("login"), c.get("author_association"))):
            return False
    if head:
        pr, _ = gh.get(f"repos/{OWNER}/{repo}/pulls/{number}")
        if pr.get("state") != "open" or pr["head"]["sha"] != head:
            return False
    orepo, onum = (owner or f"{repo}#{number}").split("#")
    issue, _ = gh.get(f"repos/{OWNER}/{orepo}/issues/{onum}")
    names = {lb["name"] for lb in issue.get("labels", [])}
    return issue["state"] == "open" and {"ready", "deepseek"} <= names and not names & {"grok", "blocked"}


# ---------------------------------------------------------------- board_sync (vws-deploy#347)
# One-way mirror of labels and bodies to the user project (Projects v2). It writes project items
# only; decide() never reads the board. Field and option creation stays with board-apply.sh.

BOARD_FIELDS = ("Status", "Owner", "Priority", "Depends on")
BOARD_BATCH = 20  # aliased mutations per GraphQL request
BOARD_PAUSE = 3  # seconds between write requests (secondary rate limit)
BOARD_LABELS = ("blocked", "in-review", "deepseek")  # a change to these triggers a sync on the next tick
BOARD_QUERY = (
    "query($p:ID!,$after:String){ rateLimit{ remaining resetAt } node(id:$p){ ... on ProjectV2{"
    " fields(first:50){ nodes{ ... on ProjectV2FieldCommon{ id name }"
    " ... on ProjectV2SingleSelectField{ options{ id name } } } }"
    " items(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ id"
    # Issue.state and PullRequest.state have different enum types, so one response name for both
    # breaks the GraphQL field-merge rule; each gets its own alias.
    " content{ ... on Issue{ url issueState: state } ... on PullRequest{ url prState: state } }"
    " fieldValues(first:30){ nodes{"
    " ... on ProjectV2ItemFieldSingleSelectValue{ name field{ ... on ProjectV2FieldCommon{ name } } }"
    " ... on ProjectV2ItemFieldTextValue{ text field{ ... on ProjectV2FieldCommon{ name } } } } } } } } } }")
# A write request is built only from these templates; every value travels in GraphQL variables.
BOARD_ADD = "m{i}: addProjectV2ItemById(input:{{projectId:$p, contentId:$c{i}}}){{ item{{ id }} }}"
BOARD_SET = ("m{i}: updateProjectV2ItemFieldValue(input:{{projectId:$p, itemId:$i{i}, fieldId:$f{i}, "
             "value:{{{kind}:$v{i}}}}}){{ clientMutationId }}")
BOARD_CLEAR = "m{i}: clearProjectV2ItemFieldValue(input:{{projectId:$p, itemId:$i{i}, fieldId:$f{i}}}){{ clientMutationId }}"
GQL_DECL_RE = re.compile(r"^\$p:ID!(, (\$[cif][0-9]+:ID|\$v[0-9]+:String)!)*$")


def board_write_ok(query):
    """True only for a batch whose every part is one of the BOARD_* templates, verbatim, with its index."""
    m = re.fullmatch(r"mutation\(([^()]*)\)\{ (.+) \}", query, re.S)
    if not m or not GQL_DECL_RE.match(m.group(1)):
        return False
    for part in re.split(r" (?=m[0-9]+: )", m.group(2)):
        i = re.match(r"m([0-9]+): ", part)
        if not i or part not in (BOARD_ADD.format(i=i.group(1)), BOARD_CLEAR.format(i=i.group(1)),
                                 BOARD_SET.format(i=i.group(1), kind="text"),
                                 BOARD_SET.format(i=i.group(1), kind="singleSelectOptionId")):
            return False
    return True


def board_values(it):
    """The board fields of one open item, from its labels and body only (design table, first match wins)."""
    L = set(it["labels"])
    if "blocked" in L:
        status = "Blocked"
    elif it["is_pr"] or "in-review" in L:
        status = "In review"
    elif "grok" in L:
        status = "Grok"
    elif "deepseek" in L:
        status = "DeepSeek"
    elif "ready" in L:
        status = "Ready"
    else:
        status = "Backlog"
    if "grok" in L:
        owner = "grok"
    elif "deepseek" in L:
        owner = "deepseek"
    elif "in-review" in L or (it["is_pr"] and not it["draft"]):
        owner = "verify"
    else:
        owner = ""
    return {"Status": status, "Owner": owner, "Priority": "Blocker" if "priority/blocker" in L else "Normal",
            "Depends on": ", ".join(parse_deps(it.get("body"), it["repo"])[0])}


def board_due(bstate, every):
    """Sync on the first tick, when ORCHESTRATE_BOARD_EVERY ticks have passed, or after a label change."""
    return bool(bstate.get("dirty")) or "last_sync" not in bstate or bstate.get("ticks", 0) + 1 >= every


def board_plan(open_items, board_items, budget, scope=""):
    """Pure diff. open_items: collect() items (open only). board_items: url -> {id, state, values}.
    Returns (mutations within the budget, number deferred). A mutation is a dict with op add|set|clear,
    target, url, field, value and item (a board item id, or None for an item added in the same run).
    Budget order: Status of existing items (closed -> Done included), adds with their Status, Owner and
    Priority, Depends on. Every mutation sets the computed value, so a cut-off run converges."""
    tiers = [[], [], [], []]
    open_urls = set()
    for key, it in sorted(open_items.items()):
        if scope and key != scope:
            continue
        open_urls.add(it["url"])
        cur = board_items.get(it["url"])
        want = board_values(it)
        have = cur["values"] if cur else {}
        if cur is None:
            tiers[1].append({"op": "add", "target": key, "url": it["url"], "content": it.get("node_id"), "item": None})
        for name in BOARD_FIELDS:
            val, was = want[name], have.get(name) or ""
            if val == was:
                continue  # empty and unset compare equal, so clearing an unset field costs nothing
            m = {"op": "set" if val else "clear", "target": key, "url": it["url"], "field": name, "value": val,
                 "was": was, "item": cur["id"] if cur else None}
            tier = {"Status": 0 if cur else 1, "Owner": 2, "Priority": 2, "Depends on": 3}[name]
            tiers[tier].append(m)
    for url, cur in sorted(board_items.items()):
        # Fallback for the built-in Done workflows: a closed or merged item not marked Done gets Done.
        if url in open_urls or cur.get("state") not in ("CLOSED", "MERGED") or cur["values"].get("Status") == "Done":
            continue
        m = URL_RE.match(url)
        if not m or m.group(1) != OWNER or not m.group(2).startswith("vws-"):
            continue
        key = f"{m.group(2)}#{m.group(3)}"
        if scope and key != scope:
            continue
        tiers[0].append({"op": "set", "target": key, "url": url, "field": "Status", "value": "Done",
                         "was": cur["values"].get("Status") or "", "item": cur["id"]})
    out, added, deferred = [], set(), 0
    for tier in tiers:
        for m in tier:
            if m["item"] is None and m["op"] != "add" and m["url"] not in added:
                deferred += 1  # its add did not fit the budget
                continue
            if len(out) >= budget:
                deferred += 1
                continue
            if m["op"] == "add":
                added.add(m["url"])
            out.append(m)
    return out, deferred


def board_read(gh, project):
    """Fields and items of the project, plus the GraphQL rate limit from the same query."""
    fields, items, after, remaining = {}, {}, None, None
    for _ in range(20):
        data = gh.graphql(BOARD_QUERY, {"p": project, "after": after})
        remaining = (data.get("rateLimit") or {}).get("remaining")
        node = data.get("node") or {}
        if "items" not in node:
            raise GhError(0, f"project {project} not found or not readable")
        for f in node["fields"]["nodes"]:
            if f and f.get("name"):
                fields[f["name"]] = {"id": f["id"], "options": {o["name"]: o["id"] for o in f.get("options") or []}}
        for n in node["items"]["nodes"]:
            url = (n.get("content") or {}).get("url")
            if not url:
                continue
            vals = {}
            for v in n["fieldValues"]["nodes"]:
                if v and v.get("field"):
                    vals[v["field"]["name"]] = v.get("name", v.get("text"))
            c = n["content"]
            items[url] = {"id": n["id"], "state": c.get("issueState") or c.get("prState"), "values": vals}
        page = node["items"]["pageInfo"]
        if not page["hasNextPage"]:
            return fields, items, remaining
        after = page["endCursor"]
    raise GhError(0, "more than 20 pages of board items")


def board_write(gh, project, fields, muts, sleep):
    """Run the planned mutations in aliased batches: adds first (they give the new item ids), then sets."""
    for name in {m["field"] for m in muts if m["op"] != "add"}:
        if name not in fields:
            raise ValueError(f"board field {name!r} is missing; create it with board-apply.sh")
    new_ids, done = {}, 0
    adds = [m for m in muts if m["op"] == "add"]
    sets = [m for m in muts if m["op"] != "add"]
    for group in (adds, sets):
        for start in range(0, len(group), BOARD_BATCH):
            chunk = group[start:start + BOARD_BATCH]
            decl, parts, variables = ["$p:ID!"], [], {"p": project}
            for i, m in enumerate(chunk):
                if m["op"] == "add":
                    if not m.get("content"):
                        raise ValueError(f"{m['target']}: no node id to add")
                    decl.append(f"$c{i}:ID!")
                    variables[f"c{i}"] = m["content"]
                    parts.append(BOARD_ADD.format(i=i))
                    continue
                f = fields[m["field"]]
                decl += [f"$i{i}:ID!", f"$f{i}:ID!"]
                variables[f"i{i}"] = m["item"] or new_ids[m["url"]]
                variables[f"f{i}"] = f["id"]
                if m["op"] == "clear":
                    parts.append(BOARD_CLEAR.format(i=i))
                    continue
                if f["options"]:
                    if m["value"] not in f["options"]:
                        raise ValueError(f"board field {m['field']!r} has no option {m['value']!r}; run board-apply.sh")
                    kind, val = "singleSelectOptionId", f["options"][m["value"]]
                else:
                    kind, val = "text", m["value"]
                decl.append(f"$v{i}:String!")  # ProjectV2FieldValue: text and singleSelectOptionId are String
                variables[f"v{i}"] = val
                parts.append(BOARD_SET.format(i=i, kind=kind))
            if done:
                sleep(BOARD_PAUSE)
            data = gh.graphql(f"mutation({', '.join(decl)}){{ {' '.join(parts)} }}", variables, write=True)
            done += len(chunk)
            for i, m in enumerate(chunk):
                if m["op"] == "add":
                    new_ids[m["url"]] = data[f"m{i}"]["item"]["id"]
    return done


def read_board_token(path):
    """The board token from its file, or None if the file is missing, unreadable or empty."""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return token or None


def board_sync(gh, snapshot, cfg, paths, log, mode, label_change):
    """After apply(): mirror labels to the board on sync ticks. Never raises and never fails the tick."""
    if cfg["ORCHESTRATE_BOARD"] == "off":
        return
    bstate = read_json(paths.board_file, {})
    token = read_board_token(cfg["ORCHESTRATE_BOARD_TOKEN_FILE"])
    if not token:
        # Not an error: the cadence is left as is, so the first tick with a token syncs.
        log("board_sync_skipped", reason="token_missing")
        if label_change:
            bstate["dirty"] = True
            write_json(paths.board_file, bstate)
        return
    try:
        _board_sync(gh, snapshot, cfg, paths, log, mode, label_change, bstate, token)
    finally:
        gh.board_token = None


def _board_sync(gh, snapshot, cfg, paths, log, mode, label_change, bstate, token):
    gh.board_token = token
    if not board_due(bstate, int(num(cfg, "ORCHESTRATE_BOARD_EVERY"))):
        bstate["ticks"] = bstate.get("ticks", 0) + 1
    else:
        bstate.update(ticks=0, dirty=False, last_sync=iso(time.time()))
        try:
            fields, items, remaining = board_read(gh, cfg["ORCHESTRATE_BOARD_PROJECT"])
            if remaining is not None and remaining < num(cfg, "ORCHESTRATE_RATE_FLOOR"):
                log("board_skipped", reason="GraphQL rate limit below the floor", remaining=remaining)
            else:
                open_items = {k: it for k, it in snapshot["items"].items() if it["state"] == "open"}
                muts, deferred = board_plan(open_items, items, int(num(cfg, "ORCHESTRATE_BOARD_WRITES")),
                                            cfg["ORCHESTRATE_SCOPE"])
                for m in muts:
                    log("board_plan", op=m["op"], target=m["target"], field=m.get("field"), value=m.get("value"),
                        was=m.get("was"))
                write = mode == "apply" and cfg["ORCHESTRATE_BOARD"] == "apply"
                if write and muts and os.path.exists(paths.stop):
                    log("board_skipped", reason="STOP file present")
                elif write and muts:
                    n = board_write(gh, cfg["ORCHESTRATE_BOARD_PROJECT"], fields, muts, gh.sleep)
                    log("board_synced", writes=n, deferred=deferred, rate_remaining=remaining)
                else:
                    log("board_synced", writes=0, planned=len(muts), deferred=deferred, rate_remaining=remaining,
                        dry_run=not write)
        except Exception as e:  # noqa: BLE001 - a board outage must never fail the tick or stop dispatch
            bstate["errors"] = bstate.get("errors", 0) + 1
            log("board_sync_error", error=scrub(str(e).replace(token, "[redacted]")))
    if label_change:
        bstate["dirty"] = True
    write_json(paths.board_file, bstate)


# ---------------------------------------------------------------- commands

def run_id_now(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()


def tick(paths, cfg, args, gh=None, now=None):
    now = now or time.time()
    run_id = run_id_now(now)
    mode = cfg["ORCHESTRATE_MODE"] if not args.fixture else "dry-run"
    log = Log(paths, cfg, run_id, mode, echo=args.verbose)
    ensure_dir(paths.state)
    lock_fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("skipped", reason="another tick holds run.lock")
        return 0
    try:
        log.rotate()
        if os.path.exists(paths.stop):
            log("stopped", reason="STOP file present")
            return 0
        exclusions = load_exclusions(os.path.join(HERE, "exclusions.tsv"))
        state = read_json(paths.state_file, {})
        health = {"last_tick": iso(now), "run_id": run_id, "mode": mode}
        if args.fixture:
            snapshot = read_json(args.fixture, None)
            snapshot["run_id"] = run_id
        else:
            gh = gh or Gh(paths.cache, status_issue=cfg["ORCHESTRATE_STATUS_ISSUE"])
            log("tick_start")
            try:
                rate, _ = gh.get("rate_limit")
                remaining = rate["resources"]["core"]["remaining"]
                health["rate_remaining"] = remaining
                if remaining < num(cfg, "ORCHESTRATE_RATE_FLOOR"):
                    log("rate_low", remaining=remaining)
                    health["result"] = "rate_low"
                    write_json(paths.health, health)
                    return 0
                repos = load_repos(args.repos, cfg["ORCHESTRATE_EXTRA_REPOS"])
                history = read_json(history_path(paths), {})
                snapshot = collect(gh, repos, now, cfg, run_id, state, history)
                write_json(history_path(paths), history)
                for ig in snapshot.get("ignored_comments", []):
                    log("comment_ignored", **ig)  # never the body
            except (GhError, PermissionError, KeyError, ValueError) as e:
                status = getattr(e, "status", 0)
                event = "auth_failed" if status in (401, 403) else "collect_failed"
                log(event, error=str(e), http_status=status)
                prev_errors = read_json(paths.health, {}).get("recent_errors", [])
                health.update(result=event, last_error=scrub(e), recent_errors=(prev_errors + [scrub(e)])[-3:])
                write_json(paths.health, health)
                gh.save_cache()
                return FAIL_EXIT
        if getattr(args, "dump_snapshot", None):
            write_json(args.dump_snapshot, snapshot)
        result = decide(snapshot, state, now, cfg, exclusions)
        for n in result["notes"]:
            log("note", **n)
        for a in result["actions"]:
            log("planned", action=a["type"], target=a["target"], reason=a["reason"])
        if args.print_plan:
            print(json.dumps({"run_id": run_id, "mode": mode, "pending_count": result["pending_count"],
                              "actions": result["actions"], "notes": result["notes"]}, indent=1, sort_keys=True))
        if mode == "apply":
            new_state, errors = apply(result, snapshot, gh, cfg, paths, log, state, now)
            write_json(paths.state_file, new_state)
        # The status issue body: written in apply mode when its content changed, else only kept locally.
        # A streak's failure section (written by --notify-failure) stays while this tick fails too.
        failure = read_json(paths.failure_file, {}).get("section", "") if mode == "apply" and errors else ""
        body = status_body(snapshot, result, cfg, new_state if mode == "apply" else result["state"], mode, now, failure)
        with open(os.path.join(paths.state, "status-body.md"), "w", encoding="utf-8") as f:
            f.write(body)
        scope = cfg["ORCHESTRATE_SCOPE"]
        if mode == "apply" and gh is not None and (not scope or scope == status_target(cfg)):
            errors += publish_status(gh, cfg, snapshot, body, log)
        elif mode == "apply":
            log("skipped", action="status_body", target=status_target(cfg), reason="ORCHESTRATE_SCOPE is set")
        if gh is not None:
            # Over-counting is harmless: a planned label action only brings the next sync forward.
            label_change = mode == "apply" and any(
                a["type"] in ("add_label", "remove_label", "swap_label")
                and {a.get("label"), a.get("add"), a.get("remove")} & set(BOARD_LABELS) for a in result["actions"])
            board_sync(gh, snapshot, cfg, paths, log, mode, label_change)
        if mode == "apply":
            if errors:
                gh.save_cache()
                prev_errors = read_json(paths.health, {}).get("recent_errors", [])
                msg = f"{errors} action(s) failed in apply; see apply_failed in the log"
                health.update(result="apply_failed", last_error=msg, recent_errors=(prev_errors + [msg])[-3:],
                              gh=gh.stats, actions=len(result["actions"]), pending=result["pending_count"])
                write_json(paths.health, health)
                log("tick_end", actions=len(result["actions"]), errors=errors, pending=result["pending_count"],
                    gh=gh.stats, gh_get_by_class=gh.by_class)
                return FAIL_EXIT
        if gh is not None:
            gh.save_cache()
            health["gh"] = gh.stats
        health.update(result="ok", actions=len(result["actions"]), pending=result["pending_count"])
        write_json(paths.health, health)
        if os.path.exists(paths.failure_file):
            os.remove(paths.failure_file)  # a good tick ends the failure streak
        log("tick_end", actions=len(result["actions"]), pending=result["pending_count"],
            gh=gh.stats if gh else {}, gh_get_by_class=gh.by_class if gh else {})
        return 0
    finally:
        os.close(lock_fd)


def failure_body(host, failures, threshold, streak_start, exit_code, errors, mode, log_path, key):
    """The '## Failed ticks' section of the status issue body for one streak."""
    lines = ["## Failed ticks", "", FAILURE_MARKER,
             f"vws-orchestrate: {failures} failed ticks in a row on host `{host}` since {iso(streak_start)} "
             f"(threshold {threshold}).", "",
             f"Last exit code: {exit_code}", f"Mode: {mode}", f"Log: `{log_path}`", "", "Last errors (redacted):"]
    lines += [f"- {scrub(e).replace(chr(10), ' ')}" for e in errors[-3:]] or ["- none recorded"]
    lines += ["", f"Idempotency key: `{key}`"]
    return "\n".join(lines)


def notify_failure(paths, cfg, args, gh=None, now=None):
    """Called by the loop after each failed tick. At the threshold it writes one '## Failed ticks' section
    per streak into the status issue body (no comment; vws-deploy#416). It bypasses the per-tick write cap
    and the rate floor (one read, one edit), so it still goes out when every tick fails; if GitHub itself
    is down the edit fails and the next failed tick tries again. The next good tick drops the section."""
    now = now or time.time()
    log = Log(paths, cfg, run_id_now(now), cfg["ORCHESTRATE_MODE"])
    threshold = int(num(cfg, "ORCHESTRATE_FAILURE_THRESHOLD"))
    health = read_json(paths.health, {})
    errors = list(health.get("recent_errors", []))
    if args.last_error:
        errors.append(args.last_error)
    if args.failures < threshold:
        log("failure_counted", failures=args.failures, threshold=threshold, exit_code=args.exit_code)
        return 0
    alert = cfg["ORCHESTRATE_FAILURE_ALERT"]
    if alert == "never" or (alert == "apply" and cfg["ORCHESTRATE_MODE"] != "apply"):
        log("failure_alert_skipped", reason=f"ORCHESTRATE_FAILURE_ALERT={alert}, mode {cfg['ORCHESTRATE_MODE']}",
            failures=args.failures)
        return 0
    scope = cfg["ORCHESTRATE_SCOPE"]
    if scope and scope != status_target(cfg):
        # A scoped trial limits every write to one target; the failure comment is no exception.
        log("failure_alert_skipped", reason=f"ORCHESTRATE_SCOPE is set to {scope}", failures=args.failures)
        return 0
    host = socket.gethostname()
    key = "sha256:" + hashlib.sha256(f"failure:{host}:{args.streak_start}".encode()).hexdigest()
    prev = read_json(paths.failure_file, {})
    if prev.get("key") == key:
        log("failure_alert_skipped", reason="already posted for this streak", failures=args.failures)
        return 0
    if now - prev.get("at", 0) < num(cfg, "ORCHESTRATE_FAILURE_MIN_GAP_HOURS") * 3600:
        log("failure_alert_skipped", reason="minimum gap between failure sections", failures=args.failures)
        return 0
    repo, number = status_target(cfg).split("#")
    gh = gh or Gh(status_issue=cfg["ORCHESTRATE_STATUS_ISSUE"])
    section = failure_body(host, args.failures, threshold, args.streak_start, args.exit_code, errors,
                           cfg["ORCHESTRATE_MODE"], paths.log, key)
    try:
        issue, _ = gh.get(f"repos/{OWNER}/{repo}/issues/{number}")
        current = (issue.get("body") or "") if isinstance(issue, dict) else ""
        if f"Idempotency key: `{key}`" in current:
            # An edit that timed out may have landed; never write the same streak twice.
            write_json(paths.failure_file, {"key": key, "at": now, "section": section})
            log("failure_alert_skipped", reason="the status issue already has this key", failures=args.failures)
            return 0
        new_body = set_failure_section(current, section)
        if new_body is None:
            log("failure_alert_failed", error=f"status_body_unowned: {status_unowned(current)}", failures=args.failures)
            return 0
        gh.write("PATCH", f"repos/{OWNER}/{repo}/issues/{number}", {"body": new_body})
    except (GhError, PermissionError) as e:
        log("failure_alert_failed", error=str(e), failures=args.failures)
        return 0
    write_json(paths.failure_file, {"key": key, "at": now, "section": section})
    log("failure_alerted", target=status_target(cfg), failures=args.failures, key=key)
    return 0


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--once", action="store_true", help="run one tick (the default action)")
    p.add_argument("--fixture", help="decide on a saved snapshot JSON; no network, never writes")
    p.add_argument("--repos", default=os.path.join(HERE, "..", "drift", "repos.tsv"))
    p.add_argument("--print-plan", action="store_true", help="print the planned actions as JSON")
    p.add_argument("--dump-snapshot", help="write the collected snapshot JSON here (for fixtures and debugging)")
    p.add_argument("--verbose", action="store_true", help="echo log lines to stdout")
    p.add_argument("--notify-failure", action="store_true")
    p.add_argument("--failures", type=int, default=0)
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--streak-start", type=float, default=0)
    p.add_argument("--last-error", default="", help="last loop.log line of the failed tick (scrubbed)")
    args = p.parse_args(argv)
    paths = Paths(environ)
    try:
        cfg = load_config(paths, environ)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.notify_failure:
        return notify_failure(paths, cfg, args)
    return tick(paths, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
