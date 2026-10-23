"""Behavior tests for scripts/codex-round-count.sh, the per-issue Codex round cap (vws-deploy#403).

Run: python3 -m unittest discover -s tests/codex
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "codex-round-count.sh"
REPO = "vws-console-api"


class RoundCountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, stdin=None, env=None):
        full_env = {k: v for k, v in os.environ.items() if k != "CODEX_ROUND_CAP"}
        full_env.update(env or {})
        return subprocess.run(
            [str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=full_env, check=False
        )

    def common(self, issue, pr):
        return ["--out-dir", self.out, "--repo", REPO, "--issue", str(issue), "--pr", str(pr)]

    def record(self, issue, pr, sha):
        res = self.run_script("record", *self.common(issue, pr), "--sha", sha)
        self.assertEqual(res.returncode, 0, res.stderr)

    def check(self, issue, pr, **kw):
        return self.run_script("check", *self.common(issue, pr), **kw)

    def test_three_rounds_on_one_pr_refuses_the_fourth(self):
        for sha in ("aaaaaaa", "bbbbbbb"):
            self.record(319, 319, sha)
            self.assertEqual(self.check(319, 319).returncode, 0)
        self.record(319, 319, "ccccccc")
        res = self.check(319, 319)
        self.assertEqual(res.returncode, 1)
        self.assertIn("already had 3 Codex rounds (max 3)", res.stderr)
        self.assertIn("split the work", res.stderr)

    def test_rebuilt_pr_for_same_issue_keeps_the_count(self):
        self.record(300, 319, "aaaaaaa")
        self.record(300, 319, "bbbbbbb")
        self.assertEqual(self.check(300, 327).returncode, 0)
        self.record(300, 327, "ccccccc")
        res = self.check(300, 327)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("issue vws-console-api#300 already had 3", res.stderr)

    def test_different_issues_are_independent(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.record(10, 11, sha)
        self.assertEqual(self.check(10, 11).returncode, 1)
        res = self.check(20, 21)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("0 of 3", res.stdout)

    def test_same_issue_number_in_another_repo_is_independent(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.record(10, 11, sha)
        res = self.run_script("check", "--out-dir", self.out, "--repo", "vws-console", "--issue", "10", "--pr", "11")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_recording_the_same_head_twice_counts_once(self):
        self.record(5, 6, "aaaaaaa1234")
        self.record(5, 6, "aaaaaaa")
        res = self.run_script("count", *self.common(5, 6))
        self.assertEqual(res.stdout.strip(), "1")

    def test_legacy_review_files_count_until_indexed(self):
        for sha in ("aaaaaaa", "bbbbbbb"):
            pathlib.Path(self.out, f"{REPO}-40-{sha}.md").write_text("review\n")
        self.assertEqual(self.run_script("count", *self.common(39, 40)).stdout.strip(), "2")
        self.record(39, 40, "aaaaaaa")
        self.assertEqual(self.run_script("count", *self.common(39, 40)).stdout.strip(), "2")
        self.record(39, 40, "ccccccc")
        self.assertEqual(self.check(39, 40).returncode, 1)

    def test_cap_is_configurable(self):
        self.record(7, 8, "aaaaaaa")
        self.record(7, 8, "bbbbbbb")
        self.record(7, 8, "ccccccc")
        self.assertEqual(self.check(7, 8, env={"CODEX_ROUND_CAP": "4"}).returncode, 0)
        self.assertEqual(self.run_script("check", *self.common(7, 8), "--cap", "2").returncode, 1)

    def test_invalid_input_is_rejected(self):
        cases = [
            ["check", *self.common(0, 8)],
            ["check", *self.common(7, "x")],
            ["check", "--out-dir", self.out, "--repo", "a/b", "--issue", "1", "--pr", "1"],
            ["check", *self.common(7, 8), "--cap", "0"],
            ["record", *self.common(7, 8), "--sha", "ZZZZZZZ"],
            ["record", *self.common(7, 8)],
            ["check-and-record", *self.common(7, 8), "--sha", "xyz"],
            ["check", *self.common(7, 8), "--bogus", "1"],
            ["nope"],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_script(*args).returncode, 2)
        self.assertFalse(pathlib.Path(self.out, "rounds.tsv").exists())

    def check_and_record(self, issue, pr, sha):
        return self.run_script("check-and-record", *self.common(issue, pr), "--sha", sha)

    def index_lines(self):
        path = pathlib.Path(self.out, "rounds.tsv")
        return path.read_text().splitlines() if path.exists() else []

    def test_check_and_record_refuses_at_the_cap_without_recording(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.assertEqual(self.check_and_record(50, 51, sha).returncode, 0)
        res = self.check_and_record(50, 52, "ddddddd")
        self.assertEqual(res.returncode, 1)
        self.assertIn("already had 3 Codex rounds (max 3)", res.stderr)
        self.assertEqual(len(self.index_lines()), 3)
        self.assertTrue(pathlib.Path(self.out, "rounds.tsv.lock").exists())

    def test_check_and_record_same_head_is_a_retry(self):
        self.assertEqual(self.check_and_record(60, 61, "aaaaaaa").returncode, 0)
        res = self.check_and_record(60, 61, "aaaaaaa")
        self.assertEqual(res.returncode, 0)
        self.assertIn("already recorded", res.stdout)
        self.assertEqual(len(self.index_lines()), 1)

    def test_parallel_check_and_record_never_passes_the_cap(self):
        n = 12
        procs = [
            subprocess.Popen(
                [str(SCRIPT), "check-and-record", *self.common(70, 71 + i % 2), "--sha", f"{i:07x}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(n)
        ]
        codes = [p.wait(timeout=60) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(sorted(codes), [0] * 3 + [1] * (n - 3))
        lines = self.index_lines()
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual(self.run_script("count", *self.common(70, 71)).stdout.strip(), "3")

    def test_issue_from_body(self):
        res = self.run_script("issue-from-body", stdin="Fix the thing.\n\nPart of #403\n")
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "403"))
        res = self.run_script("issue-from-body", stdin="Closes #55\n")
        self.assertEqual(res.stdout.strip(), "55")
        for body in ("no reference here\n", "Part of #0\n", "Part of #NN\n", ""):
            with self.subTest(body=body):
                self.assertEqual(self.run_script("issue-from-body", stdin=body).returncode, 1)


if __name__ == "__main__":
    unittest.main()
