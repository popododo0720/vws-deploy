"""Parsing tests for .github/orchestrate/orchestrate.py (vws-deploy#333).
Run: python3 -m unittest discover -s tests/orchestrate"""
import re
import unittest

from orch_testlib import SCRIPT, orch


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


class PartOf(unittest.TestCase):
    def test_owner_repo_form_is_not_a_reference(self):
        # vws-deploy#477: pr-hygiene rejects `Part of popododo0720/vws-x#N` (vws-deploy#482), so
        # Rule D accepts the same set and only #N and vws-x#N map to an issue.
        cases = [("Part of #12", "vws-deploy#12"),
                 ("Part of vws-iam#12", "vws-iam#12"),
                 ("Part of popododo0720/vws-iam#12", ""),
                 ("Part of acme/vws-iam#12", ""),
                 ("Part of popododo0720/#12", "")]
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(orch.grok_link({"repo": "vws-deploy", "is_pr": True, "body": body}), want)


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

    def test_vws_pr_key(self):
        cases = {"https://github.com/popododo0720/vws-iam/pull/130": "vws-iam#130",
                 "https://github.com/popododo0720/other-repo/pull/5": "",
                 "https://github.com/someone/vws-iam/pull/5": "",
                 "https://github.com/popododo0720/vws-../pull/5": "",
                 "not a url": ""}
        for url, want in cases.items():
            with self.subTest(url):
                self.assertEqual(orch.vws_pr_key(url), want)


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

    def test_ci_verdict_ignores_the_called_verify_gate(self):
        # vws-deploy#402: the gate runs as "gate / verify-gate" next to its bare verify-gate shim.
        runs = {"check_runs": [{"id": 1, "name": "gate / verify-gate", "conclusion": "failure"},
                               {"id": 2, "name": "verify-gate", "conclusion": "failure"}]}
        self.assertEqual(orch.ci_verdict(runs, {}), "ok")
        runs["check_runs"].append({"id": 3, "name": "hygiene / pr-hygiene", "conclusion": "failure"})
        self.assertEqual(orch.ci_verdict(runs, {}), "failing")


