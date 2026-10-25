"""vws-deploy#439 AC2 class 1: .github/drift/trusted_read_lint.py fails on workflow reads of event labels or
head_commit.committer, and on a marker match in orchestrate.py outside trusted_read.py.
Run: python3 -m unittest discover -s tests/drift"""
import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "class1")
spec = importlib.util.spec_from_file_location("trusted_read_lint", os.path.join(ROOT, ".github", "drift", "trusted_read_lint.py"))
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rules(findings):
    return sorted({f.split(": ")[1] for f in findings})


class Workflows(unittest.TestCase):
    def check(self, name):
        return lint.lint_workflow(f"fixtures/{name}", read(os.path.join(FIX, name)))

    def test_event_labels_fail(self):
        """The pre-fix verify-reset: a re-run replays the push-time labels."""
        found = self.check("verify-reset-event-labels.yml")
        self.assertEqual(rules(found), ["event-labels"])
        self.assertEqual(len(found), 1, found)

    def test_committer_fails(self):
        found = self.check("committer.yml")
        self.assertEqual(rules(found), ["committer"])
        self.assertEqual(len(found), 2, found)

    def test_live_reads_and_comments_pass(self):
        self.assertEqual(self.check("live-read.yml"), [])

    def test_index_form_fails(self):
        self.assertEqual(rules(lint.lint_workflow("x.yml", "x: ${{ github.event.pull_request['labels'] }}\n")),
                         ["event-labels"])


class Orchestrate(unittest.TestCase):
    def setUp(self):
        self.res = lint.marker_regexes(read(os.path.join(ROOT, ".github", "orchestrate", "trusted_read.py")))

    def test_marker_regexes_come_from_the_module(self):
        self.assertEqual(self.res, {"CI_FAIL_RE", "KEY_RE", "MARKER_KIND_RE", "FAILURE_SECTION_RE"})

    def test_matches_outside_the_module_fail(self):
        found = lint.marker_matches("orch.py", read(os.path.join(FIX, "orch_marker_outside.py")), self.res)
        lines = sorted(int(f.split(":")[1]) for f in found)
        self.assertEqual(rules(found), ["marker-match"])
        # KEY_RE compiled outside the module, marker_seen, recheck_key, status_wakes, keys, rework, rework_re
        self.assertEqual(lines, [6, 14, 18, 22, 26, 30, 34], found)

    def test_building_bodies_and_calling_the_module_pass(self):
        self.assertEqual(lint.marker_matches("orch.py", read(os.path.join(FIX, "orch_marker_ok.py")), self.res), [])


class Repo(unittest.TestCase):
    def test_the_repo_is_clean(self):
        self.assertEqual(lint.lint(ROOT), [])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in (".github/orchestrate", ".github/workflows"):
                os.makedirs(os.path.join(d, rel))
            shutil.copy(os.path.join(ROOT, ".github", "orchestrate", "trusted_read.py"), os.path.join(d, ".github", "orchestrate"))
            shutil.copy(os.path.join(FIX, "orch_marker_ok.py"), os.path.join(d, ".github", "orchestrate", "orchestrate.py"))
            shutil.copy(os.path.join(FIX, "live-read.yml"), os.path.join(d, ".github", "workflows"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lint.main(["--root", d]), 0)
            shutil.copy(os.path.join(FIX, "committer.yml"), os.path.join(d, ".github", "workflows"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lint.main(["--root", d]), 1)
            self.assertIn(".github/workflows/committer.yml:7: committer:", out.getvalue())

    def test_a_missing_module_fails(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github", "orchestrate"))
            shutil.copy(os.path.join(FIX, "orch_marker_ok.py"), os.path.join(d, ".github", "orchestrate", "orchestrate.py"))
            self.assertEqual(rules(lint.lint(d)), ["trusted-read-missing"])


if __name__ == "__main__":
    unittest.main()
