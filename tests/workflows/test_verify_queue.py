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
        # Every PR workflow in the vws-* repos: vws-console names its CI "CI", vws-conformance adds english.
        self.assertEqual(on["workflow_run"], {"workflows": ["ci", "CI", "english", "repo-guard"], "types": ["completed"]})
        self.assertEqual(sorted(on["pull_request"]["types"]), ["ready_for_review", "synchronize"])
        self.assertEqual(sorted(on), ["pull_request", "workflow_run"])

    def test_issues_write_is_the_only_write(self):
        wf = load()
        perms = dict(wf["permissions"])
        for job in wf["jobs"].values():
            perms.update(job.get("permissions", {}))
        self.assertEqual(perms.pop("issues"), "write")
        self.assertTrue(all(v == "read" for v in perms.values()), perms)

    def test_never_checks_out_or_interpolates(self):
        wf = load()
        for job in wf["jobs"].values():
            for s in job["steps"]:
                self.assertNotIn("uses", s, "no actions: nothing checks out PR code")
                self.assertNotRegex(s.get("run", ""), re.compile(r"\$\{\{"), "event data only reaches the script via env")


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
