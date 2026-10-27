#!/usr/bin/env python3
"""Deterministic factory orchestrator for the vws-* repos (vws-deploy#333). Python 3 stdlib only.

One tick: collect (gh API reads with an on-disk ETag cache) -> decide (pure) -> apply (writes only
when ORCHESTRATE_MODE=apply). It never calls an LLM. It runs on the shared box under
box/orchestrate-loop.sh with the box's gh login; GH_TOKEN is always removed from the gh environment.
The one exception is board_sync: its GraphQL calls run with GH_TOKEN set to the project-scope classic
PAT read from ORCHESTRATE_BOARD_TOKEN_FILE (no file: board_sync is skipped for that tick).

Rules (one module each in rules/, merged by decide, vws-deploy#501; see docs/factory-orchestrator.md):
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
     while it stays open with grok, except an ac-partial issue with boxes still open and no open PR
     (vws-deploy#545): one wake per ticked-box set, and the assignee may keep the issue
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
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# vws-deploy#439 AC2 class 1: markers, comment authors, fresh PR reads and trusted PR bodies come from
# trusted_read.py next to this file; .github/drift/trusted_read_lint.py fails on a marker match here.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import trusted_read as tr  # noqa: E402
from trusted_read import (  # noqa: E402,F401  (re-exported: tests and .github/metrics read them as orch.X)
    CI_FAIL_RE, FAILURE_MARKER, GROK_ASSIGN_MARKER, GROK_WAKE_MARKER, KEY_RE, LEAD_WAKE_MARKER,
    METRICS_ALERT_MARKER, OPT_OUT_MARKER, OWNER, REBUILD_WAKE_MARKER, REWORK_MARKER, STATUS_END_MARKER,
    STATUS_MARKER, WAKE_MARKER, ac_tick_marker, is_trusted_reset_author, key_times,
    rework_kind, status_unowned,
)


# vws-deploy#501: every rule is a module in rules/ next to this file, loaded as the package
# orchestrate_rules (not "rules": tests/orchestrate/rules is a package too). A cached package from another
# checkout is dropped first, so two copies of this script never share rules.
def _load_rules():
    init = os.path.join(HERE, "rules", "__init__.py")
    cached = sys.modules.get("orchestrate_rules")
    if cached is not None and os.path.abspath(getattr(cached, "__file__", "")) == init:
        return cached
    for name in [m for m in sys.modules if m == "orchestrate_rules" or m.startswith("orchestrate_rules.")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location("orchestrate_rules", init,
                                                  submodule_search_locations=[os.path.dirname(init)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrate_rules"] = mod
    spec.loader.exec_module(mod)
    return mod


rules = _load_rules()
from orchestrate_rules.common import (  # noqa: E402
    ACTIONS_APP_ID, AC_ISSUE_BOX_RE, AC_TICK_LABELS, DEFAULTS, EXCLUSIONS_KEY, MANAGED_LABELS, MERGEABLE_OK,
    NEEDS_VERIFY, OPT_OUT_LABELS, PART_OF_HINT, PART_OF_RE, STATUS_EVENT_KEY_RE, STATUS_REPO, URL_RE, VERIFY_CHECK,
    ac_boxes, ac_own_ticks, ac_recoverable, ac_tick_body, classify_comment, grok_allowlist, grok_busy, grok_held,
    grok_link, grok_owners, head_check_reasons, iso, num, parse_ac, parse_deps, rework_prs, run_state, scrub,
    status_target, utc, vws_pr_key, wake_target,
)
# Compat: orch.X keeps naming every helper and constant that moved into rules/ (tests, .github/metrics and
# .github/verify read them through this file). Definitions below this line win.
globals().update(rules.exports())


FAIL_EXIT = 4


# These checks fail by design until vws-verify approves, so they never make a PR "CI failing"
# or "CI red". One set for ci_verdict and pr_ci (vws-deploy#502).
# "gate / verify-gate": repo-guard.yml calls the gate next to its bare shim (vws-deploy#402).
# "needs-verify": verify-queue's own job, which verify-queue.yml also skips.
VERIFY_CHECKS = {"verify-gate", "gate / verify-gate", "vws-verify", "needs-verify"}
CI_BAD_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
STATUS_UPDATED_RE = re.compile(r"^_Updated [^\n]*_$", re.M)  # the one volatile line; ignored when comparing
STATUS_EVENTS_KEEP = 20
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
MERGE_PAYLOAD_KEYS = {"merge_method", "sha", "commit_title"}


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
        self.deploy_file = os.path.join(self.state, "deploy.json")  # box/orchestrate-update.py (vws-deploy#490)
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
    grok_logins = [x.strip() for x in cfg["ORCHESTRATE_GROK_LOGINS"].split(",")]
    if not all(LOGIN_RE.fullmatch(x) for x in grok_logins):
        raise ValueError("ORCHESTRATE_GROK_LOGINS must be a comma-separated list of GitHub logins, "
                         f"got {cfg['ORCHESTRATE_GROK_LOGINS']!r}")
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
    if cfg["ORCHESTRATE_WAKES"] not in ("on", "off"):
        raise ValueError("ORCHESTRATE_WAKES must be on or off")
    if cfg["ORCHESTRATE_VERIFY_LABELS"] not in ("on", "off"):
        raise ValueError("ORCHESTRATE_VERIFY_LABELS must be on or off")
    if cfg["ORCHESTRATE_MERGE"] not in ("off", "dry-run", "apply"):
        raise ValueError("ORCHESTRATE_MERGE must be off, dry-run or apply")
    if cfg["ORCHESTRATE_AC_TICK"] not in ("off", "dry-run", "apply"):
        raise ValueError("ORCHESTRATE_AC_TICK must be off, dry-run or apply")
    if cfg["ORCHESTRATE_BOARD"] not in ("off", "dry-run", "apply"):
        raise ValueError("ORCHESTRATE_BOARD must be off, dry-run or apply")
    if not re.fullmatch(r"PVT_[A-Za-z0-9_-]+", cfg["ORCHESTRATE_BOARD_PROJECT"]):
        raise ValueError("ORCHESTRATE_BOARD_PROJECT must be a ProjectV2 node id (PVT_...)")
    for k in ("ORCHESTRATE_BOARD_EVERY", "ORCHESTRATE_BOARD_WRITES", "ORCHESTRATE_GROK_CAP",
              "ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN"):
        if not re.fullmatch(r"[1-9][0-9]*", cfg[k]):
            raise ValueError(f"{k} must be a positive integer, got {cfg[k]!r}")
    grok_owners(cfg)  # raises ValueError on a bad ORCHESTRATE_GROK_OWNERS entry
    tf = cfg["ORCHESTRATE_BOARD_TOKEN_FILE"]
    if not os.path.isabs(tf) or re.search(r"[\x00-\x1f]", tf):
        raise ValueError(f"ORCHESTRATE_BOARD_TOKEN_FILE must be an absolute path, got {tf!r}")
    text_keys = ("ORCHESTRATE_MODE", "ORCHESTRATE_SCOPE", "ORCHESTRATE_EXTRA_REPOS", "ORCHESTRATE_STATUS_ISSUE",
                 "ORCHESTRATE_WAKE_THREAD",
                 "ORCHESTRATE_FAILURE_ALERT", "ORCHESTRATE_DISPATCH", "ORCHESTRATE_WAKES", "ORCHESTRATE_VERIFY_LABELS", "ORCHESTRATE_MERGE", "ORCHESTRATE_AC_TICK", "ORCHESTRATE_BOARD", "ORCHESTRATE_BOARD_PROJECT", "ORCHESTRATE_GROK_ALLOWLIST",
                 "ORCHESTRATE_GROK_OWNERS", "ORCHESTRATE_BOARD_TOKEN_FILE",
                 "ORCHESTRATE_DEEPSEEK_LOGINS", "ORCHESTRATE_GROK_LOGINS", "ORCHESTRATE_VERIFY_LOGIN",
                 "ORCHESTRATE_VERIFY_USER_ID")
    for k in DEFAULTS:
        if k not in text_keys:
            float(cfg[k])  # raises ValueError on a bad number
    # Other keys in the shared env file (METRICS_* for .github/metrics) belong to other scripts,
    # which validate them; they must not stop a tick.
    return cfg


# vws-deploy#521: every comment the orchestrator posts on the wake thread (ORCHESTRATE_WAKE_THREAD).
WAKE_TYPES = ("wake_verify", "lead_wake", "grok_wake", "grok_rework", "rebuild_wake")


def wake_silence(cfg):
    """vws-deploy#521: why the status bus is silent, or "" when wakes may post. STOP (ORCHESTRATE_WAKES=off)
    and the dispatch pause (ORCHESTRATE_DISPATCH=off) both silence every wake and the metrics alert, even
    when work is queued: bots ignoring a wake still burn a wake-up each, so nothing may be posted."""
    if cfg.get("ORCHESTRATE_WAKES", "on") == "off":
        return "ORCHESTRATE_WAKES=off"
    if cfg.get("ORCHESTRATE_DISPATCH", "on") == "off":
        return "ORCHESTRATE_DISPATCH=off"
    return ""


def suppress_wakes(actions, cfg, notes):
    """vws-deploy#521: under wake_silence, drop every WAKE_TYPES action at the plan boundary and record what
    would have posted (note wake_suppressed: reason, total, counts per type, items). Pure."""
    why = wake_silence(cfg)
    if not why:
        return actions
    held = [a for a in actions if a["type"] in WAKE_TYPES]
    if held:
        counts = {t: sum(1 for a in held if a["type"] == t) for t in WAKE_TYPES}
        notes.append({"kind": "wake_suppressed", "reason": why, "total": len(held),
                      "counts": {t: n for t, n in counts.items() if n},
                      "items": sorted({a.get("item", a["target"]) for a in held})})
    return [a for a in actions if a["type"] not in WAKE_TYPES]


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
        re.compile(rf"^{_REPO}/issues/[0-9]+/(events|comments|timeline)(\?.*)?$"),  # timeline: Rule G claims (#411)
        re.compile(rf"^{_REPO}/commits/[0-9a-f]{{40}}/(check-runs|status)(\?.*)?$"),
        re.compile(rf"^{_REPO}/branches/main$"),
        re.compile(rf"^{_REPO}/compare/[0-9a-f]{{40}}\.\.\.[0-9a-f]{{40}}\?per_page=1$"),
        re.compile(rf"^{_REPO}/rules/branches/main(\?.*)?$"),
        re.compile(rf"^{_REPO}/actions/runs\?event=push&branch=main&head_sha=[0-9a-f]{{40}}&per_page=100$"),
        re.compile(rf"^{_REPO}/actions/runs\?head_sha=[0-9a-f]{{40}}&per_page=100$"),
        re.compile(r"^rate_limit$"),
    ],
    "POST": [re.compile(rf"^{_REPO}/issues/[0-9]+/(comments|labels)$")],
    "DELETE": [re.compile(rf"^{_REPO}/issues/[0-9]+/labels/(blocked|in-review|deepseek|needs-verify|verify-approved|ac-partial|ac-pending)$")],
    # Rule D only: the REST squash merge pinned to a head sha. update-branch is never allowed.
    "PUT": [re.compile(rf"^{_REPO}/pulls/[0-9]+/merge$")],
    # The status issue body (vws-deploy#416), an AC tick, a repo-guard retry line (#446) or a completed close (#486); Gh.write pins the payload.
    "PATCH": [re.compile(rf"^{_REPO}/issues/[0-9]+$")],
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
    if re.match(r"^/issues/[0-9]+/(events|timeline)$", rest):
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

    def write(self, method, path, payload=None, before=None):
        if not self.allow(method, path):
            raise PermissionError(f"gh call not allowed: {method} {path}")
        if method == "PUT":
            # The only PUT is a squash merge pinned to the verified head (GitHub answers 409 if it moved).
            p = payload or {}
            if (set(p) - MERGE_PAYLOAD_KEYS or p.get("merge_method") != "squash"
                    or not re.fullmatch(r"[0-9a-f]{40}", str(p.get("sha", "")))):
                raise PermissionError("merge refused: squash with a 40-hex head sha only")
        if method == "PATCH" and re.fullmatch(rf"repos/{OWNER}/[^/]+/issues/[0-9]+", path):
            # Four issue edits only: the status issue's body with exactly one managed status block; an AC tick
            # (vws-deploy#486) that changes nothing but unticked criteria boxes of `before`, the body just read;
            # one appended repo-guard retry line (vws-deploy#446); and a completed close of any other issue.
            p = payload if isinstance(payload, dict) else {}
            body = p.get("body") if set(p) == {"body"} else None
            is_status = bool(self.status_issue) and path == f"repos/{OWNER}/{STATUS_REPO}/issues/{self.status_issue}"
            status_edit = is_status and isinstance(body, str) and not status_unowned(body)
            tick = (not is_status and isinstance(body, str) and isinstance(before, str) and ac_tick_only(before, body))
            # vws-deploy#446: append one repo-guard retry line. Nothing else in the body may change.
            retry = (not is_status and isinstance(body, str) and isinstance(before, str) and tr.guard_body_edit(before, body))
            close = not is_status and p == {"state": "closed", "state_reason": "completed"}
            if not (status_edit or tick or retry or close):
                raise PermissionError(
                    "issue edit refused: the status issue body, an AC tick, a repo-guard retry line "
                    "or a completed close only")
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
        """Projects v2 (board_sync), plus the AC tick's read-only AC_EDIT_QUERY. A write must be a batch of the three item mutations, built from
        the BOARD_* templates with every value in variables; anything else is refused before the call."""
        if write:
            if not board_write_ok(query):
                raise PermissionError("graphql mutation not allowed")
        elif query == AC_EDIT_QUERY:
            # vws-deploy#486: one PR's body edit time, read on the box login like the REST reads.
            status, _headers, body = self._run(["gh", "api", "-i", "graphql", "--input", "-"],
                                               json.dumps({"query": query, "variables": variables}))
            self.stats["get"] += 1
            self._count_get("graphql", status)
            out = json.loads(body) if body.strip() else {}
            if status != 200 or not isinstance(out, dict) or out.get("errors") or "data" not in out:
                raise GhError(status, scrub(json.dumps(out.get("errors") if isinstance(out, dict) else body[:200])[:300]))
            return out["data"]
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


def status_wakes(comments):
    """The orchestrator's own verify-wake comments on the verify-wake thread: [{"key", "at"}]. Anyone can
    comment there; a copied wake from another login is not a wake (vws-deploy#439 class 1)."""
    return tr.wake_comments(comments, utc)


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


AC_EVIDENCE_RE = re.compile(rf" \(evidence: https://github\.com/{OWNER}/vws-[a-z0-9-]+/pull/[0-9]+ merged [0-9a-f]{{40}}\)")


def ac_tick_only(old, new):
    """True when new differs from old only by unticked criteria boxes turned ticked with an evidence suffix
    (Gh.write refuses any other issue body edit but the status issue's)."""
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    a, b = old.split("\n"), new.split("\n")
    if len(a) != len(b) or a == b:
        return False
    open_lines = {x["line"] for x in ac_boxes(old) if not x["checked"]}
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        cr = "\r" if x.endswith("\r") else ""
        if i not in open_lines or not y.endswith(cr):
            return False
        mx, my = AC_ISSUE_BOX_RE.match(x[:len(x) - len(cr)]), AC_ISSUE_BOX_RE.match(y[:len(y) - len(cr)])
        if not my or my.group(2) != "x" or (mx.group(1), mx.group(3)) != (my.group(1), my.group(3)):
            return False
        rest = my.group(4)
        ev = AC_EVIDENCE_RE.search(rest)
        if not ev or ev.end() != len(rest) or rest[:ev.start()] != mx.group(4).rstrip():
            return False
    return True


def ac_tick_comment(a, ticked, still_open, close):
    lines = [f"AC tick for {a['pr_url']} (merged {a['merge_sha']}): boxes {', '.join(map(str, a['boxes']))} "
             f"{'ticked' if ticked else 'were already ticked'}."]
    if close:
        lines.append("\nEvery acceptance criteria box is ticked, so this issue is closed as completed.")
    else:
        lines.append("\nStill open:")
        lines += [f"- {b['n']}: {b['text']}" for b in still_open]
    lines.append(f"\n{ac_tick_marker(a['pr'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- decide: the thin rule dispatcher (vws-deploy#501)
# Each rule is a module in rules/ with a pure decide(snapshot, state, cfg, now) -> Decision (rules/common.py
# decision). rules/__init__.py REGISTRY fixes the order; docs/factory-orchestrator.md has the recipe.


def action_order(registry):
    """The registry with each rule that sets ACTIONS_AFTER moved right after the rule it names."""
    order = [r for r in registry if not getattr(r, "ACTIONS_AFTER", "")]
    for r in registry:
        after = getattr(r, "ACTIONS_AFTER", "")
        if not after:
            continue
        names = [x.NAME for x in order]
        if after not in names:
            raise ValueError(f"rule {r.NAME}: ACTIONS_AFTER names {after!r}, not a registered rule")
        order.insert(names.index(after) + 1, r)
    return order


def action_key(a):
    """Two actions with the same type, target and label (or key) are one write; the first is kept. ac_tick
    also keys on pr: two merged PRs ticking boxes of one issue are two writes (vws-deploy#567)."""
    if a["type"] == "ac_tick":
        return (a["type"], a["target"], a.get("pr"))
    return (a["type"], a["target"], a.get("label", a.get("key")))


def merge_decisions(registry, decisions, state):
    """Merge {rule NAME: Decision} the same way whatever order the rules ran in. Notes, state updates and
    extras merge in registry order; actions in action_order, a stable sort on that position, deduped by
    action_key. A state key outside the rule's STATE_KEYS, or a state or extra key two rules set, raises."""
    names = [r.NAME for r in registry]
    if len(set(names)) != len(names) or sorted(names) != sorted(decisions):
        raise ValueError(f"decisions {sorted(decisions)} do not match the registry {names}")
    rank = {r.NAME: i for i, r in enumerate(action_order(registry))}
    tagged = sorted(((rank[n], i, a) for n in names for i, a in enumerate(decisions[n]["actions"])),
                    key=lambda t: t[:2])
    actions, seen = [], set()
    for _, _, a in tagged:
        if action_key(a) not in seen:
            seen.add(action_key(a))
            actions.append(a)
    new_state = json.loads(json.dumps(state))
    out = {"actions": actions, "notes": [n for name in names for n in decisions[name]["notes"]], "state": new_state}
    owner = {}
    for r in registry:
        d = decisions[r.NAME]
        for k, v in d["state"].items():
            if k not in r.STATE_KEYS:
                raise ValueError(f"rule {r.NAME} set state key {k!r}, not in its STATE_KEYS")
            if k in owner:
                raise ValueError(f"state key {k!r} set by rules {owner[k]} and {r.NAME}")
            owner[k] = r.NAME
            new_state[k] = v
        for k, v in d["extra"].items():
            if k in out:
                raise ValueError(f"rule {r.NAME} set result key {k!r}, already set")
            out[k] = v
    return out


def decide(snapshot, state, now, cfg, exclusions, call_order=None):
    """Pure: the same inputs give the same output. Returns {"actions", "notes", "state", "pending_count",
    "rebuild"}. Every rule in rules.RULES decides on its own copy of state; call_order (tests) changes only
    the order the rules are called in, never the result."""
    rule_cfg = dict(cfg)
    rule_cfg[EXCLUSIONS_KEY] = exclusions
    decisions = {}
    for r in call_order or rules.RULES:
        decisions[r.NAME] = r.decide(snapshot, json.loads(json.dumps(state)), rule_cfg, now)
    result = merge_decisions(rules.RULES, decisions, state)
    # vws-deploy#521: STOP / DISPATCH=off is full wake-thread silence, even with work queued.
    result["actions"] = suppress_wakes(result["actions"], cfg, result["notes"])
    return result


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
    states = {run_state(r) for name, r in latest.items() if name not in VERIFY_CHECKS}
    for st in (status if isinstance(status, dict) else {}).get("statuses", []):
        if st.get("context") in VERIFY_CHECKS:
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
        # Own comments only (vws-deploy#439 class 1): a copied key would suppress or backdate a wake.
        snap["status"] = {"wakes": status_wakes(wake_comments), "keys": tr.keys(wake_comments),
                          "key_times": key_times(wake_comments)}
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
    if cfg.get("ORCHESTRATE_AC_TICK", "apply") != "off":
        collect_ac(gh, snap, repo_names, state, now, cfg)
    allow = grok_allowlist(cfg.get("ORCHESTRATE_GROK_ALLOWLIST", ""))[0]
    for k, it in sorted(snap["items"].items()):
        if k in allow and it["state"] == "open" and not it["is_pr"] and "grok" in it["labels"]:
            it["grok_activity"] = grok_activity(gh, k, it, cfg)
    return snap


def collect_ac(gh, snap, repo_names, state, now, cfg):
    """Inputs of the AC tick rule (vws-deploy#486): each repo's newest closed PRs (one ETag-cached page), and
    for a PR merged into main since the rule started, not yet handled, with a `Part of` issue and `AC: <n>`
    lines, that issue and its comments. A read error marks that PR only, never the tick."""
    snap["ac_merged"] = []
    if not (state or {}).get("ac_tick_since"):
        return  # decide starts the rule on this tick; nothing merged before counts
    floor = max(utc(state["ac_tick_since"]), now - int(num(cfg, "ORCHESTRATE_COMMENT_WINDOW_DAYS")) * 86400)
    done = state.get("ac_ticked", {})
    live = {r["name"] for r in snap["repos"] if not r.get("archived")}
    for name in repo_names:
        if name not in live:
            continue
        try:
            pulls, _ = gh.get(f"repos/{OWNER}/{name}/pulls?state=closed&sort=updated&direction=desc&per_page=100")
        except (GhError, PermissionError) as e:
            snap["ac_merged"].append({"pr": f"{name}#pulls", "url": "", "merged_at": iso(now), "merge_sha": "",
                                      "body": "", "issue": "", "error": scrub(e)})
            continue
        for p in pulls or []:
            k = f"{name}#{p.get('number')}"
            if (not p.get("merged_at") or (p.get("base") or {}).get("ref") != "main" or k in done
                    or utc(p["merged_at"]) < floor):
                continue
            m = {"pr": k, "url": p.get("html_url") or "", "merged_at": p["merged_at"],
                 "merge_sha": p.get("merge_commit_sha") or "", "body": p.get("body") or "", "issue": "",
                 "head_sha": (p.get("head") or {}).get("sha") or "",
                 "issue_state": "", "issue_state_reason": "", "issue_body": "", "issue_labels": [], "marker_seen": False,
                 "verified_at": "", "body_edited_at": ""}
            # A startup retry appends one HTML comment after approval. When that is the whole edit, Part of
            # and the AC lines are the pre-image the hash names; any other edit still fails closed (#499).
            restored = guard_retry_restores(m, state)
            ref = PART_OF_RE.search(m["body"])
            if ref:
                irepo, inum = ref.group(1).split("#")
                m["issue"] = f"{irepo or name}#{inum}"
            snap["ac_merged"].append(m)
            if not m["issue"] or not parse_ac(m["body"])["boxes"]:
                continue  # decide notes it; nothing to read
            try:
                if not ac_body_trusted(gh, name, p, m, cfg):
                    if not (restored and m.get("verified_at")):
                        continue  # decide notes ac_unverified / ac_body_edited; the edited body's target is never read
                    m["body_edited_at"] = ""  # the body is the pre-image; lastEditedAt is the retry line only
            except (GhError, PermissionError, KeyError, TypeError, AttributeError, ValueError) as e:
                m["error"] = scrub(e)
                continue
            irepo, inum = m["issue"].split("#")
            if irepo not in live:
                m["error"] = f"{irepo} is not a watched vws repo"
                continue
            try:
                issue, _ = gh.get(f"repos/{OWNER}/{irepo}/issues/{inum}")
                comments = gh.get_all(f"repos/{OWNER}/{irepo}/issues/{inum}/comments?per_page=100")
                m.update(issue_state=issue.get("state") or "", issue_state_reason=issue.get("state_reason") or "",
                         issue_body=issue.get("body") or "",
                         issue_labels=sorted(lb["name"] for lb in issue.get("labels") or []),
                         marker_seen=tr.has_marker(comments, ac_tick_marker(k)))
                if "pull_request" in issue:
                    m["error"] = f"{m['issue']} is a pull request"
            except (GhError, PermissionError, KeyError, TypeError, AttributeError) as e:
                m["error"] = scrub(e)


def ac_body_trusted(gh, repo, p, m, cfg):
    """vws-deploy#486, review 5640903019: sets m["verified_at"] (the newest trusted vws-verify approval of the
    merged head) and m["body_edited_at"] (the body's last edit, GraphQL lastEditedAt). True only when the body
    was not edited at or after that approval, so `Part of` and the AC lines are the ones vws-verify saw; an
    edit after the verdict or after the merge cannot retarget the tick. Raises on a read error."""
    head = (p.get("head") or {}).get("sha") or ""
    if not head:
        raise ValueError("merged PR has no head sha")
    pr = {"repo": repo, "number": p["number"], "url": m["url"]}
    m["verified_at"] = max((r["at"] for r in verify_reviews(gh, pr, cfg) if r["result"] == "pass" and r["sha"] == head),
                           default="")
    if not m["verified_at"]:
        return False
    data = gh.graphql(AC_EDIT_QUERY, {"o": OWNER, "r": repo, "n": int(p["number"])})
    node = data["repository"]["pullRequest"]
    if node["headRefOid"] != head:
        raise ValueError(f"GraphQL head {str(node['headRefOid'])[:12]} is not the merged head {head[:12]}")
    m["body_edited_at"] = node.get("lastEditedAt") or ""
    return tr.verified_body(m, utc) is not None


def grok_activity(gh, k, it, cfg):
    """vws-deploy#411: what the issue's timeline shows of a claim, for grok_release. Only comments by a
    ORCHESTRATE_GROK_LOGINS login or a current assignee count, and not ones that start with an HTML-comment
    marker (the orchestrator's and bots' machine comments). Linked PRs are cross-references from PRs in an
    OWNER repo whose `Part of` names this issue. None when the timeline cannot be read."""
    logins = cfg.get("ORCHESTRATE_GROK_LOGINS", DEFAULTS["ORCHESTRATE_GROK_LOGINS"])
    claimers = {x.strip().lower() for x in logins.split(",")} | {a.lower() for a in it.get("assignees") or []}
    try:
        timeline = gh.get_all(f"repos/{OWNER}/{it['repo']}/issues/{it['number']}/timeline?per_page=100")
    except (GhError, PermissionError):
        return None
    act = {"comments": [], "assigned": [], "prs": []}
    for e in timeline if isinstance(timeline, list) else []:
        kind, at = e.get("event"), e.get("created_at") or ""
        if tr.is_claim(e, claimers):
            act["comments"].append(at)
        elif kind == "assigned" and at:
            act["assigned"].append(at)
        elif kind == "cross-referenced":
            src = (e.get("source") or {}).get("issue") or {}
            source_repo = src.get("repository") or {}
            repo = source_repo.get("name") or ""
            owner = ((source_repo.get("owner") or {}).get("login") or "").lower()
            if owner == OWNER.lower() and "pull_request" in src and repo and grok_link({"is_pr": True, "repo": repo, "body": src.get("body")}) == k:
                act["prs"].append({"key": f"{repo}#{src.get('number')}",
                                   "merged_at": (src.get("pull_request") or {}).get("merged_at") or ""})
    return act


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
    return {"head_sha": sha, "head_ref": (pr.get("head") or {}).get("ref") or "",
            "state": pr.get("state"), "merged": bool(pr.get("merged")), "draft": bool(pr.get("draft")),
            "base": pr["base"]["ref"], "mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"),
            "title": pr.get("title") or "", "checks": checks, "unmapped": unmapped,
            # None when the runs page is truncated: do not retry on a partial list (vws-deploy#446).
            "guard_runs": guard_runs_from(wruns or {}),
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

def _guard_record(new_state, key, run, now, body_sha=""):
    """Journal the attempt before the write. An existing body_sha is kept: it is the pre-image AC tick hashes."""
    recs = new_state.setdefault("guard_retry", {})
    prev = recs.get(key) if isinstance(recs.get(key), dict) else {}
    rec = dict(prev)
    rec["run"] = run
    rec.setdefault("at", iso(now))
    rec.setdefault("reported", False)
    if body_sha and not rec.get("body_sha"):
        rec["body_sha"] = body_sha
    recs[key] = rec
    return rec


def apply_guard(a, gh, writes, max_writes, new_state, paths, log, now):
    """One guard_retry (body touch) or guard_report (#412 comment). The attempt is journaled before the
    write, and a head that moved writes nothing. Returns (writes made, errors). One GitHub write."""
    if writes >= max_writes:
        log("skipped", action=a["type"], target=a["target"], reason=f"write cap {max_writes} reached")
        return 0, 0
    repo, number = a["target"].split("#")
    key = f"{a['target']}@{a['head']}"
    try:
        pr, why = tr.pr_at_head(gh.get, repo, int(number), a["head"])
    except (GhError, PermissionError, ValueError) as e:
        log("apply_failed", action=a["type"], target=a["target"], error=scrub(e))
        return 0, 1
    if why:
        log("skipped", action=a["type"], target=a["target"], reason=f"PR changed since collect: {why}")
        return 0, 0
    method, path, payload, before = "POST", f"repos/{OWNER}/{repo}/issues/{number}/comments", {"body": a.get("body") or ""}, None
    if a["type"] == "guard_report":
        try:
            comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
        except (GhError, PermissionError) as e:
            log("apply_failed", action=a["type"], target=a["target"], error=scrub(e))
            return 0, 1
        for c in comments if isinstance(comments, list) else []:
            if not isinstance(c, dict):
                continue
            try:
                rec = comment_record(c)
            except (KeyError, TypeError):
                continue  # a comment without the fields a record needs is not a marker
            parsed = ci_fail_record(rec["body"]) if ci_fail_author_ok(rec) else None
            if parsed and parsed.get("head_sha") == a["head"]:
                _guard_record(new_state, key, a["run"], now)
                new_state["guard_retry"][key]["reported"] = True
                write_json(paths.state_file, new_state)
                log("skipped", action="guard_report", target=a["target"], reason="marker already posted")
                return 0, 0
        _guard_record(new_state, key, a["run"], now)
        new_state["guard_retry"][key]["reported"] = True
    else:
        old = pr.get("body") or ""
        try:
            new = tr.guard_retry_append(old, a["head"], a["run"])
        except ValueError as e:
            log("apply_failed", action="guard_retry", target=a["target"], error=scrub(e))
            return 0, 1
        # Hash the pre-image only when this call is the one that appends the line. A later call keeps it.
        body_sha = "" if tr.guard_retry_line(a["head"], a["run"]) in old.split("\n") else hashlib.sha256(old.encode()).hexdigest()
        _guard_record(new_state, key, a["run"], now, body_sha)
        if new == old:
            write_json(paths.state_file, new_state)
            log("skipped", action="guard_retry", target=a["target"], reason="retry line already on the body")
            return 0, 0
        method, path, payload, before = "PATCH", f"repos/{OWNER}/{repo}/issues/{number}", {"body": new}, old
    write_json(paths.state_file, new_state)  # before the write: a crash still remembers the attempt
    try:
        gh.write(method, path, payload, before=before)
    except (GhError, PermissionError) as e:
        log("apply_failed", action=a["type"], target=a["target"], error=scrub(e))
        return 1, 1  # counted: a lost response may have landed
    log("applied", action=a["type"], target=a["target"], reason=a["reason"])
    return 1, 0


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
        if a["type"] in WAKE_TYPES and wake_silence(cfg):
            # Defense in depth (vws-deploy#521): decide already drops these; a stale plan must not post either.
            log("skipped", action=a["type"], target=a["target"], reason=wake_silence(cfg))
            continue
        if a["type"] in ("wake_verify", "merge_status", "infra_status", "grok_wake", "rebuild_wake", "grok_rework", "lead_wake") and scope:
            log("skipped", action=a["type"], reason="ORCHESTRATE_SCOPE is set")
            continue
        if scope and a["target"] != scope:
            log("skipped", action=a["type"], target=a["target"], reason="outside ORCHESTRATE_SCOPE")
            continue
        if a["type"] == "ac_tick":
            if cfg.get("ORCHESTRATE_AC_TICK", "apply") != "apply":
                log("skipped", action=a["type"], target=a["target"],
                    reason=f"ORCHESTRATE_AC_TICK={cfg.get('ORCHESTRATE_AC_TICK', 'apply')}")
                continue
            n, failed = apply_ac_tick(a, gh, writes, max_writes, new_state, log)
            writes, errors = writes + n, errors + failed
            continue
        if a["type"] in ("guard_retry", "guard_report"):
            n, failed = apply_guard(a, gh, writes, max_writes, new_state, paths, log, now)
            writes, errors = writes + n, errors + failed
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
                               a.get("woken", ()), grok_posted, a.get("released", ()),
                               a.get("allow_assigned", False))
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
        if a["type"] in ("add_label", "remove_label") and a.get("head"):
            # vws-deploy#439 class 1: a PR label decided on one head is written only while the PR is still
            # open at that head (read again right before the write); the next tick decides the new head.
            try:
                why = tr.pr_at_head(gh.get, repo, number, a["head"])[1]
            except (GhError, PermissionError) as e:
                why = f"re-read failed: {scrub(e)}"
            if why:
                log("skipped", action=a["type"], target=a["target"], label=a["label"],
                    reason=f"PR changed since collect: {why}")
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


def apply_ac_tick(a, gh, writes, max_writes, new_state, log):
    """One ac_tick (vws-deploy#486). The marker and the issue are read again, and the edit is computed from
    the fresh body, so a concurrent edit is kept and a second tick for the same PR writes nothing. The marker
    comment goes last: a crash before it is finished by the next tick (ticking is idempotent).
    Returns (writes made, errors)."""
    repo, number = a["target"].split("#")
    base = f"repos/{OWNER}/{repo}/issues/{number}"
    done = new_state.setdefault("ac_ticked", {})
    try:
        comments = gh.get_all(f"{base}/comments?per_page=100")
        if tr.has_marker(comments, ac_tick_marker(a["pr"])):
            done[a["pr"]] = a["merged_at"]
            log("skipped", action="ac_tick", target=a["target"], pr=a["pr"], reason="marker already posted")
            return 0, 0
        issue, _ = gh.get(base)
    except (GhError, PermissionError) as e:
        log("apply_failed", action="ac_tick", target=a["target"], pr=a["pr"], error=str(e))
        return 0, 1
    if not isinstance(issue, dict):
        issue = {}
    body = issue.get("body") or ""
    recover = issue.get("state") == "closed" and a.get("recover") and ac_recoverable(
        "closed", issue.get("state_reason"), body, a["boxes"], a["pr_url"], a["merge_sha"])
    if issue.get("state") != "open" and not recover:
        log("skipped", action="ac_tick", target=a["target"], pr=a["pr"], reason="issue not open")
        return 0, 0
    p = ac_tick_body(body, a["boxes"], a["pr_url"], a["merge_sha"])
    if p["error"]:
        log("skipped", action="ac_tick", target=a["target"], pr=a["pr"], reason=p["error"])
        return 0, 0
    labels, close = {lb.get("name") for lb in issue.get("labels") or []}, not p["open"]
    # Text from the end state, not this run: a rerun after the body edit landed still says "ticked".
    ticked = p["ticked"] or ac_own_ticks(body, a["boxes"], a["pr_url"], a["merge_sha"])
    calls = []
    if p["body"] != body:
        calls.append(("PATCH", base, {"body": p["body"]}, body))
    if not close and a.get("partial_label") and "ac-partial" not in labels:
        calls.append(("POST", f"{base}/labels", {"labels": ["ac-partial"]}, None))
    for lb in AC_TICK_LABELS:
        if lb in labels and (close or lb == "ac-pending"):
            calls.append(("DELETE", f"{base}/labels/{lb}", None, None))
    if close and not recover:
        calls.append(("PATCH", base, {"state": "closed", "state_reason": "completed"}, None))
    calls.append(("POST", f"{base}/comments", {"body": ac_tick_comment(a, ticked, p["open"], close)}, None))
    if writes + len(calls) > max_writes:
        log("skipped", action="ac_tick", target=a["target"], pr=a["pr"], reason=f"write cap {max_writes} reached")
        return 0, 0
    n = 0
    for method, path, payload, before in calls:
        n += 1  # counted before the call: a failed write may still have landed
        try:
            if before is None:
                gh.write(method, path, payload)
            else:
                gh.write(method, path, payload, before=before)
        except GhError as e:
            if label_absent(method, path, e):
                continue
            log("apply_failed", action="ac_tick", target=a["target"], pr=a["pr"], error=str(e))
            return n, 1
        except PermissionError as e:
            log("apply_failed", action="ac_tick", target=a["target"], pr=a["pr"], error=str(e))
            return n, 1
    done[a["pr"]] = a["merged_at"]
    log("ac_ticked", target=a["target"], pr=a["pr"], ticked=p["ticked"], open=[b["n"] for b in p["open"]], closed=close,
        **({"recovered": True} if recover else {}))
    log("applied", action="ac_tick", target=a["target"], reason=a["reason"])
    return n, 0


def recheck_key(gh, repo, number, key):
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    return not tr.has_key(comments, key)


def recheck_rebuild(gh, k, head):
    """True while the PR is still open at the head the wake names (a new head gets its own wake)."""
    repo, number = k.split("#")
    try:
        return not tr.pr_at_head(gh.get, repo, number, head)[1]
    except (GhError, PermissionError):
        return False


def recheck_wake(gh, repo, number, key):
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    return key not in {w["key"] for w in status_wakes(comments)}


def add_status_event(state, a, now):
    """Journal one merge or infra entry for the status issue body; one per key, the newest kept."""
    events = [e for e in state.get("status_events", []) if e["key"] != a["key"]]
    kind = "merge" if a["type"] == "merge_status" else "infra"
    events.append({"key": a["key"], "at": now, "kind": kind, "text": scrub(a["reason"])})
    state["status_events"] = events[-STATUS_EVENTS_KEEP:]


# ---------------------------------------------------------------- auto-update (vws-deploy#490)
# box/orchestrate-update.py writes deploy.json before each tick: the sha this tick runs, origin/main as
# last fetched, the update result, and alerts (refused, fetch_failed, ff_failed, drift), each with a
# sha256 key that is stable for the same condition. Its values reach a public issue body, so every field
# is validated here; a bad file renders as unknown.
DEPLOY_RESULTS = ("updated", "current", "disabled", "refused", "fetch_failed", "ff_failed", "no_checkout", "error")
DEPLOY_ALERT_KINDS = ("refused", "fetch_failed", "ff_failed", "no_checkout", "drift")
SHA40_RE = re.compile(r"[0-9a-f]{40}")


def running_line(deploy):
    """The '- Running:' line of the status block: the running sha, origin/main and the last update result."""
    d = deploy if isinstance(deploy, dict) else {}
    running, main, result = d.get("running"), d.get("main"), d.get("result")
    if result not in DEPLOY_RESULTS:
        return "- Running: unknown (auto-update has not run)"
    if not (isinstance(running, str) and SHA40_RE.fullmatch(running)):
        return f"- Running: unknown (last update: {result})"
    shown = f"`{main[:7]}`" if isinstance(main, str) and SHA40_RE.fullmatch(main) else "unknown"
    return f"- Running: `{running[:7]}` (main {shown}, last update: {result})"


def add_deploy_alerts(state, deploy, now):
    """Journal each deploy alert as a status entry once per key (a key already listed is not touched, so
    a standing condition edits nothing). Returns the number added."""
    alerts = deploy.get("alerts") if isinstance(deploy, dict) else None
    events = list(state.get("status_events", []))
    known = {e["key"] for e in events}
    added = 0
    for a in alerts if isinstance(alerts, list) else []:
        if not (isinstance(a, dict) and a.get("kind") in DEPLOY_ALERT_KINDS and isinstance(a.get("text"), str)
                and isinstance(a.get("key"), str) and STATUS_EVENT_KEY_RE.fullmatch(f"key `{a['key']}`")):
            continue
        if a["key"] in known:
            continue
        known.add(a["key"])
        events.append({"key": a["key"], "at": now, "kind": "deploy", "text": scrub(a["text"].replace("\n", " "))[:300]})
        added += 1
    if added:
        state["status_events"] = events[-STATUS_EVENTS_KEEP:]
    return added


# ---------------------------------------------------------------- the status issue body (vws-deploy#416)

MERGE_SKIP_PRS, MERGE_SKIP_REASONS = 10, 5


def skip_reason_text(reason):
    """One Rule D reason as inline code. Check names come from workflow files a PR can edit, so a backtick
    or a line break cannot end the code span or the list item, and no angle bracket can spell an HTML
    comment such as the status block's own markers (status_unowned would refuse the whole block)."""
    text = re.sub(r"\s+", " ", str(reason)).replace("`", "'").replace("<", "\u2039").replace(">", "\u203a")
    return "`" + scrub(text)[:120] + "`"


def merge_skip_line(result):
    """The status line naming every verify-approved PR Rule D did not merge this tick, with its reasons
    (vws-deploy#505), so a CLEAN and approved PR that parks is visible outside the VM journal."""
    rows = [n for n in result.get("notes", []) if n.get("kind") == "merge_check" and not n.get("eligible")
            and "no_verify_approved" not in n.get("reasons", [])]
    parts = []
    for n in rows[:MERGE_SKIP_PRS]:
        rs = n.get("reasons", [])
        more = f" (+{len(rs) - MERGE_SKIP_REASONS} more)" if len(rs) > MERGE_SKIP_REASONS else ""
        parts.append(f"{n['target']} `{n.get('head') or '?'}` ("
                     + ", ".join(skip_reason_text(r) for r in rs[:MERGE_SKIP_REASONS]) + more + ")")
    if len(rows) > MERGE_SKIP_PRS:
        parts.append(f"+{len(rows) - MERGE_SKIP_PRS} more PRs")
    return f"- Verify-approved PRs not merged: {len(rows)}: " + "; ".join(parts) if rows else \
        "- Verify-approved PRs not merged: none"


def status_body(snapshot, result, cfg, state, mode, now, failure="", deploy=None):
    """Pure: the status issue body. Only the last line (_Updated ..._) changes from tick to tick without a
    real change, and status_same ignores it, so a quiet tick edits nothing."""
    notes = result.get("notes", [])
    in_flight = next((n["targets"] for n in notes if n.get("kind") == "in_flight"), [])
    # vws-deploy#532: the same holders decide_grok counts against ORCHESTRATE_GROK_CAP, always printed.
    grok_held_now = next((n["targets"] for n in notes if n.get("kind") == "grok_in_flight"), [])
    wake = wake_target(cfg)
    lines = [STATUS_MARKER,
             "Factory status. The orchestrator rewrites this block (down to the end marker) in place each tick; "
             "text outside it is yours and is kept. It does not post status comments.",
             "",
             f"Comments here are metrics alerts only (marker `{METRICS_ALERT_MARKER}`). verify-wake does not post here.",
             "", "## Now", "", running_line(deploy),
             f"- Mode: {mode}; dispatch {cfg['ORCHESTRATE_DISPATCH']}, "
             f"wakes {'off (' + wake_silence(cfg) + ')' if wake_silence(cfg) else 'on'}, merge {cfg['ORCHESTRATE_MERGE']}, "
             f"board {cfg['ORCHESTRATE_BOARD']}",
             f"- AC tick: {cfg['ORCHESTRATE_AC_TICK']}",
             f"- PRs waiting for verify: {result.get('pending_count', 0)} (wake threshold {int(num(cfg, 'VERIFY_WAKE_THRESHOLD'))}; "
             + (f"verify wakes go to {wake})" if wake else "verify wake is off: ORCHESTRATE_WAKE_THREAD is unset)"),
             f"- DeepSeek in flight: {len(in_flight)} of {int(num(cfg, 'ORCHESTRATE_DEEPSEEK_CAP'))}"
             + (": " + ", ".join(in_flight) if in_flight else ""),
             f"- Grok in flight: {len(grok_held_now)} of {int(num(cfg, 'ORCHESTRATE_GROK_CAP'))}"
             + (": " + ", ".join(grok_held_now) if grok_held_now else "")]
    rebuild = result.get("rebuild", [])
    lines.append(f"- PRs waiting for a rebuild: {len(rebuild)}" + (": " + ", ".join(
        f"{p['target']} ({p['owner'] or 'no Agent: line'}, {p['why']})" for p in rebuild) if rebuild else ""))
    # vws-deploy#504: a PR Rule D skips for the owner/repo Part of form needs a body edit; say which and how.
    owner_repo = sorted(n["url"] for n in notes if n.get("kind") == "merge_check" and n.get("url")
                        and "part_of_owner_repo" in n.get("reasons", []))
    lines.append(f"- Merges skipped for part_of_owner_repo: {len(owner_repo)}"
                 + (f": {', '.join(owner_repo)}; {PART_OF_HINT}" if owner_repo else ""))
    locks = state.get("merge_locks", {})
    lines.append(merge_skip_line(result))
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


def status_block(body):
    """The status block of body, from the start marker through the end marker ('' when unowned)."""
    if status_unowned(body):
        return ""
    s, e = tr.status_span(body)
    return body[s:e]


def splice_status(current, block):
    """current with its status block replaced by the block in block; everything outside is kept byte for
    byte. None when either has no single status block (the caller writes nothing)."""
    if status_unowned(current) or status_unowned(block):
        return None
    s, e = tr.status_span(current)
    bs, be = tr.status_span(block)
    return current[:s] + block[bs:be] + current[e:]


def _status_norm(body):
    text = STATUS_UPDATED_RE.sub("", (body or "").replace("\r\n", "\n"))
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))


