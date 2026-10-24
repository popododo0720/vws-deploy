"""Tests for .github/workflows/verify-queue.yml (vws-deploy#397): the needs-verify label follows CI.

The run script is executed with gh stubbed: $PR_TSV, $CHECKS_TSV and $STATUS_TSV are what the PR,
check-runs and commit-status reads print; $REVIEWS_JSON and $COMMENTS_JSON are the raw API pages of the
reviews and issue-comments reads (filtered by the script's own jq); $DELETE_FAIL makes the label DELETE
fail with that text and $READ_FAIL makes the reviews read fail.
Run: python3 -m unittest discover -s tests/workflows
"""
import json
import os
import re
import unittest

import yaml

from test_repo_guard import Stub

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(ROOT, ".github", "workflows", "verify-queue.yml")
SHA = "a" * 40
OTHER = "b" * 40

GH = """case "$*" in
  "api repos/o/r/pulls/"*) printf '%s\\n' "$PR_TSV" ;;
  *"/check-runs?per_page=100"*) [ -n "${CHECKS_TSV:-}" ] && printf '%s\\n' "$CHECKS_TSV" ;;
  *"/status --jq"*) [ -n "${STATUS_TSV:-}" ] && printf '%s\\n' "$STATUS_TSV" ;;
  *"/reviews?per_page=100") if [ -n "${READ_FAIL:-}" ]; then echo "$READ_FAIL" >&2; exit 1; fi
    [ -n "${REVIEWS_JSON:-}" ] && printf '%s\\n' "$REVIEWS_JSON" ;;
  *"/comments?per_page=100") [ -n "${COMMENTS_JSON:-}" ] && printf '%s\\n' "$COMMENTS_JSON" ;;
  *"-X DELETE"*) if [ -n "${DELETE_FAIL:-}" ]; then echo "$DELETE_FAIL" >&2; exit 1; fi ;;
  *"-X POST"*) exit 0 ;;
  *) echo "unexpected gh $*" >&2; exit 9 ;;
esac
exit 0"""


def load():
    with open(PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def pr(labels="", draft="false", base="main", sha=SHA, state="open"):
    return "\t".join([state, draft, base, sha, labels])


def run_(cid, name, status="completed", conclusion="success"):
    return "\t".join([name, str(cid), status, conclusion])


GREEN = [run_(1, "test"), run_(2, "lint"), run_(3, "pr-hygiene"), run_(4, "verify-gate", conclusion="failure"),
         run_(5, "vws-verify", "in_progress", ""), run_(6, "verify-reset", conclusion="skipped")]


class Shape(unittest.TestCase):
    def test_triggers(self):
        on = load().get("on", load().get(True))
        # Every PR workflow in the vws-* repos: vws-deploy's own (checked by EveryPrWorkflow below),
        # vws-console's CI named "CI" and vws-conformance's english.
        self.assertEqual(on["workflow_run"], {
            "workflows": ["ci", "CI", "drift-check", "english", "installer-image", "post-merge", "repo-guard"],
            "types": ["completed"]})
        self.assertEqual(sorted(on["pull_request"]["types"]), ["ready_for_review", "synchronize"])
        # vws-deploy#440: a verdict review re-runs the queue so a FAIL takes the label off at once.
        self.assertEqual(on["pull_request_review"], {"types": ["submitted"]})
        self.assertEqual(sorted(on), ["pull_request", "pull_request_review", "workflow_run"])

    def test_only_the_needs_verify_job_writes_and_only_labels(self):
        # Labels on a PR go through the issues API but need pull-requests: write too (GitHub answers
        # 403 "Resource not accessible by integration" without it; live run on 4751579, vws-deploy#397 r2).
        wf = load()
        self.assertEqual(write_scopes(wf.get("permissions", {})), set(), "workflow-level permissions grant no write")
        for name, job in wf["jobs"].items():
            # A job's permissions replace the workflow's, so they must be set on the job itself.
            want = {"issues", "pull-requests"} if name == "needs-verify" else set()
            self.assertEqual(write_scopes(effective(wf, job)), want, name)

    def test_permission_check_catches_extra_writes(self):
        # The check above must fail on any other write and on pull-requests: write outside the job.
        job = {"permissions": {"issues": "write", "pull-requests": "write", "checks": "read"}}
        self.assertEqual(write_scopes(effective({"permissions": {}}, job)), {"issues", "pull-requests"})
        self.assertEqual(write_scopes({"issues": "write", "contents": "write"}), {"issues", "contents"})
        self.assertEqual(write_scopes(effective({"permissions": {"pull-requests": "write"}}, {})), {"pull-requests"})
        self.assertEqual(write_scopes("write-all"), {"*"})
        self.assertEqual(write_scopes(effective({}, {})), {"*"}, "no permissions key means the token default")

    def test_never_checks_out_or_interpolates(self):
        wf = load()
        for job in wf["jobs"].values():
            for s in job["steps"]:
                self.assertNotIn("uses", s, "no actions: nothing checks out PR code")
                self.assertNotRegex(s.get("run", ""), re.compile(r"\$\{\{"), "event data only reaches the script via env")


def write_scopes(perms):
    """The scopes a permissions value can write ("*" for write-all or an unset default)."""
    if perms is None or perms == "write-all":
        return {"*"}
    if perms == "read-all":
        return set()
    return {k for k, v in perms.items() if v == "write"}


def effective(wf, job):
    """GitHub uses the job's permissions when set, else the workflow's (None: the token default)."""
    return job["permissions"] if "permissions" in job else wf.get("permissions")


def triggers(wf):
    """The event names in a workflow's `on:` (a string, a list or a mapping)."""
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on}
    return set(on or [])


