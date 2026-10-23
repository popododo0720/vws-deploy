"""Behavior tests for scripts/codex-round-count.sh, the per-issue Codex round cap (vws-deploy#403).

Run: python3 -m unittest discover -s tests/codex
"""
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "codex-round-count.sh"
REPO = "vws-console-api"
HEADER = "# codex-round-index v1\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.index = pathlib.Path(self.out, "rounds.tsv")
        self.bin = pathlib.Path(self.tmp.name, "bin")
        self.bin.mkdir()

    def tearDown(self):
        os.chmod(self.out, 0o755)
        if self.index.exists() and not self.index.is_dir():
            os.chmod(self.index, 0o644)
        self.tmp.cleanup()

    def run_script(self, *args, stdin=None, env=None):
        full_env = {k: v for k, v in os.environ.items() if k != "CODEX_ROUND_CAP"}
        full_env["PATH"] = f"{self.bin}:{full_env['PATH']}"
        full_env.update(env or {})
        return subprocess.run(
            [str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=full_env, check=False
        )

    def init(self):
        res = self.run_script("init", "--out-dir", self.out)
        self.assertEqual(res.returncode, 0, res.stderr)

    def q(self, issue, pr=None, repo=REPO):
        args = ["--out-dir", self.out, "--repo", repo, "--issue", str(issue)]
        return args + (["--pr", str(pr)] if pr is not None else [])

    def record(self, issue, pr, sha):
        res = self.run_script("record", *self.q(issue, pr), "--sha", sha)
        self.assertEqual(res.returncode, 0, res.stderr)

    def check(self, issue, **kw):
        return self.run_script("check", *self.q(issue), **kw)

    def car(self, issue, pr, sha, repo=REPO):
        return self.run_script("check-and-record", *self.q(issue, pr, repo), "--sha", sha)

    def count(self, issue):
        res = self.run_script("count", *self.q(issue))
        self.assertEqual(res.returncode, 0, res.stderr)
        return int(res.stdout.strip())

    def rows(self):
        return self.index.read_text().splitlines()[1:]

    def review_file(self, pr, sha, repo=REPO):
        pathlib.Path(self.out, f"{repo}-{pr}-{sha}.md").write_text("review\n")

    def stub_gh(self, bodies):
        """gh pr view N ... prints bodies[N]; unknown PRs fail like gh does."""
        cases = "".join(f'  {pr}) printf "%s\\n" "{body}" ;;\n' for pr, body in bodies.items())
        script = f'#!/bin/sh\necho "$@" >> "{self.bin}/gh.log"\ncase $3 in\n{cases}  *) echo "no PR" >&2; exit 1 ;;\nesac\n'
        path = self.bin / "gh"
        path.write_text(script)
        path.chmod(0o755)


class CapTest(Base):
    def setUp(self):
        super().setUp()
        self.init()

    def test_three_rounds_on_one_pr_refuses_the_fourth(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.assertEqual(self.car(319, 319, sha).returncode, 0)
        res = self.car(319, 319, "ddddddd")
        self.assertEqual(res.returncode, 1)
        self.assertIn("already had 3 Codex rounds (max 3)", res.stderr)
        self.assertIn("split the work", res.stderr)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.check(319).returncode, 1)

    def test_rebuilt_pr_for_same_issue_keeps_the_count(self):
        self.car(300, 319, "aaaaaaa")
        self.car(300, 319, "bbbbbbb")
        self.assertEqual(self.car(300, 327, "ccccccc").returncode, 0)
        res = self.car(300, 327, "ddddddd")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("issue vws-console-api#300 already had 3", res.stderr)

    def test_different_issues_and_repos_are_independent(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.record(10, 11, sha)
        self.assertEqual(self.check(10).returncode, 1)
        res = self.check(20)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("0 of 3", res.stdout)
        self.assertEqual(self.car(10, 11, "ddddddd", repo="vws-console").returncode, 0)

    def test_same_head_twice_counts_once(self):
        self.record(5, 6, "aaaaaaa1234")
        res = self.car(5, 6, "aaaaaaa")
        self.assertEqual(res.returncode, 0)
        self.assertIn("already recorded", res.stdout)
        self.assertEqual(self.count(5), 1)

    def test_cap_is_configurable(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.record(7, 8, sha)
        self.assertEqual(self.check(7, env={"CODEX_ROUND_CAP": "4"}).returncode, 0)
        self.assertEqual(self.run_script("check", *self.q(7), "--cap", "2").returncode, 1)

    def test_release_frees_a_round_only_without_a_review_file(self):
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.car(9, 9, sha)
        self.review_file(9, "aaaaaaa")
        res = self.run_script("release", "--out-dir", self.out, "--repo", REPO, "--pr", "9", "--sha", "aaaaaaa")
        self.assertEqual(res.returncode, 2)
        res = self.run_script("release", "--out-dir", self.out, "--repo", REPO, "--pr", "9", "--sha", "ccccccc")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.count(9), 2)
        self.assertEqual(self.car(9, 9, "ddddddd").returncode, 0)

    def test_parallel_check_and_record_never_passes_the_cap(self):
        n = 12
        procs = [
            subprocess.Popen(
                [str(SCRIPT), "check-and-record", *self.q(70, 71 + i % 2), "--sha", f"{i:07x}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(n)
        ]
        codes = [p.wait(timeout=60) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(sorted(codes), [0] * 3 + [1] * (n - 3))
        self.assertEqual(len(self.rows()), 3, self.rows())
        self.assertEqual(self.count(70), 3)

    def test_invalid_input_is_rejected(self):
        before = self.index.read_text()
        cases = [
            ["check", *self.q(0)],
            ["check", "--out-dir", self.out, "--repo", "a/b", "--issue", "1"],
            ["check", *self.q(7), "--cap", "0"],
            ["record", *self.q(7, "x"), "--sha", "aaaaaaa"],
            ["record", *self.q(7, 8), "--sha", "ZZZZZZZ"],
            ["check-and-record", *self.q(7, 8)],
            ["check", *self.q(7), "--bogus", "1"],
            ["backfill", "--out-dir", self.out, "--map", "nope"],
            ["nope"],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_script(*args).returncode, 2)
        self.assertEqual(self.index.read_text(), before)


class FailClosedTest(Base):
    """A broken index never reads as 0 rounds and never takes a record."""

    def assert_refused(self, why):
        for args in (["check", *self.q(9)], ["count", *self.q(9)], ["check-and-record", *self.q(9, 10), "--sha", "eeeeeee"]):
            with self.subTest(args=args[0]):
                res = self.run_script(*args)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertIn(why, res.stderr)

    def three_rounds(self):
        self.init()
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.record(9, 10, sha)

    def test_missing_index_without_init(self):
        self.assert_refused("index missing")

    def test_deleted_index(self):
        self.three_rounds()
        self.index.unlink()
        self.assert_refused("index missing")

    def test_corrupt_line_appended(self):
        self.three_rounds()
        with self.index.open("a") as f:
            f.write("garbage\n")
        self.assert_refused("corrupt")

    def test_index_overwritten_with_garbage(self):
        self.three_rounds()
        self.index.write_text("garbage\nmore garbage\n")
        self.assert_refused("corrupt")

    def test_truncated_to_empty(self):
        self.three_rounds()
        self.index.write_text("")
        self.assert_refused("corrupt")

    def test_truncated_last_line(self):
        self.three_rounds()
        self.index.write_text(self.index.read_text()[:-5])
        self.assert_refused("corrupt")

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_unreadable_index(self):
        self.three_rounds()
        os.chmod(self.index, 0o200)
        self.assert_refused("not readable")

    def test_index_is_a_directory(self):
        self.index.mkdir()
        self.assert_refused("not a regular file")

    def test_init_keeps_an_existing_index_and_rejects_a_corrupt_one(self):
        self.three_rounds()
        self.init()
        self.assertEqual(len(self.rows()), 3)
        with self.index.open("a") as f:
            f.write("garbage\n")
        self.assertEqual(self.run_script("init", "--out-dir", self.out).returncode, 2)


class BackfillTest(Base):
    """Rounds from PRs reviewed before the index existed (vws-deploy#403 verify finding)."""

    def setUp(self):
        super().setUp()
        self.init()
        for sha in ("aaaaaaa", "bbbbbbb", "ccccccc"):
            self.review_file(319, sha)

    def test_unindexed_review_files_refuse_check(self):
        res = self.car(300, 327, "ddddddd")
        self.assertEqual(res.returncode, 2)
        self.assertIn("not in the index", res.stderr)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.check(300).returncode, 2)

    def test_backfill_from_pr_body_then_rebuilt_pr_is_refused(self):
        self.stub_gh({319: "Rework. Part of #300"})
        res = self.run_script("backfill", "--out-dir", self.out)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sorted(self.rows()), [f"{REPO}\t300\t319\t{s}" for s in ("aaaaaaa", "bbbbbbb", "ccccccc")])
        self.assertEqual((self.bin / "gh.log").read_text().count("pr view 319 -R popododo0720/vws-console-api"), 1)
        res = self.car(300, 327, "ddddddd")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("already had 3", res.stderr)
        self.assertEqual(self.car(301, 327, "ddddddd").returncode, 0)

    def test_backfill_explicit_map_and_short_repo_names(self):
        self.review_file(5, "1234567", repo="console")
        self.stub_gh({5: "Part of popododo0720/vws-console#44"})
        res = self.run_script("backfill", "--out-dir", self.out, "--map", f"{REPO}#319=300")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("console\t44\t5\t1234567", self.rows())
        self.assertIn("pr view 5 -R popododo0720/vws-console", (self.bin / "gh.log").read_text())
        self.assertEqual(self.car(300, 327, "ddddddd").returncode, 1)

    def test_backfill_is_all_or_nothing_and_dry_run_writes_nothing(self):
        self.review_file(8, "1234567")
        self.stub_gh({319: "Part of #300"})
        res = self.run_script("backfill", "--out-dir", self.out)
        self.assertEqual(res.returncode, 2)
        self.assertIn(f"--map {REPO}#8=ISSUE", res.stderr)
        self.assertEqual(self.rows(), [])
        res = self.run_script("backfill", "--out-dir", self.out, "--map", f"{REPO}#8=12", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("would be indexed", res.stdout)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.run_script("backfill", "--out-dir", self.out, "--map", f"{REPO}#8=12").returncode, 0)
        self.assertEqual(len(self.rows()), 4)
        res = self.run_script("backfill", "--out-dir", self.out)
        self.assertIn("backfill: 0 rounds", res.stdout)
        self.assertEqual(len(self.rows()), 4)

    def test_other_file_names_are_ignored(self):
        for name in ("compute-v2-586d673.md", f"{REPO}-recopy-000abda.md", "vws-console-api-api-1-1234567.md"):
            pathlib.Path(self.out, name).write_text("x\n")
        self.stub_gh({319: "Part of #300"})
        self.assertEqual(self.run_script("backfill", "--out-dir", self.out, "--repo", REPO).returncode, 0)
        self.assertEqual(len(self.rows()), 3)


class IssueFromBodyTest(Base):
    def test_issue_from_body(self):
        for body, want in (("Fix.\n\nPart of #403\n", "403"), ("Closes #55\n", "55"), ("Part of popododo0720/vws-deploy#403\n", "403")):
            with self.subTest(body=body):
                res = self.run_script("issue-from-body", stdin=body)
                self.assertEqual((res.returncode, res.stdout.strip()), (0, want))
        for body in ("no reference here\n", "Part of #0\n", "Part of #NN\n", ""):
            with self.subTest(body=body):
                self.assertEqual(self.run_script("issue-from-body", stdin=body).returncode, 1)


if __name__ == "__main__":
    unittest.main()
