"""Rule G: wake the Grok dev bot for the top ready+grok issue (vws-deploy#378).

One marker comment on the verify-wake thread (ORCHESTRATE_WAKE_THREAD; unset: no Grok wake) for the top
ready+grok issue on ORCHESTRATE_GROK_ALLOWLIST (empty: nothing is assigned), at most ORCHESTRATE_GROK_CAP at
a time and one per repo; a woken issue holds its slot while it stays open with grok, except an ac-partial
issue with boxes still open and no open PR (vws-deploy#545): one wake per ticked-box set, and the assignee may
keep the issue. Soft-CAP parks do not count against the CAP (and do not strip `grok`): blocked|hold|
do-not-merge|needs-human on the issue or a linked open PR, or every linked open PR non-draft with
needs-verify|verify-approved (repo stays busy). An assigned claim with no linked PR and no activity for
ORCHESTRATE_GROK_IDLE_MIN also frees its slot.
"""
import hashlib
import re

from trusted_read import (
    GROK_ASSIGN_MARKER, GROK_WAKE_MARKER,
)

from .common import (
    DEFAULTS, OPT_OUT_LABELS, ac_boxes, ac_partial_slice, active_repos, decision, dep_state, find_cycles, grok_allowlist, grok_bot, grok_busy, grok_held, grok_open_pr,
    grok_cap_parked, grok_link, grok_parked_blocked, grok_parked_verify, num, parse_deps, ready_time, reset_time, utc, wake_target,
)

NAME = "grok"
STATE_KEYS = ("grok_released",)


def grok_key(k, since):
    """One wake per issue per failure reset (/orchestrate reset-failures re-arms it)."""
    return "sha256:" + hashlib.sha256(f"grok-wake\n{k}\n{int(since)}".encode()).hexdigest()


def grok_wake_body(it, k, key, slot, cap, run_id, owner="", via=""):
    who = f"Owner `{owner}` ({via})" if owner else "No owner resolved, so the lead (dr eggbot)"
    return "\n".join([GROK_WAKE_MARKER, f"vws-orchestrate: Grok task {k} {it['url']}", "",
                      f"{who}: please take this issue: one small PR per its acceptance criteria, "
                      f"`Part of #{it['number']}` in commits and the PR body, no closing keywords.",
                      f"Grok slot {slot} of {cap}; this issue holds the slot while it is open with `grok` "
                      f"(soft-CAP: blocked/hold/do-not-merge/needs-human, or every linked open PR on "
                      f"needs-verify|verify-approved, stops counting toward the CAP without removing `grok`; "
                      f"an assigned claim with no PR idle for ORCHESTRATE_GROK_IDLE_MIN frees it too).",
                      "", f"Idempotency key: `{key}`", f"Run id: `{run_id}`", f"{GROK_ASSIGN_MARKER}{k} -->"])


AC_CHECKBOX_RE = re.compile(r"^[ \t]*[-*] \[([ xX])\]", re.M)
GROK_REARM_LABELS = ("ready", "grok")


def grok_wake_key(k, it, key_times):
    """The issue's current wake key: grok_key from its reset time, re-armed (vws-deploy#411) by a `ready` or
    `grok` labeled event after the wake with that key was posted."""
    key = grok_key(k, reset_time(it))
    for _ in range(20):
        at = key_times.get(key)
        later = [utc(e["created_at"]) for e in it.get("events", []) if at and e.get("event") == "labeled"
                 and (e.get("label") or {}).get("name") in GROK_REARM_LABELS and utc(e["created_at"]) > utc(at)]
        if not later:
            break
        key = grok_key(k, max(later))
    return key


def ac_partial_key(k, it):
    """One grok wake per partial slice: sha256 of the issue and the ticked box numbers. Ticking more
    boxes changes the key; the same boxes never post a second wake."""
    sig = ",".join(str(b["n"]) for b in ac_boxes(it.get("body") or "") if b["checked"])
    return "sha256:" + hashlib.sha256(f"grok-wake\n{k}\npartial\n{sig}".encode()).hexdigest()


def grok_slot_key(k, it, items, key_times):
    """(wake key, continuation). An ac-partial issue with boxes still open uses ac_partial_key, also
    after the woken assignee opens a PR for it: falling back to grok_wake_key there let an older merged
    PR release the slot while the new PR was still open (vws-deploy#545); anything else uses the
    ordinary grok_wake_key."""
    if ac_partial_slice(it):
        return ac_partial_key(k, it), True
    return grok_wake_key(k, it, key_times), False