class EveryPrWorkflow(unittest.TestCase):
    """vws-deploy#397 round 2: a PR workflow missing from workflow_run.workflows can finish last, and then no
    verify-queue run sees the head green, so the PR never gets needs-verify. Scans .github/workflows."""

    def pr_workflows(self):
        d = os.path.dirname(PATH)
        names = {}
        for f in sorted(os.listdir(d)):
            if not f.endswith((".yml", ".yaml")) or os.path.join(d, f) == PATH:
                continue
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                wf = yaml.safe_load(fh)
            if triggers(wf) & {"pull_request", "pull_request_target"}:
                names[f] = wf.get("name", f)
        return names

    def test_scan_finds_the_known_pr_workflows(self):
        # Guards the scan itself: a parser that finds nothing would pass the check below trivially.
        found = set(self.pr_workflows().values())
        self.assertTrue({"ci", "repo-guard", "drift-check", "installer-image"} <= found, found)

    def test_every_pull_request_workflow_wakes_the_queue(self):
        listed = set(load().get("on", load().get(True))["workflow_run"]["workflows"])
        missing = {f: n for f, n in self.pr_workflows().items() if n not in listed}
        self.assertEqual(missing, {}, "PR workflows missing from verify-queue workflow_run.workflows")

    def test_triggers_parser_forms(self):
        self.assertEqual(triggers({"on": "pull_request"}), {"pull_request"})
        self.assertEqual(triggers({True: ["push", "pull_request"]}), {"push", "pull_request"})
        self.assertEqual(triggers({True: {"pull_request_target": None}}), {"pull_request_target"})


