"""Rule D merge ordering (vws-deploy#397): PRs that touch .github/orchestrate/orchestrate.py, a
repo-guard*.yml workflow or AGENTS.md merge one at a time across all repos, oldest verify approval
first. Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from orch_testlib import NOW, orch
from test_merge import candidate, check, ctx, green_checks, merges, reasons, run, snap


def pr(repo, number, files, approved_hours_ago):
    c = candidate(repo=repo, number=number)
    sha = c["head_sha"]
    checks = [x for x in green_checks(sha) if x["name"] != "vws-verify"]
    checks.append(dict(check(9, "vws-verify", sha, app=5313004), started_at=orch.iso(NOW - approved_hours_ago * 3600),
                       completed_at=orch.iso(NOW - approved_hours_ago * 3600)))
    c["merge_info"].update(checks=checks, files=list(files))
    return c


TWO = {"vws-deploy": ctx(), "vws-iam": ctx()}


class SerialMerge(unittest.TestCase):
    def test_serial_paths(self):
        for path in (".github/orchestrate/orchestrate.py", ".github/workflows/repo-guard.yml",
                     ".github/workflows/repo-guard-verify-gate.yml", "AGENTS.md"):
            self.assertTrue(orch.serial_merge_pr({"files": [path]}), path)
        for path in (".github/workflows/ci.yml", "docs/AGENTS.md.bak", "tests/orchestrate/test_merge.py"):
            self.assertFalse(orch.serial_merge_pr({"files": [path]}), path)
        self.assertTrue(orch.serial_merge_pr({}))  # files not read: treated as serial (fail closed)

    def test_one_serial_merge_per_tick_across_repos_oldest_first(self):
        a = pr("vws-deploy", 500, [".github/orchestrate/orchestrate.py"], 1)
        b = pr("vws-iam", 600, ["AGENTS.md"], 3)
        r = run(snap(a, b, merge=TWO))
        self.assertEqual(merges(r), ["vws-iam#600"])
        self.assertEqual(reasons(r, "vws-deploy#500"), ["serial_after:vws-iam#600"])

    def test_oldest_approval_wins_inside_a_repo(self):
        newer = pr("vws-deploy", 500, ["AGENTS.md"], 1)
        older = pr("vws-deploy", 501, [".github/workflows/repo-guard.yml"], 5)
        r = run(snap(newer, older))
        self.assertEqual(merges(r), ["vws-deploy#501"])
        self.assertEqual(reasons(r, "vws-deploy#500"), ["serial_after:vws-deploy#501"])

    def test_a_repo_that_loses_the_serial_slot_still_merges_other_prs(self):
        a = pr("vws-deploy", 500, ["AGENTS.md"], 5)
        b = pr("vws-iam", 600, ["AGENTS.md"], 1)
        c = pr("vws-iam", 601, ["internal/x.go"], 1)
        r = run(snap(a, b, c, merge=TWO))
        self.assertEqual(merges(r), ["vws-deploy#500", "vws-iam#601"])
        self.assertEqual(reasons(r, "vws-iam#600"), ["serial_after:vws-deploy#500"])

    def test_other_prs_are_not_serialized(self):
        a = pr("vws-deploy", 500, ["docs/a.md"], 1)
        b = pr("vws-iam", 600, ["internal/x.go"], 2)
        self.assertEqual(merges(run(snap(a, b, merge=TWO))), ["vws-deploy#500", "vws-iam#600"])

    def test_an_ineligible_serial_pr_does_not_hold_the_slot(self):
        a = pr("vws-deploy", 500, ["AGENTS.md"], 5)
        a["labels"] = []  # not verify-approved
        b = pr("vws-iam", 600, ["AGENTS.md"], 1)
        self.assertEqual(merges(run(snap(a, b, merge=TWO))), ["vws-iam#600"])


class Collect(unittest.TestCase):
    def test_pr_files_lists_the_changed_paths(self):
        class Gh:
            def get_all(self, path, limit=20):
                self.path, self.limit = path, limit
                return [{"filename": "AGENTS.md"}, {"filename": "x.go"}]

        gh = Gh()
        self.assertEqual(orch.pr_files(gh, "vws-deploy", 5), ["AGENTS.md", "x.go"])
        self.assertEqual(gh.path, "repos/popododo0720/vws-deploy/pulls/5/files?per_page=100")
        self.assertTrue(orch.allowed("GET", gh.path))


if __name__ == "__main__":
    unittest.main()
