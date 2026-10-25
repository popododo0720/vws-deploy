# Passing: markers are built into bodies and matched only through the module.
import trusted_read as tr

WAKE_MARKER = tr.WAKE_MARKER


def wake_body(key):
    return f"{WAKE_MARKER}\nvws-orchestrate: 1 PRs wait\n\nIdempotency key: `{key}`"


def recheck_key(comments, key):
    return not tr.has_key(comments, key)


def machine(body):
    return body.lstrip().startswith("<!--")  # any HTML comment, not a marker


def plain(body):
    return "Part of" in body
