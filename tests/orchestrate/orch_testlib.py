"""Shared helpers for the orchestrator tests (vws-deploy#333). Not a test module."""
import hashlib
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "orchestrate", "orchestrate.py")
BOX = os.path.join(ROOT, ".github", "orchestrate", "box")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

spec = importlib.util.spec_from_file_location("orchestrate", SCRIPT)
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)

NOW = orch.utc("2026-10-21T12:00:00Z")
CFG = dict(orch.DEFAULTS)
CFG["ORCHESTRATE_WAKE_THREAD"] = "339"  # the verify-wake thread (the old status draft PR); not the status issue
REPOS = ["vws-deploy", "vws-iam", "vws-proto", "vws-hypervisor"]
U = "https://github.com/popododo0720"


def ts(hours_ago):
    return orch.iso(NOW - hours_ago * 3600)


def comment(cid, repo, number, body, hours_ago):
    return {"id": cid, "url": f"{U}/{repo}/issues/{number}#issuecomment-{cid}", "created_at": ts(hours_ago), "body": body}


def dispatch(cid, repo, number, hours_ago):
    return comment(cid, repo, number, orch.DISPATCH_TEXT, hours_ago)


def report(cid, repo, number, for_cid, result, hours_ago, pr=""):
    target = f"{U}/{repo}/issues/{number}#issuecomment-{for_cid}"
    if result == "Done":
        body = f"[deepseek-dev] Done for {target}\nPR: {pr}" if pr else f"[deepseek-dev] Done for {target}"
    else:
        body = f"[deepseek-dev] Failed for {target}: dsh exited"
    return comment(cid, repo, number, body, hours_ago)


def event(kind, label, hours_ago):
    return {"event": kind, "label": {"name": label}, "created_at": ts(hours_ago)}


def item(repo, number, labels=(), is_pr=False, draft=False, base="main", body="", comments=(), events=(),
         ci="ok", state="open", state_reason=None, report_prs=None, created_hours_ago=100, head_sha=None):
    return {"repo": repo, "number": number, "url": f"{U}/{repo}/{'pull' if is_pr else 'issues'}/{number}",
            "is_pr": is_pr, "state": state, "state_reason": state_reason, "created_at": ts(created_hours_ago),
            "labels": sorted(labels), "body": body, "draft": draft, "base": base if is_pr else "",
            "head_sha": head_sha or (hashlib.sha1(f"{repo}#{number}".encode()).hexdigest() if is_pr else ""), "comments": list(comments),
            "events": list(events), "report_prs": report_prs or {}, "ci": ci}


def snapshot(*items, deps=None, labels=None, wakes=None):
    all_labels = ["blocked", "deepseek", "grok", "in-review", "ready", "verify-approved"]
    return {"run_id": "20261021T120000Z-abcdef",
            "repos": [{"name": r, "archived": r == "vws-hypervisor"} for r in REPOS],
            "items": {f"{i['repo']}#{i['number']}": i for i in items},
            "deps": deps or {},
            "status": {"wakes": wakes or []},
            "labels": labels or {r: all_labels for r in REPOS}}


def ready_task(repo, number, ready_hours_ago=10, extra=(), **kw):
    events = list(kw.pop("events", [])) + [event("labeled", "ready", ready_hours_ago)]
    return item(repo, number, labels=("ready", "deepseek") + tuple(extra), events=events, **kw)
