"""Trusted reads of GitHub state for the factory orchestrator (vws-deploy#439 AC2, class 1).

Class 1 of docs/lessons/verify-findings-2026-10.md is "untrusted or stale GitHub state": a decision that
uses data an untrusted actor can write, or data read before the action it guards. orchestrate.py reads
that state only through this module:

- Markers. Every marker constant and marker regex lives here, and every match of one against GitHub
  text is a function here. .github/drift/trusted_read_lint.py fails when orchestrate.py matches a marker
  (or an `Idempotency key:` line) anywhere else.
- Comments by an allowed author. A dedupe marker, an idempotency key or a wake time counts only from the
  orchestrator's own login (own_comment: OWNER, docs/factory-orchestrator.md "gh login"); anyone can comment on the public repos, so a copied marker from another login is ignored.
  A claim counts only from a Grok login or a current assignee (is_claim, vws-deploy#507).
  Snapshot comments are filtered once at collect by orchestrate.comment_distrust (vws-deploy#419/#433).
- Fresh state at the pinned head. pr_at_head reads the PR again right before a write and refuses when it
  is closed or its head moved since the tick read it.
- PR bodies from the trusted verdict. verified_body returns a merged PR body only when it was not edited
  at or after the trusted vws-verify approval of the merged head (vws-deploy#499, review 5640903019).

Python 3 stdlib only. It never imports orchestrate.py.
"""
import re

OWNER = "popododo0720"

# ---------------------------------------------------------------- markers
# Rework (vws-deploy#373): the last line of a rework dispatch, keyed to the PR head.
REWORK_MARKER = "<!-- orchestrate:rework head="
# CI-failure marker (vws-deploy#412): a PR comment that #420's ci-fail workflow posts with the caller's
# GITHUB_TOKEN. Line 1 is the marker and a fenced json block is the record; the comment counts only as
# #420 parse_comment accepts it (.github/ci-fail/ci_fail.py, ported in orchestrate.ci_fail_record).
# Exactly #420 MARKER_RE.
CI_FAIL_RE = re.compile(r"<!-- vws-ci-fail sha=([0-9a-f]{40}) v=(\d+) repo=(vws-[a-z0-9-]+) pr=(\d+) "
                        r"owner=(deepseek|grok|none) agent=([a-z0-9][a-z0-9-]{0,38}|-) n=(\d+) wake=(true|false) -->")
# The first line of a status comment is exactly one of these; vws-verify and vws-platform routines
# match on it.
WAKE_MARKER = "<!-- vws-orchestrate:verify-wake -->"
FAILURE_MARKER = "<!-- vws-orchestrate:failure -->"
# Rule G (vws-deploy#378): the Grok dev bot's GitHub-trigger routine on the verify-wake thread matches this.
GROK_WAKE_MARKER = "<!-- vws-orchestrate:grok-wake -->"
# Lead wake (vws-deploy#397): dr eggbot's GitHub-trigger routine on the wake thread matches this first line.
LEAD_WAKE_MARKER = "<!-- vws-orchestrate:lead-wake -->"
# The last line, so classify_comment() (and metrics, vws-deploy#400) sees a "grok-wake" marker comment.
GROK_ASSIGN_MARKER = "<!-- orchestrate:grok-wake item="
# vws-deploy#460: a PR to rebuild (labeled needs-rebuild, or conflicting with main) wakes its owner on the
# verify-wake thread, once per PR head.
REBUILD_WAKE_MARKER = "<!-- vws-orchestrate:rebuild-wake -->"
# The status issue (vws-deploy#416): the orchestrator owns only the block from STATUS_MARKER to
# STATUS_END_MARKER in its body and rewrites that block in place; text outside it is kept byte for byte,
# and a body without exactly one block is never written. Its comments are metrics alerts only
# (.github/metrics posts them); the orchestrator never comments there.
STATUS_MARKER = "<!-- vws-orchestrate:status -->"
STATUS_END_MARKER = "<!-- /vws-orchestrate:status -->"
METRICS_ALERT_MARKER = "<!-- vws-orchestrate:metrics-alert -->"
# A PR or issue body with this never merges. Anyone who can edit the body can add it; it only holds work back.
OPT_OUT_MARKER = "<!-- vws-orchestrate:no-merge -->"
KEY_RE = re.compile(r"^Idempotency key: `(sha256:[0-9a-f]{64})`$", re.M)
MARKER_KIND_RE = re.compile(r"<!-- orchestrate:([a-z-]+)")
FAILURE_SECTION_RE = re.compile(rf"^## Failed ticks\n.*?(?=^## |^_Updated |^{re.escape(STATUS_END_MARKER)}|\Z)",
                                re.M | re.S)


