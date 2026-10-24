"""vws-deploy#457: the owner of a PR with a vws-verify FAIL on its head is resolved from the PR body
`Agent:` line, then the PR's deepseek/grok label, then the labels of the item its `Part of #N` names.
A FAIL that no rule reworks is logged once per PR head as verify_fail_unrouted."""
import unittest

import test_runtime
from orch_testlib import CFG, NOW, item, orch, ready_task, snapshot
from test_rework import CONFLICT_BODY, review

CAP1 = dict(CFG, ORCHESTRATE_DEEPSEEK_CAP="1")


def failed_pr(number=441, repo="vws-deploy", labels=(), body="Part of #395", fail=True, **kw):
    """The recorded vws-deploy#441 shape: no label, no Agent: line, `Part of #395`."""
    pr = item(repo, number, labels=labels, is_pr=True, body=body, **kw)
    pr["reviews"] = [review(5625213917, pr, "CHANGES_REQUESTED", 3)] if fail else []
    return pr


def grok_issue(number=395, repo="vws-deploy"):
    return item(repo, number, labels=("grok", "ready"))


def run(*items, state=None, cfg=CAP1):
    return orch.decide(snapshot(*items), state or {}, NOW, cfg, set())


def unrouted(r):
    return [n for n in r["notes"] if n["kind"] == "verify_fail_unrouted"]


class Owner(unittest.TestCase):
    def items(self, *its):
        return {f"{i['repo']}#{i['number']}": i for i in its}

    def test_recorded_441_shape_resolves_to_grok(self):
        pr = failed_pr()
        self.assertEqual(orch.pr_owner(pr, self.items(pr, grok_issue())), ("grok", "part_of:vws-deploy#395"))

    def test_order_agent_then_pr_label_then_part_of(self):
        issue = grok_issue()
        cases = [
            (failed_pr(labels=("grok",), body="Part of #395\n\nAgent: vws-platform"), ("vws-platform", "agent")),
            (failed_pr(labels=("deepseek",)), ("deepseek", "label")),
            (failed_pr(), ("grok", "part_of:vws-deploy#395")),
            (failed_pr(body="Part of vws-iam#7"), ("", "")),  # the named item is not in the snapshot
            (failed_pr(body="no link"), ("", "")),
        ]
        for pr, want in cases:
            with self.subTest(want=want, body=pr["body"], labels=pr["labels"]):
                self.assertEqual(orch.pr_owner(pr, self.items(pr, issue)), want)

    def test_part_of_another_repo(self):
        pr = failed_pr(repo="vws-proto", number=60, body="Part of vws-iam#7")
        issue = item("vws-iam", 7, labels=("deepseek", "ready"))
        self.assertEqual(orch.pr_owner(pr, self.items(pr, issue)), ("deepseek", "part_of:vws-iam#7"))


class Unrouted(unittest.TestCase):
    def test_grok_owned_fail_is_logged_once_per_head(self):
        pr, issue = failed_pr(), grok_issue()
        r = run(pr, issue)
        self.assertEqual(unrouted(r), [{"kind": "verify_fail_unrouted", "target": "vws-deploy#441",
                                        "head": pr["head_sha"], "verdict": pr["reviews"][0]["url"],
                                        "owner": "grok", "reason": "owner_grok"}])
        state = r["state"]
        for _ in range(2):
            r = run(pr, issue, state=state)
            self.assertEqual(unrouted(r), [], "same head: no repeat")
            state = r["state"]
        pr2 = failed_pr(head_sha="b" * 40)
        self.assertEqual([n["head"] for n in unrouted(run(pr2, issue, state=state))], ["b" * 40], "a new head is logged")

    def test_no_owner_and_agent_owner(self):
        self.assertEqual([n["reason"] for n in unrouted(run(failed_pr(body="no link")))], ["no_owner"])
        r = run(failed_pr(body="Part of #395\n\nAgent: vws-platform"), grok_issue())
        self.assertEqual([(n["owner"], n["reason"]) for n in unrouted(r)], [("vws-platform", "owner_agent")])

    def test_deepseek_owner_that_is_not_ready(self):
        issue = item("vws-deploy", 395, labels=("deepseek",))  # not ready: rule B2 does not rework
        self.assertEqual([n["reason"] for n in unrouted(run(failed_pr(), issue))], ["owner_deepseek_not_ready"])

    def test_nothing_to_log(self):
        approved = failed_pr()
        approved["reviews"].append(review(2, approved, "APPROVED", 1))
        cases = {"no fail": failed_pr(fail=False), "approved later": approved,
                 "draft": failed_pr(draft=True), "closed": failed_pr(state="closed"),
                 "older head": failed_pr()}
        cases["older head"]["reviews"][0]["sha"] = "c" * 40
        conflict = failed_pr()
        conflict["reviews"][0]["body"] = CONFLICT_BODY  # a conflict is Rule R's and the lead's, not a rework
        cases["conflict only"] = conflict
        for why, pr in cases.items():
            with self.subTest(why):
                self.assertEqual(unrouted(run(pr, grok_issue())), [])

    def test_state_forgets_prs_that_are_gone(self):
        state = run(failed_pr(), grok_issue())["state"]
        self.assertIn("vws-deploy#441", state["verify_fail_unrouted"])
        self.assertNotIn("vws-deploy#441", run(grok_issue(), state=state)["state"]["verify_fail_unrouted"])


class DeepSeekUnchanged(unittest.TestCase):
    def test_deepseek_pr_found_through_part_of_gets_the_rework(self):
        issue = ready_task("vws-deploy", 395, 12)
        pr = failed_pr()
        r = run(issue, pr)
        d = [a for a in r["actions"] if a["type"] == "dispatch"]
        self.assertEqual([(a["target"], a["owner"]) for a in d], [("vws-deploy#441", "vws-deploy#395")])
        self.assertIn(f"<!-- orchestrate:rework head={pr['head_sha']} verdict=5625213917 -->", d[0]["body"])
        self.assertEqual(unrouted(r), [])

    def test_agent_line_keeps_a_part_of_pr_out_of_deepseek_rework(self):
        issue = ready_task("vws-deploy", 395, 12)
        pr = failed_pr(body="Part of #395\n\nAgent: vws-platform")
        self.assertEqual(orch.rework_prs("vws-deploy#395", issue, {"vws-deploy#395": issue, "vws-deploy#441": pr}), [])


class CollectTick(test_runtime.Env):
    """collect() reads vws-verify reviews for an open PR that has no needs-verify label and no owner item."""
    O = test_runtime.O

    def test_unlabeled_pr_fail_is_logged(self):
        r = test_runtime.routes()
        sha = f"{400:040x}"
        r[f"{self.O}/vws-deploy/issues"]["body"][0]["labels"] = []
        r[f"{self.O}/vws-deploy/pulls/400/reviews"] = {"body": [{
            "id": 9, "user": {"login": "vws-verify[bot]", "id": 1}, "state": "CHANGES_REQUESTED", "commit_id": sha,
            "submitted_at": "2026-10-01T00:00:00Z", "html_url": "https://github.com/popododo0720/vws-deploy/pull/400#r9",
            "body": "Result: FAIL"}]}
        self.set_routes(r)
        self.assertEqual(self.tick(), 0)
        notes = [e for e in self.log() if e["event"] == "note" and e.get("kind") == "verify_fail_unrouted"]
        self.assertEqual([(n["target"], n["reason"]) for n in notes], [("vws-deploy#400", "no_owner")])


if __name__ == "__main__":
    unittest.main()
