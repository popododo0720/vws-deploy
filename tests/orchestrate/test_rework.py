"""Rework rule (vws-deploy#373): a verify FAIL on a DeepSeek PR's current head is re-dispatched on that PR."""
import json
import os
import unittest

import test_runtime

from orch_testlib import CFG, NOW, U, comment, dispatch, item, orch, ready_task, report, snapshot, ts

CAP1 = dict(CFG, ORCHESTRATE_DEEPSEEK_CAP="1")
FAIL_BODY = "Result: FAIL\n- actions.go:137: NotFound continues with an empty ARN. Fix: return NoSuchEntity.\n- Codex: blocking=1"
CONFLICT_BODY = ("Result: FAIL (not reviewed)\n- The PR conflicts with main, so this head cannot merge. Move it onto "
                 "current main without force-push.\n- Codex: not run on this head.")


def review(rid, pr, state, hours_ago, sha=None, body=FAIL_BODY):
    return {"id": rid, "url": f"{pr['url']}#pullrequestreview-{rid}", "at": ts(hours_ago), "sha": sha or pr["head_sha"],
            "result": "fail" if state == "CHANGES_REQUESTED" else "pass", "body": body}


def ds_pr(number=253, repo="vws-iam", reviews=(), **kw):
    """A PR labeled ready+deepseek (the vws-iam#127 / console-api#253 shape)."""
    pr = ready_task(repo, number, 12, is_pr=True, **kw)
    pr["reviews"] = [r(pr) for r in reviews]
    return pr


def run(*items, cfg=CAP1, state=None):
    return orch.decide(snapshot(*items), state or {}, NOW, cfg, set())


def dispatches(r):
    return [a for a in r["actions"] if a["type"] == "dispatch"]