def ac_tick_marker(pr):
    """The last line of the AC tick comment for merged PR repo#N (vws-deploy#486)."""
    return f"<!-- vws-orchestrate:ac-tick pr={pr} -->"


# ---------------------------------------------------------------- authors
# vws-deploy#419: who may write the comments the orchestrator acts on (the repos are public).
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def author_of(c):
    """(login, association) of a REST comment or timeline event, or of an orchestrate.comment_record."""
    user = c.get("user") or c.get("actor") or {}
    return (user.get("login") or c.get("author") or ""), (c.get("author_association") or c.get("assoc") or "")


def is_trusted_reset_author(login, association):
    """vws-deploy#419, same rule as .github/ci-fail (#412): the repo owner, or an OWNER/MEMBER/COLLABORATOR
    association. A missing login or association fails closed."""
    if not login or not association:
        return False
    return login.lower() == OWNER.lower() or association in TRUSTED_ASSOCIATIONS


def own_comment(c):
    """True for a comment the orchestrator posted: its author is the box gh login, OWNER (a GitHub login
    names one account). A missing login fails closed."""
    return author_of(c)[0].lower() == OWNER.lower()


def own_comments(comments):
    return [c for c in comments or [] if isinstance(c, dict) and own_comment(c)]


def is_claim(e, claimers):
    """vws-deploy#411/#507: a timeline event that claims a woken Grok issue as a comment. It must be a
    `commented` event with a time, by a login in claimers (lowercase: ORCHESTRATE_GROK_LOGINS and the
    current assignees), and not a machine comment (one that starts with an HTML-comment marker). Anyone
    else's comment is not a claim, so an unrelated comment cannot keep a wake from expiring."""
    login = author_of(e)[0].lower()
    return (e.get("event") == "commented" and bool(e.get("created_at")) and bool(login) and login in claimers
            and not (e.get("body") or "").lstrip().startswith("<!--"))


# ---------------------------------------------------------------- marker matches

def has_marker(comments, marker):
    """True when one of the orchestrator's own comments contains marker."""
    return any(marker in (c.get("body") or "") for c in own_comments(comments))


def has_key(comments, key):
    """True when one of the orchestrator's own comments carries `Idempotency key: <key>`."""
    return any(f"Idempotency key: `{key}`" in (c.get("body") or "") for c in own_comments(comments))


def keys(comments):
    """The idempotency keys on the orchestrator's own comments."""
    return sorted({m.group(1) for c in own_comments(comments) for m in [KEY_RE.search(c.get("body") or "")] if m})


def key_times(comments):
    """{idempotency key: created_at of the first own comment carrying it} (vws-deploy#411)."""
    out = {}
    for c in own_comments(comments):
        m = KEY_RE.search(c.get("body") or "")
        if m and c.get("created_at") and (m.group(1) not in out or c["created_at"] < out[m.group(1)]):
            out[m.group(1)] = c["created_at"]
    return out


