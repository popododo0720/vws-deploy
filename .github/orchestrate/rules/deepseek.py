"""Rules B and C, per item: DeepSeek failures, infra retries, dependencies and the dispatch queue.

C counts DeepSeek failures (Failed report, 3h timeout, Done PR closed unmerged); at DEEPSEEK_MAX_FAILURES
the task swaps to grok. A Failed report that shows a provider error (INFRA_ERROR_RE) is an infra failure
instead: never counted, re-mentioned ORCHESTRATE_INFRA_RETRY_DELAY_MIN after the report, at most
INFRA_MAX_RETRIES times; then one entry in the status issue body and the item gets blocked. B builds the
dispatch queue here (triage); rules/dispatch.py hands out the slots.
"""
import hashlib
import re

import trusted_read as tr
from trusted_read import (
    FAILURE_MARKER, rework_kind,
)

from .common import (
    EXCLUSIONS_KEY, INFRA_MAX_RETRIES, active_repos, ci_fail_head, classify_comment, conflict_only, decision,
    dep_state, find_cycles, head_verdict, iso, num, open_pr_links, owned_labels, parse_deps, ready_time,
    reset_time, rework_prs, scrub, status_keys, status_target, utc, vws_pr_key,
)

NAME = "deepseek"
STATE_KEYS = ()


# A [deepseek-dev] Failed report is a provider (infra) failure, not a task failure, when its body has
# the last dsh line of the "Last output" block, `dsh: SERVER: 503: {...}` or `dsh: RATE_LIMIT: 429: ...`
# (the real reports on vws-proto#51, comments 6310291529 and 6311725351; the poller's format per the VM
# owner), or the first-line form `Failed for <url>: provider 503 (SERVER)`. A plain "dsh exited with
# code 1" without that line is a task failure. Bare HTTP statuses are never matched: a task's own test
# output can print them.
INFRA_ERROR_RE = re.compile(
    r"^dsh: (?:RATE_LIMIT|SERVER): \d{3}:"
    r"|^\[deepseek-dev\] Failed for \S+: provider \d{3} \((?:RATE_LIMIT|SERVER)\)",
    re.M)


def infra_error(body):
    """The provider error a Failed report shows, as one redacted line ('' = not an infra failure)."""
    text = body or ""
    if not INFRA_ERROR_RE.search(text):
        return ""
    for line in reversed(text.splitlines()):
        if INFRA_ERROR_RE.search(line):
            return scrub(line.strip())[:200]
    return ""


def dispatch_history(item, now, cfg, exclusions):
    """Per dispatch on one item: outcome in in_flight | done | review | failed | infra | excluded.
    infra: a Failed report that shows a provider error; it never counts toward the grok swap."""
    timeout = num(cfg, "DEEPSEEK_TIMEOUT_HOURS") * 3600
    comments = sorted(item.get("comments", []), key=lambda c: (c["created_at"], c["id"]))
    dispatches, reports = [], {}
    for c in comments:
        kind = classify_comment(c["body"])
        if not kind:
            continue
        if kind[0] == "dispatch":
            dispatches.append(c)
        elif kind[0] == "report":
            reports.setdefault(kind[1]["for"], (c, kind[1]))
    out = []
    for d in dispatches:
        rep = reports.get(d["url"])
        h = {"url": d["url"], "at": utc(d["created_at"]), "report": rep[0]["url"] if rep else ""}
        if rep is None:
            h["outcome"] = "in_flight" if now - h["at"] < timeout else "failed"
            h["kind"] = "timeout"
        elif rep[1]["result"] == "failed":
            h["outcome"], h["kind"] = "failed", "failed_report"
            err = infra_error(rep[0]["body"])
            if err and not (h["url"] in exclusions or h["report"] in exclusions):
                # An exclusions.tsv entry still wins: that attempt is ignored, not retried with a delay.
                h["outcome"], h["kind"], h["error"] = "infra", "provider_error", err
                h["report_at"] = utc(rep[0]["created_at"])
        elif rep[1]["pr"] and not vws_pr_key(rep[1]["pr"]):
            # The PR cannot be read or checked (outside popododo0720/vws-*): one failed attempt, so the
            # task is retried or moved to grok instead of waiting for review forever.
            h["outcome"], h["kind"], h["pr"] = "failed", "pr_outside_vws", rep[1]["pr"]
        else:
            pr = item.get("report_prs", {}).get(rep[1]["pr"], {})
            if pr.get("merged"):
                h["outcome"], h["kind"] = "done", "merged"
            elif pr.get("state") == "closed":
                h["outcome"], h["kind"] = "failed", "closed_unmerged"
            elif rep[1]["pr"]:
                h["outcome"], h["kind"], h["pr"] = "review", "pr_open", rep[1]["pr"]
            else:
                h["outcome"], h["kind"] = "done", "no_pr"
        if h["outcome"] == "failed" and (h["url"] in exclusions or h["report"] in exclusions):
            h["outcome"] = "excluded"
        out.append(h)
    return out


