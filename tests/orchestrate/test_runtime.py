"""Runtime tests for .github/orchestrate (vws-deploy#333): the gh wrapper, ETag cache, dry-run and
apply ticks against a stub gh, locks, STOP, logs, failure comments, and the box scripts.
Run: python3 -m unittest discover -s tests/orchestrate"""
import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from orch_testlib import BOX, ROOT, SCRIPT, orch

HERE = os.path.dirname(os.path.abspath(__file__))
O = "repos/popododo0720"
def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


NOWISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def pr_issue(repo, n):
    return {"number": n, "html_url": f"https://github.com/popododo0720/{repo}/pull/{n}", "state": "open",
            "created_at": "2026-10-01T00:00:00Z", "labels": [], "body": "", "pull_request": {}}


def routes():
    pulls = [{"number": n, "draft": False, "base": {"ref": "main"}, "head": {"sha": f"{n:040x}"}} for n in range(400, 405)]
    # The status thread is a never-merged draft PR; a draft never counts as pending.
    pulls.append({"number": 339, "draft": True, "base": {"ref": "main"}, "head": {"sha": f"{339:040x}"}})
    issues = [pr_issue("vws-deploy", n) for n in range(400, 405)] + [pr_issue("vws-deploy", 339)]
    labels = [{"name": n} for n in ("ready", "deepseek", "grok", "blocked", "in-review", "verify-approved")]
    r = {
        "rate_limit": {"body": {"resources": {"core": {"remaining": 4000}}}},
        f"{O}/vws-deploy": {"body": {"archived": False}},
        f"{O}/vws-iam": {"body": {"archived": False}},
        f"{O}/vws-hypervisor": {"body": {"archived": True}},
        f"{O}/vws-deploy/labels": {"body": labels},
        f"{O}/vws-iam/labels": {"body": labels},
        f"{O}/vws-deploy/pulls": {"body": pulls},
        f"{O}/vws-deploy/issues": {"body": issues},
        f"{O}/vws-iam/issues": {"body": [{"number": 1, "html_url": "https://github.com/popododo0720/vws-iam/issues/1",
                                          "state": "open", "created_at": "2026-10-01T00:00:00Z", "body": "",
                                          "labels": [{"name": "ready"}, {"name": "deepseek"}]}]},
        f"{O}/vws-iam/issues/1": {"body": {"state": "open", "labels": [{"name": "ready"}, {"name": "deepseek"}]}},
    }
    for n in range(400, 405):
        r[f"{O}/vws-deploy/commits/{n:040x}/check-runs"] = {"body": {"check_runs": []}}
        r[f"{O}/vws-deploy/commits/{n:040x}/status"] = {"body": {"state": "success", "statuses": []}}
    return r


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        os.symlink(os.path.join(HERE, "stub_gh.py"), os.path.join(self.bin, "gh"))
        self.repos = os.path.join(self.tmp, "repos.tsv")
        with open(self.repos, "w") as f:
            f.write("# repo\tkind\nvws-deploy\tgo\nvws-hypervisor\tgo\n")
        self.routes_file = os.path.join(self.tmp, "routes.json")
        self.set_routes(routes())
        self.calls_file = os.path.join(self.tmp, "calls.jsonl")
        self.env = {"ORCHESTRATE_STATE_DIR": os.path.join(self.tmp, "state"),
                    "ORCHESTRATE_CONFIG_DIR": os.path.join(self.tmp, "config"),
                    "ORCHESTRATE_EXTRA_REPOS": "vws-iam"}
        patch = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"],
                                             "STUB_ROUTES": self.routes_file, "STUB_LOG": self.calls_file,
                                             "GH_TOKEN": "ghp_should_never_be_used_123"})
        patch.start()
        self.addCleanup(patch.stop)
        self.paths = orch.Paths(self.env)

    def set_routes(self, r):
        with open(self.routes_file, "w") as f:
            json.dump(r, f)

    def tick(self, *extra, **env):
        return orch.main(["--once", "--repos", self.repos, *extra], dict(self.env, **env))

    def calls(self):
        if not os.path.exists(self.calls_file):
            return []
        with open(self.calls_file) as f:
            return [json.loads(line) for line in f]

    def writes(self):
        return [c for c in self.calls() if c["method"] != "GET"]

    def log(self):
        with open(self.paths.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class GhWrapper(Env):
    def test_allowlist(self):
        gh = orch.Gh()
        for method, path in [("GET", "repos/someone/vws-iam/issues"), ("GET", "user"),
                             ("POST", f"{O}/vws-iam/pulls"), ("DELETE", f"{O}/vws-iam/issues/1/labels/ready"),
                             ("PATCH", f"{O}/vws-iam/issues/comments/1"), ("POST", f"{O}/vws-iam/issues/1/comments/x")]:
            with self.subTest(f"{method} {path}"), self.assertRaises(PermissionError):
                gh.get(path) if method == "GET" else gh.write(method, path, {})
        self.assertEqual(self.calls(), [], "a refused call never reaches gh")

    def test_etag_and_no_gh_token(self):
        gh = orch.Gh(os.path.join(self.tmp, "cache.json"))
        gh.get(f"{O}/vws-deploy/labels")
        gh.save_cache()
        gh2 = orch.Gh(os.path.join(self.tmp, "cache.json"))
        data, _ = gh2.get(f"{O}/vws-deploy/labels")
        self.assertEqual(len(data), 6)
        c = self.calls()
        self.assertEqual((c[0]["inm"] == "", c[1]["inm"] != ""), (True, True))
        self.assertEqual(gh2.stats["not_modified"], 1)
        self.assertFalse(any(x["gh_token_set"] for x in c), "GH_TOKEN must be removed from the gh environment")

    def test_retry_transient_then_raise(self):
        r = routes()
        r[f"{O}/vws-deploy/labels"] = {"status": 502, "body": {}}
        self.set_routes(r)
        gh = orch.Gh(sleep=lambda s: None)
        with self.assertRaises(orch.GhError):
            gh.get(f"{O}/vws-deploy/labels")
        self.assertEqual(len(self.calls()), 3)

    def test_truncated_pagination_fails(self):
        class Paged(orch.Gh):
            def get(self, path):
                return [1], True
        with self.assertRaises(orch.GhError):
            Paged().get_all(f"{O}/vws-deploy/issues", limit=3)

    def test_write_is_one_try(self):
        with mock.patch.dict(os.environ, {"STUB_DENY_WRITES": "1"}):
            with self.assertRaises(orch.GhError):
                orch.Gh(sleep=lambda s: None).write("POST", f"{O}/vws-iam/issues/1/comments", {"body": "x"})
        self.assertEqual(len(self.calls()), 1)


class Ticks(Env):
    def test_dry_run_plans_and_never_writes(self):
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.writes(), [])
        planned = [(e["action"], e["target"]) for e in self.log() if e["event"] == "planned"]
        self.assertEqual(planned, [("wake_verify", "vws-deploy#339"), ("dispatch", "vws-iam#1")])
        self.assertFalse(os.path.exists(self.paths.state_file), "dry-run writes no decision state")
        health = load(self.paths.health)
        self.assertEqual((health["result"], health["pending"]), ("ok", 5))
        self.assertNotIn(f"{O}/vws-hypervisor/issues", [c["path"].split("?")[0] for c in self.calls()], "archived repo skipped")

    def test_second_tick_uses_etags(self):
        self.tick()
        self.tick()
        health = load(self.paths.health)
        self.assertGreater(health["gh"]["not_modified"], 10)

    def test_apply_posts_marker_comments(self):
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        w = self.writes()
        self.assertEqual([(c["method"], c["path"]) for c in w],
                         [("POST", f"{O}/vws-deploy/issues/339/comments"), ("POST", f"{O}/vws-iam/issues/1/comments")])
        wake = json.loads(w[0]["stdin"])["body"]
        self.assertEqual(wake.split("\n")[0], "<!-- vws-orchestrate:verify-wake -->")
        self.assertTrue(json.loads(w[1]["stdin"])["body"].startswith("@deepseek-dev please take this issue"))
        self.assertEqual(load(self.paths.state_file)["prev_pending_count"], 5)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.state_file).st_mode), 0o600)

    def test_wake_not_reposted_for_same_key(self):
        self.tick(ORCHESTRATE_MODE="apply")
        wake = json.loads(self.writes()[0]["stdin"])["body"]
        r = routes()
        r[f"{O}/vws-deploy/issues/339/comments"] = {"body": [{"id": 5, "created_at": NOWISO, "body": wake}]}
        disp = [{"id": 6, "issue_url": "x/vws-iam/issues/1", "created_at": NOWISO,
                 "html_url": "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-6", "body": orch.DISPATCH_TEXT}]
        r[f"{O}/vws-iam/issues/comments"] = {"body": disp}
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": disp}
        self.set_routes(r)
        os.remove(self.calls_file)
        self.tick(ORCHESTRATE_MODE="apply")
        self.assertEqual(self.writes(), [], "same pending set and an in-flight dispatch: nothing to write")

    def test_scope_and_write_cap(self):
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual([c["path"] for c in self.writes()], [f"{O}/vws-iam/issues/1/comments"])
        os.remove(self.calls_file)
        shutil.rmtree(self.paths.state)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_MAX_WRITES="1")
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(any(e["event"] == "skipped" and "write cap" in e.get("reason", "") for e in self.log()))

    def test_two_dispatches_and_the_write_cap(self):
        # Planned: wake, blocked on #3, #4 and #5 (they depend on the open #1), dispatch #1, dispatch #2.
        r = routes()
        ready = [{"name": "ready"}, {"name": "deepseek"}]
        def issue(n, body=""):
            return {"number": n, "html_url": f"https://github.com/popododo0720/vws-iam/issues/{n}", "state": "open",
                    "created_at": f"2026-10-0{n}T00:00:00Z", "body": body, "labels": ready}
        r[f"{O}/vws-iam/issues"] = {"body": [issue(1), issue(2)] + [issue(n, "Depends on #1") for n in (3, 4, 5)]}
        for n in (1, 2):
            r[f"{O}/vws-iam/issues/{n}"] = {"body": {"state": "open", "labels": ready}}
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply")
        planned = [(e["action"], e["target"]) for e in self.log() if e["event"] == "planned"]
        self.assertEqual(planned, [("wake_verify", "vws-deploy#339")] +
                         [("add_label", f"vws-iam#{n}") for n in (3, 4, 5)] +
                         [("dispatch", "vws-iam#1"), ("dispatch", "vws-iam#2")])
        self.assertEqual([(c["method"], c["path"]) for c in self.writes()],
                         [("POST", f"{O}/vws-deploy/issues/339/comments")] +
                         [("POST", f"{O}/vws-iam/issues/{n}/labels") for n in (3, 4, 5)] +
                         [("POST", f"{O}/vws-iam/issues/1/comments")], "at most 5 writes per tick")
        self.assertTrue(any(e["event"] == "skipped" and e.get("target") == "vws-iam#2"
                            and "write cap 5" in e.get("reason", "") for e in self.log()))

    def test_failed_writes_count_and_fail_the_tick(self):
        with mock.patch.dict(os.environ, {"STUB_DENY_WRITES": "1"}):
            rc = self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_MAX_WRITES="1")
        self.assertEqual(rc, orch.FAIL_EXIT, "apply errors make a failed tick, so backoff and alerts start")
        self.assertEqual(len(self.writes()), 1, "a failed attempt still uses the write budget")
        self.assertEqual(load(self.paths.health)["result"], "apply_failed")

    def test_old_dispatch_history_outside_window(self):
        r = routes()
        old = "2026-09-01T00:00:00Z"
        u = "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-"
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": [
            {"id": 1, "html_url": u + "1", "created_at": old, "body": orch.DISPATCH_TEXT},
            {"id": 2, "html_url": u + "2", "created_at": old, "body": f"[deepseek-dev] Done for {u}1"}]}
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual(self.writes(), [], "a task done long ago is not dispatched again")

    def test_recheck_rejects_newly_blocked(self):
        r = routes()
        r[f"{O}/vws-iam/issues/1"] = {"body": {"state": "open", "labels": [{"name": "ready"}, {"name": "deepseek"}, {"name": "blocked"}]}}
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(e.get("reason") == "target changed since collect" for e in self.log()))

    def test_interrupted_swap_is_finished(self):
        r = routes()
        u = "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-"
        r[f"{O}/vws-iam/issues"]["body"][0]["labels"].append({"name": "grok"})
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": [
            {"id": 1, "html_url": u + "1", "created_at": "2026-10-01T00:00:00Z", "body": orch.DISPATCH_TEXT},
            {"id": 2, "html_url": u + "2", "created_at": "2026-10-02T00:00:00Z", "body": orch.DISPATCH_TEXT},
            {"id": 3, "html_url": u + "3", "created_at": "2026-10-02T01:00:00Z", "body": "x\n<!-- orchestrate:swap -->"}]}
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual([(c["method"], c["path"]) for c in self.writes()], [("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek")])

    def test_done_report_with_pr_outside_vws_does_not_fail_the_tick(self):
        # vws-deploy#341: the PR is never read (the allowlist would refuse it); the item counts one failure.
        r = routes()
        u = "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-"
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": [
            {"id": 1, "html_url": u + "1", "created_at": "2026-10-01T00:00:00Z", "body": orch.DISPATCH_TEXT},
            {"id": 2, "html_url": u + "2", "created_at": "2026-10-01T01:00:00Z",
             "body": f"[deepseek-dev] Done for {u}1\nPR: https://github.com/popododo0720/other-repo/pull/5"},
            {"id": 3, "html_url": u + "3", "created_at": "2026-10-01T02:00:00Z", "body": orch.DISPATCH_TEXT},
            {"id": 4, "html_url": u + "4", "created_at": "2026-10-01T03:00:00Z",
             "body": f"[deepseek-dev] Done for {u}3\nPR: https://github.com/someone/vws-iam/pull/6"}]}
        self.set_routes(r)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1"), 0)
        self.assertFalse([c for c in self.calls() if "other-repo" in c["path"] or "someone" in c["path"]])
        self.assertEqual(load(self.paths.health)["result"], "ok")
        notes = [e for e in self.log() if e.get("kind") == "report_pr_outside_vws"]
        self.assertEqual(len(notes), 2)
        self.assertIn(("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek"),
                      [(c["method"], c["path"]) for c in self.writes()], "2 failures: moved to grok")

    def test_swap_removes_owned_in_review(self):
        r = routes()
        u = "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-"
        r[f"{O}/vws-iam/issues"]["body"][0]["labels"].append({"name": "in-review"})
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": [
            {"id": 1, "html_url": u + "1", "created_at": "2026-10-01T00:00:00Z", "body": orch.DISPATCH_TEXT},
            {"id": 2, "html_url": u + "2", "created_at": "2026-10-02T00:00:00Z", "body": orch.DISPATCH_TEXT}]}
        self.set_routes(r)
        orch.write_json(self.paths.state_file, {"owned_labels": {"in-review": ["vws-iam#1"], "blocked": []}})
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1"), 0)
        self.assertEqual([(c["method"], c["path"]) for c in self.writes()],
                         [("POST", f"{O}/vws-iam/issues/1/comments"), ("POST", f"{O}/vws-iam/issues/1/labels"),
                          ("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek"),
                          ("DELETE", f"{O}/vws-iam/issues/1/labels/in-review")])
        self.assertEqual(load(self.paths.state_file)["owned_labels"]["in-review"], [])

    def test_label_ownership_is_journaled_before_the_write(self):
        r = routes()
        r[f"{O}/vws-iam/issues"]["body"][0]["body"] = "Depends on #99"
        r[f"{O}/vws-iam/issues/99"] = {"body": {"state": "open"}}
        self.set_routes(r)
        with mock.patch.dict(os.environ, {"STUB_DENY_WRITES": "1"}):
            self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual(load(self.paths.state_file)["owned_labels"]["blocked"], ["vws-iam#1"])

    def test_missing_label_is_never_created(self):
        r = routes()
        r[f"{O}/vws-iam/issues"]["body"][0]["body"] = "Depends on #99"
        r[f"{O}/vws-iam/labels"] = {"body": [{"name": "ready"}, {"name": "deepseek"}]}
        r[f"{O}/vws-iam/issues/99"] = {"body": {"state": "open"}}
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual(self.writes(), [])
        self.assertTrue(any("label blocked does not exist" in e.get("reason", "") for e in self.log()))

    def test_auth_failure_and_rate_floor(self):
        r = routes()
        r["rate_limit"] = {"status": 401, "body": {"message": "Bad credentials"}}
        self.set_routes(r)
        self.assertEqual(self.tick(), orch.FAIL_EXIT)
        h = load(self.paths.health)
        self.assertEqual(h["result"], "auth_failed")
        self.assertEqual(len(h["recent_errors"]), 1)
        r["rate_limit"] = {"body": {"resources": {"core": {"remaining": 10}}}}
        self.set_routes(r)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(load(self.paths.health)["result"], "rate_low")

    def test_lock_and_stop(self):
        os.makedirs(self.paths.state, exist_ok=True)
        fd = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.tick(), 0)
        finally:
            os.close(fd)
        self.assertEqual(self.log()[-1]["reason"], "another tick holds run.lock")
        self.assertEqual(self.calls(), [])
        os.makedirs(self.paths.config, exist_ok=True)
        open(self.paths.stop, "w").close()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.log()[-1]["event"], "stopped")

    def test_env_file_mode_and_bad_config(self):
        os.makedirs(self.paths.config, exist_ok=True)
        with open(self.paths.env_file, "w") as f:
            f.write("ORCHESTRATE_MODE=apply\n")
        self.tick()
        self.assertEqual(len(self.writes()), 2, "the env file switches to apply without a restart")
        with open(self.paths.env_file, "w") as f:
            f.write("ORCHESTRATE_MODE=yes\n")
        self.assertEqual(self.tick(), 2)
        for bad in ("0", "-1", "3.5", "vws-deploy#339", "abc", ""):
            with open(self.paths.env_file, "w") as f:
                f.write(f"ORCHESTRATE_STATUS_PR={bad}\n")
            self.assertEqual(self.tick(), 2, f"ORCHESTRATE_STATUS_PR={bad!r} is refused")
        with open(self.paths.env_file, "w") as f:
            f.write("ORCHESTRATE_DEEPSEEK_CAP=0\n")
        self.assertEqual(self.tick(), 2, "ORCHESTRATE_DEEPSEEK_CAP=0 is refused")

    def test_status_pr_is_configurable(self):
        r = routes()
        r[f"{O}/vws-deploy/issues/12/comments"] = {"body": []}
        self.set_routes(r)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_STATUS_PR="12"), 0)
        self.assertIn(("POST", f"{O}/vws-deploy/issues/12/comments"),
                      [(c["method"], c["path"]) for c in self.writes()])
        self.assertNotIn(f"{O}/vws-deploy/issues/339/comments", [c["path"] for c in self.calls()])

    def test_fixture_mode(self):
        snap = os.path.join(self.tmp, "snap.json")
        self.tick("--dump-snapshot", snap)
        os.remove(self.calls_file)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = orch.main(["--fixture", snap, "--print-plan"], dict(self.env, ORCHESTRATE_MODE="apply"))
        self.assertEqual(rc, 0)
        self.assertEqual([a["type"] for a in json.loads(out.getvalue())["actions"]], ["wake_verify", "dispatch"])
        self.assertEqual(self.calls(), [], "--fixture uses no network and never writes")

    def test_log_rotation_and_scrub(self):
        cfg = dict(orch.DEFAULTS, ORCHESTRATE_LOG_MAX_BYTES="200", ORCHESTRATE_LOG_KEEP="2")
        log = orch.Log(self.paths, cfg, "r", "dry-run")
        for _ in range(4):
            log("x", error="token ghp_ABCDEFGHIJKLMNOP and Bearer abc.def " + "y" * 150)
            log.rotate()
        files = sorted(os.listdir(self.paths.log_dir))
        self.assertEqual(files, ["orchestrate.jsonl.1", "orchestrate.jsonl.2"])
        text = "".join(read(os.path.join(self.paths.log_dir, f)) for f in files)
        self.assertNotIn("ghp_ABC", text)
        self.assertNotIn("abc.def", text)