class Rework(unittest.TestCase):
    def test_newer_changes_requested_dispatches_rework(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 1)])
        d = dispatches(run(pr))
        self.assertEqual([a["target"] for a in d], ["vws-iam#253"])
        self.assertTrue(d[0]["body"].startswith("@deepseek-dev please rework this PR"))
        self.assertIn("> - actions.go:137", d[0]["body"])  # the verdict is the prompt input
        self.assertIn(f"<!-- orchestrate:rework head={pr['head_sha']} verdict=9 -->", d[0]["body"])
        self.assertEqual((d[0]["owner"], d[0]["head"]), ("vws-iam#253", pr["head_sha"]))
        self.assertEqual(orch.classify_comment(d[0]["body"])[0], "dispatch")

    def test_verdict_comment_counts_like_a_review(self):
        pr = ds_pr()
        pr["comments"].append(comment(5, "vws-iam", 253, f"# vws-iam#253 @ {pr['head_sha']}\n{FAIL_BODY}", 1))
        self.assertEqual([a["target"] for a in dispatches(run(pr))], ["vws-iam#253"])

    def test_verdict_on_older_head_is_ignored(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 1, sha="a" * 40)])
        d = dispatches(run(pr))
        self.assertFalse(any("rework" in a["body"] for a in d))
        approved = ds_pr(reviews=[lambda p: review(8, p, "CHANGES_REQUESTED", 3), lambda p: review(9, p, "APPROVED", 1)])
        self.assertFalse(any("rework" in a["body"] for a in dispatches(run(approved))))

    def test_conflict_only_verdict_is_left_to_eggbot(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 1, body=CONFLICT_BODY)])
        r = run(pr)
        self.assertEqual(dispatches(r), [])
        self.assertIn({"kind": "rework_skipped", "target": "vws-iam#253", "verdict": pr["reviews"][0]["url"],
                       "reason": "conflict_only"}, r["notes"])
        self.assertFalse(orch.conflict_only(FAIL_BODY))
        self.assertFalse(orch.conflict_only("Result: FAIL\n- conflicts with main\n- go vet fails in store.go"))

    def test_cap_full_blocks_rework(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 1)])
        busy = item("vws-proto", 51, comments=[dispatch(1, "vws-proto", 51, 1)])
        self.assertEqual(dispatches(run(pr, busy)), [])
        self.assertEqual(len(dispatches(run(pr, busy, cfg=dict(CAP1, ORCHESTRATE_DEEPSEEK_CAP="2")))), 1)
        self.assertEqual(dispatches(run(pr, cfg=dict(CAP1, ORCHESTRATE_DISPATCH="off"))), [])

    def test_same_verdict_never_dispatched_twice(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 2)])
        body = dispatches(run(pr))[0]["body"]
        # Posted, then DeepSeek reported Done without a new head: still no second rework for this head.
        pr["comments"] += [comment(20, "vws-iam", 253, body, 1.5),
                           report(21, "vws-iam", 253, 20, "Done", 1, pr=pr["url"])]
        pr["report_prs"] = {pr["url"]: {"state": "open", "merged": False}}
        r = run(pr)
        self.assertEqual(dispatches(r), [])
        self.assertEqual([n["reason"] for n in r["notes"] if n["kind"] == "rework_skipped"], ["done"])
        # A newer FAIL on the same head is still the same head: no duplicate.
        pr["reviews"].append(review(10, pr, "CHANGES_REQUESTED", 0.5))
        self.assertEqual(dispatches(run(pr)), [])
        # A new head with a new FAIL is reworked.
        pr["head_sha"] = "b" * 40
        pr["reviews"].append(review(11, pr, "CHANGES_REQUESTED", 0.2))
        self.assertEqual(len(dispatches(run(pr, cfg=dict(CAP1, DEEPSEEK_MAX_FAILURES="3")))), 1)

    def test_manual_rework_after_verdict_counts_as_done(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 2)])
        pr["comments"].append(comment(20, "vws-iam", 253, "@deepseek-dev rework this PR: fix the P1s", 1))
        pr["comments"].append(report(21, "vws-iam", 253, 20, "Failed", 0.5))
        self.assertEqual(dispatches(run(pr)), [])

    def test_rework_counts_toward_grok_escalation(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 0.5)])
        first = f"{orch.REWORK_TEXT}\n\n<!-- orchestrate:rework head={'c' * 40} verdict=3 -->"
        pr["comments"].append(comment(20, "vws-iam", 253, first, 5))
        r = run(pr)
        self.assertEqual(dispatches(r), [])
        swap = [a for a in r["actions"] if a["type"] == "swap_label"]
        self.assertEqual(len(swap), 1)
        self.assertEqual((swap[0]["remove"], swap[0]["add"]), ("deepseek", "grok"))
        self.assertIn("verify_fail_rework", swap[0]["body"])
        self.assertIn("verify_fail)", swap[0]["body"])
        # A reset clears the count.
        pr["comments"].append(comment(30, "vws-iam", 253, "/orchestrate reset-failures", 1))
        self.assertEqual(len(dispatches(run(pr))), 1)

    def test_issue_owned_pr_is_reworked_on_the_pr(self):
        pr = item("vws-iam", 140, is_pr=True)
        pr["reviews"] = [review(9, pr, "CHANGES_REQUESTED", 1)]
        issue = ready_task("vws-iam", 7, 20, comments=[dispatch(1, "vws-iam", 7, 6),
                                                         report(2, "vws-iam", 7, 1, "Done", 4, pr=pr["url"])],
                           report_prs={pr["url"]: {"state": "open", "merged": False}})
        d = dispatches(run(issue, pr))
        self.assertEqual([(a["target"], a["owner"]) for a in d], [("vws-iam#140", "vws-iam#7")])

    def test_rework_goes_before_new_tasks(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 1)])
        old = ready_task("vws-deploy", 2, 99)
        self.assertEqual([a["target"] for a in dispatches(run(old, pr))], ["vws-iam#253"])


class ReworkCollect(unittest.TestCase):
    def test_reviews_path_is_allowed_get_only(self):
        p = "repos/popododo0720/vws-iam/pulls/253/reviews?per_page=100"
        self.assertTrue(orch.allowed("GET", p))
        self.assertTrue(orch.allowed("GET", p + "&page=2"))
        self.assertFalse(orch.allowed("POST", "repos/popododo0720/vws-iam/pulls/253/reviews"))
        self.assertEqual(orch.get_class(p), "pulls")

    def test_verify_reviews_keeps_bot_verdicts(self):
        pr = item("vws-iam", 253, is_pr=True)

        class G:
            def get_all(self, path):
                assert path.endswith("/pulls/253/reviews?per_page=100"), path
                return [{"id": 1, "user": {"login": "vws-verify[bot]"}, "state": "CHANGES_REQUESTED", "commit_id": pr["head_sha"],
                         "submitted_at": ts(1), "html_url": f"{U}/vws-iam/pull/253#r1", "body": FAIL_BODY},
                        {"id": 2, "user": {"login": "someone"}, "state": "CHANGES_REQUESTED", "commit_id": pr["head_sha"],
                         "submitted_at": ts(1), "body": "x"},
                        {"id": 3, "user": {"login": "vws-verify[bot]"}, "state": "DISMISSED", "submitted_at": ts(1)}]
        out = orch.verify_reviews(G(), pr)
        self.assertEqual([(v["id"], v["result"]) for v in out], [(1, "fail")])

    def test_recheck_rejects_moved_head(self):
        class G:
            def get_all(self, path):
                return []

            def get(self, path):
                if "/pulls/" in path:
                    return {"state": "open", "head": {"sha": "d" * 40}}, False
                return {"state": "open", "labels": [{"name": "ready"}, {"name": "deepseek"}]}, False
        it = item("vws-iam", 253, is_pr=True)
        self.assertFalse(orch.recheck_dispatch(G(), "vws-iam", "253", it, "vws-iam#253", "e" * 40))
        self.assertTrue(orch.recheck_dispatch(G(), "vws-iam", "253", it, "vws-iam#253", "d" * 40))