class Label(unittest.TestCase):
    def script(self):
        return load()["jobs"]["needs-verify"]["steps"][0]["run"]

    def go(self, event="workflow_run", action="completed", prs="7", sha=SHA, **env):
        e = {"GH_TOKEN": "x", "REPO": "o/r", "EVENT": event, "ACTION": action, "PRS": prs, "HEAD_SHA": sha,
             "PR_TSV": pr(), "CHECKS_TSV": "\n".join(GREEN), "STATUS_TSV": ""}
        e.update(env)
        rc, calls, out = Stub(self, GH).run(self.script(), e)
        self.assertEqual(rc, 0, out)
        writes = [c for c in calls.splitlines() if "-X POST" in c or "-X DELETE" in c]
        return writes, out

    ADD = "api -X POST repos/o/r/issues/7/labels -f labels[]=needs-verify"
    DEL = "api -X DELETE repos/o/r/issues/7/labels/needs-verify"

    def test_green_except_verify_checks_adds_the_label(self):
        writes, _ = self.go()
        self.assertEqual(writes, [self.ADD])

    def test_called_verify_gate_waits_for_the_verdict_too(self):
        # vws-deploy#402: repo-guard.yml calls the gate as "gate / verify-gate", next to its bare shim.
        checks = GREEN + [run_(9, "gate / verify-gate", conclusion="failure"), run_(10, "hygiene / pr-hygiene")]
        writes, out = self.go(CHECKS_TSV="\n".join(checks))
        self.assertEqual(writes, [self.ADD], out)
        writes, out = self.go(CHECKS_TSV="\n".join(checks + [run_(11, "hygiene / pr-hygiene", conclusion="failure")]))
        self.assertEqual(writes, [], out)

    def test_ready_for_review_with_green_checks_adds_the_label(self):
        writes, _ = self.go(event="pull_request", action="ready_for_review")
        self.assertEqual(writes, [self.ADD])

    def test_already_labeled_green_writes_nothing(self):
        writes, _ = self.go(PR_TSV=pr("bug,needs-verify"))
        self.assertEqual(writes, [])

    def test_pending_check_never_gets_the_label(self):
        for name, checks, status in [
                ("queued check", GREEN + [run_(9, "e2e", "queued", "")], ""),
                ("in-progress check", GREEN + [run_(9, "e2e", "in_progress", "")], ""),
                ("pending commit status", GREEN, "ext\tpending"),
                ("no checks yet", [], "")]:
            with self.subTest(name):
                writes, out = self.go(CHECKS_TSV="\n".join(checks), STATUS_TSV=status)
                self.assertEqual(writes, [])
                self.assertIn("pending", out)

    def test_failed_check_never_gets_the_label_and_removes_it(self):
        for name, checks, status in [
                ("failure", GREEN + [run_(9, "e2e", conclusion="failure")], ""),
                ("cancelled", GREEN + [run_(9, "e2e", conclusion="cancelled")], ""),
                ("timed out", GREEN + [run_(9, "e2e", conclusion="timed_out")], ""),
                ("failed commit status", GREEN, "ext\tfailure")]:
            with self.subTest(name):
                writes, _ = self.go(CHECKS_TSV="\n".join(checks), STATUS_TSV=status)
                self.assertEqual(writes, [], "unlabeled red PR: nothing to add")
                writes, out = self.go(CHECKS_TSV="\n".join(checks), STATUS_TSV=status, PR_TSV=pr("needs-verify"))
                self.assertEqual(writes, [self.DEL])
                self.assertIn("red", out)

    def test_rerun_supersedes_the_failed_attempt(self):
        writes, _ = self.go(CHECKS_TSV="\n".join(GREEN + [run_(10, "e2e", conclusion="failure"), run_(11, "e2e")]))
        self.assertEqual(writes, [self.ADD])
        writes, _ = self.go(CHECKS_TSV="\n".join(GREEN + [run_(10, "e2e"), run_(11, "e2e", "in_progress", "")]))
        self.assertEqual(writes, [], "a rerun in progress is pending again")

    def test_last_check_turning_green_labels_on_that_run(self):
        # ci finished first (repo-guard still running): no label; repo-guard's completion labels.
        writes, _ = self.go(CHECKS_TSV="\n".join([run_(1, "test"), run_(3, "pr-hygiene", "in_progress", "")]))
        self.assertEqual(writes, [])
        writes, _ = self.go(CHECKS_TSV="\n".join([run_(1, "test"), run_(3, "pr-hygiene")]))
        self.assertEqual(writes, [self.ADD])

    def test_push_removes_the_label(self):
        writes, out = self.go(event="pull_request", action="synchronize", PR_TSV=pr("needs-verify"))
        self.assertEqual(writes, [self.DEL])
        self.assertIn("new push", out)

    def test_push_on_unlabeled_pr_is_ok(self):
        writes, out = self.go(event="pull_request", action="synchronize",
                              DELETE_FAIL="gh: Label does not exist (HTTP 404)")
        self.assertEqual(writes, [self.DEL])

    # gh prints the JSON error body on stdout and "gh: <message> (HTTP <code>)" on stderr (live log,
    # /workspace/handoff/verdicts/d425-r2/needs-verify-job.log).
    LIVE_404 = ('printf \'%s\' \'{"message":"Label does not exist","status":"404"}\'; '
                'echo "gh: Label does not exist (HTTP 404)" >&2; exit 1')
    LIVE_403 = ('printf \'%s\' \'{"message":"Resource not accessible by integration","status":"403"}\'; '
                'echo "gh: Resource not accessible by integration (HTTP 403)" >&2; exit 1')

    def live_delete(self, body, labels):
        gh = GH.replace('*"-X DELETE"*) if [ -n "${DELETE_FAIL:-}" ]; then echo "$DELETE_FAIL" >&2; exit 1; fi ;;',
                        '*"-X DELETE"*) %s ;;' % body)
        self.assertNotEqual(gh, GH)
        e = {"GH_TOKEN": "x", "REPO": "o/r", "EVENT": "pull_request", "ACTION": "synchronize", "PRS": "7",
             "HEAD_SHA": SHA, "PR_TSV": pr(labels)}
        return Stub(self, gh).run(self.script(), e)

    def test_label_already_absent_404_is_success(self):
        rc, calls, out = self.live_delete(self.LIVE_404, "")
        self.assertEqual(rc, 0, out)
        self.assertIn(self.DEL, calls)
        self.assertIn("#7: no needs-verify label (new push)", out)

    def test_forbidden_403_fails_and_shows_why(self):
        rc, _, out = self.live_delete(self.LIVE_403, "needs-verify")
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 403", out)
        self.assertNotIn("needs-verify removed", out)

    def test_other_delete_error_fails(self):
        e = {"GH_TOKEN": "x", "REPO": "o/r", "EVENT": "pull_request", "ACTION": "synchronize", "PRS": "7",
             "HEAD_SHA": SHA, "PR_TSV": pr("needs-verify"), "DELETE_FAIL": "gh: Server Error (HTTP 500)"}
        rc, _, _ = Stub(self, GH).run(self.script(), e)
        self.assertNotEqual(rc, 0)

    def test_stale_run_for_an_old_head_writes_nothing(self):
        writes, out = self.go(sha=OTHER, PR_TSV=pr("needs-verify"))
        self.assertEqual(writes, [])
        self.assertIn("head moved", out)

    def test_not_waiting_for_verify(self):
        for name, tsv in [("draft", pr("needs-verify", draft="true")), ("other base", pr("needs-verify", base="release")),
                          ("approved", pr("needs-verify,verify-approved")), ("closed", pr("needs-verify", state="closed"))]:
            with self.subTest(name):
                writes, _ = self.go(PR_TSV=tsv)
                self.assertEqual(writes, [self.DEL], "the label comes off a PR that does not wait for verify")
        for name, tsv in [("draft", pr(draft="true")), ("approved", pr("verify-approved"))]:
            with self.subTest(name + ", unlabeled"):
                writes, _ = self.go(PR_TSV=tsv)
                self.assertEqual(writes, [])

    def test_no_pr_for_the_run_is_a_no_op(self):
        writes, out = self.go(prs="")
        self.assertEqual(writes, [])


