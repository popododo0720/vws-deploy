# The pre-fix shapes from orchestrate.py on main 0ea10ea (vws-deploy#496/#499 dedupe-marker finding): a
# marker or key matched against GitHub text from any commenter, outside trusted_read.py.
import re

WAKE_MARKER = "<!-- vws-orchestrate:verify-wake -->"
KEY_RE = re.compile(r"^Idempotency key: `(sha256:[0-9a-f]{64})`$", re.M)


def ac_tick_marker(pr):
    return f"<!-- vws-orchestrate:ac-tick pr={pr} -->"


def marker_seen(comments, k):
    return any(ac_tick_marker(k) in (c.get("body") or "") for c in comments)


def recheck_key(comments, key):
    return not any(f"Idempotency key: `{key}`" in (c.get("body") or "") for c in comments)


def status_wakes(comments):
    return [c for c in comments if c["body"].split("\n", 1)[0].strip() == WAKE_MARKER]


def keys(comments):
    return [KEY_RE.search(c["body"]) for c in comments]


def rework(body):
    return body.startswith("<!-- orchestrate:rework head=")


def rework_re(body):
    return re.search(r"<!-- orchestrate:([a-z-]+)", body)
