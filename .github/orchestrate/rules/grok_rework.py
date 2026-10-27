"""Rule B4 (vws-deploy#398(a)): wake the Grok bot that owns a PR with a vws-verify FAIL, and note the FAILs
no rule picks up (vws-deploy#457).

ACTIONS_AFTER keeps the pre-split write order: its wakes go after rules/rebuild.py's under the write cap,
while its notes stay in registry order.
"""
import hashlib

from trusted_read import (
    GROK_WAKE_MARKER, REWORK_MARKER,
)

from .common import (
    OPT_OUT_LABELS, conflict_only, decision, grok_bot, head_verdict, pr_owner, quote, rework_prs, scrub, wake_target,
)

NAME = "grok_rework"
STATE_KEYS = ("verify_fail_unrouted",)
ACTIONS_AFTER = "rebuild"


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


def grok_rework_waiting(items, cfg):
    """vws-deploy#398(a): open non-draft PRs into main whose head has a vws-verify FAIL that is not
    conflict-only, that rule B2 does not rework, that are not parked (OPT_OUT_LABELS), and whose owner
    (pr_owner) resolves to someone other than deepseek. Sorted by key. bot is the Grok dev bot the wake
    names (vws-deploy#506): the `Agent:` owner as is, else grok_bot of the PR."""
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
            bot, bot_via = (owner, via) if via == "agent" else grok_bot(it, cfg)
            out.append({"target": k, "url": it["url"], "head_sha": it["head_sha"], "owner": owner, "via": via,
                        "bot": bot, "bot_via": bot_via if bot_via == via else f"{via}, {bot_via}", "verdict": v})
    return out


def grok_rework_body(p, key, run_id):
    v = p["verdict"]
    return "\n".join([GROK_WAKE_MARKER, f"vws-orchestrate: Grok rework {p['target']} {p['url']}", "",
                      f"Head `{p['head_sha']}` has a vws-verify FAIL: {v['url']}", "",
                      quote(scrub(v["body"] or "")).replace("<!--", "&lt;!--"), "",
                      f"Owner `{p['bot']}` ({p['bot_via']}): please fix the findings on the same branch with a new "
                      "commit (no force-push, no update-branch, no merge commits), then let verify run on the new head.",
                      "", f"Idempotency key: `{key}`", f"Run id: `{run_id}`",
                      f"{REWORK_MARKER}{p['head_sha']} verdict={v['id']} item={p['target']} -->"])


def decide_grok_rework(snapshot, cfg, posted, notes):
    """Rule B4 (vws-deploy#398(a)). Returns (grok_rework actions, the PRs the wake covers). Pure."""
    waiting = grok_rework_waiting(snapshot["items"], cfg)
    if not waiting:
        return [], set()
    if not wake_target(cfg):
        notes.append({"kind": "grok_rework_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "waiting": len(waiting)})
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


def decide(snapshot, state, cfg, now):
    """Pure."""
    notes = []
    items = snapshot["items"]
    grok_rework_actions, routed = decide_grok_rework(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), notes)
    unrouted = verify_fail_unrouted(items, state.get("verify_fail_unrouted", {}), notes, routed)
    return decision(grok_rework_actions, notes, {"verify_fail_unrouted": unrouted})