# Recorded shapes (vws-deploy#440). PR #441 head 85d35dd got vws-verify's CHANGES_REQUESTED at 07:29Z, yet
# verify-queue labeled it needs-verify again at 10:43:31Z (issue events) and the verify wake re-woke for
# 159 minutes. The review is GET repos/popododo0720/vws-deploy/pulls/441/reviews (body trimmed); its
# check runs at that head had vws-verify completed/failure. Comments follow GET issues/N/comments.
HEAD_441 = "85d35dd0fc3c1089b419a24d9e3a9dbc83dde0b4"
REVIEW_441 = {
    "author_association": "NONE", "commit_id": HEAD_441, "id": 5625213917, "node_id": "PRR_kwDOVCPQls8AAAABT0nz3Q",
    "body": "Verify FAIL for head 85d35dd0fc3c1089b419a24d9e3a9dbc83dde0b4 (blocking=1).",
    "html_url": "https://github.com/popododo0720/vws-deploy/pull/441#pullrequestreview-5625213917",
    "pull_request_url": "https://api.github.com/repos/popododo0720/vws-deploy/pulls/441",
    "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-23T07:29:02Z",
    "user": {"id": 342416846, "login": "vws-verify[bot]", "type": "Bot"}}
CHECKS_441 = [run_(120292165453, "lint"), run_(120292165444, "verify-gate", conclusion="failure"),
              run_(120292165802, "build"), run_(120292165783, "check"), run_(120292165649, "installer"),
              run_(120292165908, "drift"), run_(120293505631, "verify-reset", conclusion="skipped"),
              run_(120293565924, "verify-rerun", conclusion="skipped"),
              run_(120296243688, "vws-verify", conclusion="failure"),
              run_(120372380132, "pr-hygiene"), run_(120372380535, "verify-gate", conclusion="failure"),
              run_(120372381981, "verify-reset", conclusion="skipped"),
              run_(120372382012, "verify-rerun", conclusion="skipped")]


