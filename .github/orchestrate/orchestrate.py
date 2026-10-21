#!/usr/bin/env python3
"""Deterministic factory orchestrator for the vws-* repos (vws-deploy#333). Python 3 stdlib only.

One tick: collect (gh API reads with an on-disk ETag cache) -> decide (pure) -> apply (writes only
when ORCHESTRATE_MODE=apply). It never calls an LLM. It runs on the shared box under
box/orchestrate-loop.sh with the box's gh login; GH_TOKEN is always removed from the gh environment.

Rules (see docs/factory-orchestrator.md):
  A  wake vws-verify with one marker comment on the status thread (draft PR vws-deploy#339) when
     >= VERIFY_WAKE_THRESHOLD PRs wait for verify (edge-triggered, one comment per pending set)
  B  hand the next ready+deepseek task to DeepSeek, one at a time, with an @deepseek-dev comment
  C  count DeepSeek failures (Failed report, 3h timeout, Done PR closed unmerged); at 2, swap to grok

A failed tick streak is reported the same way, with a failure marker comment on the status thread
(--notify-failure, called by box/orchestrate-loop.sh).

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
STATUS_REPO = "vws-deploy"  # the status thread lives here; ORCHESTRATE_STATUS_PR picks the number
FAIL_EXIT = 4
DEEPSEEK = "@deepseek-dev"
DISPATCH_TEXT = (
    "@deepseek-dev please take this issue: one small PR per the acceptance criteria above. "
    "Small diff, no unrelated changes, no closing keywords."
)
MANAGED_LABELS = ("blocked", "in-review")
# These checks fail by design until vws-verify approves, so they never make a PR "CI failing".
VERIFY_CHECKS = {"verify-gate", "vws-verify"}
CI_BAD_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
# The first line of a status comment is exactly one of these; vws-verify and vws-platform routines
# match on it.
WAKE_MARKER = "<!-- vws-orchestrate:verify-wake -->"
FAILURE_MARKER = "<!-- vws-orchestrate:failure -->"
KEY_RE = re.compile(r"^Idempotency key: `(sha256:[0-9a-f]{64})`$", re.M)
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
    "VERIFY_WAKE_THRESHOLD": "5",
    "VERIFY_WAKE_EDGE_MIN_HOURS": "1",
    "VERIFY_WAKE_COOLDOWN_HOURS": "6",
    "ORCHESTRATE_STATUS_PR": "339",
    "ORCHESTRATE_FAILURE_ALERT": "apply",
    "ORCHESTRATE_FAILURE_THRESHOLD": "3",
    "ORCHESTRATE_FAILURE_MIN_GAP_HOURS": "1",
    "ORCHESTRATE_LOG_MAX_BYTES": str(5 * 1024 * 1024),
    "ORCHESTRATE_LOG_KEEP": "5",
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
    if not re.fullmatch(r"[1-9][0-9]*", cfg["ORCHESTRATE_STATUS_PR"]):
        raise ValueError(f"ORCHESTRATE_STATUS_PR must be a positive integer, got {cfg['ORCHESTRATE_STATUS_PR']!r}")
    if cfg["ORCHESTRATE_FAILURE_ALERT"] not in ("apply", "always", "never"):
        raise ValueError("ORCHESTRATE_FAILURE_ALERT must be apply, always or never")
    text_keys = ("ORCHESTRATE_MODE", "ORCHESTRATE_SCOPE", "ORCHESTRATE_EXTRA_REPOS", "ORCHESTRATE_STATUS_PR",
                 "ORCHESTRATE_FAILURE_ALERT")
    for k, v in cfg.items():
        if k not in text_keys:
            float(v)  # raises ValueError on a bad number
    return cfg


def status_target(cfg):
    """The status thread: a never-merged draft PR in vws-deploy. PR comments use the issues API."""
    return f"{STATUS_REPO}#{int(cfg['ORCHESTRATE_STATUS_PR'])}"


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
        re.compile(rf"^{_REPO}/issues/[0-9]+/(events|comments)(\?.*)?$"),
        re.compile(rf"^{_REPO}/commits/[0-9a-f]{{40}}/(check-runs|status)(\?.*)?$"),
        re.compile(r"^rate_limit$"),
    ],
    "POST": [re.compile(rf"^{_REPO}/issues/[0-9]+/(comments|labels)$")],
    "DELETE": [re.compile(rf"^{_REPO}/issues/[0-9]+/labels/(blocked|in-review|deepseek)$")],
}


class GhError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


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


class Gh:
    """Every GitHub call goes through here. Reads use If-None-Match; 304 answers come from the cache
    and do not count against the primary rate limit."""

    def __init__(self, cache_path=None, runner=subprocess.run, sleep=time.sleep, timeout=30, tries=3):
        self.cache_path = cache_path
        self.cache = read_json(cache_path, {}) if cache_path else {}
        self.used = {}
        self.runner, self.sleep, self.timeout, self.tries = runner, sleep, timeout, tries
        self.stats = {"get": 0, "not_modified": 0, "write": 0}

    def _env(self):
        env = dict(os.environ)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        return env

    def _run(self, args, stdin=None, tries=None):
        last = None
        tries = tries or self.tries
        for attempt in range(tries):
            try:
                r = self.runner(args, input=stdin, capture_output=True, text=True, timeout=self.timeout, env=self._env())
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
        if not allowed("GET", path):
            raise PermissionError(f"gh call not allowed: GET {path}")
        args = ["gh", "api", "-i", "--method", "GET", path]
        cached = self.cache.get(path)
        if cached and cached.get("etag"):
            args += ["-H", f"If-None-Match: {cached['etag']}"]
        status, headers, body = self._run(args)
        self.stats["get"] += 1
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
        if not allowed(method, path):
            raise PermissionError(f"gh call not allowed: {method} {path}")
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


def classify_comment(body):
    """Return (kind, data) for comments the orchestrator cares about, else None."""
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
    m = re.search(r"<!-- orchestrate:([a-z-]+)", text)
    if m:
        return "marker", {"marker": m.group(1)}
    return None


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


# ---------------------------------------------------------------- decide (pure)

def wake_body(pending, key, run_id, threshold, repos):
    lines = [WAKE_MARKER,
             f"vws-orchestrate: {len(pending)} PRs wait for verify (threshold {threshold}).", ""]
    lines += [f"- {p['repo']}#{p['number']} {p['url']} `{p['head_sha'][:7]}`" for p in pending[:50]]
    if len(pending) > 50:
        lines.append(f"- ... and {len(pending) - 50} more")
    lines += ["", f"Repos scanned: {', '.join(repos)}", f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def status_wakes(comments):
    """Verify-wake comments already on the status thread: [{"key", "at"}]."""
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


def dispatch_history(item, now, cfg, exclusions):
    """Per dispatch on one item: outcome in in_flight | done | review | failed | excluded."""
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
    cycles = find_cycles(snapshot)

    # Rule A: pending PRs and the verify wake
    pending = []
    for key in sorted(items):
        it = items[key]
        if (it["is_pr"] and it["state"] == "open" and not it.get("draft") and it.get("base") == "main"
                and "verify-approved" not in it["labels"] and it.get("ci") != "failing"):
            pending.append({"repo": it["repo"], "number": it["number"], "url": it["url"], "head_sha": it["head_sha"]})
    threshold = int(num(cfg, "VERIFY_WAKE_THRESHOLD"))
    key = pending_key(pending)
    wakes = snapshot.get("status", {}).get("wakes", [])
    last_at = max((w["at"] for w in wakes), default=0)
    prev = state.get("prev_pending_count")
    rising = prev is None or prev < threshold
    gap = num(cfg, "VERIFY_WAKE_EDGE_MIN_HOURS" if rising else "VERIFY_WAKE_COOLDOWN_HOURS") * 3600
    if len(pending) >= threshold and key not in {w["key"] for w in wakes} and now - last_at >= gap:
        actions.append({"type": "wake_verify", "target": status_target(cfg),
                        "reason": f"{len(pending)} PRs pending, threshold {threshold}", "key": key,
                        "body": wake_body(pending, key, snapshot["run_id"], threshold, sorted(repos))})
    new_state["prev_pending_count"] = len(pending)

    # Rules B and C
    in_flight, queue = [], []
    for k in sorted(items):
        it = items[k]
        if it["state"] != "open":
            continue
        hist = dispatch_history(it, now, cfg, exclusions)
        if any(h["outcome"] == "in_flight" for h in hist):
            in_flight.append(k)
        labels = set(it["labels"])
        if "deepseek" not in labels or "ready" not in labels:
            continue
        since = reset_time(it)
        counted = [h for h in hist if h["outcome"] == "failed" and h["at"] > since]
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
            continue
        if "grok" in labels:
            continue
        deps, bad = parse_deps(it.get("body"), it["repo"])
        unmet = [d for d in deps if dep_state(d, snapshot, repos) != "met"]
        for d in unmet:
            notes.append({"kind": "dep_unmet", "target": k, "dep": d, "state": dep_state(d, snapshot, repos)})
        for b in bad:
            notes.append({"kind": "dep_unparsed", "target": k, "token": b})
        if k in cycles:
            notes.append({"kind": "dep_cycle", "target": k})
        blocked = bool(unmet or bad or k in cycles)
        if blocked and "blocked" not in labels:
            actions.append({"type": "add_label", "target": k, "label": "blocked", "reason": "unmet dependency"})
        if not blocked and "blocked" in labels and k in owned["blocked"]:
            actions.append({"type": "remove_label", "target": k, "label": "blocked", "reason": "dependencies met"})
        if latest and latest["outcome"] == "review" and "in-review" not in labels:
            actions.append({"type": "add_label", "target": k, "label": "in-review", "reason": f"DeepSeek PR {latest.get('pr')} open"})
        if (latest and latest["outcome"] in ("done", "failed") and "in-review" in labels and k in owned["in-review"]):
            actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": f"DeepSeek PR {latest['kind']}"})
        eligible = (not blocked and "blocked" not in labels and not (it["is_pr"] and it.get("draft"))
                    and (latest is None or latest["outcome"] in ("failed", "excluded") or latest["at"] <= since))
        if eligible:
            prio = 0 if "priority/blocker" in labels else 1
            queue.append((prio, ready_time(it), it["repo"], it["number"], k))
    scope = cfg.get("ORCHESTRATE_SCOPE", "")
    if scope:
        queue = [q for q in queue if q[4] == scope]
    if in_flight:
        notes.append({"kind": "in_flight", "targets": in_flight})
    elif queue:
        queue.sort()
        k = queue[0][4]
        actions.append({"type": "dispatch", "target": k, "reason": "next in queue, nothing in flight",
                        "body": f"{DISPATCH_TEXT}\n\n<!-- orchestrate:dispatch -->"})
    return {"actions": actions, "notes": notes, "state": new_state, "pending_count": len(pending)}


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


def ci_state(gh, repo, sha):
    runs, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/check-runs?per_page=100")
    status, _ = gh.get(f"repos/{OWNER}/{repo}/commits/{sha}/status")
    return ci_verdict(runs, status)


def collect(gh, repo_names, now, cfg, run_id):
    window = int(num(cfg, "ORCHESTRATE_COMMENT_WINDOW_DAYS"))
    # A since value floored to the UTC day keeps the URL stable for the day, so ETags can match.
    since = iso((int(now) // 86400 - window) * 86400)
    snap = {"run_id": run_id, "repos": [], "items": {}, "deps": {}, "labels": {}}
    srepo, snum = status_target(cfg).split("#")
    snap["status"] = {"wakes": status_wakes(gh.get_all(f"repos/{OWNER}/{srepo}/issues/{snum}/comments?per_page=100"))}
    for name in repo_names:
        meta, _ = gh.get(f"repos/{OWNER}/{name}")
        snap["repos"].append({"name": name, "archived": bool(meta.get("archived"))})
        if meta.get("archived"):
            continue
        snap["labels"][name] = sorted(lb["name"] for lb in gh.get_all(f"repos/{OWNER}/{name}/labels?per_page=100"))
        pulls = {p["number"]: p for p in gh.get_all(f"repos/{OWNER}/{name}/pulls?state=open&per_page=100")}
        for i in gh.get_all(f"repos/{OWNER}/{name}/issues?state=open&per_page=100"):
            p = pulls.get(i["number"])
            snap["items"][f"{name}#{i['number']}"] = {
                "repo": name, "number": i["number"], "url": i["html_url"], "is_pr": "pull_request" in i,
                "state": i["state"], "state_reason": i.get("state_reason"), "created_at": i["created_at"],
                "labels": sorted(lb["name"] for lb in i.get("labels", [])), "body": i.get("body") or "",
                "draft": bool(p and p.get("draft")), "base": p["base"]["ref"] if p else "",
                "head_sha": p["head"]["sha"] if p else "", "comments": [], "events": [], "report_prs": {},
            }
        for c in gh.get_all(f"repos/{OWNER}/{name}/issues/comments?since={since}&sort=created&direction=asc&per_page=100", limit=50):
            k = f"{name}#{c['issue_url'].rsplit('/', 1)[1]}"
            if k in snap["items"] and classify_comment(c.get("body")):
                snap["items"][k]["comments"].append({"id": c["id"], "url": c["html_url"], "created_at": c["created_at"],
                                                     "body": c.get("body") or ""})
    for k, it in sorted(snap["items"].items()):
        labels = set(it["labels"])
        if labels & {"ready", "deepseek", "grok"}:
            # Candidates get their full comment history (the repo-wide list only covers the window),
            # so old dispatches and failures are never forgotten. ETags keep this cheap.
            it["comments"] = [{"id": c["id"], "url": c["html_url"], "created_at": c["created_at"], "body": c.get("body") or ""}
                              for c in gh.get_all(f"repos/{OWNER}/{it['repo']}/issues/{it['number']}/comments?per_page=100")
                              if classify_comment(c.get("body"))]
            it["events"] = [{"event": e["event"], "label": {"name": (e.get("label") or {}).get("name")},
                             "created_at": e["created_at"]}
                            for e in gh.get_all(f"repos/{OWNER}/{it['repo']}/issues/{it['number']}/events?per_page=100")
                            if e.get("event") in ("labeled", "unlabeled")]
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
        for c in it["comments"]:
            kind = classify_comment(c["body"])
            if kind and kind[0] == "report" and kind[1]["pr"] and kind[1]["pr"] not in it["report_prs"]:
                pk = pr_key(kind[1]["pr"])
                if pk:
                    prepo, pnum = pk.split("#")
                    try:
                        pr, _ = gh.get(f"repos/{OWNER}/{prepo}/pulls/{pnum}")
                        it["report_prs"][kind[1]["pr"]] = {"state": pr["state"], "merged": bool(pr.get("merged_at"))}
                    except GhError:
                        pass
        if (it["is_pr"] and it["state"] == "open" and not it["draft"] and it["base"] == "main"
                and "verify-approved" not in labels):
            it["ci"] = ci_state(gh, it["repo"], it["head_sha"])
    return snap


# ---------------------------------------------------------------- apply

def apply(result, snapshot, gh, cfg, paths, log, state):
    """Run the planned actions. Returns the state to save (dry-run never calls this)."""
    max_writes = int(num(cfg, "ORCHESTRATE_MAX_WRITES"))
    scope = cfg["ORCHESTRATE_SCOPE"]
    writes, errors = 0, 0
    new_state = result["state"]
    for a in result["actions"]:
        if a["type"] == "wake_verify" and scope:
            log("skipped", action="wake_verify", reason="ORCHESTRATE_SCOPE is set")
            continue
        if scope and a["target"] != scope:
            log("skipped", action=a["type"], target=a["target"], reason="outside ORCHESTRATE_SCOPE")
            continue
        repo, number = a["target"].split("#")
        base = f"repos/{OWNER}/{repo}/issues/{number}"
        calls = []
        if a["type"] in ("dispatch", "wake_verify"):
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
        if a["type"] == "dispatch" and not recheck_dispatch(gh, repo, number, snapshot["items"][a["target"]]):
            log("skipped", action="dispatch", target=a["target"], reason="target changed since collect")
            continue
        owned = new_state.setdefault("owned_labels", {})
        if a["type"] == "add_label" and a["label"] in MANAGED_LABELS:
            # Journal ownership before the write, so a crash right after it cannot orphan the label.
            owned.setdefault(a["label"], [])
            if a["target"] not in owned[a["label"]]:
                owned[a["label"]].append(a["target"])
            write_json(paths.state_file, new_state)
        try:
            for method, path, payload in calls:
                writes += 1  # counted before the call: a failed POST may still have landed
                gh.write(method, path, payload)
        except (GhError, PermissionError) as e:
            errors += 1
            log("apply_failed", action=a["type"], target=a["target"], error=str(e))
            continue
        if a["type"] == "remove_label" and a["target"] in owned.get(a["label"], []):
            owned[a["label"]].remove(a["target"])
        if a["type"] == "wake_verify":
            log("wake_posted", target=a["target"], key=a["key"])
        log("applied", action=a["type"], target=a["target"], reason=a["reason"])
    return new_state, errors


def recheck_wake(gh, repo, number, key):
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    return key not in {w["key"] for w in status_wakes(comments)}


def recheck_dispatch(gh, repo, number, item):
    """Re-read the target right before the dispatch comment: no new dispatch since collect."""
    known = {c["id"] for c in item["comments"]}
    comments = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
    for c in comments:
        kind = classify_comment(c.get("body"))
        if c["id"] not in known and kind and kind[0] == "dispatch":
            return False
    issue, _ = gh.get(f"repos/{OWNER}/{repo}/issues/{number}")
    names = {lb["name"] for lb in issue.get("labels", [])}
    return issue["state"] == "open" and {"ready", "deepseek"} <= names and not names & {"grok", "blocked"}


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
            gh = gh or Gh(paths.cache)
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
                snapshot = collect(gh, repos, now, cfg, run_id)
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
            new_state, errors = apply(result, snapshot, gh, cfg, paths, log, state)
            write_json(paths.state_file, new_state)
            if errors:
                gh.save_cache()
                prev_errors = read_json(paths.health, {}).get("recent_errors", [])
                msg = f"{errors} action(s) failed in apply; see apply_failed in the log"
                health.update(result="apply_failed", last_error=msg, recent_errors=(prev_errors + [msg])[-3:],
                              gh=gh.stats, actions=len(result["actions"]), pending=result["pending_count"])
                write_json(paths.health, health)
                log("tick_end", actions=len(result["actions"]), errors=errors, gh=gh.stats)
                return FAIL_EXIT
        if gh is not None:
            gh.save_cache()
            health["gh"] = gh.stats
        health.update(result="ok", actions=len(result["actions"]), pending=result["pending_count"])
        write_json(paths.health, health)
        if os.path.exists(paths.failure_file):
            os.remove(paths.failure_file)  # a good tick ends the failure streak
        log("tick_end", actions=len(result["actions"]), gh=gh.stats if gh else {})
        return 0
    finally:
        os.close(lock_fd)


def failure_body(host, failures, threshold, streak_start, exit_code, errors, mode, log_path, key):
    lines = [FAILURE_MARKER,
             f"vws-orchestrate: {failures} failed ticks in a row on host `{host}` since {iso(streak_start)} "
             f"(threshold {threshold}).", "",
             f"Last exit code: {exit_code}", f"Mode: {mode}", f"Log: `{log_path}`", "", "Last errors (redacted):"]
    lines += [f"- {scrub(e).replace(chr(10), ' ')}" for e in errors[-3:]] or ["- none recorded"]
    lines += ["", f"Idempotency key: `{key}`"]
    return "\n".join(lines)


def notify_failure(paths, cfg, args, gh=None, now=None):
    """Called by the loop after each failed tick. Posts one failure comment per streak, at the threshold.
    It bypasses the per-tick write cap and the rate floor (one call), so it still goes out when every
    tick fails; if GitHub itself is down the POST fails and the next failed tick tries again."""
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
    host = socket.gethostname()
    key = "sha256:" + hashlib.sha256(f"failure:{host}:{args.streak_start}".encode()).hexdigest()
    prev = read_json(paths.failure_file, {})
    if prev.get("key") == key:
        log("failure_alert_skipped", reason="already posted for this streak", failures=args.failures)
        return 0
    if now - prev.get("at", 0) < num(cfg, "ORCHESTRATE_FAILURE_MIN_GAP_HOURS") * 3600:
        log("failure_alert_skipped", reason="minimum gap between failure comments", failures=args.failures)
        return 0
    repo, number = status_target(cfg).split("#")
    gh = gh or Gh()
    try:
        # A POST that timed out may have landed; never post the same key twice.
        posted = gh.get_all(f"repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100")
        if any(f"Idempotency key: `{key}`" in (c.get("body") or "") for c in posted):
            write_json(paths.failure_file, {"key": key, "at": now})
            log("failure_alert_skipped", reason="the status thread already has this key", failures=args.failures)
            return 0
    except (GhError, PermissionError) as e:
        log("failure_alert_recheck_failed", error=str(e))  # GitHub may be down; still try the POST
    body = failure_body(host, args.failures, threshold, args.streak_start, args.exit_code, errors,
                        cfg["ORCHESTRATE_MODE"], paths.log, key)
    try:
        gh.write("POST", f"repos/{OWNER}/{repo}/issues/{number}/comments", {"body": body})
    except (GhError, PermissionError) as e:
        log("failure_alert_failed", error=str(e), failures=args.failures)
        return 0
    write_json(paths.failure_file, {"key": key, "at": now})
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