def wake_comments(comments, utc):
    """Verify-wake comments by the orchestrator on the verify-wake thread: [{"key", "at"}]."""
    out = []
    for c in own_comments(comments):
        body = c.get("body") or ""
        if body.split("\n", 1)[0].strip() == WAKE_MARKER:
            m = KEY_RE.search(body)
            out.append({"key": m.group(1) if m else "", "at": utc(c["created_at"])})
    return out


def is_wake(body):
    """True when line 1 of body is the verify-wake marker (a body the orchestrator built)."""
    return (body or "").split("\n", 1)[0].strip() == WAKE_MARKER


def marker_kind(text):
    """The kind of the first `<!-- orchestrate:<kind>` marker in text, or ''."""
    m = MARKER_KIND_RE.search(text or "")
    return m.group(1) if m else ""


def ci_fail_match(line):
    """The CI_FAIL_RE full match of one line, or None."""
    return CI_FAIL_RE.fullmatch(line or "")


def is_rework(body):
    return REWORK_MARKER in (body or "")


def reworked_head(body, head):
    """True when body is the rework dispatch for this head."""
    return f"{REWORK_MARKER}{head} " in (body or "")


def rework_kind(body):
    tail = body.split(REWORK_MARKER, 1)[1].split("-->", 1)[0]
    return "ci_fail_rework" if " ci=" in tail and " verdict=" not in tail else "verify_fail_rework"


def opted_out(body):
    """True when body carries the no-merge marker. It only holds work back, so any body author counts."""
    return OPT_OUT_MARKER in (body or "")


def status_unowned(body):
    """'' when body has exactly one status block (STATUS_MARKER, later STATUS_END_MARKER), else why not.
    The orchestrator writes only inside that block and never writes a body that lacks one."""
    body = body or ""
    starts, ends = body.count(STATUS_MARKER), body.count(STATUS_END_MARKER)
    if starts != 1 or ends != 1:
        return (f"the body needs exactly one {STATUS_MARKER} ... {STATUS_END_MARKER} block; "
                f"found {starts} start and {ends} end markers")
    if body.index(STATUS_END_MARKER) < body.index(STATUS_MARKER):
        return "the end marker comes before the start marker"
    return ""


def status_span(body):
    """(start, end) of the status block, end just past STATUS_END_MARKER; body must be owned."""
    return body.index(STATUS_MARKER), body.index(STATUS_END_MARKER) + len(STATUS_END_MARKER)


def status_has_key(body, key):
    """True when the status block of body carries `Idempotency key: <key>`. Text outside the block is not the
    orchestrator's, so a key copied there does not count."""
    if status_unowned(body):
        return False
    start, end = status_span(body)
    return f"Idempotency key: `{key}`" in body[start:end]


def failure_match(block):
    """The '## Failed ticks' section match inside a status block, or None."""
    return FAILURE_SECTION_RE.search(block)


# ---------------------------------------------------------------- fresh reads and trusted bodies

def pr_at_head(get, repo, number, head):
    """Read the PR again right before a write. Returns (pr, why): why is '' while the PR is open at head,
    else "not open" or "head moved". get is a gh.get-like callable; a read error propagates."""
    pr, _ = get(f"repos/{OWNER}/{repo}/pulls/{number}")
    if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("merged"):
        return pr, "not open"
    if (pr.get("head") or {}).get("sha") != head:
        return pr, "head moved"
    return pr, ""


def body_edited(m, utc):
    """True when the PR body changed at or after the trusted verdict (the same second counts as after)."""
    return bool(m.get("body_edited_at")) and utc(m["body_edited_at"]) >= utc(m["verified_at"])


def verified_body(m, utc):
    """The merged PR body m["body"] when it is the one vws-verify approved: a trusted approval of the merged
    head exists (m["verified_at"]) and the body was not edited at or after it (m["body_edited_at"],
    GraphQL lastEditedAt). Else None, and nothing may be read from the body (vws-deploy#499)."""
    if not m.get("verified_at") or body_edited(m, utc):
        return None
    return m.get("body") or ""
