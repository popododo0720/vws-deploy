"""vws-deploy#460: a PR labeled needs-rebuild, or an open non-draft PR whose mergeable_state is dirty
(conflicts with main), gets one owner wake per head SHA on the verify-wake thread, and the status issue
body lists the PRs waiting for a rebuild."""
import unittest

import test_runtime
from orch_testlib import CFG, NOW, item, orch, snapshot

EXCL = {"repos": set(), "items": set()}
AGENT = "Part of #12\n\nAgent: vws-platform\n"


def pr(number, labels=(), merge_state="clean", body=AGENT, **kw):
    it = item("vws-iam", number, labels=labels, is_pr=True, body=body, **kw)
    it["mergeable_state"] = merge_state
    return it


def run(s, cfg=CFG):
    return orch.decide(s, {}, NOW, cfg, EXCL)


def wakes(r):
    return [a for a in r["actions"] if a["type"] == "rebuild_wake"]


def posted(s, r):
    """Record tick r's wake comments on the verify-wake thread, as the next collect would see them."""
    s["status"]["keys"] = sorted(set(s["status"].get("keys", [])) | {a["key"] for a in wakes(r)})
    return s


class Rule(unittest.TestCase):
    def test_conflicting_pr_wakes_its_owner_once(self):
        r = run(snapshot(pr(5, merge_state="dirty")))
        [w] = wakes(r)
        self.assertEqual((w["target"], w["item"], w["owner"]), ("vws-deploy#339", "vws-iam#5", "vws-platform"))
        head = r["rebuild"][0]["head_sha"]
        self.assertTrue(w["body"].startswith(orch.REBUILD_WAKE_MARKER + "\n"))
        for text in ("vws-platform", "vws-iam#5", head, "conflicts with main", "vws-rebuild-pr", "-vN",
                     f"Idempotency key: `{w['key']}`"):
            self.assertIn(text, w["body"])

    def test_needs_rebuild_label_wakes_even_when_mergeable(self):
        [w] = wakes(run(snapshot(pr(5, labels=("needs-rebuild", "verify-approved")))))
        self.assertIn("labeled needs-rebuild", w["body"])

    def test_repeated_ticks_post_nothing_new(self):
        s = snapshot(pr(5, merge_state="dirty"), pr(6, labels=("needs-rebuild",)))
        first = run(s)
        self.assertEqual(len(wakes(first)), 2)
        s = posted(s, first)
        for _ in range(3):
            r = run(s)
            self.assertEqual(wakes(r), [])
            self.assertEqual([p["target"] for p in r["rebuild"]], ["vws-iam#5", "vws-iam#6"], "still listed")

    def test_new_head_gets_a_new_wake(self):
        s = posted(snapshot(pr(5, merge_state="dirty")), run(snapshot(pr(5, merge_state="dirty"))))
        s["items"]["vws-iam#5"]["head_sha"] = "f" * 40
        [w] = wakes(run(s))
        self.assertIn("f" * 40, w["body"])

    def test_key_is_per_pr_and_head(self):
        a, b = orch.rebuild_key("vws-iam#5", "a" * 40), orch.rebuild_key("vws-iam#5", "b" * 40)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, orch.rebuild_key("vws-iam#6", "a" * 40))
        self.assertRegex(a, r"^sha256:[0-9a-f]{64}$")

    def test_not_waiting(self):
        cases = {"clean": pr(5), "draft": pr(5, merge_state="dirty", draft=True),
                 "closed": pr(5, merge_state="dirty", state="closed"),
                 "other base": pr(5, merge_state="dirty", base="release"),
                 "unknown": pr(5, merge_state="unknown"), "blocked by checks": pr(5, merge_state="blocked"),
                 "issue": item("vws-iam", 5, labels=("needs-rebuild",))}
        for why, it in cases.items():
            with self.subTest(why):
                r = run(snapshot(it))
                self.assertEqual((wakes(r), r["rebuild"]), ([], []))

    def test_no_agent_line_goes_to_the_lead(self):
        [w] = wakes(run(snapshot(pr(5, merge_state="dirty", body="Part of #12"))))
        self.assertEqual(w["owner"], "")
        self.assertIn("no `Agent:` line", w["body"])

    def test_no_wake_thread_lists_but_does_not_wake(self):
        r = run(snapshot(pr(5, merge_state="dirty")), dict(CFG, ORCHESTRATE_WAKE_THREAD=""))
        self.assertEqual(wakes(r), [])
        self.assertEqual([p["target"] for p in r["rebuild"]], ["vws-iam#5"])
        self.assertIn({"kind": "rebuild_wake_off", "reason": "ORCHESTRATE_WAKE_THREAD is unset", "waiting": 1}, r["notes"])

    def test_dispatch_off_still_wakes(self):
        self.assertEqual(len(wakes(run(snapshot(pr(5, merge_state="dirty")), dict(CFG, ORCHESTRATE_DISPATCH="off")))), 1,
                         "a rebuild wake is a notice to the owner, not a DeepSeek dispatch")


