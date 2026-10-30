"""vws-deploy#545: a partial AC tick must not leave the assignee holding the one Grok slot with no wake.

Rule T leaves the issue open, labeled ac-partial, with some acceptance-criteria boxes ticked and some
open. The assignee stays. On main that holds the slot until a person posts a grok-wake. After the fix,
one grok-wake goes out for that ticked-box set (the assignee may keep the issue), a second tick posts
nothing, and a fully ticked issue is still grok_task_done.
"""
import unittest

from orch_testlib import CFG, NOW, item, orch, snapshot, ts
from test_grok import FakeGh, gh_item, grok_task, wakes

ON = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1, vws-proto#4")
PARTIAL = (
    "## Acceptance criteria\n"
    "- [x] one\n"
    "- [ ] two\n"
    "- [ ] three\n"
)
TWO = (
    "## Acceptance criteria\n"
    "- [x] one\n"
    "- [x] two\n"
    "- [ ] three\n"
)
DONE = (
    "## Acceptance criteria\n"
    "- [x] one\n"
    "- [x] two\n"
)


def partial(body=PARTIAL, labels=("ac-partial",), assignees=("grok-dev",), activity=None):
    """vws-iam#1, already woken, assigned, the shape left after a partial tick whose merged PR
    never shows merged_at on the timeline cross-reference (the live stall)."""
    it = grok_task("vws-iam", 1, ready_hours_ago=50, extra=tuple(labels), body=body)
    it["assignees"] = list(assignees)
    it["grok_activity"] = (
        {"comments": [ts(1)], "assigned": [ts(1)],
         "prs": [{"key": "vws-iam#539", "merged_at": ""}]}
        if activity is None else activity)
    return it


def snap(*items, keys=()):
    s = snapshot(*items, grok_task("vws-proto", 4, ready_hours_ago=40))
    # The original wake is on the thread, so main treats the assignee as in flight.
    key = orch.grok_key("vws-iam#1", 0)
    s["status"]["keys"] = [key, *keys]
    s["status"]["key_times"] = {key: ts(3)}
    for k in keys:
        s["status"]["key_times"][k] = ts(0.5)
    return s


def run(s, cfg=ON):
    return orch.decide(s, {}, NOW, cfg, set())


def woke(r):
    return [a["item"] for a in wakes(r)]


def in_flight(r):
    return next((n["targets"] for n in r["notes"] if n.get("kind") == "grok_in_flight"), [])


