"""vws-deploy#505: a required check that one event of a dual-event workflow skips must not park a
verify-approved PR forever, and Rule D's skip reasons for verify-approved PRs reach the #415 status block.

Primary fix: no vws-* repo-guard workflow runs on both pull_request and pull_request_target (drift-check
flags it, tests/drift). Belt: head_check_reasons passes a skipped required check when another workflow or
event key of the same name and app concluded success on the head. Evidence: the real check-run matrix of
vws-conformance#32 and #33 before the lead merged them (fixtures/conformance-32-33-checks.json).
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import os
import unittest

from orch_testlib import CFG, NOW, item, orch, snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "conformance-32-33-checks.json")
ACTIONS, VERIFY = 15368, 5313004
SHA = "d" * 40
REQ = [{"context": "pr-hygiene", "app_id": ACTIONS}, {"context": "verify-gate", "app_id": ACTIONS},
       {"context": "vws-verify", "app_id": VERIFY}]


def load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


class FixtureGh:
    """The four GETs merge_info makes, answered from the fixture."""

    def __init__(self, pr):
        self.pr = pr

    def get(self, path):
        sha = self.pr["pull"]["head"]["sha"]
        table = {f"repos/popododo0720/vws-conformance/pulls/{self.pr['pull']['number']}": self.pr["pull"],
                 f"repos/popododo0720/vws-conformance/commits/{sha}/check-runs?per_page=100": self.pr["check_runs"],
                 f"repos/popododo0720/vws-conformance/commits/{sha}/status": self.pr["status"],
                 f"repos/popododo0720/vws-conformance/actions/runs?head_sha={sha}&per_page=100": self.pr["workflow_runs"]}
        return table[path], None


def conformance_snapshot(*items, merge):
    """snapshot() with vws-conformance among the repos (orch_testlib lists four others)."""
    s = snapshot(*items)
    s["repos"].append({"name": "vws-conformance", "archived": False})
    s["labels"]["vws-conformance"] = s["labels"]["vws-deploy"]
    s["merge"] = merge
    s["status"]["keys"] = []
    return s


def run(cid, name, wf, conclusion="success", app=ACTIONS, status="completed", started="2026-10-24T18:00:00Z"):
    return {"id": cid, "name": name, "app_id": app, "status": status, "conclusion": conclusion, "head_sha": SHA,
            "started_at": started, "suite_id": cid, "workflow": wf}


def base_runs():
    return [run(1, "vws-verify", None, app=VERIFY),
            run(2, "verify-gate", ".github/workflows/repo-guard.yml@pull_request"),
            run(3, "verify-gate", ".github/workflows/repo-guard.yml@pull_request_target")]


def reasons(runs, required=REQ):
    return orch.head_check_reasons({"head_sha": SHA, "checks": runs, "statuses": []}, required)


PR = ".github/workflows/repo-guard.yml@pull_request"
PRT = ".github/workflows/repo-guard.yml@pull_request_target"


class ConformanceMatrix(unittest.TestCase):
    """The pre-merge vws-conformance#32@f3927fa and #33@d23ee5d matrices merge on the next tick."""

    def test_fixture_is_the_dual_event_shape(self):
        for n, pr in load()["prs"].items():
            with self.subTest(pr=n):
                runs = pr["check_runs"]["check_runs"]
                suites = {w["check_suite_id"]: w["event"] for w in pr["workflow_runs"]["workflow_runs"]}
                hyg = {(suites[r["check_suite"]["id"]], r["conclusion"]) for r in runs if r["name"] == "pr-hygiene"}
                self.assertEqual(hyg, {("pull_request", "success"), ("pull_request_target", "skipped")})

    def test_head_check_reasons_pass_with_the_belt(self):
        data = load()
        required = [{"context": c["context"], "app_id": c["integration_id"]} for c in data["required"]]
        for n, pr in data["prs"].items():
            with self.subTest(pr=n):
                info = orch.merge_info(FixtureGh(pr), "vws-conformance", int(n))
                self.assertEqual(info["unmapped"], [])
                self.assertEqual(orch.head_check_reasons(info, required), [])

    def test_decide_merges_the_approved_head(self):
        data = load()
        required = [{"context": c["context"], "app_id": c["integration_id"]} for c in data["required"]]
        for n, pr in data["prs"].items():
            with self.subTest(pr=n):
                it = item("vws-conformance", int(n), labels=("verify-approved",), is_pr=True,
                          body="Part of #31\n\nAgent: vws-platform", head_sha=pr["pull"]["head"]["sha"])
                it["merge_info"] = dict(orch.merge_info(FixtureGh(pr), "vws-conformance", int(n)), files=[])
                s = conformance_snapshot(it, merge={"vws-conformance": {"main_sha": "a" * 40, "main_ci": "green",
                                                                        "required": required}})
                r = orch.decide(s, {}, NOW, dict(CFG, ORCHESTRATE_MERGE="apply"), set())
                self.assertEqual([a["target"] for a in r["actions"] if a["type"] == "merge"], [f"vws-conformance#{n}"])


