"""Rule R: wake dr eggbot to rebuild a PR whose verify FAIL is conflict-only or that is labeled needs-rebuild."""
import hashlib

from trusted_read import (
    REBUILD_WAKE_MARKER,
)

from .common import (
    agent_of, decision, wake_target,
)

NAME = "rebuild"
STATE_KEYS = ()


NEEDS_REBUILD = "needs-rebuild"


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


def decide(snapshot, state, cfg, now):
    """Pure. Extra: rebuild (the PRs waiting for a rebuild, for the status body)."""
    notes = []
    rebuild_actions, rebuild = decide_rebuild(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), notes)
    return decision(rebuild_actions, notes, rebuild=rebuild)
