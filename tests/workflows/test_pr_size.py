"""Tests for scripts/guard-agents.sh pr-size, the PR size limit in repo-guard pr-hygiene (vws-deploy#401).

Each test builds a throwaway git repo with a base and a head commit. Run: python3 -m unittest discover -s tests/workflows
"""
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(ROOT, "scripts", "guard-agents.sh")
BODY = "Part of #401\n"
REASON = "Size-exception: one generated client regenerated after the proto bump\n"


def lines(n, tag="x"):
    return "".join(f"{tag} {i}\n" for i in range(n))


class PrSize(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.repo])
        self.git("init", "-q", "-b", "main")
        self.write(".github/pr-size.conf", "# PR size limit\nlimit=800\n")
        self.write("README.md", "base\n")
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@e", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        p = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def run_size(self, body=BODY, head=None):
        summary = os.path.join(self.repo, ".summary")
        env = dict(os.environ, PR_BODY=body, GITHUB_STEP_SUMMARY=summary)
        r = subprocess.run(["bash", GUARD, "pr-size", self.base, head or self.git("rev-parse", "HEAD")],
                           cwd=self.repo, env=env, capture_output=True, text=True)
        s = open(summary).read() if os.path.exists(summary) else ""
        return r.returncode, r.stdout + r.stderr, s

    def test_under_limit_passes(self):
        self.write("a.txt", lines(10))
        self.commit("small")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 0, out)
        self.assertIn("10 changed lines", out)

    def test_exactly_the_limit_passes(self):
        self.write("a.txt", lines(800))
        self.commit("edge")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 0, out)

    def test_over_limit_fails(self):
        self.write("a.txt", lines(700))
        self.write("b.go", lines(101))
        self.commit("big")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 1, out)
        self.assertIn("801 changed lines", out)
        self.assertIn("limit 800", out)
        self.assertIn("Size-exception:", out)

    def test_deleted_lines_count(self):
        self.write("a.txt", lines(500))
        self.commit("add")
        self.base = self.git("rev-parse", "HEAD")
        self.write("a.txt", lines(500, "y"))
        self.commit("rewrite")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 1, out)
        self.assertIn("1000 changed lines", out)

    def test_excluded_paths_are_ignored(self):
        for p in ("api/v1/ec2.pb.go", "vendor/github.com/x/y.go", "internal/vendor/z.go", "third_party/lib/a.c",
                  "go.sum", "tools/go.sum", "web/package-lock.json", "pnpm-lock.yaml", "pkg/testdata/big.json",
                  "testdata/golden.txt"):
            self.write(p, lines(900))
        self.write("main.go", lines(20))
        self.commit("generated")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 0, out)
        self.assertIn("20 changed lines", out)
        self.assertIn("9000 excluded", out)

    def test_lookalike_paths_still_count(self):
        for p in ("vendors/a.go", "myvendor/b.go", "go.sum.bak", "pb.go.txt", "testdata.go", "lock/pnpm-lock.yml"):
            self.write(p, lines(150))
        self.commit("lookalikes")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 1, out)
        self.assertIn("900 changed lines", out)

    def test_rename_without_edit_is_small(self):
        self.write("old/a.txt", lines(900))
        self.commit("add")
        self.base = self.git("rev-parse", "HEAD")
        self.git("mv", "old", "new")
        self.commit("move")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 0, out)

    def test_size_exception_passes_with_a_warning(self):
        self.write("a.txt", lines(900))
        self.commit("big")
        rc, out, summary = self.run_size(BODY + REASON)
        self.assertEqual(rc, 0, out)
        self.assertIn("::warning::", out)
        self.assertIn("one generated client regenerated after the proto bump", summary)
        self.assertIn("900", summary)

    def test_bad_size_exceptions_fail(self):
        self.write("a.txt", lines(900))
        self.commit("big")
        for line in ("Size-exception:\n", "Size-exception:    \n", "Size-exception: <reason>\n",
                     "Size-exception: big\n", "Size-exception: 1234567890\n", "size-exception: a large but needed change\n",
                     "Size exception: a large but needed change\n", "  Size-exception: a large but needed change\n",
                     "<!-- " + REASON.strip() + " -->\n", "```\n" + REASON + "```\n"):
            with self.subTest(line=line):
                rc, out, _ = self.run_size(BODY + line)
                self.assertEqual(rc, 1, out)

    def test_malformed_exception_fails_even_under_the_limit(self):
        self.write("a.txt", lines(10))
        self.commit("small")
        rc, out, _ = self.run_size(BODY + "Size-exception:\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("Size-exception", out)

    def test_limit_comes_from_the_base_commit(self):
        self.write("a.txt", lines(900))
        self.write(".github/pr-size.conf", "limit=100000\n")
        self.commit("raise own limit")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 1, out)
        self.assertIn("limit 800", out)

    def test_lower_base_limit(self):
        self.write(".github/pr-size.conf", "limit=5\n")
        self.base = self.commit("lower")
        self.write("a.txt", lines(6))
        self.commit("six")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 1, out)
        self.assertIn("limit 5", out)

    def test_missing_config_defaults_to_800(self):
        self.git("rm", "-q", ".github/pr-size.conf")
        self.base = self.commit("no conf")
        self.write("a.txt", lines(801))
        self.commit("big")
        rc, out, _ = self.run_size()
        self.assertEqual(rc, 1, out)
        self.assertIn("limit 800", out)

    def test_bad_config_fails(self):
        for conf in ("limit=abc\n", "limit=0\n", "limit=\n", "limit=8 00\n", "size=800\n", "limit=800\nlimit=900\n"):
            with self.subTest(conf=conf):
                self.write(".github/pr-size.conf", conf)
                self.base = self.commit("conf")
                self.write("a.txt", lines(1) + conf)
                self.commit("small")
                rc, out, _ = self.run_size()
                self.assertEqual(rc, 1, out)
                self.assertIn("pr-size.conf", out)

    def test_needs_two_commits(self):
        rc, _, _ = self.run_size(head="0" * 40)
        self.assertNotEqual(rc, 0)


class Wiring(unittest.TestCase):
    def test_repo_config_is_800(self):
        self.assertIn("limit=800", open(os.path.join(ROOT, ".github", "pr-size.conf")).read().splitlines())

    def test_pr_hygiene_runs_pr_size(self):
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "repo-guard-reusable.yml")))
        steps = wf["jobs"]["pr-hygiene"]["steps"]
        s = next(s for s in steps if "pr-size" in s.get("run", ""))
        self.assertEqual(s["run"].strip(), 'scripts/guard-agents.sh pr-size "$BASE_SHA" "$HEAD_SHA"')
        self.assertEqual(s["env"]["PR_BODY"], "${{ github.event.pull_request.body }}")
        self.assertEqual(s["env"]["BASE_SHA"], "${{ github.event.pull_request.base.sha }}")
        self.assertEqual(s["env"]["HEAD_SHA"], "${{ github.event.pull_request.head.sha }}")
        self.assertLess(steps.index(s), len(steps) - 1, "pr-size must run before the passed step")

    def test_selftest_covers_pr_size(self):
        r = subprocess.run(["bash", GUARD, "selftest"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for label in ("pr-size under the limit passes", "pr-size over the limit fails",
                      "pr-size over the limit only in excluded paths passes", "pr-size with a Size-exception passes",
                      "pr-size with an empty Size-exception reason fails"):
            self.assertIn(label, r.stdout)


if __name__ == "__main__":
    unittest.main()
