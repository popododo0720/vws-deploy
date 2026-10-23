"""decide() table tests for .github/orchestrate/orchestrate.py (vws-deploy#333).
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import socket
import subprocess
import unittest
from unittest import mock

from orch_testlib import CFG, NOW, dispatch, event, item, orch, ready_task, report, snapshot

EXCL = {"https://github.com/popododo0720/vws-iam/issues/123#issuecomment-6309297481"}


def run(snap, state=None, cfg=None, now=NOW, excl=EXCL):
    return orch.decide(snap, state or {}, now, cfg or CFG, excl)


def kinds(result):
    return [(a["type"], a["target"]) for a in result["actions"]]


def prs(n, **kw):
    return [item("vws-deploy", 400 + i, is_pr=True, **kw) for i in range(n)]


class Purity(unittest.TestCase):
    def test_same_input_same_output_no_io(self):
        snap = snapshot(*prs(5), ready_task("vws-iam", 1))
        state = {"prev_pending_count": 2}
        before = json.dumps([snap, state], sort_keys=True)
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                mock.patch("builtins.open", side_effect=AssertionError("file")):
            a = run(snap, state)
            b = run(snap, state)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(json.dumps([snap, state], sort_keys=True), before, "decide mutated its input")


def queued(n, waited_min=5, **kw):
    """n PRs labeled needs-verify by verify-queue.yml waited_min minutes ago (vws-deploy#397)."""
    labels = ("needs-verify",) + tuple(kw.pop("extra", ()))
    return [item("vws-deploy", 400 + i, is_pr=True, labels=labels,
                 events=[event("labeled", "needs-verify", waited_min / 60)], **kw) for i in range(n)]


class RuleA(unittest.TestCase):
    """Rule A counts only needs-verify PRs; it wakes when the oldest waited 30 min or 3 wait (vws-deploy#397)."""

    def wake(self, result):
        return [a for a in result["actions"] if a["type"] == "wake_verify"]

    def test_wake_table(self):
        key3 = orch.pending_key([{"repo": p["repo"], "number": p["number"], "head_sha": p["head_sha"]} for p in queued(3)])
        cases = [
            ("0 labeled: green PRs without needs-verify", prs(5), [], 0),
            ("2 labeled, both under 30 min", queued(2, 29), [], 0),
            ("1 labeled for 30 min", queued(1, 30), [], 1),
            ("1 labeled for 2 h", queued(1, 120), [], 1),
            ("3 labeled, all fresh", queued(3, 1), [], 1),
            ("same set already posted", queued(3, 1), [{"key": key3, "at": NOW - 60}], 0),
            ("a changed set right after a wake: no cooldown", queued(4, 1), [{"key": key3, "at": NOW - 60}], 1),
        ]
        for name, items, wakes, want in cases:
            with self.subTest(name):
                self.assertEqual(len(self.wake(run(snapshot(*items, wakes=wakes)))), want)

    def test_repeated_ticks_post_once(self):
        snap = snapshot(*queued(3, 40))
        first = self.wake(run(snap))
        self.assertEqual(len(first), 1)
        posted = snapshot(*queued(3, 45), wakes=[{"key": first[0]["key"], "at": NOW}])
        for minutes in (5, 60, 600):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.wake(run(posted, now=NOW + minutes * 60)), [])

    def test_wait_is_from_the_latest_label(self):
        # Labeled 2 h ago, removed by a push, labeled again 10 min ago: the wait restarts.
        ev = [event("labeled", "needs-verify", 2), event("unlabeled", "needs-verify", 1), event("labeled", "needs-verify", 1 / 6)]
        it = item("vws-deploy", 400, is_pr=True, labels=("needs-verify",), events=ev)
        self.assertEqual(self.wake(run(snapshot(it))), [])
        self.assertEqual(len(self.wake(run(snapshot(it), now=NOW + 20 * 60))), 1)

    def test_not_counted(self):
        def nv(n, **kw):
            return item("vws-iam", n, is_pr=True, labels=("needs-verify",) + kw.pop("extra", ()),
                        events=[event("labeled", "needs-verify", 5)], **kw)
        extra = [nv(1, draft=True), nv(2, base="release"), nv(3, extra=("verify-approved",)), nv(4, ci="failing"),
                 item("vws-iam", 5, is_pr=True), item("vws-iam", 6, labels=("needs-verify",))]
        r = run(snapshot(*queued(2, 1), *extra))
        self.assertEqual(r["pending_count"], 2)
        self.assertEqual(self.wake(r), [])

    def test_comment_body_and_state(self):
        r = run(snapshot(*queued(3, 1)), {"prev_pending_count": 0})
        a = self.wake(r)[0]
        self.assertEqual(a["target"], "vws-deploy#339")
        lines = a["body"].split("\n")
        self.assertEqual(lines[0], "<!-- vws-orchestrate:verify-wake -->", "routines match the exact first line")
        for p in queued(3):
            self.assertIn(f"- {p['repo']}#{p['number']}@{p['head_sha']} {p['url']} (CI pre-check: green, waited 1m)", lines)
        self.assertIn(f"Idempotency key: `{a['key']}`", lines)
        self.assertIn("Run id: `20261021T120000Z-abcdef`", lines)
        self.assertIn("Repos scanned: vws-deploy, vws-iam, vws-proto", lines, "archived repos are not listed")
        self.assertNotIn("@deepseek-dev", a["body"])
        self.assertEqual(orch.status_wakes([{"body": a["body"], "created_at": "2026-10-21T12:00:00Z"}])[0]["key"], a["key"],
                         "the key is read back from the posted comment")
        self.assertEqual(r["state"]["prev_pending_count"], 3)

    def test_marker_must_be_first_line(self):
        body = "hello\n<!-- vws-orchestrate:verify-wake -->\nIdempotency key: `sha256:" + "a" * 64 + "`"
        self.assertEqual(orch.status_wakes([{"body": body, "created_at": "2026-10-21T12:00:00Z"}]), [])

    def test_body_lists_every_waiting_pr(self):
        many = queued(60, 1)
        a = self.wake(run(snapshot(*many)))[0]
        self.assertIn("60 PRs wait for verify", a["body"])
        for p in many:
            self.assertIn(f"- {p['repo']}#{p['number']}@{p['head_sha']} ", a["body"])


