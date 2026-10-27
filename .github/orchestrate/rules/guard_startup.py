"""Repo-guard startup retry (vws-deploy#446).

GitHub does not emit workflow_run for startup_failure, so ci-fail never sees the run that blocks a
verify-approved merge. One re-trigger per head (an HTML comment, pull_request edited). A second
startup_failure, or the same run still newest after GUARD_STARTUP_WAIT, is one #412 marker.
"""
import importlib.util
import os

import trusted_read as tr
from .common import (
    CI_FAIL_CAP, DEFAULTS, OPT_OUT_LABELS, ci_fail_author_ok, ci_fails, ci_reset_time, decision, iso, utc,
)

NAME = "guard_startup"
STATE_KEYS = ("guard_retry",)

# A fresh repo-guard run is started by appending one HTML comment. If that run has not appeared, or the
# same startup_failure is still the newest, wait this long and then report it instead of touching the body again.
GUARD_STARTUP_WAIT = 15 * 60


def ci_fail_module():
    """The #412 marker writer. Loaded on use so a tick that posts no marker does not import it.
    FileNotFoundError when this tree has no .github/ci-fail (an archive install that did not pack it)."""
    mod = getattr(ci_fail_module, "mod", None)
    if mod is None:
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ci-fail", "ci_fail.py"))
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        spec = importlib.util.spec_from_file_location("vws_ci_fail", path)
        if spec is None or spec.loader is None:
            raise FileNotFoundError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ci_fail_module.mod = mod
    return mod


def guard_runs_from(payload):
    """repo-guard pull_request runs from a GET actions/runs body, oldest first.
    None when the page is truncated or not a runs payload: a missing later success must not retry."""
    if not isinstance(payload, dict):
        return None
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return None
    total = payload.get("total_count", len(runs))
    if type(total) is not int or total > len(runs):
        return None
    out = []
    for w in runs:
        if not isinstance(w, dict) or w.get("name") != "repo-guard" or w.get("event") != "pull_request":
            continue
        rid, created = w.get("id"), w.get("created_at") or ""
        if type(rid) is not int or rid <= 0 or not created:
            continue
        attempt = w.get("run_attempt") or 1
        if type(attempt) is not int or attempt <= 0:
            attempt = 1
        out.append({"id": rid, "status": w.get("status") or "", "conclusion": w.get("conclusion") or "",
                    "created_at": created, "run_attempt": attempt, "html_url": w.get("html_url") or ""})
    out.sort(key=lambda r: (r["created_at"], r["id"]))
    return out


def guard_startup_decision(it, rec, now):
    """'retry', 'report', 'wait', 'truncated' or '' for one PR. Pure.
    retry: the newest repo-guard pull_request run on a verify-approved head is startup_failure, nothing
    newer succeeded, and this head has not been re-triggered. report: a second such run, or the same one
    still newest after GUARD_STARTUP_WAIT. wait: a newer run is in progress, or the one retry is recent."""
    if not (it.get("is_pr") and it.get("state") == "open" and not it.get("draft") and it.get("base") == "main"):
        return ""
    labels = set(it.get("labels") or [])
    if "verify-approved" not in labels or labels & set(OPT_OUT_LABELS) or tr.opted_out(it.get("body")):
        return ""
    info = it.get("merge_info")
    if not isinstance(info, dict) or "guard_runs" not in info:
        return ""
    runs = info.get("guard_runs")
    if runs is None:
        return "truncated"
    if not runs:
        return ""
    newest = runs[-1]
    if newest.get("status") != "completed":
        return "wait"
    if newest.get("conclusion") != "startup_failure":
        return ""
    if (rec and rec.get("reported")) or any(m["sha"] == it.get("head_sha") for m in ci_fails(it)[0]):
        return ""
    failures = [r for r in runs if r.get("conclusion") == "startup_failure"]
    if not (rec and rec.get("run")):
        return "report" if len(failures) >= 2 else "retry"
    if any(r["id"] != rec["run"] for r in failures):
        return "report"
    try:
        waited = now - utc(rec.get("at") or "")
    except (TypeError, ValueError):
        waited = GUARD_STARTUP_WAIT
    return "report" if waited >= GUARD_STARTUP_WAIT else "wait"


