"""Rule Z (vws-deploy#377): close the old PR when a rework or rebuild lands as a new one.

A DeepSeek rework that opens a new PR instead of pushing to the existing one leaves two open PRs for one
issue (vws-console-api#255 -> #264). The newer PR is known from one of two signals:

- its title or visible body names its source ("rework of #255", "rebuilt of #255", "-v2 of #255",
  "supersedes #255", "replaces #255"), the same forms vws-verify reads for a rebuild; or
- a Done report for a dispatch on the old PR names the newer PR.

Only the old PR the newer one points at is closed, with one comment linking the newer PR. A source that is
not an open PR here (an issue, a closed or draft PR, another repo's PR) and a report that does not answer a
dispatch on the old PR are never touched. STATE_KEYS is empty: the closed state makes the next tick write
nothing, and the comment's idempotency key keeps a retried tick from posting it twice.
"""
import hashlib
import re

from .common import classify_comment, decision, visible_text, vws_pr_key

NAME = "superseded"
STATE_KEYS = ()

# The reference forms vws-verify accepts for a rebuild (docs/verify-check.md) plus the rework form.
SUPERSEDED_RE = re.compile(
    r"(?i)(?:-v[0-9]+ of|\brebuil(?:d|t) of|\brework(?:ed)? (?:of|from)|\bsupersedes|\breplaces)"
    r"[ \t]+#([0-9]+)\b")


def superseded_numbers(title, body):
    """The PR numbers a newer PR names as its source, from its title and visible body, sorted."""
    return sorted({int(n) for n in SUPERSEDED_RE.findall(f"{title or ''}\n{visible_text(body)}")})


def superseded_key(target, source):
    return "sha256:" + hashlib.sha256(f"superseded\n{target}\n{source}".encode()).hexdigest()


def newer_pr(old, new):
    """True while new is an open non-draft PR into main in the same repo, created after old, so it may
    supersede it."""
    return (new["repo"] == old["repo"] and new["is_pr"] and new["state"] == "open" and not new.get("draft")
            and new.get("base") == "main"
            and (new["created_at"], new["number"]) > (old["created_at"], old["number"]))


def report_sources(items):
    """{old PR: newer PR} when the newest Done report on the old PR names a newer open PR. A report for
    another item's dispatch, a report naming the old PR itself, and a latest report whose PR is not open
    (the rework was closed) are no link."""
    out = {}
    for k in sorted(items):
        old = items[k]
        if not (old["is_pr"] and old["state"] == "open"):
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


def superseded_prs(items):
    """Open PRs a newer open PR says it reworks or rebuilds, as [{"target": old, "source": new, "via":
    "report" | "body"}] sorted by target. One entry per source PR; when two newer PRs name one source, a
    report link wins, then the first by key."""
    out = {k: {"target": k, "source": s, "via": "report"} for k, s in report_sources(items).items()}
    for k in sorted(items):
        new = items[k]
        if not (new["is_pr"] and new["state"] == "open" and not new.get("draft") and new.get("base") == "main"):
            continue
        for n in superseded_numbers(new.get("title"), new.get("body")):
            sk = f"{new['repo']}#{n}"
            old = items.get(sk)
            if n == new["number"] or sk in out or not old:
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
    for p in superseded_prs(items):
        key = superseded_key(p["target"], p["source"])
        actions.append({"type": "close_pr", "target": p["target"], "source": p["source"], "via": p["via"], "key": key,
                        "reason": f"superseded by {p['source']}",
                        "body": superseded_body(p, items[p["source"]]["url"])})
        notes.append({"kind": "superseded_close", "target": p["target"], "source": p["source"]})
    return decision(actions, notes)
