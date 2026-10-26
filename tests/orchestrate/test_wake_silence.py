"""vws-deploy#521: STOP is full wake-thread silence even when work is queued. Under ORCHESTRATE_WAKES=off
(set by STOP, cleared by RESUME) or ORCHESTRATE_DISPATCH=off, a tick plans zero wake_verify, lead_wake,
grok_wake, grok_rework and rebuild_wake actions, and the note wake_suppressed counts what would have posted.
With both on, the plan is unchanged. Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import unittest

from orch_testlib import CFG, NOW, item, orch, snapshot
from test_decide import queued
from test_grok import ALLOW, grok_task
from test_lead_wake import approved
from test_verify_fail_owner import failed_pr, grok_issue

ON = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST=ALLOW)
STOP = dict(ON, ORCHESTRATE_WAKES="off", ORCHESTRATE_DISPATCH="off", ORCHESTRATE_MERGE="off", ORCHESTRATE_BOARD="off")
WAKES_OFF = dict(ON, ORCHESTRATE_WAKES="off")
DISPATCH_OFF = dict(ON, ORCHESTRATE_DISPATCH="off")
RESUME = dict(ON, ORCHESTRATE_WAKES="on", ORCHESTRATE_DISPATCH="on")
ALL = ["grok_rework", "grok_wake", "lead_wake", "rebuild_wake", "wake_verify"]


def queue():
    """Every wake rule due at once: 3 needs-verify PRs waiting 45 min (threshold and max-wait), a
    verify-approved PR that conflicts with main (lead wake), a Grok PR with a vws-verify FAIL (grok rework),
    a PR that conflicts with main (rebuild wake) and an allowlisted ready+grok issue (grok wake)."""
    rebuild = item("vws-iam", 5, is_pr=True, body="Agent: vws-iam")
    rebuild["mergeable_state"] = "dirty"
    s = snapshot(*queued(3, waited_min=45), approved(), failed_pr(), grok_issue(), rebuild,
                 grok_task("vws-proto", 3))
    s["status"]["keys"] = []
    s["merge"] = {}
    return s


def run(cfg):
    return orch.decide(queue(), {}, NOW, cfg, set())


def wake_types(r):
    return sorted({a["type"] for a in r["actions"] if a["type"] in orch.WAKE_TYPES})


def suppressed(r):
    return [n for n in r["notes"] if n["kind"] == "wake_suppressed"]


class QueuedIsNotNoisy(unittest.TestCase):
    def test_the_fixture_queues_every_wake_type(self):
        self.assertEqual(sorted(orch.WAKE_TYPES), ALL)
        self.assertEqual(wake_types(run(ON)), ALL, "with wakes on, every rule is due")

    def test_stop_plans_zero_wakes_with_work_queued(self):
        r = run(STOP)
        self.assertEqual(wake_types(r), [])
        self.assertFalse([a for a in r["actions"] if a.get("target") == "vws-deploy#339"], "nothing for the thread")
        self.assertEqual(r["pending_count"], 3, "the queue is still there")
        [n] = suppressed(r)
        self.assertEqual(n["reason"], "ORCHESTRATE_WAKES=off")
        self.assertEqual(n["counts"], {t: c for t, c in self.baseline_counts().items()})
        self.assertEqual(n["total"], sum(n["counts"].values()))

    def test_wakes_off_alone_plans_zero_wakes(self):
        r = run(WAKES_OFF)
        self.assertEqual(wake_types(r), [])
        [n] = suppressed(r)
        self.assertEqual((n["reason"], n["counts"]), ("ORCHESTRATE_WAKES=off", self.baseline_counts()))

    def test_dispatch_off_alone_plans_zero_wakes(self):
        r = run(DISPATCH_OFF)
        self.assertEqual(wake_types(r), [])
        self.assertNotIn("dispatch", [a["type"] for a in r["actions"]])
        [n] = suppressed(r)
        self.assertEqual((n["reason"], n["counts"]), ("ORCHESTRATE_DISPATCH=off", self.baseline_counts()))

    def test_resume_matches_the_plan_with_wakes_on(self):
        on, resumed = run(ON), run(RESUME)
        self.assertEqual(json.dumps(on["actions"], sort_keys=True), json.dumps(resumed["actions"], sort_keys=True))
        self.assertEqual(suppressed(resumed), [])

    def test_stop_keeps_the_rest_of_the_plan(self):
        keep = [a for a in run(DISPATCH_OFF)["actions"]]
        on = [a for a in run(ON)["actions"] if a["type"] not in orch.WAKE_TYPES and a["type"] != "dispatch"]
        self.assertEqual(json.dumps(keep, sort_keys=True), json.dumps(on, sort_keys=True),
                         "only wakes (and DeepSeek dispatches) drop out; labels and merges stay")

    def test_a_suppressed_wake_posts_after_resume(self):
        # Silence posts nothing and records no key, so the first tick after RESUME wakes once.
        s = queue()
        orch.decide(s, {}, NOW, STOP, set())
        self.assertEqual(wake_types(orch.decide(s, {}, NOW, RESUME, set())), ALL)

    def test_nothing_due_adds_no_note(self):
        s = snapshot()
        s["status"]["keys"], s["merge"] = [], {}
        self.assertEqual(suppressed(orch.decide(s, {}, NOW, STOP, set())), [])

    def baseline_counts(self):
        r = run(ON)
        return {t: sum(1 for a in r["actions"] if a["type"] == t) for t in orch.WAKE_TYPES
                if any(a["type"] == t for a in r["actions"])}


class Signal(unittest.TestCase):
    def test_default_on_and_values(self):
        self.assertEqual(CFG["ORCHESTRATE_WAKES"], "on")
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        for bad in ("", "OFF", "no", "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_WAKES=bad))
        for v in ("on", "off"):
            self.assertEqual(orch.load_config(paths, dict(env, ORCHESTRATE_WAKES=v))["ORCHESTRATE_WAKES"], v)

    def test_wake_silence(self):
        self.assertEqual(orch.wake_silence(ON), "")
        self.assertEqual(orch.wake_silence(RESUME), "")
        self.assertEqual(orch.wake_silence(STOP), "ORCHESTRATE_WAKES=off")
        self.assertEqual(orch.wake_silence(WAKES_OFF), "ORCHESTRATE_WAKES=off")
        self.assertEqual(orch.wake_silence(DISPATCH_OFF), "ORCHESTRATE_DISPATCH=off")

    def test_status_body_shows_the_silence(self):
        for cfg, text in ((ON, "wakes on,"), (STOP, "wakes off (ORCHESTRATE_WAKES=off),"),
                          (DISPATCH_OFF, "wakes off (ORCHESTRATE_DISPATCH=off),")):
            with self.subTest(text=text):
                body = orch.status_body(queue(), run(cfg), cfg, {}, "apply", NOW)
                self.assertIn(text, body)


class FakeGh:
    def __init__(self):
        self.writes = []

    def get(self, path):
        raise AssertionError(f"no read expected: {path}")

    def get_all(self, path, limit=20):
        raise AssertionError(f"no read expected: {path}")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {"id": 1}


class Apply(unittest.TestCase):
    def test_a_stale_wake_plan_does_not_post_under_stop(self):
        # Defense in depth: a plan made with wakes on (for example before STOP) is applied under STOP.
        s = queue()
        r = run(ON)
        r["actions"] = [a for a in r["actions"] if a["type"] in orch.WAKE_TYPES]
        gh, events = FakeGh(), []
        orch.apply(r, s, gh, dict(STOP, ORCHESTRATE_MAX_WRITES="50"), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        self.assertEqual(gh.writes, [])
        self.assertEqual(sorted({e["action"] for e in events if e.get("reason") == "ORCHESTRATE_WAKES=off"}), ALL)


if __name__ == "__main__":
    unittest.main()