class RuleB(unittest.TestCase):
    def test_queue_table(self):
        cases = [
            ("idle, one ready", [ready_task("vws-iam", 1)], [("dispatch", "vws-iam#1")]),
            ("needs both labels", [item("vws-iam", 1, labels=("ready",)), item("vws-iam", 2, labels=("deepseek",))], []),
            ("grok is skipped", [ready_task("vws-iam", 1, extra=("grok",))], []),
            ("draft PR is skipped", [ready_task("vws-iam", 1, is_pr=True, draft=True)], []),
            ("PR target works", [ready_task("vws-proto", 51, is_pr=True)], [("dispatch", "vws-proto#51")]),
            ("oldest ready first", [ready_task("vws-iam", 1, 5), ready_task("vws-deploy", 9, 8)],
             [("dispatch", "vws-deploy#9"), ("dispatch", "vws-iam#1")]),
            ("priority/blocker first", [ready_task("vws-iam", 1, 50), ready_task("vws-deploy", 2, 1, extra=("priority/blocker",))],
             [("dispatch", "vws-deploy#2"), ("dispatch", "vws-iam#1")]),
            ("tie: repo then number", [ready_task("vws-iam", 3, 5), ready_task("vws-iam", 2, 5), ready_task("vws-deploy", 7, 5)],
             [("dispatch", "vws-deploy#7"), ("dispatch", "vws-iam#2")]),
            ("human blocked label", [ready_task("vws-iam", 1, extra=("blocked",))], []),
            ("closed is skipped", [ready_task("vws-iam", 1, state="closed")], []),
        ]
        for name, items, want in cases:
            with self.subTest(name):
                self.assertEqual(kinds(run(snapshot(*items))), want)

    def test_full_cap_in_flight_anywhere_blocks(self):
        busy = [item("vws-proto", 51, is_pr=True, comments=[dispatch(1, "vws-proto", 51, 1)]),
                item("vws-deploy", 8, comments=[dispatch(2, "vws-deploy", 8, 2)])]
        r = run(snapshot(*busy, ready_task("vws-iam", 1)))
        self.assertEqual(kinds(r), [])
        self.assertIn({"kind": "in_flight", "targets": ["vws-deploy#8", "vws-proto#51"]}, r["notes"])

    def test_after_report_next_target(self):
        for result in ("Done", "Failed"):
            with self.subTest(result):
                done = item("vws-proto", 51, comments=[dispatch(1, "vws-proto", 51, 2), report(2, "vws-proto", 51, 1, result, 1)])
                r = run(snapshot(done, ready_task("vws-iam", 1)))
                self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")])

    def test_done_target_not_redispatched(self):
        t = ready_task("vws-iam", 1, comments=[dispatch(1, "vws-iam", 1, 2),
                                                report(2, "vws-iam", 1, 1, "Done", 1, pr="https://github.com/popododo0720/vws-iam/pull/9")],
                       report_prs={"https://github.com/popododo0720/vws-iam/pull/9": {"state": "open", "merged": False}})
        self.assertEqual(kinds(run(snapshot(t))), [("add_label", "vws-iam#1")])

    def test_dispatch_body_has_marker(self):
        a = run(snapshot(ready_task("vws-iam", 1)))["actions"][0]
        self.assertTrue(a["body"].startswith("@deepseek-dev please take this issue"))
        self.assertIn("<!-- orchestrate:dispatch -->", a["body"])

    def test_scope_limits_queue(self):
        cfg = dict(CFG, ORCHESTRATE_SCOPE="vws-iam#2")
        r = run(snapshot(ready_task("vws-iam", 1, 9), ready_task("vws-iam", 2, 1)), cfg=cfg)
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#2")])


