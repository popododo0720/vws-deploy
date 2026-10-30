"""Rule H: serial-merge hold auto-release (vws-deploy#501 box 8).

A serial-merge hold is an open PR labeled `hold` whose body has exactly one line `Held for: #N` (same repo) or
`Held for: vws-<repo>#N`. The hold must name an open PR: once #N is not one (merged, closed, missing, or an
issue), the rule removes `hold` from the held PR (note `hold_release`), and Rule D can merge it on a later
tick. A `hold` with no `Held for:` line is a human hold and is never touched. The decision uses only the open
items collect already has: an active repo's open issues and PRs are all in the snapshot, so a reference that
is not there is not open. No extra read.

Trust (vws-deploy#439 class 1): the repos are public, and a PR author can edit its body, so the line counts
only when the PR author is the owner, a member or a collaborator; then everyone who can edit the body could
remove the label by hand anyway. Any other author keeps the hold (note `hold_ref_untrusted`).
"""
import re

import trusted_read as tr

from .common import (
    REF_RE, active_repos, decision,
)

NAME = "hold_release"
STATE_KEYS = ()

HOLD_LABEL = "hold"
HELD_FOR_RE = re.compile(r"(?im)^[ \t]*Held for:[ \t]*(.*?)[ \t]*$")


def held_for(body, repo):
    """(target key, why) for a PR body. ("", "") when there is no `Held for:` line (a human hold); a target
    key such as "vws-iam#3" for exactly one valid reference; ("", why) for anything else."""
    refs = HELD_FOR_RE.findall(body or "")
    if not refs:
        return "", ""
    if len(refs) > 1:
        return "", "more than one Held for: line"
    m = REF_RE.match(refs[0])
    if not m:
        return "", f"Held for: {refs[0]!r} is not #N or vws-<repo>#N"
    return f"{m.group(1) or repo}#{m.group(2)}", ""


def held_for_trusted(login, assoc):
    """True when a PR body by this author may release a hold (see the module docstring). Fails closed."""
    return tr.is_trusted_reset_author(login, assoc)


def decide_hold_release(snapshot, cfg):
    """(actions, notes). Pure. Nothing while ORCHESTRATE_MERGE is off: a hold only matters for merges."""
    if cfg.get("ORCHESTRATE_MERGE", "dry-run") == "off":
        return [], []
    items, repos = snapshot["items"], active_repos(snapshot)
    actions, notes = [], []
    for k, it in sorted(items.items()):
        if not it.get("is_pr") or it.get("state") != "open" or HOLD_LABEL not in it["labels"]:
            continue
        target, why = held_for(it.get("body"), it["repo"])
        if not target and not why:
            continue  # a human hold
        if target == k:
            why = "Held for: names the held PR itself"
        if why:
            notes.append({"kind": "hold_ref_invalid", "target": k, "why": why})
            continue
        if not held_for_trusted(it.get("author", ""), it.get("author_assoc", "")):
            notes.append({"kind": "hold_ref_untrusted", "target": k, "held_for": target,
                          "author": it.get("author", ""), "assoc": it.get("author_assoc", "")})
            continue
        t = items.get(target)
        if t is None and target.split("#")[0] not in repos:
            notes.append({"kind": "hold_ref_unresolved", "target": k, "held_for": target,
                          "why": "repo not collected or archived"})
            continue
        if t is not None and t.get("is_pr") and t.get("state") == "open":
            continue  # still waiting for an open PR
        if t is None:
            reason, text = "target_not_open", "is not an open PR (merged, closed or missing)"
        else:
            reason, text = "target_not_a_pr", "is an issue, not a PR"
        actions.append({"type": "remove_label", "target": k, "label": HOLD_LABEL, "head": it["head_sha"],
                        "held_for": target, "reason": f"hold released: {target} {text}"})
        notes.append({"kind": "hold_release", "target": k, "held_for": target, "reason": reason})
    return actions, notes


def decide(snapshot, state, cfg, now):
    """Pure."""
    return decision(*decide_hold_release(snapshot, cfg))