class StatusBody(unittest.TestCase):
    def body(self, *items):
        s = snapshot(*items)
        return orch.status_body(s, run(s), CFG, {}, "apply", NOW)

    def test_lists_prs_waiting_for_a_rebuild(self):
        b = self.body(pr(5, merge_state="dirty"), pr(6, labels=("needs-rebuild",), body="x"))
        self.assertIn("- PRs waiting for a rebuild: 2: vws-iam#5 (vws-platform, conflicts with main), "
                      "vws-iam#6 (no Agent: line, labeled needs-rebuild)", b)

    def test_none_waiting(self):
        self.assertIn("- PRs waiting for a rebuild: 0\n", self.body(pr(5)))


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
        s = snapshot(pr(5, merge_state="dirty"))
        r = run(s, cfg)
        if gh.head is None:
            gh.head = s["items"]["vws-iam#5"]["head_sha"]
        events = []
        orch.apply(r, s, gh, dict(cfg, ORCHESTRATE_MERGE="dry-run"), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        return r, events

    def test_posts_on_the_wake_thread(self):
        gh = FakeGh()
        r, events = self.apply(gh)
        self.assertEqual(gh.writes, [("POST", "repos/popododo0720/vws-deploy/issues/339/comments",
                                      {"body": wakes(r)[0]["body"]})])
        self.assertIn("rebuild_wake_posted", [e["event"] for e in events])

    def test_skips_a_key_already_posted(self):
        r = run(snapshot(pr(5, merge_state="dirty")))
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
        self.apply(gh, dict(CFG, ORCHESTRATE_SCOPE="vws-iam#5"))
        self.assertEqual(gh.writes, [])


class CollectTick(test_runtime.Env):
    """collect() reads mergeable_state from GET pulls/N for each open non-draft PR into main."""
    O = test_runtime.O

    def test_dirty_pr_is_planned(self):
        r = test_runtime.routes()
        r[f"{self.O}/vws-deploy/pulls/401"] = {"body": {"state": "open", "mergeable_state": "dirty",
                                                        "head": {"sha": f"{401:040x}"}}}
        self.set_routes(r)
        self.assertEqual(self.tick(), 0)
        planned = [e for e in self.log() if e["event"] == "planned" and e["action"] == "rebuild_wake"]
        self.assertEqual([e["target"] for e in planned], ["vws-deploy#339"])

    def test_pull_read_error_does_not_fail_the_tick(self):
        r = test_runtime.routes()
        r[f"{self.O}/vws-deploy/pulls/401"] = {"status": 403, "body": {"message": "Forbidden"}}
        self.set_routes(r)
        self.assertEqual(self.tick(), 0)
        self.assertFalse([e for e in self.log() if e["event"] == "planned" and e["action"] == "rebuild_wake"])

    def test_unreadable_pull_is_not_a_conflict(self):
        self.assertEqual(self.tick(), 0)  # routes() has no pulls/N: the stub answers []
        self.assertFalse([e for e in self.log() if e["event"] == "planned" and e["action"] == "rebuild_wake"])


if __name__ == "__main__":
    unittest.main()