class VerifyCheckParity(unittest.TestCase):
    """ci_verdict is failing exactly when pr_ci is red (vws-deploy#502). One set of check names."""

    def cr(self, rid, name, status="completed", conclusion="success"):
        return {"id": rid, "name": name, "status": status, "conclusion": conclusion}

    def test_vectors(self):
        ok = self.cr(1, "test")
        # expect_red is the shared answer. A pending check is neither failing nor red.
        cases = [
            ("gate / verify-gate failing", [ok, self.cr(2, "gate / verify-gate", conclusion="failure")], None, False),
            ("verify-gate failing", [ok, self.cr(2, "verify-gate", conclusion="failure")], None, False),
            ("vws-verify failing", [ok, self.cr(2, "vws-verify", conclusion="failure")], None, False),
            ("needs-verify job failing", [ok, self.cr(2, "needs-verify", conclusion="failure")], None, False),
            ("needs-verify status failing", [ok], [{"context": "needs-verify", "state": "failure"}], False),
            ("real check failing", [self.cr(1, "test", conclusion="failure")], None, True),
            ("real status failing", [ok], [{"context": "ci", "state": "error"}], True),
            ("pending check", [self.cr(1, "test", status="in_progress", conclusion=None)], None, False),
        ]
        for name, runs, statuses, expect_red in cases:
            with self.subTest(name):
                status = {"statuses": list(statuses or [])}
                body = {"check_runs": runs, "total_count": len(runs)}
                failing = orch.ci_verdict(body, status) == "failing"
                red = orch.pr_ci(body, status)[0] == "red"
                self.assertEqual(failing, red)
                self.assertEqual(failing, expect_red)

    def test_one_literal_verify_check_set(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        sets = re.findall(r"\{[^{}]*gate / verify-gate[^{}]*\}", src)
        self.assertEqual(sets, ['{"verify-gate", "gate / verify-gate", "vws-verify", "needs-verify"}'])
        self.assertFalse(hasattr(orch, "WAIT_CHECKS"))
        self.assertEqual(orch.VERIFY_CHECKS,
                         {"verify-gate", "gate / verify-gate", "vws-verify", "needs-verify"})


class CheckIdentity(unittest.TestCase):
    """vws-deploy#368: a same-named passing job in another workflow must not hide a failing one."""

    @staticmethod
    def cr(rid, conclusion, suite, workflow, started):
        return {"id": rid, "name": "test", "app_id": 15368, "status": "completed", "conclusion": conclusion,
                "check_suite": {"id": suite}, "workflow": workflow, "started_at": started}

    def runs(self, second_workflow):
        return {"check_runs": [
            self.cr(1, "failure", 10, ".github/workflows/a.yml@pull_request", "2026-10-28T01:00:00Z"),
            self.cr(2, "success", 11, second_workflow, "2026-10-28T02:00:00Z")], "total_count": 2}

    def test_failure_in_another_workflow_is_not_hidden(self):
        runs = self.runs(".github/workflows/b.yml@pull_request")
        self.assertEqual(orch.ci_verdict(runs, {"statuses": []}), "failing")
        self.assertEqual(orch.pr_ci(runs, {"statuses": []}), ("red", False))

    def test_rerun_in_the_same_workflow_supersedes(self):
        runs = self.runs(".github/workflows/a.yml@pull_request")
        self.assertEqual(orch.ci_verdict(runs, {"statuses": []}), "ok")
        self.assertEqual(orch.pr_ci(runs, {"statuses": []}), ("green", False))


class CiReads(unittest.TestCase):
    """ci_reads annotates a name collision with the workflow map, and only then pays for the extra GET."""

    SHA = "a" * 40

    class Gh:
        def __init__(self, checks, fail_map=False):
            self.calls, self.checks, self.fail_map = [], checks, fail_map

        def get(self, path):
            self.calls.append(path)
            if path.endswith("/check-runs?per_page=100"):
                return {"check_runs": self.checks}, None
            if path.endswith("/status"):
                return {"statuses": []}, None
            if self.fail_map:
                raise orch.GhError(500, "workflow map read failed")
            return {"workflow_runs": [{"check_suite_id": 10, "path": ".github/workflows/a.yml", "event": "pull_request"},
                                      {"check_suite_id": 11, "path": ".github/workflows/b.yml", "event": "pull_request"}]}, None

    @staticmethod
    def cr(rid, conclusion, suite):
        return {"id": rid, "name": "test", "status": "completed", "conclusion": conclusion,
                "app": {"id": 15368}, "check_suite": {"id": suite}, "started_at": f"2026-10-28T0{suite - 9}:00:00Z"}

    def test_collision_maps_suites_to_workflows(self):
        gh = self.Gh([self.cr(1, "failure", 10), self.cr(2, "success", 11)])
        runs, _ = orch.ci_reads(gh, "vws-deploy", self.SHA)
        self.assertEqual([r["workflow"] for r in runs["check_runs"]],
                         [".github/workflows/a.yml@pull_request", ".github/workflows/b.yml@pull_request"])
        self.assertEqual(orch.pr_ci(runs, {"statuses": []}), ("red", False))
        self.assertIn(f"repos/popododo0720/vws-deploy/actions/runs?head_sha={self.SHA}&per_page=100", gh.calls)

    def test_a_single_suite_needs_no_workflow_map(self):
        gh = self.Gh([self.cr(1, "success", 10)])
        runs, _ = orch.ci_reads(gh, "vws-deploy", self.SHA)
        self.assertEqual(len(gh.calls), 2)
        self.assertNotIn("workflow", runs["check_runs"][0])

    def test_a_failed_workflow_map_keeps_the_failure_blocking(self):
        gh = self.Gh([self.cr(1, "failure", 10), self.cr(2, "success", 11)], fail_map=True)
        runs, _ = orch.ci_reads(gh, "vws-deploy", self.SHA)
        self.assertEqual(orch.pr_ci(runs, {"statuses": []}), ("red", False))


if __name__ == "__main__":
    unittest.main()