class PartialRewake(unittest.TestCase):
    def test_partial_tick_wakes_the_assignee_once(self):
        # Fails on main: the assignee holds the slot and neither issue is woken.
        s = snap(partial())
        r = run(s)
        self.assertEqual(woke(r), ["vws-iam#1"])
        self.assertNotIn("vws-iam#1", in_flight(r), "waiting for the re-wake is not a cap hold")
        a = wakes(r)[0]
        self.assertTrue(a["allow_assigned"])
        self.assertTrue(a["body"].startswith(orch.GROK_WAKE_MARKER + "\n"))
        self.assertIn("remaining acceptance criteria", a["reason"])
        # The same ticked boxes post nothing on the next tick, including after a restart that
        # only has the wake comment (the key) and no extra state.
        s["status"]["keys"] = sorted(set(s["status"]["keys"]) | {a["key"]})
        s["status"]["key_times"][a["key"]] = ts(0.2)
        again = run(s)
        self.assertEqual(woke(again), [])
        self.assertIn("vws-iam#1", in_flight(again))
        self.assertEqual(woke(run(s)), [], "a second decide with the same inputs posts nothing")

    def test_a_later_slice_is_one_new_wake(self):
        first = wakes(run(snap(partial())))[0]["key"]
        r = run(snap(partial(body=TWO), keys=(first,)))
        self.assertEqual(woke(r), ["vws-iam#1"])
        self.assertNotEqual(wakes(r)[0]["key"], first)

    def test_open_pr_still_holds_the_slot(self):
        pr = item("vws-iam", 20, is_pr=True, labels=("grok",), body="Part of #1\n")
        for linked in (pr, item("vws-iam", 21, is_pr=True, body="Part of #1\n")):
            with self.subTest(labels=linked["labels"]):
                r = run(snap(partial(), linked))
                self.assertEqual(woke(r), [])
                self.assertIn("vws-iam#1", in_flight(r))

    def test_open_pr_after_partial_wake_keeps_the_slot(self):
        # Live over-release (console-api#324, deploy#391): an older PR merged after the original wake,
        # the partial wake went out, then Grok opened a new PR. The open PR flipped ac_partial_idle off,
        # the slot key fell back to the original wake key, and its merged PR read as grok_task_done.
        part = orch.ac_partial_key("vws-iam#1", partial())
        act = {"comments": [ts(1)], "assigned": [ts(1)],
               "prs": [{"key": "vws-iam#539", "merged_at": ts(2)}, {"key": "vws-iam#20", "merged_at": ""}]}
        pr = item("vws-iam", 20, is_pr=True, labels=("grok",), body="Part of #1\n")
        r = run(snap(partial(activity=act), pr, keys=(part,)))
        self.assertEqual([n for n in r["notes"] if n.get("kind") == "grok_task_done"], [])
        self.assertEqual(woke(r), [])
        self.assertIn("vws-iam#1", in_flight(r))

    def test_release_keeps_the_slot_while_a_linked_pr_is_open(self):
        it = partial(activity={"comments": [], "assigned": [], "prs": [{"key": "vws-iam#539", "merged_at": ts(2)}]})
        pr = item("vws-iam", 20, is_pr=True, labels=("grok",), body="Part of #1\n")
        items = {"vws-iam#1": it, "vws-iam#20": pr}
        self.assertEqual(orch.grok_release("vws-iam#1", it, items, ts(3), NOW, 60), ("", {}))
        it["body"] = DONE
        self.assertEqual(orch.grok_release("vws-iam#1", it, items, ts(3), NOW, 60), ("", {}))
        del items["vws-iam#20"]
        self.assertEqual(orch.grok_release("vws-iam#1", it, items, ts(3), NOW, 60)[0], "grok_task_done")

    def test_assigned_without_a_partial_tick_still_holds(self):
        # No ac-partial, and a checkbox outside the acceptance-criteria heading, are not a slice.
        outside = "- [x] note\n- [ ] other\n"
        for it in (partial(labels=(), body=PARTIAL), partial(body=outside)):
            with self.subTest(body=it["body"][:24], labels=it["labels"]):
                r = run(snap(it))
                self.assertEqual(woke(r), [])
                self.assertIn("vws-iam#1", in_flight(r))

    def test_fully_ticked_issue_is_still_done(self):
        # ac-partial may still be on for this tick; every box ticked is ac_done, not a re-wake.
        it = partial(body=DONE)
        r = run(snap(it))
        self.assertEqual(woke(r), ["vws-proto#4"])
        self.assertEqual(
            [n["reason"] for n in r["notes"] if n.get("kind") == "grok_task_done"],
            ["ac_done"])
        self.assertNotIn("vws-iam#1", woke(r))


class ApplyRecheck(unittest.TestCase):
    """The continuation wake is posted even though the assignee is still on the issue."""

    def test_recheck_allows_the_assignee_on_a_continuation(self):
        s = snap(partial())
        r = run(s)
        self.assertEqual(woke(r), ["vws-iam#1"])
        labels = ("ready", "grok", "ac-partial")
        me = gh_item(1, labels, PARTIAL, assignees=("grok-dev",))
        gh = FakeGh(labels=labels, assignees=("grok-dev",), open_items=[me])
        events = []
        orch.apply(r, s, gh, dict(ON, ORCHESTRATE_MERGE="dry-run"), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        self.assertEqual(len(gh.writes), 1)
        self.assertIn(orch.GROK_WAKE_MARKER, gh.writes[0][2]["body"])
        # A normal wake is still skipped when an assignee appears after collect.
        plain = snapshot(grok_task("vws-iam", 1))
        plain_r = orch.decide(plain, {}, NOW, ON, set())
        gh2 = FakeGh(assignees=("grok-dev",))
        orch.apply(plain_r, plain, gh2, dict(ON, ORCHESTRATE_MERGE="dry-run"), None,
                   lambda ev, **kw: None, {}, NOW)
        self.assertEqual(gh2.writes, [])


if __name__ == "__main__":
    unittest.main()
