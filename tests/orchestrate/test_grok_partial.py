"""vws-deploy#545: after a partial AC tick (rule T ticked some boxes, others stay open) the assignee no
longer holds the Grok slot forever. Path B: rule G posts one grok-wake for the remaining boxes, keyed on
the merged PR, so each merged slice re-wakes the issue at most once and a tick never posts it twice."""
import unittest

import test_runtime  # noqa: F401  (sets up the import path like the other rule G tests)
from orch_testlib import CFG, NOW, item, orch, snapshot, ts
from test_grok import grok_task

ON = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1, vws-proto#4")
K = "vws-iam#1"
KEY1 = orch.grok_key(K, 0)
PARTIAL = ("## Acceptance criteria\n"
           "- [x] one (evidence: https://github.com/popododo0720/vws-iam/pull/9 merged " + "a" * 40 + ")\n"
           "- [ ] two\n- [ ] three\n\n## Context\n- [ ] not an AC box\n")


def partial(merged=((9, 1),), assignees=("vws-platform",), body=PARTIAL, extra=("ac-partial",)):
    """vws-iam#1 after rule T ticked box 1 of 3: still open, ready+grok+ac-partial, still assigned."""
    it = grok_task("vws-iam", 1, ready_hours_ago=50, extra=extra, body=body)
    it["assignees"] = list(assignees)
    it["grok_activity"] = {"comments": [ts(6)], "assigned": [ts(6)],
                           "prs": [{"key": f"vws-iam#{n}", "merged_at": ts(h)} for n, h in merged]}
    return it


def snap(*items, keys=None):
    s = snapshot(*items, grok_task("vws-proto", 4, ready_hours_ago=40))
    s["status"]["keys"] = sorted(keys or {})
    s["status"]["key_times"] = dict(keys or {})
    return s


def run(s, cfg=ON, state=None):
    return orch.decide(s, state or {}, NOW, cfg, set())


def wakes(r):
    return [a for a in r["actions"] if a["type"] == "grok_wake"]


def posted(s, r, hours_ago=0.5):
    """Record tick r's wake comments on the verify-wake thread, as the next collect would see them."""
    for a in wakes(r):
        s["status"]["key_times"][a["key"]] = ts(hours_ago)
    s["status"]["keys"] = sorted(s["status"]["key_times"])
    return s


class Rewake(unittest.TestCase):
    def test_manual_claim_then_partial_tick_rewakes_once(self):
        # The #404 / #526 shape: claimed by hand (no orchestrator wake key), a PR merged, a box left open.
        s = snap(partial())
        r = run(s)
        [w] = wakes(r)
        self.assertEqual((w["item"], w["key"]), (K, orch.grok_partial_key(K, "vws-iam#9", ts(1))))
        self.assertEqual(w["partial"], {"pr": "vws-iam#9", "open": [2, 3]})
        self.assertIn("remaining acceptance criteria 2, 3", w["body"])
        self.assertIn(f"Idempotency key: `{w['key']}`", w["body"])
        note = next(n for n in r["notes"] if n["kind"] == "grok_ac_partial")
        self.assertEqual((note["item"], note["pr"], note["open"], note["rewake"]), (K, "vws-iam#9", [2, 3], True))

    def test_no_wake_storm(self):
        s = snap(partial())
        s = posted(s, run(s))
        for _ in range(3):
            r = run(s)
            self.assertEqual(wakes(r), [], "one re-wake per merged PR: the key is posted")
            self.assertIn(K, next(n["targets"] for n in r["notes"] if n["kind"] == "grok_in_flight"),
                          "the re-woken issue holds the slot again, so the cap is not exceeded")

    def test_one_wake_per_issue_per_tick(self):
        r = run(snap(partial(merged=((9, 2), (10, 1)))))
        self.assertEqual([w["item"] for w in wakes(r)], [K])
        self.assertEqual(wakes(r)[0]["partial"]["pr"], "vws-iam#10", "keyed on the newest merged slice")

    def test_orchestrator_woken_issue_rewakes_after_its_pr_merged(self):
        # Before #545 grok_task_done freed the slot and the same issue was never woken again.
        r = run(snap(partial(), keys={KEY1: ts(3)}))
        self.assertEqual([w["item"] for w in wakes(r)], [K])

    def test_next_slice_rewakes_again(self):
        s = snap(partial(merged=((9, 3),)))
        s = posted(s, run(s), hours_ago=2.5)
        s["items"][K] = partial(merged=((9, 3), (10, 1)))
        [w] = wakes(run(s))
        self.assertEqual(w["key"], orch.grok_partial_key(K, "vws-iam#10", ts(1)))

    def test_apply_recheck_allows_the_assignee_on_a_partial_rewake(self):
        a = wakes(run(snap(partial())))[0]
        gh = FakeGh()
        self.assertEqual(orch.recheck_grok(gh, K, ["vws-iam", "vws-proto"], 1, partial=a["partial"]), "")
        self.assertEqual(orch.recheck_grok(gh, K, ["vws-iam", "vws-proto"], 1), "the issue changed since collect",
                         "a plain wake still needs an unassigned issue")
        gh.open_pr = True
        self.assertIn("already in flight", orch.recheck_grok(gh, K, ["vws-iam", "vws-proto"], 1, partial=a["partial"]))