def grok_release(k, it, items, woken_at, now, timeout_min, idle_min=90):
    """vws-deploy#411: why a woken Grok task no longer holds its slot, as (note kind, details), or ("", {}).
    Soft-CAP parks (before the open-PR early return): grok_parked_blocked when the issue or a linked open PR
    has blocked|hold|do-not-merge|needs-human; grok_parked_verify when every linked open PR is non-draft and
    has needs-verify|verify-approved. Labels stay (no strip — B4 rework needs `grok`). grok_task_done: a PR
    whose `Part of` names it merged after the wake, or every acceptance-criteria box in its body is ticked.
    grok_idle_expired: claimed and still assigned, no PR on its timeline or open in the snapshot, and no
    comment or assignment for idle_min minutes (done-unlinked claim); any other claim keeps the slot.
    grok_wake_expired: nothing claimed it within timeout_min of the wake. An unread timeline or wake time, or
    an open PR that is not soft-CAP parked, keeps the slot."""
    act = it.get("grok_activity")
    if act is None or not woken_at:
        return "", {}
    # Soft-CAP parks apply before the open-PR early return (decide_grok release path + grok_held).
    if grok_parked_blocked(k, items):
        return "grok_parked_blocked", {}
    if grok_parked_verify(k, items):
        return "grok_parked_verify", {}
    if grok_open_pr(k, items):  # a linked PR still open is work in flight, whatever merged before it
        return "", {}
    t0 = utc(woken_at)
    merged = [p for p in act.get("prs", []) if p.get("merged_at") and utc(p["merged_at"]) > t0]
    if merged:
        return "grok_task_done", {"reason": "pr_merged", "pr": merged[0]["key"]}
    boxes = AC_CHECKBOX_RE.findall(it.get("body") or "")
    if boxes and " " not in boxes:
        return "grok_task_done", {"reason": "ac_done"}
    times = [utc(t) for t in act.get("comments", []) + act.get("assigned", []) if t]
    claimed = (it.get("assignees") or act.get("prs")
               or any(t > t0 for t in times)
               or any(p["is_pr"] and p["state"] == "open" and grok_link(p) == k for p in items.values()))
    # Idle expiry only for a done-unlinked claim: still assigned, no PR ever named it, and no comment or
    # assignment for idle_min. A comment-, assignment-event- or PR-only claim keeps the slot as before (#411).
    if claimed and it.get("assignees") and not act.get("prs"):
        last = max(times) if times else t0
        if now - last >= idle_min * 60:
            return "grok_idle_expired", {"idle_min": idle_min, "last_activity": last}
    if claimed:
        return "", {}
    if now - t0 < timeout_min * 60:
        return "", {}
    return "grok_wake_expired", {"woken_at": woken_at, "timeout_min": timeout_min}


def decide_grok(snapshot, cfg, posted, repos, cycles, notes, now=None, released_before=None, released=None):
    """Rule G (vws-deploy#378). Returns the grok_wake actions. Pure. With now (vws-deploy#411), a woken task
    that grok_release frees no longer holds a slot; it is recorded in released ({repo#N: key}) and noted
    once per key (not when released_before already has it)."""
    items = snapshot["items"]
    key_times = snapshot.get("status", {}).get("key_times", {})
    allow, bad = grok_allowlist(cfg.get("ORCHESTRATE_GROK_ALLOWLIST", ""))
    notes += [{"kind": "grok_allowlist_invalid", "token": b} for b in bad]
    held, queue, woken, freed = grok_held(items), [], set(), []
    for k in sorted(items):
        it = items[k]
        labels = set(it["labels"])
        if it["state"] != "open" or "grok" not in labels:
            continue
        unit = grok_link(it) or k  # a PR and the issue it is part of are one Grok task
        key, _continuing = grok_slot_key(k, it, items, key_times)
        if key in posted:
            kind, details = ("", {}) if now is None else grok_release(
                k, it, items, key_times.get(key), now, int(num(cfg, "ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN")),
                int(float(cfg.get("ORCHESTRATE_GROK_IDLE_MIN", DEFAULTS["ORCHESTRATE_GROK_IDLE_MIN"]))))
            if kind:  # done or never claimed: the slot is free, and the same key is never posted again
                held.discard(unit)
                held.discard(k)
                freed.append(k)
                if released is not None:
                    released[k] = key
                if (released_before or {}).get(k) != key:
                    notes.append(dict({"kind": kind, "item": k, "key": key}, **details))
                continue
            held.add(unit)  # woken and still open with grok: the slot stays taken
            woken.add(k)
            continue
        if unit in held or k in held:
            continue
        # Soft-CAP parks free the CAP but must not be re-woken while still parked (keep `grok`).
        if grok_cap_parked(grok_link(it) or k, items):
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
    free = max(0, cap - len(running))  # paused by wake_silence at the plan boundary (vws-deploy#521)
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
        it = items[q[4]]
        key, continuing = grok_slot_key(q[4], it, items, key_times)
        slot = len(running) + len(actions) + 1
        # woken: the tasks this tick saw as woken, so the apply-time re-check still counts them (it
        # cannot rebuild their reset time from the bare issues list).
        owner, via = grok_bot(it, cfg)
        why = (f"{q[4]}: ac-partial, remaining acceptance criteria, Grok slot {slot} of {cap}" if continuing
               else f"{q[4]}: allowlisted ready+grok, Grok slot {slot} of {cap}")
        actions.append({"type": "grok_wake", "target": wake_target(cfg), "item": q[4], "key": key, "owner": owner,
                        "allow_assigned": continuing, "woken": sorted(woken), "released": sorted(freed),
                        "reason": why, "body": grok_wake_body(it, q[4], key, slot, cap, snapshot["run_id"], owner, via)})
    return actions


def decide(snapshot, state, cfg, now):
    """Pure."""
    notes, released = [], {}
    actions = decide_grok(snapshot, cfg, set(snapshot.get("status", {}).get("keys", [])), active_repos(snapshot),
                          find_cycles(snapshot), notes, now, state.get("grok_released", {}), released)
    return decision(actions, notes, {"grok_released": released})
