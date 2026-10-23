"""Tests for .github/workflows/verify-queue.yml (vws-deploy#397): the needs-verify label follows CI.

The run script is executed with gh stubbed: $PR_TSV, $CHECKS_TSV and $STATUS_TSV are what the PR,
check-runs and commit-status reads print; $DELETE_FAIL makes the label DELETE fail with that text.
Run: python3 -m unittest discover -s tests/workflows
"""
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
        self.assertEqual(sorted(on), ["pull_request", "workflow_run"])

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


if __name__ == "__main__":
    unittest.main()