class ReworkTick(test_runtime.Env):
    """FAIL -> rework dispatch posted on the PR -> new head with a new FAIL -> no duplicate for one head."""
    O = test_runtime.O

    def setup_pr(self, head, reviews, comments=()):
        # A new comment or push bumps updated_at, so the history cache re-reads the comments.
        self.n = getattr(self, "n", 0) + 1
        r = test_runtime.routes()
        ready = [{"name": "ready"}, {"name": "deepseek"}]
        r[f"{self.O}/vws-iam/issues"] = {"body": [{"number": 7, "html_url": f"{U}/vws-iam/pull/7", "state": "open",
                                                   "created_at": "2026-10-01T00:00:00Z", "body": "", "labels": ready,
                                                   "pull_request": {}, "updated_at": orch.iso(NOW - 3600 * (10 - self.n))}]}
        r[f"{self.O}/vws-iam/pulls"] = {"body": [{"number": 7, "draft": False, "base": {"ref": "main"}, "head": {"sha": head}}]}
        r[f"{self.O}/vws-iam/issues/7"] = {"body": {"state": "open", "labels": ready}}
        r[f"{self.O}/vws-iam/pulls/7"] = {"body": {"state": "open", "head": {"sha": head}}}
        r[f"{self.O}/vws-iam/pulls/7/reviews"] = {"body": reviews}
        r[f"{self.O}/vws-iam/issues/7/comments"] = {"body": list(comments)}
        self.set_routes(r)

    def rev(self, rid, sha, at):
        return {"id": rid, "user": {"login": "vws-verify[bot]"}, "state": "CHANGES_REQUESTED", "commit_id": sha,
                "submitted_at": at, "html_url": f"{U}/vws-iam/pull/7#pullrequestreview-{rid}",
                "body": f"# vws-iam#7 @ {sha}\n{FAIL_BODY}"}

    def posted(self):
        return [json.loads(c["stdin"])["body"] for c in self.writes() if c["path"] == f"{self.O}/vws-iam/issues/7/comments"]

    def test_fail_rework_new_head_no_duplicate(self):
        h1, h2 = "1" * 40, "2" * 40
        self.setup_pr(h1, [self.rev(1, h1, "2026-10-21T10:00:00Z")])
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_DEEPSEEK_CAP="1")
        self.assertTrue(any(c["path"] == f"{self.O}/vws-iam/pulls/7/reviews?per_page=100" for c in self.calls()))
        bodies = self.posted()
        self.assertEqual(len(bodies), 1)
        self.assertIn(f"orchestrate:rework head={h1} verdict=1", bodies[0])
        posted = {"id": 50, "html_url": f"{U}/vws-iam/pull/7#issuecomment-50", "created_at": "2026-10-21T10:05:00Z",
                  "body": bodies[0]}
        done = {"id": 51, "html_url": f"{U}/vws-iam/pull/7#issuecomment-51", "created_at": "2026-10-21T11:00:00Z",
                "body": f"[deepseek-dev] Done for {posted['html_url']}"}
        # Same head, verdict repeated: nothing new is posted.
        os.remove(self.calls_file)
        self.setup_pr(h1, [self.rev(1, h1, "2026-10-21T10:00:00Z"), self.rev(2, h1, "2026-10-21T11:30:00Z")], [posted, done])
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_DEEPSEEK_CAP="1", DEEPSEEK_MAX_FAILURES="3")
        self.assertEqual(self.posted(), [])
        # New head, new FAIL: one rework for the new head.
        os.remove(self.calls_file)
        self.setup_pr(h2, [self.rev(1, h1, "2026-10-21T10:00:00Z"), self.rev(3, h2, "2026-10-21T12:00:00Z")], [posted, done])
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_DEEPSEEK_CAP="1", DEEPSEEK_MAX_FAILURES="3")
        bodies = self.posted()
        self.assertEqual(len(bodies), 1)
        self.assertIn(f"orchestrate:rework head={h2} verdict=3", bodies[0])
        planned = [e for e in self.log() if e["event"] == "planned" and e["action"] == "dispatch"]
        self.assertEqual(len(planned), 2, "decide itself plans no rework for an already reworked head")


if __name__ == "__main__":
    unittest.main()
