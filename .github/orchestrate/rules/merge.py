"""Rule D: serialized squash merges of PRs that passed vws-verify (vws-deploy#346, #397).

At most one per repo per tick, only while main's push CI is green for the current main head; after a merge
the repo is locked until the push CI for the new main head is green (red stops merges there and adds one
entry to the status issue body). PRs that touch SERIAL_MERGE_RE paths merge one at a time across repos.
A verify-approved head whose newest repo-guard run is a startup_failure gets one PR body touch for a fresh
run; a second startup_failure is reported instead of touched again (vws-deploy#446).
"""
import hashlib
import json
import re

import trusted_read as tr
from trusted_read import (
    FAILURE_MARKER,
)

from .common import (
    MERGEABLE_OK, OPT_OUT_LABELS, PART_OF_HINT, PART_OF_RE, VERIFY_APP_ID, VERIFY_CHECK, active_repos, agent_of,
    decision, dep_state, head_check_reasons, num, owner_repo_part_of, parse_deps, status_keys, status_target,
    utc, wake_target,
)

NAME = "merge"
STATE_KEYS = ("merge_locks", "merge_attempts", "guard_touches")


# Rule D serial set (vws-deploy#397): PRs touching these merge one at a time across all repos, oldest approval first.
# vws-deploy#501: the rule modules are what orchestrate.py was, so they stay serial too (rules/*.py, one level).
SERIAL_MERGE_RE = re.compile(r"^(?:\.github/orchestrate/orchestrate\.py|\.github/orchestrate/rules/[^/]+\.py"
                             r"|\.github/workflows/repo-guard[^/]*\.ya?ml|AGENTS\.md)$")