def review(sha=SHA, state="CHANGES_REQUESTED", at="2026-10-23T07:29:02Z", login="vws-verify[bot]", rid=1):
    return dict(REVIEW_441, commit_id=sha, state=state, submitted_at=at, id=rid,
                user=dict(REVIEW_441["user"], login=login))


def comment(body, at="2026-10-23T08:00:00Z", login="popododo0720", assoc="OWNER", cid=6359281322):
    return {"author_association": assoc, "body": body, "created_at": at, "updated_at": at, "id": cid,
            "html_url": f"https://github.com/o/r/pull/7#issuecomment-{cid}", "user": {"login": login, "type": "User"}}


def verdict(result, sha=SHA, target="r#7"):
    return f"# {target} @ {sha}\n\nResult: {result}\n\n- finding"


def page(*objs):
    return json.dumps(list(objs))


class HeadFail(unittest.TestCase):
    """vws-deploy#440: a FAIL verdict on the current head keeps the PR out of the verify queue."""

    script, go, ADD, DEL = Label.script, Label.go, Label.ADD, Label.DEL

    VERDICT_GREEN = [r for r in GREEN if "vws-verify" not in r]  # verify has finished on this head

    def test_pr_441_recorded_fail_is_not_relabeled(self):
        # The real case: green CI, vws-verify failed the head; the run that relabeled it must not.
        e = dict(PR_TSV=pr(sha=HEAD_441), CHECKS_TSV="\n".join(CHECKS_441), REVIEWS_JSON=page(REVIEW_441))
        writes, out = self.go(sha=HEAD_441, **e)
        self.assertEqual(writes, [])
        self.assertIn("FAIL on this head (verdict review 5625213917)", out)

    def test_pr_441_label_already_on_is_removed(self):
        e = dict(PR_TSV=pr("needs-verify", sha=HEAD_441), CHECKS_TSV="\n".join(CHECKS_441), REVIEWS_JSON=page(REVIEW_441))
        writes, out = self.go(sha=HEAD_441, **e)
        self.assertEqual(writes, [self.DEL])
        self.assertIn("needs-verify removed (verify FAIL on this head)", out)

    def test_fail_review_event_removes_the_label(self):
        writes, _ = self.go(event="pull_request_review", action="submitted", PR_TSV=pr("needs-verify"),
                            REVIEWS_JSON=page(review()))
        self.assertEqual(writes, [self.DEL])

    def test_fail_on_old_head_does_not_block_a_new_push(self):
        # A new push: the FAIL names the previous head, so the new head is labeled again.
        writes, _ = self.go(REVIEWS_JSON=page(review(sha=OTHER)), CHECKS_TSV="\n".join(GREEN),
                            COMMENTS_JSON=page(comment(verdict("FAIL", sha=OTHER))))
        self.assertEqual(writes, [self.ADD])

    def test_no_verdict_yet_is_labeled(self):
        writes, out = self.go(REVIEWS_JSON=page(), COMMENTS_JSON=page())
        self.assertEqual(writes, [self.ADD])
        self.assertNotIn("FAIL", out)

    def test_fail_verdict_comment_on_head(self):
        for name, c in [("owner account", comment(verdict("FAIL"))),
                        ("verify bot", comment(verdict("FAIL"), login="vws-verify[bot]", assoc="NONE")),
                        ("crlf and leading blank", comment("\r\n" + verdict("FAIL").replace("\n", "\r\n")))]:
            with self.subTest(name):
                writes, out = self.go(COMMENTS_JSON=page(c))
                self.assertEqual(writes, [])
                self.assertIn("verdict comment 6359281322", out)
                writes, _ = self.go(COMMENTS_JSON=page(c), PR_TSV=pr("needs-verify"))
                self.assertEqual(writes, [self.DEL])

    def test_comments_that_are_not_a_fail_on_this_head_are_ignored(self):
        for name, c in [("outsider", comment(verdict("FAIL"), login="rando", assoc="NONE")),
                        ("other PR", comment(verdict("FAIL", target="r#8"))),
                        ("other repo", comment(verdict("FAIL", target="vws-iam#7"))),
                        ("FAIL quoted later", comment("note\n" + verdict("FAIL"))),
                        ("PASS", comment(verdict("PASS")))]:
            with self.subTest(name):
                writes, _ = self.go(COMMENTS_JSON=page(c))
                self.assertEqual(writes, [self.ADD])

    def test_review_filters(self):
        for name, r in [("other reviewer", review(login="popododo0720")), ("commented only", review(state="COMMENTED")),
                        ("approved", review(state="APPROVED")), ("pending review", dict(review(), submitted_at=None))]:
            with self.subTest(name):
                writes, _ = self.go(REVIEWS_JSON=page(r))
                self.assertEqual(writes, [self.ADD])

    def test_newest_verdict_on_the_head_counts(self):
        fail = review(at="2026-10-23T07:00:00Z", rid=1)
        passed = comment(verdict("PASS"), at="2026-10-23T08:00:00Z")
        writes, _ = self.go(REVIEWS_JSON=page(fail), COMMENTS_JSON=page(passed))
        self.assertEqual(writes, [self.ADD], "a later PASS on the same head supersedes the FAIL")
        writes, _ = self.go(REVIEWS_JSON=page(review(at="2026-10-23T09:00:00Z")), COMMENTS_JSON=page(passed))
        self.assertEqual(writes, [], "a later FAIL supersedes the PASS")

    def test_paginated_reviews_are_joined(self):
        # gh api --paginate prints one JSON array per page.
        writes, _ = self.go(REVIEWS_JSON=page(review(sha=OTHER, rid=1)) + "\n" + page(review(rid=2)))
        self.assertEqual(writes, [])

    def test_vws_verify_check_failure_on_head(self):
        writes, out = self.go(CHECKS_TSV="\n".join(self.VERDICT_GREEN + [run_(20, "vws-verify", conclusion="failure")]))
        self.assertEqual(writes, [])
        self.assertIn("vws-verify check failure", out)
        rerun = self.VERDICT_GREEN + [run_(20, "vws-verify", conclusion="failure"), run_(21, "vws-verify", "in_progress", "")]
        writes, _ = self.go(CHECKS_TSV="\n".join(rerun))
        self.assertEqual(writes, [self.ADD], "a newer vws-verify run on the head supersedes the failed one")
        writes, _ = self.go(CHECKS_TSV="\n".join(self.VERDICT_GREEN + [run_(20, "vws-verify")]))
        self.assertEqual(writes, [self.ADD])

    def test_red_ci_skips_the_verdict_reads(self):
        e = {"GH_TOKEN": "x", "REPO": "o/r", "EVENT": "workflow_run", "ACTION": "completed", "PRS": "7",
             "HEAD_SHA": SHA, "PR_TSV": pr(), "CHECKS_TSV": "\n".join(GREEN + [run_(9, "e2e", conclusion="failure")])}
        rc, calls, out = Stub(self, GH).run(self.script(), e)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("/reviews", calls)

    def test_failed_verdict_read_fails_the_job_without_labeling(self):
        e = {"GH_TOKEN": "x", "REPO": "o/r", "EVENT": "workflow_run", "ACTION": "completed", "PRS": "7",
             "HEAD_SHA": SHA, "PR_TSV": pr(), "CHECKS_TSV": "\n".join(GREEN), "READ_FAIL": "gh: Server Error (HTTP 500)"}
        rc, calls, out = Stub(self, GH).run(self.script(), e)
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("-X POST", calls)


if __name__ == "__main__":
    unittest.main()
