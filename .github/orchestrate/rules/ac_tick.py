"""Rule T: tick the acceptance-criteria boxes a merged PR covers (vws-deploy#486)."""
import json

import trusted_read as tr

from .common import (
    AC_TICK_LABELS, ac_recoverable, ac_tick_body, decision, iso, num, parse_ac, utc,
)

NAME = "ac_tick"
STATE_KEYS = ("ac_tick_since", "ac_ticked")


def decide_ac_ticks(snapshot, new_state, now, cfg, notes):
    """Pure: one ac_tick action per PR merged since the rule started whose `Part of` issue has the boxes its
    `AC: <n>` lines name (vws-deploy#486), when vws-verify approved the merged head and the body was not edited
    since (ac_unverified / ac_body_edited otherwise). Oldest merge first. new_state keeps the start time and the PRs
    already handled (marker seen or applied), pruned after the comment window."""
    if cfg.get("ORCHESTRATE_AC_TICK", "apply") == "off":
        return []
    if "ac_tick_since" not in new_state:
        new_state["ac_tick_since"] = iso(now)  # no back-fill: only merges after the first tick count
        notes.append({"kind": "ac_tick_start", "since": new_state["ac_tick_since"]})
        return []
    since = utc(new_state["ac_tick_since"])
    floor = now - int(num(cfg, "ORCHESTRATE_COMMENT_WINDOW_DAYS")) * 86400
    done = new_state.setdefault("ac_ticked", {})
    for k in [k for k, at in done.items() if utc(at) < floor]:
        del done[k]
    actions = []
    for m in sorted(snapshot.get("ac_merged", []), key=lambda m: (m["merged_at"], m["pr"])):
        at = utc(m["merged_at"])
        if m["pr"] in done or at < since or at < floor:
            continue
        note = {"target": m["issue"] or "", "pr": m["pr"]}
        if m.get("error"):
            notes.append(dict(note, kind="ac_read_error", error=m["error"]))
            continue
        if not m["issue"]:
            notes.append(dict(note, kind="ac_no_part_of"))
            done[m["pr"]] = m["merged_at"]
            continue
        ac = parse_ac(m["body"])
        kind = "ac_unparsed" if ac["error"] else "ac_none" if not ac["boxes"] else ""
        if not kind and not m.get("verified_at"):
            kind = "ac_unverified"  # no trusted vws-verify approval of the merged head: no body is trusted
        if not kind and tr.body_edited(m, utc):
            kind = "ac_body_edited"  # edited after the verdict (or the merge): may retarget, so tick nothing
        if not kind and m.get("marker_seen"):
            kind = "ac_tick_done"
        recover = not kind and m["issue_state"] != "open" and ac_recoverable(
            m["issue_state"], m.get("issue_state_reason"), m["issue_body"], ac["boxes"], m["url"], m["merge_sha"])
        if not kind and m["issue_state"] != "open" and not recover:
            kind = "ac_issue_closed"
        # The boxes come from the body vws-verify approved (vws-deploy#439 class 1); kind is '' only then.
        p = ac_tick_body(m["issue_body"], parse_ac(tr.verified_body(m, utc))["boxes"], m["url"], m["merge_sha"]) if not kind else None
        if p and p["error"]:
            kind = p["error"]
        if kind:
            notes.append(dict(note, kind=kind, **({"reason": ac["error"] or ac["none"]} if kind in ("ac_unparsed", "ac_none") else {})))
            done[m["pr"]] = m["merged_at"]
            continue
        labels, close = set(m["issue_labels"]), not p["open"]
        repo = m["issue"].split("#")[0]
        partial_ok = "ac-partial" in snapshot.get("labels", {}).get(repo, [])
        if not close and not partial_ok:
            notes.append(dict(note, kind="ac_label_missing", label="ac-partial"))
        actions.append({"type": "ac_tick", "target": m["issue"], "pr": m["pr"], "pr_url": m["url"],
                        "merge_sha": m["merge_sha"], "merged_at": m["merged_at"], "boxes": ac["boxes"], "close": close,
                        "partial_label": partial_ok, **({"recover": True} if recover else {}),
                        "labels_add": [] if close or "ac-partial" in labels or not partial_ok else ["ac-partial"],
                        "labels_remove": [x for x in AC_TICK_LABELS if x in labels and (close or x == "ac-pending")],
                        "reason": f"{m['pr']} merged {m['merge_sha'][:7]}: AC {', '.join(map(str, ac['boxes']))}"
                                  + (", closed, marker missing" if recover else ", last open box" if close
                                     else f", {len(p['open'])} left")})
    return actions


def decide(snapshot, state, cfg, now):
    """Pure."""
    notes = []
    new_state = json.loads(json.dumps(state))
    actions = decide_ac_ticks(snapshot, new_state, now, cfg, notes)
    return decision(actions, notes, {k: new_state[k] for k in STATE_KEYS if k in new_state})
