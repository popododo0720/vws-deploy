"""vws-deploy#439 AC2 class 1: untrusted or stale GitHub state. orchestrate.py reads markers, comments, PR
heads and merged PR bodies through .github/orchestrate/trusted_read.py. Fixtures come from the real findings:
the #499 edited PR body (review 5640903019) and the #507 foreign claim comment (review 5641696302)."""
import importlib.util
import json
import os
import unittest

import test_ac_tick
from orch_testlib import CFG, FIXTURES, NOW, ROOT, item, orch, snapshot, ts

TRUSTED = os.path.join(ROOT, ".github", "orchestrate", "trusted_read.py")
_spec = importlib.util.spec_from_file_location("trusted_read_under_test", TRUSTED)
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)

KEY = "sha256:" + "c" * 64


def fixture(name):
    with open(os.path.join(FIXTURES, "class1", name), encoding="utf-8") as f:
        return json.load(f)


def own(body, at="2026-10-21T11:00:00Z", cid=1):
    return {"id": cid, "body": body, "created_at": at, "user": {"login": "popododo0720", "id": 1},
            "author_association": "OWNER"}


def foreign(body, at="2026-10-21T10:00:00Z", cid=2, login="drive-by-user", assoc="NONE"):
    return {"id": cid, "body": body, "created_at": at, "user": {"login": login, "id": 2}, "author_association": assoc}


class Module(unittest.TestCase):
    def test_orchestrate_reads_through_the_module(self):
        self.assertEqual(os.path.abspath(orch.tr.__file__), os.path.abspath(TRUSTED))
        for name in ("WAKE_MARKER", "REWORK_MARKER", "STATUS_MARKER", "OPT_OUT_MARKER", "KEY_RE"):
            self.assertIs(getattr(orch, name), getattr(orch.tr, name), name)

    def test_own_comment_needs_the_owner_login_and_association(self):
        self.assertTrue(tr.own_comment(own("x")))
        self.assertTrue(tr.own_comment({"author": "popododo0720", "assoc": "OWNER"}))  # a comment_record
        for c in (foreign("x"), foreign("x", login="popododo0720", assoc="NONE"),
                  foreign("x", login="someone", assoc="OWNER"), {"body": "x"}):
            self.assertFalse(tr.own_comment(c), c)


class EditedBody499(unittest.TestCase):
    """The #499 finding: the AC tick must read Part of and AC lines only from the body vws-verify approved."""

    def merged(self, attack=False):
        f = fixture("pr499-edited-body.json")
        src = f["attack"] if attack else f
        return {"pr": f["pr"], "url": f["url"], "merged_at": f["merged_at"], "merge_sha": "a" * 40,
                "body": src["body"], "issue": "vws-deploy#439" if attack else "vws-deploy#486",
                "issue_state": "open", "issue_body": test_ac_tick.ISSUE, "issue_labels": [], "marker_seen": False,
                "verified_at": max(f["approvals"]), "body_edited_at": src["body_edited_at"]}

    def test_the_real_body_edited_before_the_approval_is_read(self):
        m = self.merged()
        self.assertEqual(tr.verified_body(m, orch.utc), m["body"])

    def test_a_body_edited_after_the_approval_is_never_read(self):
        m = self.merged(attack=True)
        self.assertIsNone(tr.verified_body(m, orch.utc))
        self.assertIsNone(tr.verified_body(dict(m, body_edited_at=m["verified_at"]), orch.utc),
                          "the same second counts as after")
        self.assertIsNone(tr.verified_body(dict(m, verified_at=""), orch.utc), "no trusted approval")

    def test_the_attack_ticks_and_closes_nothing(self):
        notes, state = [], {"ac_tick_since": "2026-10-24T00:00:00Z"}
        now = orch.utc("2026-10-25T05:00:00Z")
        acts = orch.decide_ac_ticks({"ac_merged": [self.merged(attack=True)]}, state, now,
                                    dict(CFG, ORCHESTRATE_AC_TICK="apply"), notes)
        self.assertEqual(acts, [])
        self.assertEqual([n["kind"] for n in notes], ["ac_body_edited"])


class ForeignClaim507(unittest.TestCase):
    """The #507 finding: only a Grok login or a current assignee claims a woken issue."""

    def test_a_foreign_comment_is_not_a_claim(self):
        f = fixture("pr507-foreign-claim.json")
        self.assertFalse(tr.is_claim(f["foreign"], set(f["claimers"])))
        self.assertFalse(tr.is_claim(f["grok_marker"], set(f["claimers"])), "a machine comment is not a claim")
        self.assertTrue(tr.is_claim(f["grok"], set(f["claimers"])))
        self.assertTrue(tr.is_claim(f["foreign"], {"drive-by-user"}), "a current assignee claims")

    def test_grok_activity_counts_only_the_grok_comment(self):
        f = fixture("pr507-foreign-claim.json")

        class Gh:
            def get_all(self, path, limit=20):
                return [f["foreign"], f["grok_marker"], f["grok"]]

        it = item("vws-deploy", 411, labels=("ready", "grok"))
        act = orch.grok_activity(Gh(), "vws-deploy#411", it, CFG)
        self.assertEqual(act["comments"], [f["grok"]["created_at"]])