# vws-deploy#446: the repo-guard caller and how long a touch waits for the fresh run before it is reported.
GUARD_WORKFLOW = ".github/workflows/repo-guard.yml"
GUARD_TOUCH_WAIT = 30 * 60
CLOSING_RE = re.compile(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b:?\s*([\w.-]+/[\w.-]+)?#[0-9]+")


def merge_reasons(it, ctx, snapshot, repos, cfg, attempts):
    """Every reason PR item `it` cannot be merged this tick ([] = eligible). Pure.
    ctx is the repo's merge context from collect (main head, required checks, lock inputs)."""
    k = f"{it['repo']}#{it['number']}"
    labels = set(it["labels"])
    info = it.get("merge_info")
    reasons = []
    if it.get("draft"):
        reasons.append("draft")
    if it.get("base") != "main":
        reasons.append("base_not_main")
    if "verify-approved" not in labels:
        reasons.append("no_verify_approved")
    if labels & set(OPT_OUT_LABELS) or tr.opted_out(it.get("body")) or k in (status_target(cfg), wake_target(cfg)):
        reasons.append("opted_out")
    if reasons:
        return reasons  # the cheap rules decide; nothing else was read for this PR
    if not info:
        return ["merge_info_unreadable"]
    if info.get("head_sha") != it.get("head_sha") or not re.fullmatch(r"[0-9a-f]{40}", info.get("head_sha") or ""):
        reasons.append("head_moved_during_collect")
    if info.get("state") != "open" or info.get("merged"):
        reasons.append("not_open")
    if info.get("draft") or info.get("base") != "main":
        reasons.append("draft_or_base_changed")
    if info.get("mergeable") is None:
        reasons.append("mergeable_unknown")
    elif info.get("mergeable") is False:
        reasons.append("conflicts")
    elif info.get("mergeable_state") not in MERGEABLE_OK:
        reasons.append(f"mergeable_state:{info.get('mergeable_state')}")
    body = it.get("body") or ""
    if not PART_OF_RE.search(body):
        reasons.append("part_of_owner_repo" if owner_repo_part_of(body) else "no_part_of")
    if CLOSING_RE.search(info.get("title") or ""):
        reasons.append("closing_keyword_in_title")
    deps, bad = parse_deps(it.get("body"), it["repo"])
    reasons += [f"dep_unmet:{d}" for d in deps if dep_state(d, snapshot, repos) != "met"]
    reasons += [f"dep_unparsed:{b}" for b in bad]
    reasons += head_check_reasons(info, ctx.get("required"))
    if attempts.get(f"{k}@{it.get('head_sha')}", 0) >= int(num(cfg, "ORCHESTRATE_MERGE_MAX_ATTEMPTS")):
        reasons.append("merge_attempts_exhausted")
    return reasons


def merge_status_body(kind, repo, sha, detail, key, run_id):
    lines = [FAILURE_MARKER,
             f"vws-orchestrate: {kind} in {repo}; merges in {repo} are stopped.", "",
             f"Kind: merge-{kind.replace(' ', '-')}", f"Repo: {repo}", f"Main commit: `{sha}`", f"Detail: {detail}", "",
             "Merges in this repo resume by themselves once a newer main head has a green push CI run.",
             f"Idempotency key: `{key}`", f"Run id: `{run_id}`"]
    return "\n".join(lines)


def status_key(kind, repo, sha):
    return "sha256:" + hashlib.sha256(f"merge-{kind}:{repo}:{sha}".encode()).hexdigest()


def serial_merge_pr(info):
    """True when the PR touches a serial-merge path (SERIAL_MERGE_RE). Unread files count as serial."""
    files = (info or {}).get("files")
    return files is None or any(SERIAL_MERGE_RE.match(f or "") for f in files)


def verify_approved_at(info):
    """When vws-verify approved the PR head: the newest successful vws-verify check run on it ('' = unknown)."""
    runs = [r for r in (info or {}).get("checks", []) if r.get("name") == VERIFY_CHECK and r.get("app_id") == VERIFY_APP_ID
            and r.get("head_sha") == (info or {}).get("head_sha") and r.get("conclusion") == "success"]
    return max((r.get("completed_at") or r.get("started_at") or "" for r in runs), default="")


def guard_startup(k, it, touches, now):
    """vws-deploy#446: what to do about a repo-guard startup_failure on verify-approved PR `it`. Pure.
    Returns (step, why): "touch" (one PR body edit starts a fresh repo-guard run), "report" (the one touch
    did not help; never a second touch) or "" (no startup_failure is the newest repo-guard run, or the
    touched run has not started yet). touches is state["guard_touches"]: {"repo#N@head": {"at": epoch}}."""
    info, head = it.get("merge_info") or {}, it.get("head_sha")
    runs = sorted((r for r in info.get("guard_runs", []) if r.get("head_sha") == head),
                  key=lambda r: (r.get("created_at") or "", r.get("id", 0)))
    if not runs or runs[-1].get("conclusion") != "startup_failure":
        return "", ""  # a later run (any outcome, or still running) means repo-guard started again
    last = runs[-1]
    rec = touches.get(f"{k}@{head}")
    if rec is None and not tr.guard_touched(it.get("body"), head):
        return "touch", f"repo-guard run {last['id']} ended in startup_failure"
    if rec is None:
        return "report", f"repo-guard run {last['id']} ended in startup_failure after a touch (state lost)"
    if utc(last["created_at"]) > rec["at"]:
        return "report", f"repo-guard run {last['id']} ended in startup_failure again after the touch"
    if now - rec["at"] >= GUARD_TOUCH_WAIT:
        return "report", f"no repo-guard run {GUARD_TOUCH_WAIT // 60} min after the touch"
    return "", "waiting for the repo-guard run the touch started"


def guard_status_body(k, head, why, key, run_id):
    return "\n".join([FAILURE_MARKER, f"vws-orchestrate: repo-guard startup_failure on {k} at `{head}`.", "",
                      "Kind: repo-guard-startup-failure", f"PR: {k}", f"Head: `{head}`", f"Detail: {why}", "",
                      "The one automatic touch is used; the PR is labeled needs-human (vws-deploy#446).",
                      f"Idempotency key: `{key}`", f"Run id: `{run_id}`"])


def guard_steps(k, it, reasons, touches, posted, now, cfg, run_id):
    """Rule D's #446 actions and note for one PR read in a readable repo: (actions, notes)."""
    if not it.get("merge_info") or set(reasons) & {"draft", "base_not_main", "no_verify_approved", "opted_out"}:
        return [], []
    step, why = guard_startup(k, it, touches, now)
    if not why:
        return [], []
    head = it["head_sha"]
    actions = []
    notes = [{"kind": "guard_startup_failure", "target": k, "head": head[:7], "step": step or "wait", "detail": why}]
    if step == "touch":
        actions.append({"type": "guard_touch", "target": k, "repo": it["repo"], "head": head, "reason": why})
    elif step == "report":
        key = status_key("repo-guard startup_failure", k, head)
        if key not in posted:
            posted.add(key)
            actions.append({"type": "merge_status", "target": status_target(cfg), "key": key, "repo": it["repo"],
                            "reason": f"repo-guard startup_failure on {k} at {head[:7]}: {why}",
                            "body": guard_status_body(k, head, why, key, run_id)})
        if "needs-human" not in it["labels"]:
            actions.append({"type": "add_label", "target": k, "label": "needs-human", "head": head,
                            "reason": "repo-guard startup_failure after the one touch (vws-deploy#446)"})
    return actions, notes


def decide_merges(snapshot, new_state, now, cfg, repos):
    """Rule D. Updates new_state["merge_locks"] / ["merge_attempts"]; returns (actions, notes). Pure."""
    actions, notes = [], []
    if cfg.get("ORCHESTRATE_MERGE", "dry-run") == "off":
        return actions, notes
    items = snapshot["items"]
    merge = snapshot.get("merge", {})
    posted = status_keys(snapshot, new_state)
    locks = new_state.setdefault("merge_locks", {})
    attempts = new_state.setdefault("merge_attempts", {})
    # Only apply_guard_touch creates guard_touches, so a tick without a touch leaves the state as before.
    touches = new_state.get("guard_touches", {})
    open_heads = {f"{k}@{it.get('head_sha')}" for k, it in items.items() if it["is_pr"] and it["state"] == "open"}
    for book in (attempts, touches):
        for a in list(book):
            if a not in open_heads:
                del book[a]  # the PR merged, closed or moved: its attempts no longer matter
    timeout = num(cfg, "ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS") * 3600
    rows = []  # (repo, PR item, reasons, main) for every PR in a readable repo; chosen after the loop

    def comment(kind, repo, sha, detail):
        key = status_key(kind, repo, sha)
        if key in posted:
            return
        posted.add(key)
        actions.append({"type": "merge_status", "target": status_target(cfg), "key": key, "repo": repo,
                        "reason": f"{kind} in {repo} at {sha[:7]}",
                        "body": merge_status_body(kind, repo, sha, detail, key, snapshot["run_id"])})

    for repo in sorted(repos):
        ctx = merge.get(repo)
        lock = locks.get(repo)
        prs = sorted((it for it in items.values() if it["repo"] == repo and it["is_pr"] and it["state"] == "open"),
                     key=lambda it: it["number"])
        if ctx is None or ctx.get("error"):
            # Not read (no verify-approved candidate and no lock) or unreadable: nothing merges here.
            if ctx is not None:
                notes.append({"kind": "merge_repo_unreadable", "repo": repo, "error": ctx["error"]})
            elif lock:
                notes.append({"kind": "merge_lock", "repo": repo, "status": lock.get("status"), "detail": "repo not read"})
            for it in prs:
                reasons = merge_reasons(it, {}, snapshot, repos, cfg, attempts)
                notes.append({"kind": "merge_check", "target": f"{repo}#{it['number']}", "eligible": False,
                              "reasons": reasons if ctx is None else ["merge_repo_unreadable"] + reasons,
                              "head": (it.get("head_sha") or "")[:7], "main": "not read"})
            continue
        main_sha, main_ci = ctx.get("main_sha", ""), ctx.get("main_ci", "none")
        held = ""
        if lock and lock.get("status") == "merging":
            lp = ctx.get("lock_pr") or {}
            if lp.get("merged") and re.fullmatch(r"[0-9a-f]{40}", lp.get("merge_commit_sha") or ""):
                lock = dict(lock, status="waiting", merge_sha=lp["merge_commit_sha"], merged_at=lock.get("at", now))
                locks[repo] = lock
                notes.append({"kind": "merge_confirmed", "repo": repo, "pr": lock["pr"], "merge_sha": lock["merge_sha"]})
            elif lp.get("state") == "open" and not lp.get("merged"):
                notes.append({"kind": "merge_not_landed", "repo": repo, "pr": lock["pr"]})
                del locks[repo]
                lock = None
            else:
                held = "merge outcome unknown"
        if lock and lock.get("status") == "waiting":
            lci = main_ci if lock["merge_sha"] == main_sha else ctx.get("lock_ci", "none")
            if lci == "green" or (lci != "red" and ctx.get("lock_ahead") is True and main_ci == "green"):
                # Green for the squash commit, or a newer main head that contains it is already green. A
                # different main sha alone is not enough: it may be a stale read of the pre-merge head.
                notes.append({"kind": "merge_lock_released", "repo": repo, "pr": lock["pr"], "merge_sha": lock["merge_sha"],
                              "via": "merge commit" if lci == "green" else f"newer main {main_sha[:7]}"})
                del locks[repo]
                lock = None
            elif lci == "red":
                lock = dict(lock, status="red", red_sha=lock["merge_sha"])
                locks[repo] = lock
                comment("main CI red", repo, lock["merge_sha"], f"push CI failed on the squash commit of {lock['pr']}")
            else:
                held = f"push CI {lci} for {lock['merge_sha'][:7]}"
                if now - lock.get("merged_at", now) >= timeout:
                    comment("main CI wait timeout", repo, lock["merge_sha"],
                            f"no green push CI {int(timeout // 60)} min after merging {lock['pr']} (now {lci})")
        if lock and lock.get("status") == "red":
            if ctx.get("red_ahead") is True and main_ci == "green":
                notes.append({"kind": "merge_red_cleared", "repo": repo, "red_sha": lock["red_sha"], "main_sha": main_sha})
                del locks[repo]
                lock = None
            else:
                held = f"main CI red at {lock['red_sha'][:7]}"
        gate = []
        if lock:
            gate.append(f"repo_locked:{held or lock.get('status')}")
            notes.append({"kind": "merge_lock", "repo": repo, "status": lock.get("status"), "pr": lock.get("pr"),
                          "detail": held})
        elif main_ci != "green":
            gate.append(f"main_ci_{main_ci}")
            if main_ci == "red":
                comment("main CI red", repo, main_sha, "the push CI run for the current main head failed")
        for it in prs:
            reasons = merge_reasons(it, ctx, snapshot, repos, cfg, attempts)
            rows.append((repo, it, reasons or list(gate), f"{main_sha[:7]} {main_ci}"))
            more, why = guard_steps(f"{repo}#{it['number']}", it, reasons, touches, posted, now, cfg, snapshot["run_id"])
            actions += more
            notes += why
    # The serial set (vws-deploy#397): at most one merge per tick among PRs that touch orchestrate.py,
    # repo-guard*.yml or AGENTS.md, in any repo; the oldest verify approval goes first.
    serial = sorted((verify_approved_at(it.get("merge_info")) or "~", repo, it["number"])
                    for repo, it, reasons, _ in rows if not reasons and serial_merge_pr(it.get("merge_info")))
    first = f"{serial[0][1]}#{serial[0][2]}" if serial else ""
    chosen = {}
    for repo, it, reasons, main in sorted(rows, key=lambda r: (r[0], f"{r[0]}#{r[1]['number']}" != first, r[1]["number"])):
        k = f"{repo}#{it['number']}"
        if not reasons and k != first and serial_merge_pr(it.get("merge_info")):
            reasons = [f"serial_after:{first}"]
        if not reasons and repo in chosen:
            reasons = [f"one_merge_per_repo:{chosen[repo]}"]
        eligible = not reasons
        if eligible:
            chosen[repo] = k
            head = it["head_sha"]
            actions.append({"type": "merge", "target": k, "repo": repo, "head": head, "reason": "verify PASS for the head, checks green, main green",
                            "agent": agent_of(it.get("body")),
                            "payload": {"merge_method": "squash", "sha": head,
                                        "commit_title": f"{it['merge_info']['title']} (#{it['number']})"}})
        note = {"kind": "merge_check", "target": k, "eligible": eligible, "reasons": reasons,
                "head": (it.get("head_sha") or "")[:7], "main": main}
        if "part_of_owner_repo" in reasons:
            note["url"] = it.get("url", k)
            note["hint"] = f"{note['url']}: {PART_OF_HINT}"
        unmapped = (it.get("merge_info") or {}).get("unmapped")
        if unmapped:
            note["unmapped_suite_checks"] = unmapped  # keyed per suite (conservative)
        notes.append(note)
    return actions, notes


def decide(snapshot, state, cfg, now):
    """Pure."""
    new_state = json.loads(json.dumps(state))
    merge_actions, merge_notes = decide_merges(snapshot, new_state, now, cfg, active_repos(snapshot))
    return decision(merge_actions, merge_notes, {k: new_state[k] for k in STATE_KEYS if k in new_state})