def status_same(old, new):
    return _status_norm(old) == _status_norm(new)


def failure_section(body):
    """The '## Failed ticks' section inside the status block ('' when there is none or no block)."""
    m = tr.failure_match(status_block(body))
    return m.group(0).rstrip("\n") if m else ""


def set_failure_section(body, section):
    """body with the failure section inside its status block replaced by section; text outside the block
    is kept byte for byte. None when body has no single status block (the caller writes nothing)."""
    if status_unowned(body):
        return None
    s, e = tr.status_span(body)
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


def recheck_grok(gh, k, repos, cap, woken=(), posted=(), released=(), allow_assigned=False):
    """Rule G, right before a wake is posted. Returns '' when it may go ahead, else why not.
    The issue must still be open, ready+grok, unassigned, without `deepseek` or an opt-out label.
    allow_assigned (vws-deploy#545) is only the ac-partial continuation: that assignee may stay.
    Then occupancy is rebuilt from a fresh read of the open grok items of every scoped repo: Grok work
    held now (grok_held: assigned, or named by an open grok PR; an ac-partial idle issue is neither),
    the tasks the tick saw as woken that are still open with grok, and the wakes already posted this
    tick all count toward cap; the issue's own repo must not be busy (grok_busy). A failed read skips
    the wake (fails closed)."""
    repo, number = k.split("#")
    try:
        issue, _ = gh.get(f"repos/{OWNER}/{repo}/issues/{number}")
        names = {lb["name"] for lb in issue.get("labels", [])}
        assigned_ok = allow_assigned or not issue.get("assignees")
        if not (issue.get("state") == "open" and {"ready", "grok"} <= names and assigned_ok
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
    held = grok_held(fresh) - set(released)  # done or expired at decide time (#411), even if still assigned
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
    _, why = tr.pr_at_head(lambda path: (pr, None), repo, number, head)
    if why:
        return why
    if pr.get("draft") or pr["base"]["ref"] != "main":
        return "draft or base changed"
    if "verify-approved" not in names or names & set(OPT_OUT_LABELS) or tr.opted_out(pr.get("body")):
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
    if head and tr.pr_at_head(gh.get, repo, number, head)[1]:
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
# vws-deploy#486: when a PR body was last edited, so the AC tick trusts only a body vws-verify saw.
AC_EDIT_QUERY = ("query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){"
                 " pullRequest(number:$n){ lastEditedAt headRefOid } } }")
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
        deploy = read_json(paths.deploy_file, {})
        if mode == "apply":
            new_state, errors = apply(result, snapshot, gh, cfg, paths, log, state, now)
            if add_deploy_alerts(new_state, deploy, now):
                log("deploy_alert", running=running_line(deploy))
            write_json(paths.state_file, new_state)
        else:
            add_deploy_alerts(result["state"], deploy, now)
        # The status issue body: written in apply mode when its content changed, else only kept locally.
        # A streak's failure section (written by --notify-failure) stays while this tick fails too.
        failure = read_json(paths.failure_file, {}).get("section", "") if mode == "apply" and errors else ""
        body = status_body(snapshot, result, cfg, new_state if mode == "apply" else result["state"], mode, now, failure,
                           deploy)
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
        if tr.status_has_key(current, key):
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
