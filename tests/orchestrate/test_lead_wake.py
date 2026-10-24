"""Lead wake (vws-deploy#397): one marker comment on the wake thread per PR head that no rule moves on its
own: verify-approved but conflicting with main, or a verify FAIL with no DeepSeek rework (a Grok PR, a PR
with no owner, or a conflict-only verdict). Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from orch_testlib import CFG, NOW, comment, item, orch, ready_task, snapshot
from test_rework import CONFLICT_BODY, FAIL_BODY, review

MARKER = "<!-- vws-orchestrate:lead-wake -->"


def run(*items, cfg=CFG, keys=()):
    s = snapshot(*items)
    s["status"]["keys"] = list(keys)
    s["merge"] = {}
    return orch.decide(s, {}, NOW, cfg, set())


def lead(r):
    return [a for a in r["actions"] if a["type"] == "lead_wake"]


def approved(number=500, mergeable=False, repo="vws-deploy"):
    it = item(repo, number, labels=("verify-approved",), is_pr=True, body="Part of #397")
    it["merge_info"] = {"head_sha": it["head_sha"], "state": "open", "merged": False, "draft": False, "base": "main",
                        "mergeable": mergeable, "mergeable_state": "dirty" if mergeable is False else "clean",
                        "title": "ci: x", "checks": [], "statuses": []}
    return it


def failed(number=501, labels=(), body=FAIL_BODY, repo="vws-iam", deepseek=False):
    it = (ready_task(repo, number, 12, is_pr=True, body="Part of #1") if deepseek
          else item(repo, number, labels=labels, is_pr=True, body="Part of #1"))
    it["reviews"] = [review(9, it, "CHANGES_REQUESTED", 1, body=body)]
    return it


class LeadWake(unittest.TestCase):
    def test_approved_but_conflicting_wakes_the_lead(self):
        pr = approved()
        a = lead(run(pr))
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["target"], "vws-deploy#339")
        body = a[0]["body"]
        self.assertEqual(body.split("\n", 1)[0], MARKER)
        self.assertIn("vws-deploy#500", body)
        self.assertIn(pr["head_sha"], body)
        self.assertIn("conflicts with main", body)
        self.assertIn(f"Idempotency key: `{a[0]['key']}`", body)

    def test_approved_and_mergeable_is_quiet(self):
        self.assertEqual(lead(run(approved(mergeable=True))), [])
        self.assertEqual(lead(run(approved(mergeable=None))), [])  # unknown is not a conflict

    def test_one_wake_per_head(self):
        pr = approved()
        key = lead(run(pr))[0]["key"]
        self.assertEqual(lead(run(pr, keys=[key])), [])
        moved = approved()
        moved["head_sha"] = moved["merge_info"]["head_sha"] = "d" * 40
        self.assertNotEqual(lead(run(moved, keys=[key]))[0]["key"], key)

    def test_fail_on_grok_or_ownerless_pr_wakes_the_lead(self):
        for labels, why in ((("grok",), "Grok"), ((), "no DeepSeek owner")):
            pr = failed(labels=labels)
            a = lead(run(pr))
            self.assertEqual(len(a), 1, labels)
            self.assertIn("vws-iam#501", a[0]["body"])
            self.assertIn(pr["head_sha"], a[0]["body"])
            self.assertIn(why, a[0]["body"])

    def test_conflict_only_fail_on_deepseek_pr_wakes_the_lead(self):
        a = lead(run(failed(deepseek=True, body=CONFLICT_BODY)))
        self.assertEqual(len(a), 1)
        self.assertIn("conflict-only", a[0]["body"])

    def test_fail_that_deepseek_reworks_is_quiet(self):
        r = run(failed(deepseek=True))
        self.assertEqual(lead(r), [])
        self.assertTrue([a for a in r["actions"] if a["type"] == "dispatch"])

    def test_fail_on_an_older_head_or_a_pass_is_quiet(self):
        pr = failed()
        pr["reviews"] = [review(9, pr, "CHANGES_REQUESTED", 1, sha="e" * 40)]
        self.assertEqual(lead(run(pr)), [])
        pr["reviews"] = [review(9, pr, "CHANGES_REQUESTED", 2), review(10, pr, "APPROVED", 1)]
        self.assertEqual(lead(run(pr)), [])

    def test_verdict_comment_counts(self):
        pr = item("vws-iam", 502, is_pr=True, body="Part of #1")
        pr["comments"].append(comment(5, "vws-iam", 502, f"# vws-iam#502 @ {pr['head_sha']}\n{FAIL_BODY}", 1))
        self.assertEqual(len(lead(run(pr))), 1)

    def test_draft_closed_and_wake_thread_are_skipped(self):
        d = failed(labels=("grok",))
        d["draft"] = True
        c = failed(labels=("grok",))
        c["state"] = "closed"
        self.assertEqual(lead(run(d)), [])
        self.assertEqual(lead(run(c)), [])
        self.assertEqual(lead(run(approved(number=339))), [])

    def test_wake_thread_unset_posts_nothing(self):
        r = run(approved(), cfg=dict(CFG, ORCHESTRATE_WAKE_THREAD=""))
        self.assertEqual(lead(r), [])
        self.assertIn("lead_wake_off", [n["kind"] for n in r["notes"]])


class FakeGh:
    def __init__(self, comments=()):
        self.comments, self.writes = list(comments), []

    def get(self, path):
        return {}, None

    def get_all(self, path, limit=20):
        return self.comments

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {"id": 1}


class Apply(unittest.TestCase):
    def go(self, gh, cfg=CFG):
        s = snapshot(approved())
        s["merge"] = {}
        r = orch.decide(s, {}, NOW, cfg, set())
        events = []
        orch.apply(r, s, gh, dict(cfg, ORCHESTRATE_MERGE="dry-run"), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        return r, events

    def test_posts_once_on_the_wake_thread(self):
        gh = FakeGh()
        r, events = self.go(gh)
        self.assertEqual(gh.writes, [("POST", "repos/popododo0720/vws-deploy/issues/339/comments",
                                      {"body": lead(r)[0]["body"]})])
        self.assertIn("lead_wake_posted", [e["event"] for e in events])

    def test_recheck_skips_a_key_already_posted(self):
        r, _ = self.go(FakeGh())
        gh = FakeGh(comments=[{"body": lead(r)[0]["body"]}])
        self.go(gh)
        self.assertEqual(gh.writes, [])

    def test_scope_skips_it(self):
        gh = FakeGh()
        self.go(gh, cfg=dict(CFG, ORCHESTRATE_SCOPE="vws-iam#1"))
        self.assertEqual(gh.writes, [])


if __name__ == "__main__":
    unittest.main()