def with_repo(snap, name):
    snap["repos"].append({"name": name, "archived": False})
    return snap


class DeepSeekCap(unittest.TestCase):
    """ORCHESTRATE_DEEPSEEK_CAP (default 2) DeepSeek dispatches in flight at once."""

    def test_default_cap_is_2(self):
        self.assertEqual(CFG["ORCHESTRATE_DEEPSEEK_CAP"], "2")

    def test_cap_2_dispatches_two_independent_tasks(self):
        r = run(snapshot(ready_task("vws-iam", 1, 9), ready_task("vws-deploy", 2, 8), ready_task("vws-proto", 3, 7)))
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#1"), ("dispatch", "vws-deploy#2")])
        self.assertEqual([a["reason"] for a in r["actions"]],
                         ["next in queue, DeepSeek slot 1 of 2", "next in queue, DeepSeek slot 2 of 2"])

    def test_cap_is_configurable(self):
        names = ("vws-iam", "vws-deploy", "vws-proto", "vws-network")
        tasks = [ready_task(name, n, 10 - n) for n, name in enumerate(names, 1)]
        for cap, want in (("1", 1), ("3", 3), ("9", 4)):
            with self.subTest(cap=cap):
                r = run(with_repo(snapshot(*tasks), "vws-network"), cfg=dict(CFG, ORCHESTRATE_DEEPSEEK_CAP=cap))
                self.assertEqual(len(kinds(r)), want)

    def test_one_in_flight_leaves_one_slot(self):
        busy = item("vws-proto", 51, is_pr=True, comments=[dispatch(1, "vws-proto", 51, 1)])
        r = run(snapshot(busy, ready_task("vws-iam", 1, 9), ready_task("vws-iam", 2, 8)))
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")])
        self.assertEqual(r["actions"][0]["reason"], "next in queue, DeepSeek slot 2 of 2")
        self.assertIn({"kind": "in_flight", "targets": ["vws-proto#51"]}, r["notes"])

    def test_cap_respected_across_ticks(self):
        tasks = {1: ready_task("vws-iam", 1, 9), 2: ready_task("vws-deploy", 2, 8), 3: ready_task("vws-proto", 3, 7)}
        first = run(snapshot(*tasks.values()))
        self.assertEqual(kinds(first), [("dispatch", "vws-iam#1"), ("dispatch", "vws-deploy#2")])
        # The next tick sees both dispatch comments: the cap is full, nothing goes out.
        tasks[1]["comments"] = [dispatch(11, "vws-iam", 1, 0.1)]
        tasks[2]["comments"] = [dispatch(12, "vws-deploy", 2, 0.1)]
        second = run(snapshot(*tasks.values()), first["state"])
        self.assertEqual(kinds(second), [])
        self.assertIn({"kind": "in_flight", "targets": ["vws-deploy#2", "vws-iam#1"]}, second["notes"])
        # One slot frees up when a report lands; only the third task is dispatched.
        tasks[1]["comments"].append(report(13, "vws-iam", 1, 11, "Done", 0.05))
        third = run(snapshot(*tasks.values()), second["state"])
        self.assertEqual(kinds(third), [("dispatch", "vws-proto#3")])

    def test_running_target_not_redispatched_after_reset(self):
        reset = {"id": 2, "url": "u2", "created_at": orch.iso(NOW - 1800), "body": "/orchestrate reset-failures runner fixed"}
        t = ready_task("vws-iam", 1, comments=[dispatch(1, "vws-iam", 1, 1), reset])
        r = run(snapshot(t, ready_task("vws-deploy", 2, 5)))
        self.assertEqual(kinds(r), [("dispatch", "vws-deploy#2")], "the free slot goes to another task")
        self.assertEqual(r["actions"][0]["reason"], "next in queue, DeepSeek slot 2 of 2")

    def test_slots_count_dispatches_not_targets(self):
        # Two running dispatches on one item (for example a manual one) fill both slots.
        busy = item("vws-proto", 51, comments=[dispatch(1, "vws-proto", 51, 2), dispatch(2, "vws-proto", 51, 1)])
        self.assertEqual(kinds(run(snapshot(busy, ready_task("vws-iam", 1)))), [])

    def test_same_repo_pair_dispatches_one(self):
        r = run(snapshot(ready_task("vws-iam", 1, 9), ready_task("vws-iam", 2, 8)))
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")])
        self.assertIn({"kind": "repo_busy", "target": "vws-iam#2", "repo": "vws-iam"}, r["notes"])

    def test_same_repo_skipped_for_the_next_other_repo(self):
        r = run(snapshot(ready_task("vws-iam", 1, 9), ready_task("vws-iam", 2, 8), ready_task("vws-proto", 3, 7)))
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#1"), ("dispatch", "vws-proto#3")])
        self.assertEqual(r["actions"][1]["reason"], "next in queue, DeepSeek slot 2 of 2")

    def test_repo_with_a_task_in_flight_is_skipped(self):
        busy = item("vws-iam", 7, comments=[dispatch(1, "vws-iam", 7, 1)])
        r = run(snapshot(busy, ready_task("vws-iam", 1, 9), ready_task("vws-deploy", 2, 8)))
        self.assertEqual(kinds(r), [("dispatch", "vws-deploy#2")])
        self.assertIn({"kind": "repo_busy", "target": "vws-iam#1", "repo": "vws-iam"}, r["notes"])

    def test_same_repo_task_goes_out_when_its_repo_frees(self):
        a, b = ready_task("vws-iam", 1, 9), ready_task("vws-iam", 2, 8)
        first = run(snapshot(a, b))
        self.assertEqual(kinds(first), [("dispatch", "vws-iam#1")])
        a["comments"] = [dispatch(11, "vws-iam", 1, 0.5)]
        second = run(snapshot(a, b), first["state"])
        self.assertEqual(kinds(second), [], "a slot is free, but vws-iam is busy")
        a["comments"].append(report(12, "vws-iam", 1, 11, "Done", 0.1))
        third = run(snapshot(a, b), second["state"])
        self.assertEqual(kinds(third), [("dispatch", "vws-iam#2")])

    def test_dependent_pair_dispatches_only_the_dependency(self):
        proto = ready_task("vws-proto", 51, 1, is_pr=True)
        api = ready_task("vws-console-api", 227, 50, body="Depends on vws-proto#51")
        r = run(with_repo(snapshot(proto, api), "vws-console-api"))
        self.assertEqual(kinds(r), [("add_label", "vws-console-api#227"), ("dispatch", "vws-proto#51")])
        self.assertIn({"kind": "dep_unmet", "target": "vws-console-api#227", "dep": "vws-proto#51", "state": "open"},
                      r["notes"])

    def test_dependency_in_flight_blocks_dependent_with_a_free_slot(self):
        proto = ready_task("vws-proto", 51, 9, is_pr=True, comments=[dispatch(1, "vws-proto", 51, 1)])
        api = ready_task("vws-console-api", 227, 50, body="Depends on vws-proto#51")
        other = ready_task("vws-iam", 1, 2)
        r = run(with_repo(snapshot(proto, api, other), "vws-console-api"))
        self.assertEqual(kinds(r), [("add_label", "vws-console-api#227"), ("dispatch", "vws-iam#1")],
                         "the free slot goes to the independent task")

    def test_dependency_in_review_still_blocks(self):
        pr = "https://github.com/popododo0720/vws-proto/pull/60"
        proto = ready_task("vws-proto", 51, 9, comments=[dispatch(1, "vws-proto", 51, 3), report(2, "vws-proto", 51, 1, "Done", 2, pr=pr)],
                           report_prs={pr: {"state": "open", "merged": False}}, extra=("in-review",))
        api = ready_task("vws-console-api", 227, 50, body="Depends on vws-proto#51")
        r = run(with_repo(snapshot(proto, api), "vws-console-api"))
        self.assertEqual(kinds(r), [("add_label", "vws-console-api#227")])

    def test_met_dependency_runs_alongside_another_task(self):
        api = ready_task("vws-console-api", 227, 50, body="Depends on vws-proto#51")
        deps = {"vws-proto#51": {"state": "closed", "merged": True}}
        r = run(with_repo(snapshot(api, ready_task("vws-iam", 1, 2), deps=deps), "vws-console-api"))
        self.assertEqual(kinds(r), [("dispatch", "vws-console-api#227"), ("dispatch", "vws-iam#1")])

    def test_cap_must_be_a_positive_integer(self):
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        for bad in ("0", "-1", "1.5", "two", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_DEEPSEEK_CAP=bad))
        self.assertEqual(orch.load_config(paths, dict(env, ORCHESTRATE_DEEPSEEK_CAP="3"))["ORCHESTRATE_DEEPSEEK_CAP"], "3")


