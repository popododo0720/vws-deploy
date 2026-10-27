"""Rule B: hand the next ready+deepseek tasks to DeepSeek.

At most ORCHESTRATE_DEEPSEEK_CAP in flight and one per repo, with an @deepseek-dev comment; a task whose
Depends on target is still open is never dispatched. The queue comes from rules/deepseek.py triage.
"""
from trusted_read import (
    REWORK_MARKER,
)

from .common import (
    CI_FAIL_CAP, INFRA_MAX_RETRIES, decision, num, quote, reset_time, scrub,
)
from .deepseek import (
    rework_plan, triage,
)

NAME = "dispatch"
STATE_KEYS = ()


DISPATCH_TEXT = (
    "@deepseek-dev please take this issue: one small PR per the acceptance criteria above. "
    "Small diff, no unrelated changes, no closing keywords."
)
# Rework (vws-deploy#373): a verify FAIL on the current head of a DeepSeek PR is re-dispatched on the
# same branch with the verdict quoted. The marker keys it to the head, so one head is reworked once.
REWORK_TEXT = (
    "@deepseek-dev please rework this PR: push new commits to this branch (no force-push, no new PR) "
    "that fix every blocking point of the verify verdict below. Small diff, no unrelated changes, no closing keywords."
)


def ci_fail_text(body):
    """The human part of a CI-failure comment (failed jobs, log tails): no marker line, no JSON record,
    and no HTML comment opener, so a log line cannot add an orchestrator marker."""
    lines = (body or "").replace("\r\n", "\n").split("\n")[1:]
    end = next((i for i, ln in enumerate(lines) if ln.strip() == "```json"), len(lines))
    return scrub("\n".join(lines[:end]).strip()).replace("<!--", "&lt;!--")


def rework_body(pr, v, cf=None):
    parts, keys = [REWORK_TEXT], ""
    if v:
        parts.append(f"Verify verdict: {v['url']} (head `{pr['head_sha'][:7]}`)\n\n{quote(scrub(v['body']))}")
        keys += f" verdict={v['id']}"
    if cf:
        parts.append(f"CI failure: {cf['url']} (head `{pr['head_sha'][:7]}`, failing head {cf['n']} of {CI_FAIL_CAP})"
                     f"\n\n{quote(ci_fail_text(cf['body']))}")
        keys += f" ci={cf['id']}"
    parts.append(f"{REWORK_MARKER}{pr['head_sha']}{keys} -->")
    return "\n\n".join(parts)


def decide(snapshot, state, cfg, now):
    """Pure."""
    actions, notes = [], []
    items = snapshot["items"]
    cap = int(num(cfg, "ORCHESTRATE_DEEPSEEK_CAP"))
    t = triage(snapshot, state, cfg, now)
    queue, in_flight, slots_used = t["queue"], t["in_flight"], t["slots_used"]
    scope = cfg.get("ORCHESTRATE_SCOPE", "")
    if scope:
        queue = [q for q in queue if q[4] == scope]
    if in_flight:
        notes.append({"kind": "in_flight", "targets": in_flight})
    # Dependent tasks never run together without an extra guard: a queued task has every Depends on
    # target met (closed as completed or merged), while an in-flight or same-tick target is an open item,
    # so its dependents are blocked above.
    # dsh runs one task per repo at a time and queues the rest, and a queued task could hit the 3h
    # timeout and count as a failure. So one DeepSeek task per repo: a repo with a task in flight or
    # picked in this tick is skipped, and the slot goes to the next task from another repo.
    busy = {items[k]["repo"] for k in in_flight}
    free = max(0, cap - slots_used)
    if cfg.get("ORCHESTRATE_DISPATCH", "on") == "off":
        free = 0  # pause switch: no new dispatches; in-flight tracking and merges carry on, wakes go silent
    picked, seen = 0, set()
    for q in sorted(queue):
        if picked >= free:
            break
        if q[4] in seen:
            continue  # a PR queued by both itself and its issue
        seen.add(q[4])
        if q[2] in busy:
            notes.append({"kind": "repo_busy", "target": q[4], "repo": q[2]})
            continue
        busy.add(q[2])
        picked += 1
        if q[0] == 1:
            pr = items[q[4]]
            plan = rework_plan(q[6], items[q[6]], items, reset_time(items[q[6]]))
            v, cf = plan["verdict"], plan["ci"]
            why = f"verify FAIL {v['url']}" if v else f"CI failure {cf['url']}"
            actions.append({"type": "dispatch", "target": q[4], "owner": q[6], "head": pr["head_sha"],
                            "reason": f"rework after {why}, DeepSeek slot {slots_used + picked} of {cap}",
                            "body": rework_body(pr, v, cf)})
            continue
        retry_line = f"Retry {q[5]}/{INFRA_MAX_RETRIES} after provider error.\n\n" if q[5] else ""
        actions.append({"type": "dispatch", "target": q[4],
                        "reason": (f"infra retry {q[5]}/{INFRA_MAX_RETRIES}, " if q[5] else "next in queue, ")
                        + f"DeepSeek slot {slots_used + picked} of {cap}",
                        "body": f"{DISPATCH_TEXT}\n\n{retry_line}<!-- orchestrate:dispatch -->"})
    return decision(actions, notes)
