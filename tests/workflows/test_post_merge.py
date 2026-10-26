"""Tests for .github/ac/ac-lint.sh (ported from the box vws-issue-ac skill), vws-deploy#399, and the
retirement of the post-merge ac-pending labeler, vws-deploy#526 AC4: orchestrate rule T ticks boxes
and removes ac-pending, so no workflow may add it back after a merge.

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LINT = os.path.join(ROOT, ".github", "ac", "ac-lint.sh")
POST = os.path.join(ROOT, ".github", "ac", "post-merge.sh")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
WF_PATH = os.path.join(ROOT, ".github", "workflows", "post-merge.yml")

OPEN_BODY = "## Acceptance criteria\n- [x] parser (evidence: tests/x_test.go:12)\n- [ ] live run on vws-test\n"
DONE_BODY = ("## Acceptance criteria\n- [x] parser (evidence: tests/x_test.go:12)\n"
             "- [x] live run\n  evidence: /workspace/vws-test-x.txt lines 3-9\n")
NOEV_BODY = "- [x] parser works\n- [x] also done, see https://github.com/o/r/actions/runs/1\n"
NO_AC_BODY = "Just a description, no boxes.\n"


def lint(body):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    try:
        return subprocess.run(["bash", LINT, "--file", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)


class AcLint(unittest.TestCase):
    def test_open_box_is_listed_and_exit_0(self):
        r = lint(OPEN_BODY)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"(?m)^OK +\[x\] parser")
        self.assertRegex(r.stdout, r"(?m)^OPEN +\[ \] live run")
        self.assertIn("2 boxes, 0 ticked without evidence", r.stdout)

    def test_indented_evidence_counts(self):
        r = lint(DONE_BODY)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("OPEN", r.stdout)
        self.assertNotIn("NO-EVIDENCE", r.stdout)

    def test_tick_without_evidence_exits_1(self):
        r = lint(NOEV_BODY)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"(?m)^NO-EVIDENCE +\[x\] parser works")
        self.assertRegex(r.stdout, r"(?m)^OK +\[x\] also done")

    def test_evidence_kinds(self):
        for ev in ("https://x.y/z", "/workspace/a.txt", "foo.go:12", "#123", "vws-iam#4", "TestParse"):
            with self.subTest(ev=ev):
                self.assertRegex(lint(f"- [x] item {ev}\n").stdout, r"(?m)^OK ")

    def test_crlf_and_star_bullets(self):
        r = lint("* [X] item #1\r\n* [ ] open one\r\n")
        self.assertRegex(r.stdout, r"(?m)^OK +\[X\] item")
        self.assertRegex(r.stdout, r"(?m)^OPEN ")

    def test_no_boxes(self):
        r = lint(NO_AC_BODY)
        self.assertEqual(r.returncode, 0)
        self.assertIn("0 boxes", r.stdout)

    def test_usage_error(self):
        self.assertEqual(subprocess.run(["bash", LINT], capture_output=True).returncode, 2)


class PostMergeRetired(unittest.TestCase):
    def test_post_merge_workflow_is_gone(self):
        self.assertFalse(os.path.exists(WF_PATH), "post-merge.yml must stay removed (vws-deploy#526 AC4)")

    def test_post_merge_labeler_script_is_gone(self):
        self.assertFalse(os.path.exists(POST), "post-merge.sh must stay removed (vws-deploy#526 AC4)")

    def test_no_workflow_adds_ac_pending(self):
        # Only orchestrate rule T touches ac-pending now, and it only removes it.
        for name in sorted(os.listdir(WORKFLOWS)):
            with open(os.path.join(WORKFLOWS, name)) as f:
                self.assertNotIn("ac-pending", f.read(), name)

    def test_ac_lint_stays_for_pr_hygiene(self):
        self.assertTrue(os.path.exists(LINT))
        with open(os.path.join(WORKFLOWS, "repo-guard-pr-hygiene.yml")) as f:
            self.assertIn(".github/ac/ac-lint.sh", f.read())


if __name__ == "__main__":
    unittest.main()
