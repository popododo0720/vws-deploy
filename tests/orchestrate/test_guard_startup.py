"""vws-deploy#446: a verify-approved PR whose newest repo-guard run on the head is a startup_failure gets
one PR body touch (a fresh pull_request edited run), the attempt is recorded, and a second startup_failure
on the same head is reported (status issue entry + needs-human) instead of touched again.

Recorded API fixture: fixtures/repo-guard-startup-failure.json, the GET actions/runs?head_sha= answers for
vws-storage#133 and vws-iam#144 (run 39917192133 / 39917184445 ended in startup_failure at 07:21Z on
2026-10-23; the 08:55Z runs are the fresh runs the manual body edit started). Note run_attempt 2: a plain
re-run of a startup_failure run failed the same way, so the retry is a body touch, not a re-run.
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import os
import unittest

import test_runtime as rt
from orch_testlib import CFG, FIXTURES, orch
from test_merge import candidate, ctx, routes, snap

O = "repos/popododo0720"
STARTUP = 39917192133
TOUCHED_AT = orch.utc("2026-10-23T07:30:00Z")


def recorded(pr="vws-storage#133"):
    with open(os.path.join(FIXTURES, "repo-guard-startup-failure.json"), encoding="utf-8") as f:
        return json.load(f)["prs"][pr]


def guard_runs(pr="vws-storage#133", before=None, extra=()):
    """merge_info's guard_runs from the recorded answer, optionally cut at a time (the state at that moment)."""
    rec = recorded(pr)

    class G:
        def get(self, path):
            if path.endswith("/check-runs?per_page=100"):
                return {"total_count": 0, "check_runs": []}, None
            if path.endswith("/status"):
                return {"statuses": [], "total_count": 0}, None
            if "/actions/runs?" in path:
                runs = [w for w in rec["workflow_runs"]["workflow_runs"] if before is None or w["created_at"] < before]
                return {"total_count": len(runs) + len(extra), "workflow_runs": runs + list(extra)}, None
            return {"head": {"sha": rec["head_sha"]}, "base": {"ref": "main"}}, None
    repo, num = pr.split("#")
    return orch.merge_info(G(), repo, int(num))["guard_runs"], rec["head_sha"]


def approved(runs, head, body="Part of #446\nAgent: vws-platform"):
    return candidate(body=body, head_sha=head, info={"guard_runs": runs, "mergeable_state": "blocked"})


def decide(c, state=None, now=TOUCHED_AT + 60, keys=()):
    return orch.decide(snap(c, keys=keys), state or {}, now, dict(CFG, ORCHESTRATE_STATUS_ISSUE="415"), set())


def kinds(r):
    return [(a["type"], a.get("label", "")) for a in r["actions"] if a["type"] in ("guard_touch", "merge_status", "add_label")]


def second_failure(head):
    return {"id": STARTUP + 1, "name": "repo-guard", "path": ".github/workflows/repo-guard.yml", "event": "pull_request",
            "status": "completed", "conclusion": "startup_failure", "run_attempt": 1,
            "created_at": "2026-10-23T07:31:00Z", "head_sha": head}


class Detect(unittest.TestCase):
    def test_merge_info_keeps_only_pull_request_repo_guard_runs(self):
        runs, head = guard_runs()
        self.assertEqual(sorted(r["id"] for r in runs), [39909585530, 39917192133, 39928104809])
        self.assertTrue(all(r["head_sha"] == head for r in runs))

    def test_recorded_startup_failure_gets_one_touch(self):
        runs, head = guard_runs(before="2026-10-23T08:00:00Z")
        r = decide(approved(runs, head))
        self.assertEqual(kinds(r), [("guard_touch", "")])
        a = next(a for a in r["actions"] if a["type"] == "guard_touch")
        self.assertEqual((a["target"], a["head"]), ("vws-deploy#500", head))
        self.assertIn(str(STARTUP), a["reason"])

    def test_iam_recording_too(self):
        runs, head = guard_runs("vws-iam#144", before="2026-10-23T08:00:00Z")
        self.assertEqual(kinds(decide(approved(runs, head))), [("guard_touch", "")])

    def test_later_run_after_the_touch_clears_it(self):
        # The full recording: the 08:55Z repo-guard success is newer than the startup_failure.
        runs, head = guard_runs()
        state = {"guard_touches": {f"vws-deploy#500@{head}": {"at": TOUCHED_AT}}}
        self.assertEqual(kinds(decide(approved(runs, head), state)), [])
        self.assertEqual(kinds(decide(approved(runs, head))), [])

    def test_a_running_later_run_also_waits(self):
        runs, head = guard_runs(before="2026-10-23T08:00:00Z")
        runs.append(dict(second_failure(head), status="in_progress", conclusion=None))
        self.assertEqual(kinds(decide(approved(runs, head))), [])

    def test_not_approved_or_opted_out_is_left_alone(self):
        runs, head = guard_runs(before="2026-10-23T08:00:00Z")
        for labels in ((), ("verify-approved", "hold"), ("verify-approved", "blocked")):
            with self.subTest(labels=labels):
                c = approved(runs, head)
                c["labels"] = sorted(labels)
                self.assertEqual(kinds(decide(c)), [])