class Dependencies(unittest.TestCase):
    def test_table(self):
        cases = [
            ("open same-repo dep", "Depends on #2", [item("vws-iam", 2)], {}, [("add_label", "vws-iam#1")]),
            ("closed completed", "Depends on #2", [], {"vws-iam#2": {"state": "closed", "state_reason": "completed"}},
             [("dispatch", "vws-iam#1")]),
            ("closed not planned", "Depends on #2", [], {"vws-iam#2": {"state": "closed", "state_reason": "not_planned"}},
             [("add_label", "vws-iam#1")]),
            ("cross-repo merged PR", "Depends on vws-proto#51", [], {"vws-proto#51": {"state": "closed", "merged": True}},
             [("dispatch", "vws-iam#1")]),
            ("cross-repo open PR", "Depends on vws-proto#51", [item("vws-proto", 51, is_pr=True, ci="failing")], {},
             [("add_label", "vws-iam#1")]),
            ("unreadable", "Depends on #77", [], {"vws-iam#77": {"unreadable": True}}, [("add_label", "vws-iam#1")]),
            ("outside the repo list", "Depends on vws-other#1", [], {}, [("add_label", "vws-iam#1")]),
            ("archived repo", "Depends on vws-hypervisor#1", [], {}, [("add_label", "vws-iam#1")]),
        ]
        for name, body, extra, deps, want in cases:
            with self.subTest(name):
                r = run(snapshot(ready_task("vws-iam", 1, body=body), *extra, deps=deps))
                self.assertEqual(kinds(r), want)

    def test_cycle_reported_not_dispatched(self):
        a = ready_task("vws-iam", 1, body="Depends on #2")
        b = ready_task("vws-iam", 2, body="Depends on #1")
        r = run(snapshot(a, b))
        self.assertNotIn("dispatch", [t for t, _ in kinds(r)])
        self.assertIn({"kind": "dep_cycle", "target": "vws-iam#1"}, r["notes"])

    def test_cycle_detection_is_linear(self):
        chain = [ready_task("vws-iam", n, body=f"Depends on #{n + 1}, #{n + 2}") for n in range(1, 80)]
        start = orch.time.time()
        self.assertEqual(orch.find_cycles(snapshot(*chain)), set())
        self.assertLess(orch.time.time() - start, 1.0)
        loop = [ready_task("vws-iam", 1, body="Depends on #2"), ready_task("vws-iam", 2, body="Depends on #3"),
                ready_task("vws-iam", 3, body="Depends on #1"), ready_task("vws-iam", 4, body="Depends on #1")]
        self.assertEqual(orch.find_cycles(snapshot(*loop)), {"vws-iam#1", "vws-iam#2", "vws-iam#3"})

    def test_stale_ownership_is_pruned(self):
        r = run(snapshot(ready_task("vws-iam", 1)), {"owned_labels": {"blocked": ["vws-iam#1", "vws-iam#9"]}})
        self.assertEqual(r["state"]["owned_labels"]["blocked"], [], "label absent: the journaled write never landed")

    def test_blocked_removed_only_when_owned(self):
        t = ready_task("vws-iam", 1, extra=("blocked",))
        self.assertEqual(kinds(run(snapshot(t), {"owned_labels": {"blocked": ["vws-iam#1"]}})), [("remove_label", "vws-iam#1")])
        self.assertEqual(kinds(run(snapshot(t), {"owned_labels": {"blocked": []}})), [])