def infra_status_body(target, hist, key, run_id):
    last = hist[-1]
    lines = [FAILURE_MARKER,
             f"vws-orchestrate: {target} failed {len(hist)} times on provider errors; "
             f"{INFRA_MAX_RETRIES} infra retries are used up. The item is now `blocked` for a human.", "",
             "Kind: infra-retries-exhausted", f"Item: {target}", f"Last report: {last['report']}",
             f"Last error (redacted): {scrub(last.get('error', '')).replace(chr(10), ' ')}", "",
             "These failures did not count toward the grok swap. To retry, fix the provider and comment "
             "`/orchestrate reset-failures` on the item (the orchestrator then removes its `blocked`).",
             f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def infra_key(target, report_url):
    return "sha256:" + hashlib.sha256(f"infra-exhausted:{target}:{report_url}".encode()).hexdigest()


def rework_plan(k, it, items, since):
    """Rework state of the item's DeepSeek PR (vws-deploy#373, #412). counted: the reworks already
    dispatched since the last reset plus a pending FAIL, each one failure toward the grok swap. A head
    with a verify FAIL and a CI failure is one rework and one count. state: pending | done (this head
    was reworked already) | conflict_only (left to eggbot) | None (no FAIL on the head)."""
    plan = {"target": "", "verdict": None, "ci": None, "state": None, "counted": [], "ci_skipped": []}
    for pk in rework_prs(k, it, items):
        pr = items[pk]
        dispatches = [c for c in pr.get("comments", []) if (classify_comment(c["body"]) or ("",))[0] == "dispatch"]
        plan["counted"] += [{"url": c["url"], "kind": rework_kind(c["body"]), "report": ""} for c in dispatches
                            if tr.is_rework(c["body"]) and utc(c["created_at"]) > since]
        if plan["state"]:
            continue
        v = head_verdict(pr)
        if v and v["result"] != "fail":
            v = None
        cf, why = ci_fail_head(pr, it)
        if cf and why:
            plan["ci_skipped"].append({"kind": "ci_fail_skipped", "target": pk, "comment": cf["url"],
                                       "reason": why, "n": cf["n"]})
            cf = None
        if v and conflict_only(v["body"]):
            if not cf:
                plan["target"], plan["verdict"], plan["state"] = pk, v, "conflict_only"
                continue
            v = None  # the CI failure is still reworked; the conflict stays with eggbot
        if not v and not cf:
            continue
        plan["target"], plan["verdict"], plan["ci"] = pk, v, cf
        first = min(x["at"] for x in (v, cf) if x)
        if any(tr.reworked_head(c["body"], pr["head_sha"]) or c["created_at"] >= first for c in dispatches):
            plan["state"] = "done"
        else:
            plan["state"] = "pending"
            plan["counted"].append({"url": (v or cf)["url"], "kind": "verify_fail" if v else "ci_fail", "report": ""})
    return plan


def stale_in_review_actions(items, actions, linked, now, cfg, exclusions):
    """Remove in-review from an open issue that no open PR names, even with no ownership journal.
    A Done report whose PR is still open keeps the label: that PR may not say Part of yet, and the
    next tick would add the label back. A pull request's own in-review label is left alone (vws-deploy#560)."""
    planned = {a["target"] for a in actions if a.get("type") == "remove_label" and a.get("label") == "in-review"}
    out = []
    for k in sorted(items):
        it = items[k]
        if it.get("state") != "open" or it.get("is_pr") or "in-review" not in it.get("labels", []):
            continue
        if k in linked or k in planned:
            continue
        hist = dispatch_history(it, now, cfg, exclusions)
        latest = hist[-1] if hist else None
        if latest and latest["outcome"] == "review":
            continue
        out.append({"type": "remove_label", "target": k, "label": "in-review",
                    "reason": "no open Part of, Closes or Fixes PR"})
    return out


def triage(snapshot, state, cfg, now):
    """Pure. Returns {"actions", "notes", "queue", "in_flight", "slots_used"} for every open item."""
    actions, notes = [], []
    repos = active_repos(snapshot)
    items = snapshot["items"]
    exclusions = cfg[EXCLUSIONS_KEY]
    owned = owned_labels(state, items)
    # Open PRs that name an issue keep its in-review label (vws-deploy#560).
    linked = open_pr_links(items)
    max_fail = int(num(cfg, "DEEPSEEK_MAX_FAILURES"))
    infra_delay = int(num(cfg, "ORCHESTRATE_INFRA_RETRY_DELAY_MIN")) * 60
    posted = status_keys(snapshot, state)
    cycles = find_cycles(snapshot)

    # Rules B and C
    in_flight, queue, slots_used = [], [], 0
    for k in sorted(items):
        it = items[k]
        if it["state"] != "open":
            continue
        hist = dispatch_history(it, now, cfg, exclusions)
        for h in hist:
            if h["kind"] == "pr_outside_vws":
                notes.append({"kind": "report_pr_outside_vws", "target": k, "pr": h["pr"]})
        running = sum(1 for h in hist if h["outcome"] == "in_flight")
        if running:
            in_flight.append(k)
            slots_used += running
        labels = set(it["labels"])
        if ("grok" in labels and "deepseek" not in labels and "in-review" in labels and k in owned["in-review"]
                and k not in linked):
            # Moved to grok earlier (or a swap stopped before this label call): DeepSeek review is over.
            # An open PR that still names the issue keeps in-review (vws-deploy#560).
            actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": "task moved to grok"})
        if "deepseek" not in labels or "ready" not in labels:
            continue
        since = reset_time(it)
        rw = rework_plan(k, it, items, since)
        notes += rw["ci_skipped"]
        counted = [h for h in hist if h["outcome"] == "failed" and h["at"] > since] + rw["counted"]
        latest = hist[-1] if hist else None
        if len(counted) >= max_fail:
            marker = any((classify_comment(c["body"]) or ("",))[0] == "marker"
                         and "orchestrate:swap" in c["body"] and utc(c["created_at"]) > since
                         for c in it.get("comments", []))
            lines = "\n".join(f"- {h['url']} ({h['kind']}{', report ' + h['report'] if h['report'] else ''})" for h in counted)
            # "grok" may already be there when an earlier swap stopped between its two label calls.
            actions.append({"type": "swap_label", "target": k, "reason": f"{len(counted)} counted DeepSeek failures",
                            "remove": "deepseek", "add": "" if "grok" in labels else "grok", "comment": not marker,
                            "body": f"Moving this task from DeepSeek to grok after {len(counted)} failed attempts:\n"
                                    f"{lines}\n\n<!-- orchestrate:swap -->"})
            if "in-review" in labels and k in owned["in-review"] and k not in linked:
                actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": "task moved to grok"})
            continue
        if "grok" in labels:
            continue
        # Infra failures (provider 429/503) since the last reset: never counted above. While the latest
        # attempt is one, re-mention it after the delay, at most INFRA_MAX_RETRIES times; the count
        # comes from the comments, so a restart or rerun sees the same number.
        infra = [h for h in hist if h["outcome"] == "infra" and h["at"] > since]
        retry, exhausted = 0, False
        if latest and latest["outcome"] == "infra" and latest["at"] > since:
            if len(infra) > INFRA_MAX_RETRIES:
                exhausted = True
                notes.append({"kind": "infra_exhausted", "target": k, "failures": len(infra), "report": latest["report"]})
                key = infra_key(k, latest["report"])
                if key not in posted:
                    posted.add(key)
                    actions.append({"type": "infra_status", "target": status_target(cfg), "key": key, "item": k,
                                    "reason": f"{k}: infra retries exhausted",
                                    "body": infra_status_body(k, infra, key, snapshot["run_id"])})
            else:
                due = latest["report_at"] + infra_delay
                if now < due:
                    notes.append({"kind": "infra_wait", "target": k, "retry": len(infra), "due": iso(due),
                                  "error": latest.get("error", "")})
                else:
                    retry = len(infra)
        deps, bad = parse_deps(it.get("body"), it["repo"])
        unmet = [d for d in deps if dep_state(d, snapshot, repos) != "met"]
        for d in unmet:
            notes.append({"kind": "dep_unmet", "target": k, "dep": d, "state": dep_state(d, snapshot, repos)})
        for b in bad:
            notes.append({"kind": "dep_unparsed", "target": k, "token": b})
        if k in cycles:
            notes.append({"kind": "dep_cycle", "target": k})
        blocked = bool(unmet or bad or k in cycles or exhausted)
        if blocked and "blocked" not in labels:
            actions.append({"type": "add_label", "target": k, "label": "blocked",
                            "reason": "infra retries exhausted" if exhausted else "unmet dependency"})
        if not blocked and "blocked" in labels and k in owned["blocked"]:
            actions.append({"type": "remove_label", "target": k, "label": "blocked", "reason": "dependencies met"})
        if latest and latest["outcome"] == "review" and "in-review" not in labels:
            actions.append({"type": "add_label", "target": k, "label": "in-review", "reason": f"DeepSeek PR {latest.get('pr')} open"})
        if (latest and latest["outcome"] in ("done", "failed", "infra") and "in-review" in labels
                and k in owned["in-review"] and k not in linked):
            actions.append({"type": "remove_label", "target": k, "label": "in-review", "reason": f"DeepSeek PR {latest['kind']}"})
        if rw["state"]:
            # A FAIL on the PR head replaces the issue-style dispatch with a rework of that PR.
            pr = items[rw["target"]]
            pr_running = any(h["outcome"] == "in_flight" for h in dispatch_history(pr, now, cfg, exclusions))
            if rw["state"] == "pending" and not (running or pr_running or blocked or "blocked" in labels or pr.get("draft")):
                queue.append((1, ready_time(it), pr["repo"], pr["number"], rw["target"], 0, k))
            else:
                notes.append({"kind": "rework_skipped", "target": rw["target"], "verdict": (rw["verdict"] or rw["ci"])["url"],
                              "reason": "in flight or blocked" if rw["state"] == "pending" else rw["state"]})
            continue
        # A running target is never dispatched again, even after a failure reset.
        eligible = (not running and not blocked and "blocked" not in labels and not (it["is_pr"] and it.get("draft"))
                    and (latest is None or latest["outcome"] in ("failed", "excluded") or latest["at"] <= since
                         or retry > 0))
        if eligible:
            prio = 0 if "priority/blocker" in labels else 2
            queue.append((prio, ready_time(it), it["repo"], it["number"], k, retry, k))
    # After the owned removals above, so a label already planned is not removed twice (vws-deploy#560).
    actions += stale_in_review_actions(items, actions, linked, now, cfg, exclusions)
    return {"actions": actions, "notes": notes, "queue": queue, "in_flight": in_flight, "slots_used": slots_used}


def decide(snapshot, state, cfg, now):
    """Pure. The label, swap and infra-status actions and notes of triage."""
    t = triage(snapshot, state, cfg, now)
    return decision(t["actions"], t["notes"])
