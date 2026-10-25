"""vws-deploy#411: a Grok wake that nobody claims frees its slot after ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN,
and a woken task whose PR merged (or whose acceptance criteria are all ticked) frees it too. A freed issue
is not woken again until /orchestrate reset-failures or a new ready/grok label event."""
import time
import unittest

import test_runtime
from orch_testlib import CFG, NOW, comment, event, item, orch, snapshot, ts
from test_grok import grok_task

ON = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1, vws-proto#4")
KEY1 = orch.grok_key("vws-iam#1", 0)


def woken(hours_ago=3, activity=None, **kw):
    """vws-iam#1, woken hours_ago; activity is what collect read from its timeline ({} = nothing)."""
    it = grok_task("vws-iam", 1, ready_hours_ago=50, **kw)
    it["grok_activity"] = {"comments": [], "assigned": [], "prs": []} if activity is None else activity
    return it


def snap(*items, wake_hours_ago=3):
    s = snapshot(*items, grok_task("vws-proto", 4, ready_hours_ago=40))
    s["status"]["keys"] = [KEY1]
    s["status"]["key_times"] = {KEY1: ts(wake_hours_ago)}
    return s


def run(s, cfg=ON, state=None):
    return orch.decide(s, state or {}, NOW, cfg, set())


def woke(r):
    return [a["item"] for a in r["actions"] if a["type"] == "grok_wake"]


def freed(r):
    return [{k: v for k, v in n.items() if k in ("kind", "item", "reason")} for n in r["notes"]
            if n["kind"] in ("grok_wake_expired", "grok_task_done")]