class RuleC(unittest.TestCase):
    PR = "https://github.com/popododo0720/vws-iam/pull/9"

    def failures(self, comments, report_prs=None, events=(), extra=()):
        t = ready_task("vws-iam", 1, comments=comments, report_prs=report_prs, events=list(events), extra=extra)
        return run(snapshot(t))

    def test_each_failure_kind_counts_once(self):
        one = {
            "failed report": [dispatch(1, "vws-iam", 1, 9), report(2, "vws-iam", 1, 1, "Failed", 8)],
            "timeout": [dispatch(1, "vws-iam", 1, 4)],
            "closed unmerged": [dispatch(1, "vws-iam", 1, 9), report(2, "vws-iam", 1, 1, "Done", 8, pr=self.PR)],
        }
        prs = {self.PR: {"state": "closed", "merged": False}}
        for name, comments in one.items():
            with self.subTest(name):
                self.assertEqual(kinds(self.failures(comments, prs)), [("dispatch", "vws-iam#1")], "1 failure: redispatch")

    def test_not_yet_timed_out_is_in_flight(self):
        self.assertEqual(kinds(self.failures([dispatch(1, "vws-iam", 1, 2.9)])), [])

    def test_two_failures_swap(self):
        r = self.failures([dispatch(1, "vws-iam", 1, 20), report(2, "vws-iam", 1, 1, "Failed", 19), dispatch(3, "vws-iam", 1, 10)])
        self.assertEqual(kinds(r), [("swap_label", "vws-iam#1")])
        a = r["actions"][0]
        self.assertEqual((a["remove"], a["add"], a["comment"]), ("deepseek", "grok", True))
        self.assertNotIn("@deepseek-dev", a["body"], "the poller must not see the notice as a task")
        self.assertIn("<!-- orchestrate:swap -->", a["body"])

    def test_swap_notice_not_repeated(self):
        from orch_testlib import comment
        notice = comment(9, "vws-iam", 1, "Moving this task\n<!-- orchestrate:swap -->", 1)
        r = self.failures([dispatch(1, "vws-iam", 1, 20), report(2, "vws-iam", 1, 1, "Failed", 19), dispatch(3, "vws-iam", 1, 10), notice])
        self.assertFalse(r["actions"][0]["comment"])

    def test_resets(self):
        two = [dispatch(1, "vws-iam", 1, 20), report(2, "vws-iam", 1, 1, "Failed", 19),
               dispatch(3, "vws-iam", 1, 10), report(4, "vws-iam", 1, 3, "Failed", 9)]
        from orch_testlib import comment
        relabel = [event("labeled", "grok", 8), event("unlabeled", "grok", 7), event("labeled", "deepseek", 7)]
        self.assertEqual(kinds(self.failures(two, events=relabel)), [("dispatch", "vws-iam#1")], "grok to deepseek relabel")
        marker = comment(5, "vws-iam", 1, "/orchestrate reset-failures runner fixed", 5)
        self.assertEqual(kinds(self.failures(two + [marker])), [("dispatch", "vws-iam#1")], "reset marker")

    def test_vws_iam_123_excluded(self):
        # The real comments: dispatch 6309138804, then the Failed report 6309297481 (token problem).
        from orch_testlib import comment
        d = {"id": 6309138804, "url": "https://github.com/popododo0720/vws-iam/issues/123#issuecomment-6309138804",
             "created_at": "2026-10-21T05:54:25Z", "body": orch.DISPATCH_TEXT}
        f = {"id": 6309297481, "url": "https://github.com/popododo0720/vws-iam/issues/123#issuecomment-6309297481",
             "created_at": "2026-10-21T06:06:11Z",
             "body": "[deepseek-dev] Failed for https://github.com/popododo0720/vws-iam/issues/123#issuecomment-6309138804: dsh exited with code 0."}
        t = ready_task("vws-iam", 123, comments=[d, f])
        hist = orch.dispatch_history(t, NOW, CFG, EXCL)
        self.assertEqual([h["outcome"] for h in hist], ["excluded"])
        second = comment(7, "vws-iam", 123, "[deepseek-dev] Failed for https://github.com/popododo0720/vws-iam/issues/123#issuecomment-8: x", 1)
        t2 = ready_task("vws-iam", 123, comments=[d, f, dispatch(8, "vws-iam", 123, 2), second])
        self.assertEqual(kinds(run(snapshot(t2))), [("dispatch", "vws-iam#123")], "only 1 counted failure")
        self.assertEqual(kinds(orch.decide(snapshot(t2), {}, NOW, CFG, set())), [("swap_label", "vws-iam#123")])


    def test_done_report_with_pr_outside_vws_counts_as_failure(self):
        # vws-deploy#341: a Done report whose PR is not popododo0720/vws-* cannot be checked; it is one failure.
        for url in ("https://github.com/popododo0720/other-repo/pull/5", "https://github.com/someone/vws-iam/pull/5"):
            with self.subTest(url):
                one = [dispatch(1, "vws-iam", 1, 9), report(2, "vws-iam", 1, 1, "Done", 8, pr=url)]
                hist = orch.dispatch_history(ready_task("vws-iam", 1, comments=one), NOW, CFG, EXCL)
                self.assertEqual([(h["outcome"], h["kind"]) for h in hist], [("failed", "pr_outside_vws")])
                r = self.failures(one)
                self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")], "1 failure: redispatch, not stuck in review")
                self.assertIn({"kind": "report_pr_outside_vws", "target": "vws-iam#1", "pr": url}, r["notes"])
                two = one + [dispatch(3, "vws-iam", 1, 7), report(4, "vws-iam", 1, 3, "Done", 6, pr=url)]
                self.assertEqual(kinds(self.failures(two)), [("swap_label", "vws-iam#1")])