class NoLoop(unittest.TestCase):
    def setUp(self):
        runs, self.head = guard_runs(before="2026-10-23T08:00:00Z")
        self.runs = runs
        self.state = {"guard_touches": {f"vws-deploy#500@{self.head}": {"at": TOUCHED_AT}}}

    def test_touched_head_waits_for_its_run(self):
        r = decide(approved(self.runs, self.head), self.state)
        self.assertEqual(kinds(r), [])
        note = next(n for n in r["notes"] if n["kind"] == "guard_startup_failure")
        self.assertEqual(note["step"], "wait")

    def test_second_startup_failure_is_reported_not_touched(self):
        c = approved(self.runs + [second_failure(self.head)], self.head)
        r = decide(c, self.state)
        self.assertEqual(kinds(r), [("merge_status", ""), ("add_label", "needs-human")])
        st = next(a for a in r["actions"] if a["type"] == "merge_status")
        self.assertEqual(st["key"], orch.status_key("repo-guard startup_failure", "vws-deploy#500", self.head))
        self.assertTrue(st["body"].startswith(orch.FAILURE_MARKER))
        lab = next(a for a in r["actions"] if a["type"] == "add_label")
        self.assertEqual(lab["head"], self.head)
        # The next tick: the entry is already in the status issue and the label is on: nothing more, no touch.
        c["labels"] = sorted(c["labels"] + ["needs-human"])
        again = decide(c, dict(self.state, status_events=[{"key": st["key"], "at": TOUCHED_AT, "kind": "merge", "text": ""}]))
        self.assertEqual(kinds(again), [])

    def test_no_new_run_after_the_wait_is_reported(self):
        c = approved(self.runs, self.head)
        self.assertEqual(kinds(decide(c, self.state, now=TOUCHED_AT + orch.GUARD_TOUCH_WAIT - 1)), [])
        self.assertEqual(kinds(decide(c, self.state, now=TOUCHED_AT + orch.GUARD_TOUCH_WAIT))[0], ("merge_status", ""))

    def test_lost_state_with_the_body_line_reports_instead_of_touching_again(self):
        c = approved(self.runs, self.head, body=orch.tr.guard_touch_body("Part of #446", self.head))
        self.assertEqual(kinds(decide(c)), [("merge_status", ""), ("add_label", "needs-human")])

    def test_a_new_head_starts_over_and_old_records_are_pruned(self):
        r = decide(approved(self.runs, self.head), {"guard_touches": {"vws-deploy#500@" + "d" * 40: {"at": TOUCHED_AT}}})
        self.assertEqual(kinds(r), [("guard_touch", "")])
        self.assertEqual(r["state"]["guard_touches"], {})


class WriteGuard(unittest.TestCase):
    def test_pr_edit_is_the_touch_line_only(self):
        gh = orch.Gh(runner=lambda *a, **k: self.fail("no call expected"))
        path = f"{O}/vws-deploy/pulls/500"
        sha = "a" * 40
        for before, body in (("x", "y"), ("x", "x"), ("x", orch.tr.guard_touch_body("x", sha) + "\nmore"),
                             (orch.tr.guard_touch_body("x", sha), orch.tr.guard_touch_body(orch.tr.guard_touch_body("x", sha), sha)),
                             ("x", orch.tr.guard_touch_body("x", "A" * 40)), (None, orch.tr.guard_touch_body("", sha))):
            with self.subTest(before=before, body=body):
                with self.assertRaises(PermissionError):
                    gh.write("PATCH", path, {"body": body}, before=before)
        with self.assertRaises(PermissionError):
            gh.write("PATCH", path, {"body": orch.tr.guard_touch_body("x", sha), "title": "t"}, before="x")
        self.assertTrue(orch.tr.guard_touch_only("x", orch.tr.guard_touch_body("x", sha)))


class Apply(rt.Env):
    """The touch against the stub gh: journaled, pinned to the head, never repeated."""

    def setUp(self):
        super().setUp()
        r = routes()
        head = "1" * 40
        r[f"{O}/vws-deploy/pulls/500"]["body"]["mergeable_state"] = "blocked"
        r[f"{O}/vws-deploy/actions/runs?head_sha={head}&per_page=100"] = {"body": {"total_count": 1, "workflow_runs": [
            dict(second_failure(head), id=STARTUP)]}}
        self.set_routes(r)
        self.env_ = dict(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")

    def patches(self):
        return [c for c in self.writes() if c["method"] == "PATCH"]

    def test_touch_once_then_wait(self):
        self.assertEqual(self.tick(**self.env_), 0)
        p = self.patches()
        self.assertEqual([c["path"] for c in p], [f"{O}/vws-deploy/pulls/500"])
        self.assertEqual(json.loads(p[0]["stdin"]),
                         {"body": orch.tr.guard_touch_body("Part of #346\nAgent: vws-platform", "1" * 40)})
        self.assertIn("vws-deploy#500@" + "1" * 40, rt.load(self.paths.state_file)["guard_touches"])
        self.assertEqual([c for c in self.writes() if c["method"] == "PUT"], [], "a blocked PR is not merged")
        os.remove(self.calls_file)
        self.assertEqual(self.tick(**self.env_), 0)
        self.assertEqual(self.patches(), [], "one touch per head")

    def test_merge_dry_run_never_touches(self):
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-deploy#500"), 0)
        self.assertEqual(self.patches(), [])


if __name__ == "__main__":
    unittest.main()