class Config(unittest.TestCase):
    def test_default_and_validation(self):
        self.assertEqual(orch.DEFAULTS["ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN"], "120")
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        for bad in ("0", "-5", "1.5", "abc", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN=bad))
        self.assertEqual(orch.load_config(paths, dict(env, ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN="30"))
                         ["ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN"], "30")

    def test_grok_logins_default_and_validation(self):
        self.assertEqual(orch.DEFAULTS["ORCHESTRATE_GROK_LOGINS"], "popododo0720")
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        for bad in ("", "a b", "bad/login", "a,,b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_GROK_LOGINS=bad))


class Unclaimed(unittest.TestCase):
    def test_expired_wake_frees_the_slot_and_is_not_rewoken(self):
        r = run(snap(woken()))
        self.assertEqual(freed(r), [{"kind": "grok_wake_expired", "item": "vws-iam#1"}])
        self.assertEqual(woke(r), ["vws-proto#4"], "the freed slot goes to the next allowlisted task")
        note = next(n for n in r["notes"] if n["kind"] == "grok_wake_expired")
        self.assertEqual((note["key"], note["woken_at"], note["timeout_min"]), (KEY1, ts(3), 120))

    def test_within_the_timeout_the_slot_stays_held(self):
        r = run(snap(woken(), wake_hours_ago=1))
        self.assertEqual((freed(r), woke(r)), ([], []))
        r = run(snap(woken()), dict(ON, ORCHESTRATE_GROK_WAKE_TIMEOUT_MIN="240"))
        self.assertEqual((freed(r), woke(r)), ([], []))

    def test_note_once_and_the_same_plan_after_a_restart(self):
        s = snap(woken())
        first = run(s)
        again = run(s, state=first["state"])
        self.assertEqual(freed(again), [], "no repeat while the state remembers it")
        self.assertEqual(woke(again), woke(first))
        restarted = run(s)  # state lost: the decision comes from GitHub data, so the plan is the same
        self.assertEqual(woke(restarted), ["vws-proto#4"])
        self.assertNotIn("vws-iam#1", woke(restarted))

    def test_reset_failures_rearms_the_wake(self):
        reset = comment(70, "vws-iam", 1, "/orchestrate reset-failures", 2)
        reset.update(author="popododo0720", assoc="OWNER")
        r = run(snap(woken(comments=[reset])))
        self.assertIn("vws-iam#1", woke(r))

    def test_new_label_event_rearms_the_wake(self):
        r = run(snap(woken(events=[event("labeled", "grok", 1)])))
        self.assertIn("vws-iam#1", woke(r))
        r = run(snap(woken(events=[event("labeled", "grok", 5)])))  # before the wake: no re-arm
        self.assertNotIn("vws-iam#1", woke(r))

    def test_unknown_activity_or_wake_time_keeps_the_slot(self):
        it = woken()
        it["grok_activity"] = None  # the timeline read failed
        self.assertEqual(woke(run(snap(it))), [])
        s = snap(woken())
        s["status"]["key_times"] = {}
        self.assertEqual(woke(run(s)), [])


class FakeGh:
    """vws-iam#1 is assigned (a done task keeps its assignee); vws-proto#4 is the next task."""
    def get(self, path):
        return {"state": "open", "labels": [{"name": "ready"}, {"name": "grok"}], "assignees": []}, None

    def get_all(self, path):
        if "/vws-iam/" in path:
            return [{"number": 1, "labels": [{"name": "grok"}, {"name": "ready"}], "assignees": [{"login": "grok-dev"}]}]
        return []


class ApplyRecheck(unittest.TestCase):
    def test_released_tasks_do_not_count_at_apply_time(self):
        it = woken(activity={"comments": [], "assigned": [], "prs": [{"key": "vws-iam#9", "merged_at": ts(1)}]})
        it["assignees"] = ["grok-dev"]
        a = next(a for a in run(snap(it, wake_hours_ago=2))["actions"] if a["type"] == "grok_wake")
        self.assertEqual((a["item"], a["released"]), ("vws-proto#4", ["vws-iam#1"]))
        repos = ["vws-iam", "vws-proto"]
        self.assertIn("cap 1 reached", orch.recheck_grok(FakeGh(), "vws-proto#4", repos, 1))
        self.assertEqual(orch.recheck_grok(FakeGh(), "vws-proto#4", repos, 1, released=a["released"]), "")


class Claimed(unittest.TestCase):
    def test_claims_keep_the_slot_past_the_timeout(self):
        pr = item("vws-iam", 9, is_pr=True, body="Part of #1")
        cases = {
            "comment after the wake": (woken(activity={"comments": [ts(2)], "assigned": [], "prs": []}), ()),
            "assigned after the wake": (woken(activity={"comments": [], "assigned": [ts(2)], "prs": []}), ()),
            "a linked PR on the timeline": (woken(activity={"comments": [], "assigned": [],
                                                            "prs": [{"key": "vws-iam#9", "merged_at": ""}]}), ()),
            "an open linked PR without labels": (woken(), (pr,)),
        }
        for why, (it, extra) in cases.items():
            with self.subTest(why):
                r = run(snap(it, *extra))
                self.assertEqual((freed(r), woke(r)), ([], []))

    def test_a_comment_before_the_wake_is_not_a_claim(self):
        r = run(snap(woken(activity={"comments": [ts(4)], "assigned": [], "prs": []})))
        self.assertEqual([n["item"] for n in freed(r)], ["vws-iam#1"])


class Done(unittest.TestCase):
    def test_merged_pr_frees_the_slot(self):
        it = woken(activity={"comments": [ts(2)], "assigned": [], "prs": [{"key": "vws-iam#9", "merged_at": ts(1)}]})
        it["assignees"] = ["grok-dev"]
        r = run(snap(it, wake_hours_ago=1.5))
        self.assertEqual(freed(r), [{"kind": "grok_task_done", "item": "vws-iam#1", "reason": "pr_merged"}])
        self.assertEqual(woke(r), ["vws-proto#4"])
        self.assertEqual(next(n for n in r["notes"] if n["kind"] == "grok_task_done")["pr"], "vws-iam#9")

    def test_pr_merged_before_the_wake_does_not_count(self):
        it = woken(activity={"comments": [ts(0.5)], "assigned": [], "prs": [{"key": "vws-iam#9", "merged_at": ts(5)}]})
        self.assertEqual(woke(run(snap(it))), [])

    def test_all_acceptance_criteria_ticked(self):
        r = run(snap(woken(body="## Acceptance criteria\n- [x] one\n- [X] two\n", activity={
            "comments": [ts(0.5)], "assigned": [], "prs": []}), wake_hours_ago=1))
        self.assertEqual(freed(r), [{"kind": "grok_task_done", "item": "vws-iam#1", "reason": "ac_done"}])
        r = run(snap(woken(body="- [x] one\n- [ ] two\n", activity={"comments": [ts(0.5)], "assigned": [], "prs": []}),
                     wake_hours_ago=1))
        self.assertEqual(freed(r), [])


class CollectTick(test_runtime.Env):
    """A real tick: the wake time comes from the wake thread comment, the claim from the issue timeline."""
    O = test_runtime.O

    def setup(self, timeline, wake_hours_ago=3):
        r = test_runtime.routes()
        labels = [{"name": "ready"}, {"name": "grok"}]
        r[f"{self.O}/vws-iam/issues"] = {"body": [{"number": 1, "html_url": "https://github.com/popododo0720/vws-iam/issues/1",
                                                   "state": "open", "created_at": "2026-10-01T00:00:00Z", "body": "",
                                                   "labels": labels, "assignees": []}]}
        r[f"{self.O}/vws-iam/issues/1"] = {"body": {"state": "open", "labels": labels}}
        wake_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - wake_hours_ago * 3600))
        body = orch.grok_wake_body({"url": "u", "number": 1}, "vws-iam#1", KEY1, 1, 1, "run")
        r[f"{self.O}/vws-deploy/issues/339/comments"] = {"body": [{"id": 5, "body": body, "created_at": wake_at,
                                                                   "user": {"login": "popododo0720"}}]}
        r[f"{self.O}/vws-iam/issues/1/timeline"] = timeline
        self.set_routes(r)

    def notes(self, kind):
        return [e for e in self.log() if e["event"] == "note" and e.get("kind") == kind]

    def test_unclaimed_wake_expires_once_across_apply_ticks(self):
        self.setup({"body": [{"event": "commented", "created_at": "2026-10-01T00:00:00Z", "body": "old"}]})
        for _ in range(2):
            self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual([n["item"] for n in self.notes("grok_wake_expired")], ["vws-iam#1"])
        posted = [c for c in self.writes() if orch.GROK_WAKE_MARKER in c["stdin"]]
        self.assertEqual(posted, [], "the expired issue is not woken again")

    def test_comment_after_the_wake_claims_it(self):
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        self.setup({"body": [{"event": "commented", "created_at": later, "body": "taking this",
                              "actor": {"login": "popododo0720"}}]})
        self.assertEqual(self.tick(ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual(self.notes("grok_wake_expired"), [])

    def test_a_comment_from_another_user_is_not_a_claim(self):
        """AC1: only the woken bot's login (ORCHESTRATE_GROK_LOGINS) or a current assignee claims by comment."""
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        self.setup({"body": [{"event": "commented", "created_at": later, "body": "+1", "actor": {"login": "someone-else"}},
                             {"event": "commented", "created_at": later, "body": "no author"}]})
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual([n["item"] for n in self.notes("grok_wake_expired")], ["vws-iam#1"])

    def test_a_comment_from_a_configured_grok_login_claims_it(self):
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        self.setup({"body": [{"event": "commented", "created_at": later, "body": "on it",
                              "actor": {"login": "grok-dev"}}]})
        self.assertEqual(self.tick(ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1", ORCHESTRATE_GROK_LOGINS="a, grok-dev"), 0)
        self.assertEqual(self.notes("grok_wake_expired"), [])

    def cross_ref(self, owner, merged_at):
        return {"event": "cross-referenced", "created_at": merged_at,
                "source": {"issue": {"number": 9, "body": "Part of #1",
                                     "pull_request": {"merged_at": merged_at},
                                     "repository": {"name": "vws-iam", "owner": {"login": owner}}}}}

    def test_a_foreign_owner_cross_reference_is_ignored(self):
        """Security: a merged PR in someone else's repo named vws-iam neither frees nor claims the slot."""
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        self.setup({"body": [self.cross_ref("attacker", later)]})
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual(self.notes("grok_task_done"), [])
        self.assertEqual([n["item"] for n in self.notes("grok_wake_expired")], ["vws-iam#1"])

    def test_an_owner_cross_reference_that_merged_frees_the_slot(self):
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        self.setup({"body": [self.cross_ref("popododo0720", later)]})
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual([n["item"] for n in self.notes("grok_task_done")], ["vws-iam#1"])

    def test_marker_comments_are_not_claims(self):
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        self.setup({"body": [{"event": "commented", "created_at": later, "body": "<!-- vws-orchestrate:x -->\nhi"}]})
        self.assertEqual(self.tick(ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual([n["item"] for n in self.notes("grok_wake_expired")], ["vws-iam#1"])

    def test_timeline_read_error_keeps_the_slot(self):
        self.setup({"status": 403, "body": {"message": "Forbidden"}})
        self.assertEqual(self.tick(ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0, "a read error never fails the tick")
        self.assertEqual(self.notes("grok_wake_expired"), [])


if __name__ == "__main__":
    unittest.main()
