"""Rule A: wake vws-verify for needs-verify PRs (vws-deploy#397).

One marker comment on the verify-wake thread (ORCHESTRATE_WAKE_THREAD; never the status issue) when
needs-verify PRs wait: the oldest for VERIFY_WAKE_MAX_WAIT_MIN or at least VERIFY_WAKE_THRESHOLD of them;
one comment per pending set.
"""
import hashlib

from trusted_read import (
    WAKE_MARKER,
)

from .common import (
    NEEDS_VERIFY, OPT_OUT_LABELS, active_repos, decision, head_verdict, num, utc, wake_target,
)

NAME = "verify_wake"
STATE_KEYS = ("prev_pending_count",)


# ---------------------------------------------------------------- decide (pure)

def wake_body(pending, key, run_id, threshold, max_wait, repos):
    lines = [WAKE_MARKER,
             f"vws-orchestrate: {len(pending)} PRs wait for verify (wake at {threshold} or after {max_wait} min).", ""]
    lines += [f"- {p['repo']}#{p['number']}@{p['head_sha']} {p['url']} (CI pre-check: green, waited {p['waited'] // 60}m)"
              for p in pending]
    lines += ["", f"Repos scanned: {', '.join(repos)}", f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def pending_key(prs):
    lines = sorted(f"{p['repo']}#{p['number']}@{p['head_sha']}" for p in prs)
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


def decide(snapshot, state, cfg, now):
    """Pure. Extra: pending_count (the tick result and health report it)."""
    actions, notes = [], []
    repos = active_repos(snapshot)
    items = snapshot["items"]

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
    return decision(actions, notes, {"prev_pending_count": len(pending)}, pending_count=len(pending))