class ForeignMarkers(unittest.TestCase):
    """Dedupe markers and idempotency keys count only from the orchestrator's own login: anyone can comment
    on the public wake thread and issues (the #496/#499 finding: the dedupe marker came from any commenter)."""

    def wake(self, key=KEY):
        return f"{orch.WAKE_MARKER}\nvws-orchestrate: 1 PRs wait\n\nIdempotency key: `{key}`\nRun id: `r`"

    def test_a_copied_wake_is_not_a_wake(self):
        self.assertEqual(orch.status_wakes([foreign(self.wake())]), [])
        self.assertEqual([w["key"] for w in orch.status_wakes([own(self.wake())])], [KEY])

    def test_a_copied_key_does_not_suppress_a_wake(self):
        class Gh:
            def __init__(self, comments):
                self.comments = comments

            def get_all(self, path, limit=20):
                return self.comments

        self.assertTrue(orch.recheck_key(Gh([foreign(self.wake())]), "vws-deploy", 339, KEY))
        self.assertTrue(orch.recheck_wake(Gh([foreign(self.wake())]), "vws-deploy", 339, KEY))
        self.assertFalse(orch.recheck_key(Gh([own(self.wake())]), "vws-deploy", 339, KEY))
        self.assertFalse(orch.recheck_wake(Gh([own(self.wake())]), "vws-deploy", 339, KEY))

    def test_a_copied_key_does_not_backdate_a_wake(self):
        """An older copied key would make a Grok wake look expired and free its slot (cap bypass)."""
        times = orch.key_times([foreign(self.wake(), at="2026-10-21T01:00:00Z"), own(self.wake(), at="2026-10-21T11:00:00Z")])
        self.assertEqual(times, {KEY: "2026-10-21T11:00:00Z"})

    def test_a_copied_ac_tick_marker_does_not_skip_the_tick(self):
        marker = "x\n<!-- vws-orchestrate:ac-tick pr=vws-deploy#500 -->"
        gh = test_ac_tick.FakeGh(comments=[foreign(marker)])
        st, _, errors = test_ac_tick.Apply().apply(gh)
        self.assertEqual(errors, 0)
        self.assertEqual(len(gh.writes), 3, "the tick still writes")
        gh = test_ac_tick.FakeGh(comments=[own(marker)])
        test_ac_tick.Apply().apply(gh)
        self.assertEqual(gh.writes, [], "the orchestrator's own marker still dedupes")

    def test_the_failure_key_counts_only_inside_the_status_block(self):
        body = (f"Outside: Idempotency key: `{KEY}`\n{orch.STATUS_MARKER}\nstatus\n{orch.STATUS_END_MARKER}\n")
        self.assertFalse(tr.status_has_key(body, KEY))
        inside = body.replace("status\n", f"status\nIdempotency key: `{KEY}`\n")
        self.assertTrue(tr.status_has_key(inside, KEY))


class PinnedHead(unittest.TestCase):
    """A PR label write re-reads the PR at the head the tick decided on, right before the write."""

    def pr(self):
        return item("vws-deploy", 600, is_pr=True, labels=("ready",))

    def actions(self):
        it = dict(self.pr(), verify_ci="green", verify_reviews=[], created_at=ts(5))
        return orch.decide_verify_labels({"vws-deploy#600": it}, CFG, [])

    def test_verify_label_actions_pin_the_head(self):
        acts = self.actions()
        self.assertEqual([(a["type"], a["label"]) for a in acts], [("add_label", "needs-verify")])
        self.assertEqual(acts[0]["head"], self.pr()["head_sha"])

    def run_apply(self, live_head, state="open"):
        acts = self.actions()

        class Gh:
            def __init__(self):
                self.writes, self.reads = [], []

            def get(self, path):
                self.reads.append(path)
                return {"state": state, "head": {"sha": live_head}, "labels": []}, None

            def write(self, method, path, payload=None, before=None):
                self.writes.append((method, path, payload))
                return {}

        gh, events = Gh(), []
        s = snapshot(self.pr(), labels={"vws-deploy": ["needs-verify", "ready"]})
        orch.apply({"actions": acts, "state": {}}, s, gh, dict(CFG, ORCHESTRATE_SCOPE=""), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        return gh, events

    def test_head_moved_since_the_tick_read_writes_nothing(self):
        gh, events = self.run_apply("f" * 40)
        self.assertEqual(gh.writes, [])
        self.assertIn("repos/popododo0720/vws-deploy/pulls/600", gh.reads)
        self.assertTrue(any(e["event"] == "skipped" and "head moved" in e.get("reason", "") for e in events), events)

    def test_closed_since_the_tick_read_writes_nothing(self):
        gh, _ = self.run_apply(self.pr()["head_sha"], state="closed")
        self.assertEqual(gh.writes, [])

    def test_same_head_writes(self):
        gh, _ = self.run_apply(self.pr()["head_sha"])
        self.assertEqual([(w[0], w[1]) for w in gh.writes],
                         [("POST", "repos/popododo0720/vws-deploy/issues/600/labels")])


if __name__ == "__main__":
    unittest.main()
