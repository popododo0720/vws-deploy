"""Lead wake: wake dr eggbot for a PR no rule moves on its own (vws-deploy#491, #504)."""
import hashlib

import trusted_read as tr
from trusted_read import (
    LEAD_WAKE_MARKER,
)

from .common import (
    OPT_OUT_LABELS, PART_OF_HINT, conflict_only, decision, grok_link, head_verdict, owner_repo_part_of, rework_prs,
    status_target, wake_target,
)
from .grok_rework import (
    decide_grok_rework,
)

NAME = "lead"
STATE_KEYS = ()


def lead_key(k, sha):
    """One lead wake per PR head (vws-deploy#397)."""
    return "sha256:" + hashlib.sha256(f"lead-wake\n{k}@{sha}".encode()).hexdigest()


LEAD_REBUILD = ("No rule moves this PR head on its own: dr eggbot rebuilds it onto main as a new -vN PR "
                "(no force-push) or routes the FAIL to its owner.")
# vws-deploy#504: a part_of_owner_repo skip needs a body edit only; the head and its verdict stay.
LEAD_BODY_EDIT = ("No rule moves this PR on its own: dr eggbot (or the PR's owner) edits the PR body; the head and "
                  "its verify verdict stay, and Rule D merges it on a later tick once nothing else holds it.")


def lead_wake_body(it, k, reason, key, run_id, action=LEAD_REBUILD):
    return "\n".join([LEAD_WAKE_MARKER, f"vws-orchestrate: lead wake for {k} at `{it['head_sha']}` {it['url']}", "",
                      f"Reason: {reason}.", action, "",
                      f"Idempotency key: `{key}`", f"Run id: `{run_id}`"])


def decide_lead(snapshot, cfg, notes, routed=()):
    """Lead wake (vws-deploy#397). Returns lead_wake actions: one per PR head that is verify-approved but
    conflicts with main, or has a vws-verify FAIL that no rework picks up (a Grok PR, a PR with no
    DeepSeek owner, or a conflict-only verdict), or is verify-approved but Rule D skips it with
    part_of_owner_repo (vws-deploy#504; the wake names the allowed forms and the PR URL). routed are the PRs the grok rework wake (rule B4,
    vws-deploy#398) covers this tick: their owner is woken, so they get no lead wake. Pure."""
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
            if v and v["result"] == "fail" and (k not in routed or conflict_only(v["body"])):
                owner = items.get(grok_link(it), {})
                if conflict_only(v["body"]):
                    reason = f"verify FAIL is conflict-only ({v['url']})"
                elif "grok" in it["labels"] or "grok" in owner.get("labels", []):
                    reason = f"verify FAIL on a Grok PR and no Grok rework wake covers it ({v['url']})"
                elif k not in reworked:
                    reason = f"verify FAIL on a PR with no DeepSeek owner ({v['url']})"
        action = LEAD_REBUILD
        if ("verify-approved" in it["labels"] and info.get("head_sha") == it["head_sha"]
                and not set(it["labels"]) & set(OPT_OUT_LABELS) and not tr.opted_out(it.get("body"))
                and owner_repo_part_of(it.get("body"))):
            fix = f"verify-approved but Rule D skips it with part_of_owner_repo ({it['url']}): {PART_OF_HINT}"
            reason, action = (f"{reason}; also {fix}", action) if reason else (fix, LEAD_BODY_EDIT)
        if reason:
            due.append((k, it, reason, action))
    if due and not wake_target(cfg):
        notes.append({"kind": "lead_wake_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "due": len(due)})
        return []
    actions = []
    for k, it, reason, action in due:
        key = lead_key(k, it["head_sha"])
        if key in posted:
            continue
        actions.append({"type": "lead_wake", "target": wake_target(cfg), "item": k, "key": key,
                        "reason": f"{k}: {reason}",
                        "body": lead_wake_body(it, k, reason, key, snapshot["run_id"], action)})
    return actions


def decide(snapshot, state, cfg, now):
    """Pure. The PRs rules/grok_rework.py routes this tick are left to it."""
    notes = []
    _, routed = decide_grok_rework(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), [])
    return decision(decide_lead(snapshot, cfg, notes, routed), notes)
