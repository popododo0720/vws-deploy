"""Rule Z (vws-deploy#377): close the old PR when a rework or rebuild lands as a new one.

A DeepSeek rework that opens a new PR instead of pushing to the existing one leaves two open PRs for one
issue (vws-console-api#255 -> #264). The newer PR is known from one of two signals:

- a structured claim by a trusted author: a title `-vN of #M`, or a visible body line that is exactly
  `Rework of #M`, `Rebuild of #M`, `Rebuilt of #M`, `Supersedes #M` or `Replaces #M` (an optional period); or
- a Done report for a dispatch on the old PR names the newer PR.

Only the old PR the newer one points at is closed, with one comment linking the newer PR. A source that is
not an open non-draft PR here (an issue, a closed or draft PR, another repo's PR), the status issue, the
verify-wake thread, and a report that does not answer a dispatch on the old PR are never touched. Free prose
never counts: a body that says "this replaces #M's list" leaves #M open. STATE_KEYS is empty: the closed
state makes the next tick write nothing, and the comment's idempotency key keeps a retried tick from posting
it twice.
"""
import hashlib
import re

import trusted_read as tr
from .common import classify_comment, decision, status_target, visible_text, vws_pr_key, wake_target

NAME = "superseded"
STATE_KEYS = ()

# A title names its source as `-vN of #M`; a body claim is one of these forms alone on its line. The line
# forms are vws-verify's rebuild forms (docs/verify-check.md) plus the rework form; `-vN of` counts only in
# the title, so free prose is never a claim.
SUPERSEDED_TITLE_RE = re.compile(r"(?i)-v[0-9]+ of #([0-9]+)\b")
SUPERSEDED_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:rework of|rebuild of|rebuilt of|supersedes|replaces) #([0-9]+)\.?[ \t]*$")


def superseded_numbers(title, body):
    """The PR numbers a newer PR names as its source: from its title (`-vN of #M`) and from a claim line
    alone in its visible body, sorted."""
    return sorted({int(n) for n in SUPERSEDED_TITLE_RE.findall(title or "")}
                  | {int(n) for n in SUPERSEDED_LINE_RE.findall(visible_text(body))})


def superseded_key(target, source):
    return "sha256:" + hashlib.sha256(f"superseded\n{target}\n{source}".encode()).hexdigest()


def newer_pr(old, new):
    """True while new is an open non-draft PR into main in the same repo, created after old, so it may
    supersede it. A draft old PR is never a source."""
    return (new["repo"] == old["repo"] and new["is_pr"] and new["state"] == "open" and not new.get("draft")
            and new.get("base") == "main" and not old.get("draft")
            and (new["created_at"], new["number"]) > (old["created_at"], old["number"]))


def factory_thread(k, cfg):
    """True for the status issue and the verify-wake thread: the orchestrator owns both, so no PR closes them."""
    return k in (status_target(cfg), wake_target(cfg))


def report_sources(items, cfg):
    """{old PR: newer PR} when the newest Done report on the old PR names a newer open PR. A report for
    another item's dispatch, a report naming the old PR itself, a draft or factory-thread target, and a
    latest report whose PR is not open (the rework was closed) are no link."""
    out = {}
    for k in sorted(items):
        old = items[k]
        if not (old["is_pr"] and old["state"] == "open") or factory_thread(k, cfg):
            continue
        dispatches = {c["url"] for c in old.get("comments", [])
                      if (classify_comment(c["body"]) or ("",))[0] == "dispatch"}
        reports = []
        for c in old.get("comments", []):
            kind = classify_comment(c["body"])
            if not kind or kind[0] != "report" or kind[1]["result"] != "done" or kind[1]["for"] not in dispatches:
                continue
            sk = vws_pr_key(kind[1]["pr"])
            if sk:
                reports.append((c["created_at"], c["id"], sk))
        if not reports:
            continue
        sk = max(reports)[2]
        new = items.get(sk) if sk != k else None
        if new and newer_pr(old, new):
            out[k] = sk
    return out


def superseded_prs(items, cfg):
    """Open PRs a newer open PR says it reworks or rebuilds, as [{"target": old, "source": new, "via":
    "report" | "body"}] sorted by target. One entry per source PR; when two newer PRs name one source, a
    report link wins, then the first by key. A body or title claim counts only from a trusted author."""
    skip = {status_target(cfg), wake_target(cfg)}
    out = {k: {"target": k, "source": s, "via": "report"} for k, s in report_sources(items, cfg).items()}
    for k in sorted(items):
        new = items[k]
        if not (new["is_pr"] and new["state"] == "open" and not new.get("draft") and new.get("base") == "main"):
            continue
        if not tr.is_trusted_reset_author(new.get("author") or "", new.get("author_assoc") or ""):
            continue
        for n in superseded_numbers(new.get("title"), new.get("body")):
            sk = f"{new['repo']}#{n}"
            old = items.get(sk)
            if n == new["number"] or sk in out or sk in skip or not old:
                continue
            if old["is_pr"] and old["state"] == "open" and newer_pr(old, new):
                out[sk] = {"target": sk, "source": k, "via": "body"}
    return [out[k] for k in sorted(out)]


def superseded_body(p, source_url):
    return "\n".join([
        f"Superseded by {source_url}.",
        "",
        "The newer PR is this PR's rework or rebuild, so this PR is closed to leave one open PR for the work.",
        "", f"Idempotency key: `{superseded_key(p['target'], p['source'])}`",
    ])


def decide(snapshot, state, cfg, now):
    """Pure."""
    items = snapshot["items"]
    actions, notes = [], []
    for p in superseded_prs(items, cfg):
        key = superseded_key(p["target"], p["source"])
        actions.append({"type": "close_pr", "target": p["target"], "source": p["source"], "via": p["via"], "key": key,
                        "reason": f"superseded by {p['source']}",
                        "body": superseded_body(p, items[p["source"]]["url"])})
        notes.append({"kind": "superseded_close", "target": p["target"], "source": p["source"]})
    return decision(actions, notes)
