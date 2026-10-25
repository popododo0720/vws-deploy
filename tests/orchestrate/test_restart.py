"""Restart tests (vws-deploy#517 AC2, class 3: partial failure and rerun recovery). Every orchestrate function
that makes two or more GitHub writes runs here under fault_gh.restart_check: the tick stops or fails at each
write in turn, runs again, and must reach the end state of one clean run. test_restart_lint.py keeps that
list complete. Fixtures in fixtures/class3/ come from the vws-deploy#495 close-then-marker finding."""
import argparse
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import test_runtime
from fault_gh import O, FaultGitHub, SimulatedCrash, restart_check
from orch_testlib import FIXTURES, orch

NOW = float(int(time.time()))
NOWISO = orch.iso(NOW)
RUN_ID = "20261025T000000Z-c1a553"
STATUS = ("vws-deploy#415",)  # the status issue body is a report rewritten every tick
HEAD = "b" * 40
SHA = "a" * 40
URL = f"https://github.com/{orch.OWNER}/vws-deploy/pull/500"


def fixture(name):
    with open(os.path.join(FIXTURES, "class3", name), encoding="utf-8") as f:
        return json.load(f)


class TickWorld:
    """One box: a state directory and a FaultGitHub. run() is one in-process `orchestrate.py --once`."""

    def __init__(self, tc, routes, env, repos=("vws-deploy",), state=None, project=None):
        self.tmp = tempfile.mkdtemp()
        tc.addCleanup(shutil.rmtree, self.tmp)
        self.repos = os.path.join(self.tmp, "repos.tsv")
        with open(self.repos, "w", encoding="utf-8") as f:
            f.write("# repo\tkind\n" + "".join(f"{r}\tgo\n" for r in repos))
        self.env = dict({"ORCHESTRATE_STATE_DIR": os.path.join(self.tmp, "state"),
                         "ORCHESTRATE_CONFIG_DIR": os.path.join(self.tmp, "config"),
                         "ORCHESTRATE_MODE": "apply", "ORCHESTRATE_BOARD": "off", "ORCHESTRATE_EXTRA_REPOS": "",
                         "ORCHESTRATE_MAX_WRITES": "20"}, **env)
        self.paths = orch.Paths(self.env)
        if state is not None:
            os.makedirs(self.paths.state, exist_ok=True)
            orch.write_json(self.paths.state_file, state)
        self.gh = FaultGitHub(routes, NOW, project=project)

    def run(self):
        cfg = orch.load_config(self.paths, self.env)
        args = argparse.Namespace(fixture=None, verbose=False, repos=self.repos, print_plan=False, dump_snapshot=None)
        gh = orch.Gh(self.paths.cache, runner=self.gh, sleep=lambda s: None, status_issue=cfg["ORCHESTRATE_STATUS_ISSUE"])
        with mock.patch.object(orch, "run_id_now", lambda now: RUN_ID):
            return orch.tick(self.paths, cfg, args, gh=gh, now=NOW)

    def log(self):
        with open(self.paths.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


def base_routes():
    """A one-repo world: vws-deploy with no open items and the status issue."""
    labels = [{"name": n} for n in ("ready", "deepseek", "grok", "blocked", "in-review", "verify-approved",
                                    "needs-verify", "ac-partial", "ac-pending")]
    return {"rate_limit": {"body": {"resources": {"core": {"remaining": 4000}}}},
            f"{O}/vws-deploy": {"body": {"archived": False}},
            f"{O}/vws-deploy/labels": {"body": labels},
            f"{O}/vws-deploy/issues/415": {"body": {"number": 415, "state": "open", "body": test_runtime.STATUS_SEED}}}


def ac_routes(fx):
    """The AC tick inputs of a class 3 fixture: the merged PR, its trusted verdict, the issue."""
    r = base_routes()
    n, i = fx["pr"]["number"], fx["issue"]
    r[f"{O}/vws-deploy/pulls?state=closed&sort=updated&direction=desc&per_page=100"] = {"body": [
        {"number": n, "html_url": URL, "merged_at": orch.iso(NOW - 3600), "merge_commit_sha": SHA,
         "base": {"ref": "main"}, "head": {"sha": HEAD}, "body": fx["pr"]["body"]}]}
    r[f"{O}/vws-deploy/pulls/{n}/reviews"] = {"body": [
        {"id": 9, "html_url": URL, "user": {"login": orch.VERIFY_LOGIN, "id": 1}, "submitted_at": orch.iso(NOW - 7200),
         "commit_id": HEAD, "state": "APPROVED", "body": "vws-verify: PASS"}]}
    r["graphql"] = {"body": {"data": {"repository": {"pullRequest": {"lastEditedAt": None, "headRefOid": HEAD}}}}}
    issue = {"number": i["number"], "html_url": f"https://github.com/{orch.OWNER}/vws-deploy/issues/{i['number']}",
             "state": "open", "state_reason": None, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
             "body": i["body"], "labels": [{"name": x} for x in i["labels"]]}
    r[f"{O}/vws-deploy/issues"] = {"body": [issue]}
    r[f"{O}/vws-deploy/issues/{i['number']}"] = {"body": dict(issue)}
    r[f"{O}/vws-deploy/issues/{i['number']}/comments"] = {"body": []}
    return r


AC_ENV = {"ORCHESTRATE_AC_TICK": "apply"}
AC_STATE = {"ac_tick_since": "2026-01-01T00:00:00Z"}


class AcTickFixtures(unittest.TestCase):
    """The #495 finding, failing first: a close that lands before its marker comment."""

    def world(self, name):
        return TickWorld(self, ac_routes(fixture(name)), AC_ENV, state=AC_STATE)

    def test_clean_run_matches_the_fixture(self):
        for name in ("pr495-close-then-marker.json", "pr495-partial-then-marker.json"):
            with self.subTest(name):
                fx, w = fixture(name), self.world(name)
                self.assertEqual(w.run(), 0)
                end = w.gh.end_state(STATUS)
                issue = end["issues"]["vws-deploy#7"]
                self.assertEqual((issue["state"], issue["state_reason"], issue["labels"]),
                                 (fx["expect"]["state"], fx["expect"]["state_reason"], fx["expect"]["labels"]))
                self.assertEqual(len(end["comments"]["vws-deploy#7"]), fx["expect"]["comments"])
                self.assertIn(fx["expect"]["comment_has"], end["comments"]["vws-deploy#7"][0])
                self.assertIn(orch.ac_tick_marker("vws-deploy#500"), end["comments"]["vws-deploy#7"][0])

    def test_495_close_lands_and_the_marker_comment_fails(self):
        """The exact Codex reproduction: write 4 (the comment, after body, label removal and close) fails."""
        w = self.world("pr495-close-then-marker.json")
        w.gh.fault = (4, "error_lost")
        self.assertEqual(w.run(), orch.FAIL_EXIT)
        self.assertEqual(w.gh.end_state(STATUS)["issues"]["vws-deploy#7"]["state"], "closed")
        self.assertNotIn("vws-deploy#7", w.gh.end_state(STATUS)["comments"])
        w.gh.fault = None
        self.assertEqual(w.run(), 0)
        comments = w.gh.end_state(STATUS)["comments"].get("vws-deploy#7", [])
        self.assertEqual(len(comments), 1, "the rerun posts the marker comment once")
        self.assertIn("this issue is closed as completed", comments[0])
        self.assertIn("boxes 1, 2, 3 ticked.", comments[0])
        w.run()
        self.assertEqual(len(w.gh.end_state(STATUS)["comments"]["vws-deploy#7"]), 1, "and never again")

    def test_495_crash_right_after_the_close(self):
        w = self.world("pr495-close-then-marker.json")
        w.gh.fault = (3, "crash_after")
        with self.assertRaises(SimulatedCrash):
            w.run()
        w.gh.fault = None
        self.assertEqual(w.run(), 0)
        self.assertEqual(len(w.gh.end_state(STATUS)["comments"].get("vws-deploy#7", [])), 1)

    def test_a_human_close_is_not_recovered(self):
        """Only a completed close of an issue whose boxes all carry ticks, at least one this PR's own, gets
        the late marker; an issue closed as not planned by hand gets nothing."""
        fx = fixture("pr495-close-then-marker.json")
        r = ac_routes(fx)
        ticked = orch.ac_tick_body(fx["issue"]["body"], [1, 2, 3], URL, SHA)["body"]
        for reason, body in (("not_planned", ticked), ("completed", fx["issue"]["body"].replace("- [ ]", "- [x]"))):
            with self.subTest(reason=reason):
                rr = json.loads(json.dumps(r))
                for k in (f"{O}/vws-deploy/issues/7",):
                    rr[k]["body"].update(state="closed", state_reason=reason, body=body, labels=[])
                rr[f"{O}/vws-deploy/issues"]["body"] = []
                w = TickWorld(self, rr, AC_ENV, state=AC_STATE)
                self.assertEqual(w.run(), 0)
                self.assertNotIn("vws-deploy#7", w.gh.end_state(STATUS)["comments"])
                self.assertIn("ac_issue_closed", [e.get("kind") for e in w.log() if e["event"] == "note"])


class Restart(unittest.TestCase):
    """restart_check for each multi-write orchestrate function (test_restart_lint.py)."""

    def test_ac_tick_close(self):
        restart_check(self, lambda: TickWorld(self, ac_routes(fixture("pr495-close-then-marker.json")), AC_ENV,
                                              state=AC_STATE), covers=("apply_ac_tick",), ignore=STATUS)

    def test_ac_tick_partial(self):
        restart_check(self, lambda: TickWorld(self, ac_routes(fixture("pr495-partial-then-marker.json")), AC_ENV,
                                              state=AC_STATE), covers=("apply_ac_tick",), ignore=STATUS)

    def test_apply_wake_and_dispatch(self):
        """test_runtime's world: a verify wake on the #339 thread and a dispatch on vws-iam#1."""
        env = {"ORCHESTRATE_EXTRA_REPOS": "vws-iam", "ORCHESTRATE_WAKE_THREAD": "339"}
        restart_check(self, lambda: TickWorld(self, test_runtime.routes(), env, repos=("vws-deploy", "vws-hypervisor")),
                      covers=("apply",), ignore=STATUS)

    def test_apply_swap_to_grok(self):
        """Two counted DeepSeek failures: the swap notice, grok added, deepseek removed (three writes)."""
        r = base_routes()
        u = f"https://github.com/{orch.OWNER}/vws-deploy/issues/3#issuecomment-"

        def c(cid, hours, body):
            return {"id": cid, "user": {"login": orch.OWNER, "id": 1}, "author_association": "OWNER",
                    "html_url": u + str(cid), "issue_url": f"https://api.github.com/{O}/vws-deploy/issues/3",
                    "created_at": orch.iso(NOW - hours * 3600), "body": body}
        comments = [c(1, 20, orch.DISPATCH_TEXT), c(2, 19, f"[deepseek-dev] Failed for {u}1: dsh exited"),
                    c(3, 10, orch.DISPATCH_TEXT), c(4, 9, f"[deepseek-dev] Failed for {u}3: dsh exited")]
        issue = {"number": 3, "html_url": u[:-len("#issuecomment-")], "state": "open", "created_at": "2026-10-01T00:00:00Z",
                 "updated_at": orch.iso(NOW - 9 * 3600), "body": "", "labels": [{"name": "ready"}, {"name": "deepseek"}]}
        r[f"{O}/vws-deploy/issues"] = {"body": [issue]}
        r[f"{O}/vws-deploy/issues/3"] = {"body": dict(issue)}
        r[f"{O}/vws-deploy/issues/3/comments"] = {"body": comments}
        r[f"{O}/vws-deploy/issues/comments"] = {"body": [dict(x) for x in comments]}
        r[f"{O}/vws-deploy/issues/3/events"] = {"body": [{"event": "labeled", "label": {"name": "ready"},
                                                          "created_at": orch.iso(NOW - 30 * 3600)}]}
        clean = TickWorld(self, r, {})
        clean.run()
        end = clean.gh.end_state(STATUS)
        self.assertEqual(end["issues"]["vws-deploy#3"]["labels"], ["grok", "ready"], "the scenario swaps")
        self.assertEqual(len([b for b in end["comments"]["vws-deploy#3"] if "orchestrate:swap" in b]), 1)
        restart_check(self, lambda: TickWorld(self, r, {}), covers=("apply",), ignore=STATUS)


class BoardWorld:
    """board_sync on its own, against a FaultGitHub project (ORCHESTRATE_BOARD=apply, a sync every tick)."""
    FIELDS = {"FS": {"name": "Status", "options": {f"os{i}": n for i, n in enumerate(
                  ("Backlog", "Ready", "DeepSeek", "Grok", "In review", "Blocked", "Done"))}},
              "FO": {"name": "Owner", "options": {"oo1": "grok", "oo2": "deepseek", "oo3": "verify"}},
              "FP": {"name": "Priority", "options": {"op1": "Normal", "op2": "Blocker"}},
              "FD": {"name": "Depends on", "options": {}}}

    def __init__(self, tc):
        self.tmp = tempfile.mkdtemp()
        tc.addCleanup(shutil.rmtree, self.tmp)
        token = os.path.join(self.tmp, "token")
        with open(token, "w", encoding="utf-8") as f:
            f.write("board-token-for-tests\n")
        env = {"ORCHESTRATE_STATE_DIR": os.path.join(self.tmp, "state"), "ORCHESTRATE_CONFIG_DIR": os.path.join(self.tmp, "c"),
               "ORCHESTRATE_BOARD": "apply", "ORCHESTRATE_BOARD_EVERY": "1", "ORCHESTRATE_BOARD_TOKEN_FILE": token}
        self.paths = orch.Paths(env)
        os.makedirs(self.paths.state, exist_ok=True)
        self.cfg = orch.load_config(self.paths, env)
        u = f"https://github.com/{orch.OWNER}/vws-deploy/issues/"
        its = {1: ("ready", "deepseek"), 2: ("ready", "grok"), 3: ("blocked",), 4: ("ready",)}
        self.snapshot = {"items": {f"vws-deploy#{n}": {"repo": "vws-deploy", "number": n, "url": u + str(n), "state": "open",
                                                       "is_pr": False, "draft": False, "labels": list(lb), "node_id": f"I_{n}",
                                                       "body": "Depends on #1" if n == 3 else ""}
                                   for n, lb in its.items()}}
        project = {"fields": self.FIELDS, "content": {f"I_{n}": {"url": u + str(n), "state": "OPEN"} for n in its},
                   "items": {"PVTI_old": {"content": "I_1", "values": {"FS": "os0"}}}}
        self.gh = FaultGitHub({}, NOW, project=project)

    def run(self):
        gh = orch.Gh(None, runner=self.gh, sleep=lambda s: None)
        with mock.patch.object(orch, "BOARD_BATCH", 2):
            orch.board_sync(gh, self.snapshot, self.cfg, self.paths, lambda *a, **k: None, "apply", False)


class BoardRestart(unittest.TestCase):
    def test_board_write(self):
        clean = BoardWorld(self)
        clean.run()
        board = dict(clean.gh.end_state()["board"])
        self.assertEqual(len(board), 4, "every open item is on the board")
        restart_check(self, lambda: BoardWorld(self), covers=("board_write",))


if __name__ == "__main__":
    unittest.main()