class NoRewake(unittest.TestCase):
    def assert_none(self, s, why):
        self.assertNotIn(K, [w["item"] for w in wakes(run(s))], why)

    def test_open_linked_pr_keeps_the_slot(self):
        pr = item("vws-iam", 11, is_pr=True, body="Part of #1")
        s = snap(partial(), pr)
        self.assert_none(s, "the next slice is in flight")
        self.assertEqual(wakes(run(s)), [], "and the slot stays held: vws-proto#4 waits")

    def test_merge_before_the_last_wake(self):
        self.assert_none(snap(partial(merged=((9, 5),)), keys={KEY1: ts(3)}), "the wake already covers it")

    def test_no_merged_pr(self):
        s = snap(partial(merged=()))
        self.assert_none(s, "nothing merged: the claim is still working")
        self.assertEqual(wakes(run(s)), [], "an assigned issue with no merged slice still holds the slot")

    def test_no_box_ticked_or_none_open(self):
        none = PARTIAL.replace("- [x] one", "- [ ] one")
        self.assert_none(snap(partial(body=none)), "no partial tick")
        full = PARTIAL.replace("- [ ] two\n- [ ] three", "- [x] two\n- [x] three")
        self.assert_none(snap(partial(body=full)), "fully ticked: unchanged (rule T closes it)")

    def test_filters_still_apply(self):
        for why, extra in {"blocked": ("ac-partial", "blocked"), "deepseek": ("ac-partial", "deepseek")}.items():
            with self.subTest(why):
                self.assert_none(snap(partial(extra=extra)), why)
        r = run(snap(partial()), dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="vws-proto#4"))
        self.assertEqual([w["item"] for w in wakes(r)], ["vws-proto#4"],
                         "not allowlisted: no re-wake, but the finished slice no longer holds the slot")
        note = next(n for n in r["notes"] if n["kind"] == "grok_ac_partial")
        self.assertFalse(note["rewake"])


class FullyTicked(unittest.TestCase):
    def test_full_tick_unchanged(self):
        # A woken issue whose boxes are all ticked frees the slot with grok_task_done, as before.
        full = PARTIAL.replace("- [ ] two\n- [ ] three", "- [x] two\n- [x] three")
        r = run(snap(partial(body=full), keys={KEY1: ts(3)}))
        self.assertIn({"kind": "grok_task_done", "item": K, "key": KEY1, "reason": "pr_merged", "pr": "vws-iam#9"},
                      r["notes"])
        self.assertEqual([w["item"] for w in wakes(r)], ["vws-proto#4"])
        self.assertFalse([n for n in r["notes"] if n["kind"] == "grok_ac_partial"])


class StatusBody(unittest.TestCase):
    def body(self, s):
        return orch.status_body(s, run(s), ON, {}, "apply", NOW)

    def test_now_shows_ac_partial_with_its_wake_path(self):
        s = snap(partial())
        self.assertIn("- Grok in flight: 0 of 1; ac-partial: vws-iam#1 (re-wake queued for AC 2, 3 after vws-iam#9)\n",
                      self.body(s))
        s = posted(s, run(s))
        self.assertIn("- Grok in flight: 1 of 1: vws-iam#1; ac-partial: vws-iam#1 (re-woken for AC 2, 3 after vws-iam#9)\n",
                      self.body(s))

    def test_now_without_partial(self):
        self.assertIn("- Grok in flight: 0 of 1\n", self.body(snapshot(item("vws-iam", 3))))


class FakeGh:
    """vws-iam#1 is still assigned after its partial tick; open_pr adds an open grok PR for its next slice."""
    open_pr = False

    def get(self, path):
        return {"state": "open", "labels": [{"name": "ready"}, {"name": "grok"}, {"name": "ac-partial"}],
                "assignees": [{"login": "vws-platform"}]}, None

    def get_all(self, path):
        if "/vws-iam/" in path:
            out = [{"number": 1, "labels": [{"name": "grok"}, {"name": "ready"}], "assignees": [{"login": "vws-platform"}]}]
            if self.open_pr:
                out.append({"number": 11, "pull_request": {}, "body": "Part of #1", "labels": [{"name": "grok"}]})
            return out
        return []


if __name__ == "__main__":
    unittest.main()