def guard_report_body(it, run, now):
    """The #412 marker comment for a second repo-guard startup_failure. Same bytes as ci_fail.format_comment."""
    cf = ci_fail_module()
    labels = list(it.get("labels") or [])
    branch = (it.get("merge_info") or {}).get("head_ref") or ""
    owner, agent, source = cf.resolve_owner(it["repo"], labels, it.get("body") or "", branch)
    # Same n as ci_fail_head: distinct failing heads since the last authorized reset. Markers from
    # before /orchestrate reset-failures or a needs-human removal do not count (vws-deploy#446).
    since = ci_reset_time(it, it)
    prior = []
    for m in ci_fails(it)[0]:
        if m["sha"] == it.get("head_sha") or utc(m["at"]) <= since or m["sha"] in prior:
            continue
        prior.append(m["sha"])
    n = len(prior) + 1
    wake = n <= CI_FAIL_CAP and owner in ("deepseek", "grok") and "needs-human" not in labels
    stamp = iso(now)
    rec = {"v": 1, "repo": it["repo"], "pr": it["number"], "head_sha": it["head_sha"],
           "head_branch": branch or "unknown", "owner": owner, "agent": agent, "owner_source": source,
           "chain": [it["number"]], "n": n, "cap": CI_FAIL_CAP, "wake": wake,
           "runs": [{"workflow": "repo-guard", "run_id": run["id"], "run_attempt": run.get("run_attempt") or 1,
                     "conclusion": "startup_failure", "url": run.get("html_url") or ""}],
           "failed_jobs": [], "created_at": stamp, "updated_at": stamp}
    return cf.format_comment(rec, {})


def decide_guard_startup(items, new_state, now, cfg, notes):
    """One re-trigger per verify-approved head whose newest repo-guard run is startup_failure; a second one
    is a #412 marker instead. Records live in state['guard_retry']. The attempt itself is journaled in apply."""
    window = int(float(cfg.get("ORCHESTRATE_COMMENT_WINDOW_DAYS", DEFAULTS["ORCHESTRATE_COMMENT_WINDOW_DAYS"]))) * 86400
    kept = {}
    for key, rec in (new_state.get("guard_retry") or {}).items():
        if not isinstance(rec, dict):
            continue
        repo_n, _, sha = key.rpartition("@")
        it = items.get(repo_n)
        live = bool(it and it.get("state") == "open" and it.get("head_sha") == sha)
        try:
            recent = bool(rec.get("at")) and now - utc(rec["at"]) < window
        except (TypeError, ValueError):
            recent = False
        if live or recent:
            kept[key] = rec
    new_state["guard_retry"] = kept
    actions = []
    for k in sorted(items):
        it = items[k]
        if "verify-approved" not in (it.get("labels") or []):
            continue
        key = f"{k}@{it.get('head_sha')}"
        kind = guard_startup_decision(it, kept.get(key), now)
        if kind == "truncated":
            notes.append({"kind": "guard_startup_truncated", "target": k})
            continue
        if kind == "wait":
            notes.append({"kind": "guard_startup_wait", "target": k, "run": (kept.get(key) or {}).get("run", 0)})
            continue
        if kind not in ("retry", "report"):
            continue
        run = it["merge_info"]["guard_runs"][-1]
        if kind == "retry":
            actions.append({"type": "guard_retry", "target": k, "head": it.get("head_sha") or "", "run": run["id"],
                            "reason": f"repo-guard startup_failure run {run['id']} on {(it.get('head_sha') or '')[:7]}; re-trigger once"})
        else:
            try:
                body = guard_report_body(it, run, now)
            except FileNotFoundError:
                notes.append({"kind": "guard_report_unavailable", "target": k})
                continue
            actions.append({"type": "guard_report", "target": k, "head": it.get("head_sha") or "", "run": run["id"],
                            "reason": f"second repo-guard startup_failure on {(it.get('head_sha') or '')[:7]}; #412 marker",
                            "body": body})
    return actions


def guard_retry_restores(m, state):
    """When m['body'] is the pre-image plus exactly one retry line and the stored hash matches, replace
    m['body'] with that pre-image and return True. An edit that changes anything else returns False, so
    AC tick keeps failing closed (vws-deploy#446, #499)."""
    import hashlib
    pres = tr.guard_retry_preimages(m.get("body") or "")
    head = m.get("head_sha") or ""
    if not pres or not head:
        return False
    rec = (state or {}).get("guard_retry", {}).get(f"{m.get('pr')}@{head}") or {}
    want = rec.get("body_sha") or ""
    if not want:
        return False
    for pre in pres:
        if hashlib.sha256(pre.encode()).hexdigest() == want:
            m["body"] = pre
            return True
    return False


def decide(snapshot, state, cfg, now):
    """Pure. State carries guard_retry only when this tick prunes or keeps a record."""
    notes = []
    working = {"guard_retry": dict(state.get("guard_retry") or {})}
    actions = decide_guard_startup(snapshot["items"], working, now, cfg, notes)
    kept = working.get("guard_retry") or {}
    incoming = state.get("guard_retry") or {}
    return decision(actions, notes, {"guard_retry": kept} if kept != incoming else {})
