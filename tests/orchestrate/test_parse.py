"""Parsing tests for .github/orchestrate/orchestrate.py (vws-deploy#333).
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from orch_testlib import orch


class DependsOn(unittest.TestCase):
    def test_table(self):
        cases = [
            ("", [], []),
            ("Depends on #12", ["vws-iam#12"], []),
            ("depends on vws-proto#51, #7", ["vws-proto#51", "vws-iam#7"], []),
            ("Text\n  Depends on vws-deploy#333.\nDepends on #12, #12", ["vws-deploy#333", "vws-iam#12"], []),
            ("Depends on the proto PR", [], ["the proto PR"]),
            ("This depends on #4 mid-sentence", [], []),
        ]
        for body, deps, bad in cases:
            with self.subTest(body=body):
                self.assertEqual(orch.parse_deps(body, "vws-iam"), (deps, bad))


class Comments(unittest.TestCase):
    def test_table(self):
        u = "https://github.com/popododo0720/vws-iam/issues/123#issuecomment-1"
        cases = [
            ("@deepseek-dev please take this issue", ("dispatch", {})),
            ("  @deepseek-dev apply the fix", ("dispatch", {})),
            ("please ask @deepseek-dev later", None),
            (f"[deepseek-dev] Failed for {u}: dsh exited with code 0.",
             ("report", {"result": "failed", "for": u, "reason": "dsh exited with code 0.", "pr": ""})),
            (f"[deepseek-dev] Done for {u}\nPR: https://github.com/popododo0720/vws-iam/pull/130",
             ("report", {"result": "done", "for": u, "reason": "", "pr": "https://github.com/popododo0720/vws-iam/pull/130"})),
            (f"Quoting: [deepseek-dev] Done for {u}", None),
            ("/orchestrate reset-failures because the runner was fixed", ("reset", {})),
            ("Moving this task\n<!-- orchestrate:swap -->", ("marker", {"marker": "swap"})),
            ("just a comment", None),
            (None, None),
        ]
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(orch.classify_comment(body), want)

    def test_pr_key(self):
        self.assertEqual(orch.pr_key("https://github.com/popododo0720/vws-iam/pull/130"), "vws-iam#130")
        self.assertEqual(orch.pr_key("https://github.com/someone/vws-iam/pull/130"), "")


class Files(unittest.TestCase):
    def test_exclusions_has_vws_iam_123(self):
        ex = orch.load_exclusions(orch.os.path.join(orch.HERE, "exclusions.tsv"))
        self.assertIn("https://github.com/popododo0720/vws-iam/issues/123#issuecomment-6309297481", ex)

    def test_repos_tsv_plus_extra(self):
        names = orch.load_repos(orch.os.path.join(orch.HERE, "..", "drift", "repos.tsv"), "vws-conformance,vws-iam")
        self.assertIn("vws-deploy", names)
        self.assertEqual(names.count("vws-iam"), 1)
        self.assertEqual(names[-1], "vws-conformance")

    def test_ci_verdict_ignores_verify_checks(self):
        runs = {"check_runs": [{"id": 1, "name": "verify-gate", "conclusion": "failure"},
                               {"id": 2, "name": "lint", "conclusion": "failure"},
                               {"id": 3, "name": "lint", "conclusion": "success"}]}
        self.assertEqual(orch.ci_verdict(runs, {"statuses": []}), "ok")
        runs["check_runs"].append({"id": 4, "name": "test", "conclusion": "timed_out"})
        self.assertEqual(orch.ci_verdict(runs, {}), "failing")
        self.assertEqual(orch.ci_verdict({}, {"statuses": [{"context": "ci", "state": "error"}]}), "failing")


if __name__ == "__main__":
    unittest.main()