class Labels(unittest.TestCase):
    PR = "https://github.com/popododo0720/vws-iam/pull/9"

    def test_in_review_add_and_owned_remove(self):
        done = [dispatch(1, "vws-iam", 1, 9), report(2, "vws-iam", 1, 1, "Done", 8, pr=self.PR)]
        t = ready_task("vws-iam", 1, comments=done, report_prs={self.PR: {"state": "open", "merged": False}})
        self.assertEqual(kinds(run(snapshot(t))), [("add_label", "vws-iam#1")])
        merged = ready_task("vws-iam", 1, comments=done, extra=("in-review",), report_prs={self.PR: {"state": "closed", "merged": True}})
        self.assertEqual(kinds(run(snapshot(merged), {"owned_labels": {"in-review": ["vws-iam#1"]}})), [("remove_label", "vws-iam#1")])
        self.assertEqual(kinds(run(snapshot(merged))), [], "a human-set in-review stays")


if __name__ == "__main__":
    unittest.main()


class InReviewOnSwap(unittest.TestCase):
    """vws-deploy#341: the in-review label the orchestrator added goes away when the task moves to grok."""
    PR = "https://github.com/popododo0720/vws-iam/pull/9"
    TWO = [dispatch(1, "vws-iam", 1, 20), report(2, "vws-iam", 1, 1, "Failed", 19),
           dispatch(3, "vws-iam", 1, 10), report(4, "vws-iam", 1, 3, "Failed", 9)]
    OWNED = {"owned_labels": {"in-review": ["vws-iam#1"]}}

    def test_swap_removes_owned_in_review(self):
        t = ready_task("vws-iam", 1, comments=self.TWO, extra=("in-review",))
        r = run(snapshot(t), self.OWNED)
        self.assertEqual([(a["type"], a.get("label") or a.get("remove")) for a in r["actions"]],
                         [("swap_label", "deepseek"), ("remove_label", "in-review")])

    def test_swap_keeps_human_in_review(self):
        t = ready_task("vws-iam", 1, comments=self.TWO, extra=("in-review",))
        self.assertEqual(kinds(run(snapshot(t))), [("swap_label", "vws-iam#1")])

    def test_already_swapped_task_loses_owned_in_review(self):
        # A swap that ran before this fix, or stopped before the label call, left in-review behind.
        t = item("vws-iam", 1, labels=("ready", "grok", "in-review"), comments=self.TWO)
        r = run(snapshot(t), self.OWNED)
        self.assertEqual([(a["type"], a["label"]) for a in r["actions"]], [("remove_label", "in-review")])
        self.assertEqual(kinds(run(snapshot(t))), [], "a human-set in-review stays")
