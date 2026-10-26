"""vws-deploy#398(a): an open non-draft PR into main whose head has a vws-verify FAIL, and whose owner
(pr_owner, vws-deploy#457) is not deepseek, gets one rework wake per head on the verify-wake thread.
Rule B2 keeps the DeepSeek reworks; a conflict-only FAIL stays with Rule R and the lead."""
import unittest

import test_runtime
from orch_testlib import CFG, NOW, item, orch, ready_task, snapshot
from test_rework import CONFLICT_BODY, review
from test_verify_fail_owner import failed_pr, grok_issue, unrouted


def run(s, cfg=CFG, state=None):
    return orch.decide(s, state or {}, NOW, cfg, set())


def wakes(r):
    return [a for a in r["actions"] if a["type"] == "grok_rework"]


def posted(s, r):
    """Record tick r's wake comments on the verify-wake thread, as the next collect would see them."""
    s["status"]["keys"] = sorted(set(s["status"].get("keys", [])) | {a["key"] for a in wakes(r)})
    return s


class Rule(unittest.TestCase):
    def test_grok_fail_gets_one_wake(self):
        pr = failed_pr()
        [w] = wakes(run(snapshot(pr, grok_issue())))
        self.assertEqual((w["target"], w["item"], w["owner"], w["head"]),
                         ("vws-deploy#339", "vws-deploy#441", "grok", pr["head_sha"]))
        self.assertTrue(w["body"].startswith(orch.GROK_WAKE_MARKER + "\n"))
        for text in ("vws-deploy#441", pr["head_sha"], pr["reviews"][0]["url"], f"Idempotency key: `{w['key']}`",
                     "no force-push"):
            self.assertIn(text, w["body"])
        self.assertTrue(w["body"].endswith(
            f"<!-- orchestrate:rework head={pr['head_sha']} verdict=5625213917 item=vws-deploy#441 -->"))

    def test_the_verdict_is_quoted(self):
        pr = failed_pr()
        pr["reviews"][0]["body"] = "Result: FAIL\n- tests/x.py fails on line 3"
        [w] = wakes(run(snapshot(pr, grok_issue())))
        self.assertIn("> - tests/x.py fails on line 3", w["body"])

    def test_same_head_on_the_next_tick_posts_nothing(self):
        s = snapshot(failed_pr(), grok_issue())
        s = posted(s, run(s))
        for _ in range(3):
            self.assertEqual(wakes(run(s)), [])

    def test_new_head_resets(self):
        s = posted(snapshot(failed_pr(), grok_issue()), run(snapshot(failed_pr(), grok_issue())))
        new = failed_pr(head_sha="b" * 40)
        s["items"]["vws-deploy#441"] = new
        [w] = wakes(run(s))
        self.assertEqual(w["head"], "b" * 40)
        self.assertNotIn(w["key"], s["status"]["keys"])

    def test_key_is_per_pr_and_head(self):
        self.assertNotEqual(orch.grok_rework_key("vws-deploy#441", "a" * 40), orch.grok_rework_key("vws-deploy#441", "b" * 40))
        self.assertNotEqual(orch.grok_rework_key("vws-deploy#441", "a" * 40), orch.grok_rework_key("vws-deploy#442", "a" * 40))
        self.assertNotEqual(orch.grok_rework_key("vws-deploy#441", "a" * 40), orch.rebuild_key("vws-deploy#441", "a" * 40))

    def test_owners(self):
        # The body names the Grok dev bot (vws-deploy#506): a grok label resolves to the repo's bot.
        cases = {"grok label on the PR": (failed_pr(labels=("grok",), body="no link"), "grok", "vws-platform"),
                 "Agent: line": (failed_pr(body="Part of #395\n\nAgent: vws-platform"), "vws-platform", "vws-platform")}
        for why, (pr, owner, bot) in cases.items():
            with self.subTest(why):
                self.assertEqual([w["owner"] for w in wakes(run(snapshot(pr, grok_issue())))], [owner])
                self.assertIn(f"Owner `{bot}`", wakes(run(snapshot(pr, grok_issue())))[0]["body"])

    def test_no_wake(self):
        approved = failed_pr()
        approved["reviews"].append(review(2, approved, "APPROVED", 1))
        conflict = failed_pr()
        conflict["reviews"][0]["body"] = CONFLICT_BODY
        older = failed_pr()
        older["reviews"][0]["sha"] = "c" * 40
        cases = {"no fail": failed_pr(fail=False), "approved later": approved, "older head": older,
                 "conflict only (Rule R)": conflict, "draft": failed_pr(draft=True),
                 "closed": failed_pr(state="closed"), "not into main": failed_pr(base="dev"),
                 "no owner": failed_pr(body="no link"),
                 "deepseek, not ready": failed_pr(labels=("deepseek",), body="no link")}
        for label in orch.OPT_OUT_LABELS:
            cases[f"parked: {label}"] = failed_pr(labels=(label,))
        for why, pr in cases.items():
            with self.subTest(why):
                self.assertEqual(wakes(run(snapshot(pr, grok_issue()))), [])

    def test_deepseek_rework_stays_with_rule_b2(self):
        r = run(snapshot(ready_task("vws-deploy", 395, 12), failed_pr()), dict(CFG, ORCHESTRATE_DEEPSEEK_CAP="1"))
        self.assertEqual(wakes(r), [])
        self.assertEqual([a["target"] for a in r["actions"] if a["type"] == "dispatch"], ["vws-deploy#441"])

    def test_no_wake_thread_logs_unrouted(self):
        r = run(snapshot(failed_pr(), grok_issue()), dict(CFG, ORCHESTRATE_WAKE_THREAD=""))
        self.assertEqual(wakes(r), [])
        self.assertIn({"kind": "grok_rework_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "waiting": 1}, r["notes"])
        self.assertEqual([n["reason"] for n in unrouted(r)], ["owner_grok"])

    def test_a_woken_fail_is_not_unrouted(self):
        r = run(snapshot(failed_pr(), grok_issue()))
        self.assertEqual(len(wakes(r)), 1)
        self.assertEqual(unrouted(r), [])

    def test_dispatch_off_pauses(self):
        # vws-deploy#521: the pause is the plan boundary's wake_suppressed note, with what would have posted.
        r = run(snapshot(failed_pr(), grok_issue()), dict(CFG, ORCHESTRATE_DISPATCH="off"))
        self.assertEqual(wakes(r), [])
        self.assertIn({"kind": "wake_suppressed", "reason": "ORCHESTRATE_DISPATCH=off", "total": 1,
                       "counts": {"grok_rework": 1}, "items": ["vws-deploy#441"]}, r["notes"])


class FakeGh:
    def __init__(self, comments=(), head=None, state="open"):
        self.comments, self.writes, self.head, self.state = list(comments), [], head, state

    def get(self, path):
        return {"state": self.state, "head": {"sha": self.head}}, None

    def get_all(self, path, limit=20):
        return self.comments

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {"id": 1}


class Apply(unittest.TestCase):
    def apply(self, gh, cfg=CFG):
        s = snapshot(failed_pr(), grok_issue())
        r = run(s, cfg)
        if gh.head is None:
            gh.head = s["items"]["vws-deploy#441"]["head_sha"]
        events = []
        orch.apply(r, s, gh, dict(cfg, ORCHESTRATE_MERGE="dry-run"), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        return r, events

    def test_posts_on_the_wake_thread(self):
        gh = FakeGh()
        r, events = self.apply(gh)
        self.assertEqual(gh.writes, [("POST", "repos/popododo0720/vws-deploy/issues/339/comments",
                                      {"body": wakes(r)[0]["body"]})])
        self.assertIn("grok_rework_posted", [e["event"] for e in events])

    def test_skips_a_key_already_posted(self):
        r = run(snapshot(failed_pr(), grok_issue()))
        gh = FakeGh(comments=[{"user": {"login": "popododo0720"}, "body": wakes(r)[0]["body"]}])
        _, events = self.apply(gh)
        self.assertEqual(gh.writes, [])
        self.assertIn("a comment with this key already exists", [e.get("reason") for e in events])

    def test_skips_when_the_pr_changed_since_collect(self):
        for gh in (FakeGh(head="e" * 40), FakeGh(state="closed")):
            with self.subTest(head=gh.head, state=gh.state):
                _, events = self.apply(gh)
                self.assertEqual(gh.writes, [])
                self.assertIn("PR changed since collect", [e.get("reason") for e in events])

    def test_scope_skips(self):
        gh = FakeGh()
        self.apply(gh, dict(CFG, ORCHESTRATE_SCOPE="vws-deploy#441"))
        self.assertEqual(gh.writes, [])


class CollectTick(test_runtime.Env):
    """A tick plans the wake from the reviews collect() reads for an unlabeled PR."""
    O = test_runtime.O

    def test_grok_pr_fail_is_planned(self):
        r = test_runtime.routes()
        sha = f"{400:040x}"
        r[f"{self.O}/vws-deploy/issues"]["body"][0]["labels"] = [{"name": "grok"}]
        r[f"{self.O}/vws-deploy/pulls/400/reviews"] = {"body": [{
            "id": 9, "user": {"login": "vws-verify[bot]", "id": 1}, "state": "CHANGES_REQUESTED", "commit_id": sha,
            "submitted_at": "2026-10-24T00:00:00Z", "body": "Result: FAIL\n- a test fails",
            "html_url": "https://github.com/popododo0720/vws-deploy/pull/400#pullrequestreview-9"}]}
        self.set_routes(r)
        self.assertEqual(self.tick(), 0)
        planned = [e for e in self.log() if e["event"] == "planned" and e["action"] == "grok_rework"]
        self.assertEqual([e["target"] for e in planned], ["vws-deploy#339"])


if __name__ == "__main__":
    unittest.main()
