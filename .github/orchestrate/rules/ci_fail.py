"""CI-failure markers (vws-deploy#412): notes for untrusted markers and for grok-owned CI failures."""
from .common import (
    ci_fails, decision,
)

NAME = "ci_fail"
STATE_KEYS = ()


def decide(snapshot, state, cfg, now):
    """Pure. Notes only."""
    notes = []
    items = snapshot["items"]

    # CI-failure markers (vws-deploy#412): untrusted authors are ignored; owner=grok waits for #398(a).
    for k in sorted(items):
        it = items[k]
        if not (it["is_pr"] and it["state"] == "open"):
            continue
        ok, bad = ci_fails(it)
        for c in bad:
            notes.append({"kind": "ci_fail_untrusted", "target": k, "comment": c["url"], "author": c.get("author", "")})
        for m in ok:
            if m["sha"] == it["head_sha"] and m["owner"] == "grok":
                notes.append({"kind": "ci_fail_grok_pending", "target": k, "comment": m["url"], "sha": m["sha"],
                              "agent": m["agent"]})
                break
    return decision(notes=notes)
