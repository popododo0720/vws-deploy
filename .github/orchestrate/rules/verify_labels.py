"""Rule V: needs-verify and verify-approved labels on every vws-* PR (vws-deploy#487).

Registered last: label repairs take the writes left under the cap, and a PR missed this tick converges on
the next.
"""
from .common import (
    NEEDS_VERIFY, OPT_OUT_LABELS, decision, head_verdict,
)

NAME = "verify_labels"
STATE_KEYS = ()


def verify_labels_wanted(it):
    """(needs-verify, verify-approved, why) for one PR collect read (vws-deploy#487 table). Pure.
    verify-approved True means "may stay"; decide_verify_labels never adds it (see there)."""
    if it["verify_ci"] == "ineligible":
        return False, False, "draft or not into main"
    if it.get("verify_check_failed"):
        # The newest vws-verify check on this head failed: it beats an earlier PASS review on the same head.
        return False, False, f"vws-verify check failed on head {it['head_sha'][:7]}"
    v = head_verdict(it)
    if v and v["result"] == "pass":
        return False, True, f"PASS on head {it['head_sha'][:7]} {v['url']}"
    if v:
        return False, False, f"FAIL on head {it['head_sha'][:7]} {v['url']}"
    if it["verify_ci"] == "green":
        return True, False, f"checks green on head {it['head_sha'][:7]}, no verdict yet"
    return False, False, f"checks {it['verify_ci']} on head {it['head_sha'][:7]}"


def decide_verify_labels(items, cfg, notes):
    """vws-deploy#487: needs-verify and verify-approved as one rule over every vws-* repo (verify-queue.yml
    is retired). Only PRs collect read (verify_ci set) are touched; an unreadable state writes nothing. A
    blocked, held or do-not-merge PR gets removals only. verify-approved is only ever removed: verify-gate
    rejects one added by the PR author or pusher, and this rule writes as the repo owner (vws-verify adds it,
    review on vws-deploy#488). Longest-waiting PRs first, so a write cap that
    stops this tick short serves them first; the rest converge on later ticks. Pure."""
    if cfg.get("ORCHESTRATE_VERIFY_LABELS", "on") != "on":
        return []
    out = []
    for k, it in sorted(items.items(), key=lambda kv: (kv[1].get("created_at") or "", kv[0])):
        if not it.get("is_pr") or it.get("verify_ci") not in ("green", "pending", "red", "ineligible"):
            continue
        labels = set(it["labels"])
        want_nv, want_va, why = verify_labels_wanted(it)
        parked = sorted(labels & set(OPT_OUT_LABELS))
        if parked:
            want_nv = False  # a parked PR does not wait for verify (AC7)
        for label, want in ((NEEDS_VERIFY, want_nv), ("verify-approved", want_va)):
            if want and label not in labels:
                if label == "verify-approved":
                    continue  # vws-verify adds it; an orchestrator-added one would fail verify-gate
                if parked:
                    notes.append({"kind": "verify_label_parked", "target": k, "label": label, "parked": parked})
                    continue
                out.append({"type": "add_label", "target": k, "label": label, "head": it["head_sha"],
                            "reason": f"verify labels: {why}"})
            elif not want and label in labels:
                extra = f" ({', '.join(parked)})" if parked and label == NEEDS_VERIFY else ""
                out.append({"type": "remove_label", "target": k, "label": label, "head": it["head_sha"],
                            "reason": f"verify labels: {why}{extra}"})
    return out


def decide(snapshot, state, cfg, now):
    """Pure."""
    notes = []
    return decision(decide_verify_labels(snapshot["items"], cfg, notes), notes)