class FailureComments(Env):
    def notify(self, failures, start=1000.0, **env):
        return orch.main(["--notify-failure", "--failures", str(failures), "--exit-code", "4",
                          "--streak-start", str(start), "--last-error", "Traceback: boom ghp_SECRETSECRET123"],
                         dict(self.env, **env))

    def test_once_per_streak_at_threshold(self):
        self.notify(2, ORCHESTRATE_MODE="apply")
        self.assertEqual(self.writes(), [])
        self.notify(3, ORCHESTRATE_MODE="apply")
        self.notify(4, ORCHESTRATE_MODE="apply")
        w = self.writes()
        self.assertEqual([c["path"] for c in w], [f"{O}/vws-deploy/issues/339/comments"])
        body = json.loads(w[0]["stdin"])["body"]
        self.assertEqual(body.split("\n")[0], "<!-- vws-orchestrate:failure -->")
        self.assertIn("3 failed ticks in a row", body)
        self.assertNotIn("SECRETSECRET", body)
        self.assertIn("[redacted]", body)

    def test_new_streak_after_success_and_min_gap(self):
        self.notify(3, ORCHESTRATE_MODE="apply")
        self.notify(3, start=2000.0, ORCHESTRATE_MODE="apply")
        self.assertEqual(len(self.writes()), 1, "a loop restart inside the gap does not post again")
        self.tick()  # a good tick ends the streak
        self.assertFalse(os.path.exists(self.paths.failure_file))
        self.notify(3, start=3000.0, ORCHESTRATE_MODE="apply")
        self.assertEqual(len(self.writes()), 2)

    def test_alert_mode(self):
        self.notify(3)
        self.assertEqual(self.writes(), [], "dry-run with ORCHESTRATE_FAILURE_ALERT=apply posts nothing")
        self.notify(3, ORCHESTRATE_FAILURE_ALERT="always")
        self.assertEqual(len(self.writes()), 1)

    def test_scoped_trial_posts_no_failure_comment(self):
        # vws-deploy#341: ORCHESTRATE_SCOPE limits every write, the failure comment included.
        self.notify(3, ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.notify(3, ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1", ORCHESTRATE_FAILURE_ALERT="always")
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(e.get("event") == "failure_alert_skipped" and "ORCHESTRATE_SCOPE" in e.get("reason", "")
                            for e in self.log()))
        self.assertFalse(os.path.exists(self.paths.failure_file), "a later unscoped streak still posts")
        self.notify(3, ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-deploy#339")
        self.assertEqual(len(self.writes()), 1, "a scope on the status thread itself allows the comment")

    def test_key_already_on_status_thread_is_not_reposted(self):
        key = "sha256:" + orch.hashlib.sha256(f"failure:{orch.socket.gethostname()}:1000.0".encode()).hexdigest()
        r = routes()
        r[f"{O}/vws-deploy/issues/339/comments"] = {"body": [{"id": 1, "created_at": NOWISO,
                                                             "body": f"<!-- vws-orchestrate:failure -->\nIdempotency key: `{key}`"}]}
        self.set_routes(r)
        self.notify(3, ORCHESTRATE_MODE="apply")
        self.assertEqual(self.writes(), [])
        self.assertTrue(os.path.exists(self.paths.failure_file))

    def test_post_failure_is_retried_next_time(self):
        with mock.patch.dict(os.environ, {"STUB_DENY_WRITES": "1"}):
            self.assertEqual(self.notify(3, ORCHESTRATE_MODE="apply"), 0)
        self.assertFalse(os.path.exists(self.paths.failure_file))
        self.notify(4, ORCHESTRATE_MODE="apply")
        self.assertEqual(len(self.writes()), 2)


class BoxScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.record = os.path.join(self.tmp, "py-calls")
        self.py = os.path.join(self.tmp, "fake_orchestrate.py")
        self.env = dict(os.environ, ORCHESTRATE_STATE_DIR=os.path.join(self.tmp, "state"),
                        ORCHESTRATE_CONFIG_DIR=os.path.join(self.tmp, "config"), ORCHESTRATE_PY=self.py,
                        ORCHESTRATE_BACKOFF_BASE="0", ORCHESTRATE_INTERVAL="0", FAKE_RECORD=self.record)

    def fake(self, rc):
        with open(self.py, "w") as f:
            f.write("import os, sys\n"
                    "open(os.environ['FAKE_RECORD'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    f"sys.exit(0 if '--notify-failure' in sys.argv else {rc})\n")

    def lines(self):
        return read(self.record).splitlines()

    def test_loop_backoff_and_failure_notify(self):
        self.fake(1)
        env = dict(self.env, ORCHESTRATE_LOOP_ITERATIONS="3")
        subprocess.run([os.path.join(BOX, "orchestrate-loop.sh")], env=env, check=True, timeout=30)
        notes = [ln for ln in self.lines() if ln.startswith("--notify-failure")]
        self.assertEqual([n.split()[2] for n in notes], ["1", "2", "3"])
        self.assertEqual(len({n.split()[6] for n in notes}), 1, "one streak start for the whole streak")

    def test_loop_success_resets_and_lock(self):
        self.fake(0)
        env = dict(self.env, ORCHESTRATE_LOOP_ITERATIONS="2")
        subprocess.run([os.path.join(BOX, "orchestrate-loop.sh")], env=env, check=True, timeout=30)
        self.assertEqual(self.lines(), ["--once", "--once"])
        os.makedirs(env["ORCHESTRATE_STATE_DIR"], exist_ok=True)
        fd = os.open(os.path.join(env["ORCHESTRATE_STATE_DIR"], "loop.lock"), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            r = subprocess.run([os.path.join(BOX, "orchestrate-loop.sh")], env=env, timeout=30, capture_output=True, text=True)
        finally:
            os.close(fd)
        self.assertEqual(r.returncode, 0)
        self.assertIn("another loop holds", r.stderr)
        self.assertEqual(len(self.lines()), 2, "a second loop runs no tick")

    def test_killing_the_loop_stops_the_tick(self):
        with open(self.py, "w") as f:
            f.write("import os, time\nopen(os.environ['FAKE_RECORD'], 'a').write(str(os.getpid()) + '\\n')\ntime.sleep(60)\n")
        p = subprocess.Popen([os.path.join(BOX, "orchestrate-loop.sh")], env=self.env)
        for _ in range(100):
            if os.path.exists(self.record) and read(self.record).strip():
                break
            time.sleep(0.05)
        child = int(read(self.record).split()[0])
        self.assertEqual(os.getpgid(child), child, "each tick runs in its own process group")
        p.terminate()
        self.assertEqual(p.wait(timeout=10), 143)
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        lock = os.path.join(self.env["ORCHESTRATE_STATE_DIR"], "loop.lock")
        self.assertEqual(subprocess.run(["flock", "-n", lock, "true"]).returncode, 0, "the lock is free right away")

    def test_ensure_is_idempotent(self):
        self.fake(0)
        env = dict(self.env, ORCHESTRATE_LOOP_ITERATIONS="20", ORCHESTRATE_INTERVAL="1")
        ensure = os.path.join(BOX, "orchestrate-ensure.sh")
        a = subprocess.run([ensure], env=env, capture_output=True, text=True, timeout=30)
        b = subprocess.run([ensure], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((a.returncode, b.returncode), (0, 0))
        self.assertIn("started", a.stdout)
        self.assertIn("orchestrate-ensure: running (pid", b.stdout, "the loop must not keep ensure.lock")
        pid = int(read(os.path.join(env["ORCHESTRATE_STATE_DIR"], "loop.pid")))
        os.kill(pid, 15)


class Static(unittest.TestCase):
    def test_no_webhook_or_secret_paths(self):
        text = read(SCRIPT)
        for word in ("urllib", "webhook", "X-Automation-Key"):
            self.assertNotIn(word, text)

    def test_scripts_executable(self):
        for f in (SCRIPT, os.path.join(BOX, "orchestrate-loop.sh"), os.path.join(BOX, "orchestrate-ensure.sh")):
            self.assertTrue(os.access(f, os.X_OK), f)
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".github", "drift", "repos.tsv")))


if __name__ == "__main__":
    unittest.main()