class SkippedRequiredBelt(unittest.TestCase):
    """A skipped required check passes only next to a success of the same name and app on the head."""

    def test_skip_next_to_a_success_passes(self):
        self.assertEqual(reasons(base_runs() + [run(4, "pr-hygiene", PR), run(5, "pr-hygiene", PRT, "skipped")]), [])

    def test_skip_alone_still_blocks(self):
        self.assertIn("required_skipped:pr-hygiene", reasons(base_runs() + [run(5, "pr-hygiene", PRT, "skipped")]))

    def test_skip_does_not_hide_a_failure_or_a_pending_run(self):
        out = reasons(base_runs() + [run(4, "pr-hygiene", PR, "failure"), run(5, "pr-hygiene", PRT, "skipped"),
                                     run(6, "pr-hygiene", ".github/workflows/other.yml@pull_request")])
        self.assertIn("required_failure:pr-hygiene", out)
        out = reasons(base_runs() + [run(4, "pr-hygiene", PR, None, status="in_progress"),
                                     run(5, "pr-hygiene", PRT, "skipped")])
        self.assertIn("check_pending:pr-hygiene", out)
        self.assertTrue(any(r.startswith("required_") and r.endswith(":pr-hygiene") for r in out), out)

    def test_success_from_another_app_does_not_count(self):
        out = reasons(base_runs() + [run(4, "pr-hygiene", None, app=99), run(5, "pr-hygiene", PRT, "skipped")])
        self.assertIn("required_skipped:pr-hygiene", out)

    def test_neutral_is_not_tolerated(self):
        out = reasons(base_runs() + [run(4, "pr-hygiene", PR), run(5, "pr-hygiene", PRT, "neutral")])
        self.assertIn("required_neutral:pr-hygiene", out)

    def test_verify_gate_stays_strict(self):
        runs = [run(1, "vws-verify", None, app=VERIFY), run(2, "verify-gate", PR), run(3, "verify-gate", PRT, "skipped"),
                run(4, "pr-hygiene", PR)]
        self.assertIn("verify_gate_skipped", reasons(runs))


class StatusSkipReasons(unittest.TestCase):
    """Verify-approved PRs that Rule D did not merge are listed with their reasons in the status block."""

    def body(self, *items, merge=None):
        s = conformance_snapshot(*items, merge=merge or {})
        r = orch.decide(s, {}, NOW, dict(CFG, ORCHESTRATE_MERGE="apply"), set())
        return orch.status_body(s, r, CFG, {}, "apply", NOW)

    def test_approved_but_parked_pr_shows_its_reasons(self):
        it = item("vws-conformance", 32, labels=("verify-approved",), is_pr=True, body="Part of #31", head_sha="f3927fa" + "0" * 33)
        it["merge_info"] = {"head_sha": it["head_sha"], "state": "open", "merged": False, "draft": False, "base": "main",
                            "mergeable": True, "mergeable_state": "clean", "title": "t", "files": [], "statuses": [],
                            "checks": [dict(run(5, "pr-hygiene", PRT, "skipped"), head_sha=it["head_sha"])]}
        b = self.body(it, merge={"vws-conformance": {"main_sha": "a" * 40, "main_ci": "green", "required": REQ}})
        line = next(ln for ln in b.splitlines() if ln.startswith("- Verify-approved PRs not merged"))
        self.assertIn("vws-conformance#32 `f3927fa`", line)
        self.assertIn("`required_skipped:pr-hygiene`", line)
        self.assertTrue(line.startswith("- Verify-approved PRs not merged: 1: "), line)

    def test_unapproved_prs_are_not_listed(self):
        b = self.body(item("vws-deploy", 7, is_pr=True, body="Part of #1"))
        self.assertIn("- Verify-approved PRs not merged: none", b)

    def test_reason_text_cannot_break_the_markdown(self):
        self.assertEqual(orch.skip_reason_text("check_failed:a`b\n@x"), "`check_failed:a'b @x`")

    def test_long_lists_are_bounded(self):
        notes = [{"kind": "merge_check", "target": f"vws-lib#{i}", "eligible": False, "head": "abcdef1",
                  "reasons": [f"r{j}" for j in range(9)]} for i in range(15)]
        line = orch.merge_skip_line({"notes": notes})
        self.assertIn("(+4 more)", line)
        self.assertIn("; +5 more PRs", line)
        self.assertTrue(line.startswith("- Verify-approved PRs not merged: 15: "))


if __name__ == "__main__":
    unittest.main()
