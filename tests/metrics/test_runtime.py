"""Runtime tests for .github/metrics/metrics.py (vws-deploy#345) against a stub gh: allowlist, dry-run,
publishing one fast-forward commit, idempotence, drift and --recompute, refusals, rate floor, read budget,
--init, --if-due, and the box loop integration.
Run: python3 -m unittest discover -s tests/metrics"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from metrics_testlib import BOX, DAY, SCRIPT, metrics, orch

HERE = os.path.dirname(os.path.abspath(__file__))
O = "repos/popododo0720"
D = f"{O}/vws-deploy"
H, T, F = "1" * 40, "2" * 40, "3" * 40
NOW = orch.utc("2026-10-21T00:00:00Z")  # 09:00 KST on 10-21: 10-20 is due and is "yesterday"
TRAILER = metrics.GENERATOR_TRAILER


def root_commit(sha=H):
    return {"sha": sha, "commit": {"message": f"{metrics.INIT_SUBJECT}\n\n{TRAILER}\n"}, "parents": []}


def routes(head=H, history=None, files=None, tree_extra=None, rate=4000):
    r = {
        "rate_limit": {"body": {"resources": {"core": {"remaining": rate}}}},
        D: {"body": {"archived": False}},
        f"{O}/vws-hypervisor": {"body": {"archived": True}},
        f"{D}/git/ref/heads/factory-metrics": {"body": {"object": {"sha": head}}} if head else {"status": 404, "body": {"message": "Not Found"}},
        f"{D}/commits?sha={head}&per_page=100": {"body": history or [root_commit()]},
        f"{D}/commits/{head}": {"body": {"sha": head, "commit": {"tree": {"sha": T}}, "files": files or [{"filename": "metrics/README.md"}]}},
        f"{D}/git/trees/{T}?recursive=1": {"body": {"truncated": False, "tree": [{"path": "metrics/README.md", "type": "blob", "sha": "4" * 40}] + (tree_extra or [])}},
    }
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
        self.state = os.path.join(self.tmp, "state")
        self.env = {"ORCHESTRATE_STATE_DIR": self.state, "ORCHESTRATE_CONFIG_DIR": os.path.join(self.tmp, "config"),
                    "ORCHESTRATE_EXTRA_REPOS": ""}
        patch = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"],
                                             "STUB_ROUTES": self.routes_file, "STUB_LOG": self.calls_file,
                                             "GH_TOKEN": "ghp_should_never_be_used_123"})
        patch.start()
        self.addCleanup(patch.stop)

    def set_routes(self, r):
        with open(self.routes_file, "w") as f:
            json.dump(r, f)

    def run_m(self, *argv, now=NOW, **env):
        return metrics.main(["--repos", self.repos, "--orchestrator-log-dir", os.path.join(self.tmp, "nolog"), *argv],
                            dict(self.env, **env), now=now)

    def calls(self):
        if not os.path.exists(self.calls_file):
            return []
        with open(self.calls_file) as f:
            return [json.loads(line) for line in f]

    def writes(self):
        return [c for c in self.calls() if c["method"] != "GET"]

    def log(self):
        p = os.path.join(self.state, "log", "metrics.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def events(self):
        return [e["event"] for e in self.log()]

    def local(self, mode="dry-run"):
        return os.path.join(self.state, "metrics", mode, "daily", f"{DAY}.json")


class Allowlist(Env):
    def test_refused_calls_never_reach_gh(self):
        gh = metrics.MetricsGh()
        bad = [("GET", "user"), ("GET", "repos/someone/vws-deploy"), ("GET", f"{D}/git/ref/heads/main"),
               ("GET", f"{O}/vws-iam/git/ref/heads/factory-metrics"), ("POST", f"{D}/pulls"),
               ("POST", f"{D}/git/blobs"), ("PATCH", f"{D}/git/refs/heads/main"), ("DELETE", f"{D}/git/refs/heads/factory-metrics"),
               ("POST", f"{D}/issues/1/comments"), ("PUT", f"{D}/contents/metrics/x.json")]
        for method, path in bad:
            with self.subTest(f"{method} {path}"), self.assertRaises(PermissionError):
                gh.get(path) if method == "GET" else gh.write(method, path, {"force": False})
        for payload in (None, {"sha": H}, {"sha": H, "force": True}, {"sha": H, "force": 0}):
            with self.subTest(payload=payload), self.assertRaises(PermissionError):
                gh.write("PATCH", f"{D}/git/refs/heads/factory-metrics", payload)
        with self.assertRaises(PermissionError):
            gh.write("POST", f"{D}/git/refs", {"ref": "refs/heads/factory-metrics", "sha": H})
        with self.assertRaises(PermissionError):
            metrics.MetricsGh(init=True).write("POST", f"{D}/git/refs", {"ref": "refs/heads/main", "sha": H})
        self.assertEqual(self.calls(), [])


class DryRun(Env):
    def test_default_mode_never_writes(self):
        self.assertEqual(self.run_m("--date", DAY), 0)
        self.assertEqual(self.writes(), [])
        with open(self.local()) as f:
            d = json.load(f)
        self.assertEqual((d["day"], d["archived_skipped"], d["snapshot_at"]), (DAY, ["vws-hypervisor"], "2026-10-21T00:00:00Z"))
        self.assertNotIn(f"{D}/git/ref/heads/factory-metrics", [c["path"] for c in self.calls()], "dry-run never reads the branch")
        self.assertFalse(any(c["gh_token_set"] for c in self.calls()), "GH_TOKEN must be removed")
        self.assertTrue(os.path.exists(os.path.join(self.state, "metrics-etag-cache.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "etag-cache.json")), "the orchestrator cache is never touched")

    def test_second_run_uses_etags(self):
        self.run_m("--date", DAY)
        n = len(self.calls())
        self.run_m("--date", DAY)
        second = self.calls()[n:]
        self.assertTrue(all(c["inm"] for c in second if c["path"] != "rate_limit"))

    def test_env_file_shared_with_the_orchestrator(self):
        os.makedirs(self.env["ORCHESTRATE_CONFIG_DIR"])
        with open(os.path.join(self.env["ORCHESTRATE_CONFIG_DIR"], "env"), "w") as f:
            f.write("ORCHESTRATE_MODE=apply\nMETRICS_ENABLED=1\nMETRICS_MODE=dry-run\nMETRICS_TIME=00:20\n")
        self.assertEqual(self.run_m("--date", DAY), 0)
        self.assertEqual(self.writes(), [])

    def test_fixture_never_inits_or_runs_if_due(self):
        fx = os.path.join(HERE, "fixtures", f"raw-{DAY}.json")
        self.set_routes(routes(head=None))
        for flag in ("--init", "--if-due"):
            with self.subTest(flag):
                self.assertEqual(metrics.main([flag, "--fixture", fx], dict(self.env, METRICS_MODE="apply"), now=NOW), 2)
        self.assertEqual(self.calls(), [])

    def test_fixture_mode_reads_no_network(self):
        fx = os.path.join(HERE, "fixtures", f"raw-{DAY}.json")
        self.assertEqual(self.run_m("--date", DAY, "--fixture", fx, METRICS_MODE="apply"), 0)
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.exists(self.local()))


class Publish(Env):
    def publish(self, *extra):
        return self.run_m("--date", DAY, *extra, METRICS_MODE="apply")

    def test_one_fast_forward_commit_then_idempotent(self):
        self.assertEqual(self.publish(), 0)
        w = self.writes()
        self.assertEqual([(c["method"], c["path"]) for c in w],
                         [("POST", f"{D}/git/trees"), ("POST", f"{D}/git/commits"), ("PATCH", f"{D}/git/refs/heads/factory-metrics")])
        tree, commit, ref = (json.loads(c["stdin"]) for c in w)
        self.assertEqual(tree["base_tree"], T)
        self.assertEqual(sorted(e["path"] for e in tree["tree"]),
                         ["metrics/SUMMARY.md", f"metrics/daily/{DAY}.json", "metrics/summary.json"])
        self.assertEqual(commit["parents"], [H])
        self.assertTrue(commit["message"].startswith(f"metrics: daily {DAY}\n"))
        self.assertIn(TRAILER, commit["message"].splitlines())
        self.assertIs(ref["force"], False)
        self.assertIn("metrics_published", self.events())
        # The branch now has the file with the same content: a second run writes nothing.
        daily = next(e["content"] for e in tree["tree"] if e["path"].endswith(f"{DAY}.json"))
        r = routes(tree_extra=[{"path": f"metrics/daily/{DAY}.json", "type": "blob", "sha": "5" * 40}])
        r[f"{D}/git/blobs/{'5' * 40}"] = {"body": {"content": base64.b64encode(daily.encode()).decode()}}
        self.set_routes(r)
        n = len(self.writes())
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.writes()), n)
        self.assertIn("metrics_unchanged", self.events())

    def test_drift_is_logged_and_recompute_rewrites(self):
        old = json.dumps({"day": DAY, "content_sha256": "sha256:old"})
        r = routes(tree_extra=[{"path": f"metrics/daily/{DAY}.json", "type": "blob", "sha": "5" * 40}])
        r[f"{D}/git/blobs/{'5' * 40}"] = {"body": {"content": base64.b64encode(old.encode()).decode()}}
        self.set_routes(r)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("metrics_drift", self.events())
        self.assertEqual(self.run_m("--recompute", DAY, METRICS_MODE="apply"), 0)
        self.assertEqual(len(self.writes()), 3)

    def test_summary_includes_earlier_days(self):
        prev = {"day": "2026-10-19", "prs": {"totals": {"opened": 4, "merged": 2}}, "lead_time_hours": {"all": {"median": 1}},
                "verify": {"pass_rate": 1.0}, "deepseek": {"dispatched": 1, "done": 1, "failed": 0, "timeout": 0},
                "grok_swaps": {"labeled": 0}, "ci": {"failure_rate": 0.0}, "queue_depth": None, "content_sha256": "x"}
        r = routes(tree_extra=[{"path": "metrics/daily/2026-10-19.json", "type": "blob", "sha": "6" * 40}])
        r[f"{D}/git/blobs/{'6' * 40}"] = {"body": {"content": base64.b64encode(json.dumps(prev).encode()).decode()}}
        self.set_routes(r)
        self.assertEqual(self.publish(), 0)
        tree = json.loads(self.writes()[0]["stdin"])
        summary = json.loads(next(e["content"] for e in tree["tree"] if e["path"] == "metrics/summary.json"))
        self.assertEqual([d["day"] for d in summary["days"]], ["2026-10-19", DAY])

    def test_foreign_commits_are_refused(self):
        daily = lambda msg, parents: {"sha": F, "commit": {"message": msg}, "parents": [{"sha": H}] * parents}
        cases = {
            "no trailer": (daily(f"metrics: daily {DAY}\n", 1), None),
            "wrong subject": (daily(f"ci: something\n\n{TRAILER}\n", 1), None),
            "merge commit": (daily(f"metrics: daily {DAY}\n\n{TRAILER}\n", 2), None),
            "outside metrics/": (daily(f"metrics: daily {DAY}\n\n{TRAILER}\n", 1), [{"filename": "scripts/guard.sh"}]),
        }
        for name, (commit, files) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.state, ignore_errors=True)
                r = routes(head=F, history=[commit, root_commit()], files=files)
                r[f"{D}/commits/{H}"] = {"body": {"sha": H, "files": [{"filename": "metrics/README.md"}]}}
                self.set_routes(r)
                n = len(self.writes())
                self.assertEqual(self.publish(), metrics.REFUSE_EXIT)
                self.assertEqual(len(self.writes()), n)
                self.assertEqual(self.events()[-1], "metrics_foreign_commit")

    def test_non_ascii_output_is_refused(self):
        r = routes()
        r[f"{D}/actions/runs"] = {"body": {"workflow_runs": [{"id": 1, "name": "\uac80\uc0ac", "event": "push",
                                                               "status": "completed", "conclusion": "success"}]}}
        self.set_routes(r)
        self.assertEqual(self.publish(), metrics.REFUSE_EXIT)
        self.assertEqual(self.writes(), [])
        self.assertFalse(os.path.exists(self.local("apply")))
        self.assertIn("metrics_non_ascii", self.events())

    def test_missing_branch_fails_without_writing(self):
        self.set_routes(routes(head=None))
        self.assertEqual(self.publish(), metrics.FAIL_EXIT)
        self.assertEqual(self.writes(), [])
        self.assertIn("metrics_no_branch", self.events())

    def test_failed_write_fails_the_run(self):
        r = routes()
        r[f"PATCH {D}/git/refs/heads/factory-metrics"] = {"status": 422, "body": {"message": "Update is not a fast forward"}}
        self.set_routes(r)
        self.assertEqual(self.publish(), metrics.FAIL_EXIT)
        self.assertEqual(len(self.writes()), 3, "each write is tried once")
        self.assertIn("metrics_failed", self.events())


class Limits(Env):
    def test_rate_floor(self):
        self.set_routes(routes(rate=999))
        self.assertEqual(self.run_m("--date", DAY), 0)
        self.assertEqual([c["path"] for c in self.calls()], ["rate_limit"])
        self.assertEqual(self.events()[-1], "metrics_rate_low")

    def test_read_budget(self):
        self.assertEqual(self.run_m("--date", DAY, METRICS_MAX_GETS="3"), metrics.FAIL_EXIT)
        self.assertEqual(len(self.calls()), 3)
        self.assertIn("METRICS_MAX_GETS", self.log()[-1]["error"])

    def test_bad_config(self):
        for k, v in (("METRICS_MODE", "yes"), ("METRICS_TIME", "25:00"), ("METRICS_MAX_GETS", "0"),
                     ("METRICS_BACKFILL_DAYS", str(metrics.STATE_KEEP_DAYS + 1))):
            with self.subTest(k):
                self.assertEqual(self.run_m("--date", DAY, **{k: v}), 2)
        self.assertEqual(self.run_m("--date", "20261020"), 2)
        self.assertEqual(self.calls(), [])

    def test_stop_file(self):
        os.makedirs(self.env["ORCHESTRATE_CONFIG_DIR"])
        open(os.path.join(self.env["ORCHESTRATE_CONFIG_DIR"], "STOP"), "w").close()
        self.assertEqual(self.run_m("--date", DAY), 0)
        self.assertEqual(self.calls(), [])


class Init(Env):
    def test_init_creates_an_orphan_branch_once(self):
        self.set_routes(routes(head=None))
        self.assertEqual(self.run_m("--init", METRICS_MODE="apply"), 0)
        w = self.writes()
        self.assertEqual([(c["method"], c["path"]) for c in w],
                         [("POST", f"{D}/git/trees"), ("POST", f"{D}/git/commits"), ("POST", f"{D}/git/refs")])
        commit, ref = json.loads(w[1]["stdin"]), json.loads(w[2]["stdin"])
        self.assertEqual(commit["parents"], [])
        self.assertTrue(commit["message"].startswith(metrics.INIT_SUBJECT))
        self.assertEqual(ref["ref"], "refs/heads/factory-metrics")
        self.set_routes(routes())
        self.assertEqual(self.run_m("--init", METRICS_MODE="apply"), 0)
        self.assertEqual(len(self.writes()), 3, "an existing branch is left alone")

    def test_init_needs_apply_mode(self):
        self.set_routes(routes(head=None))
        self.assertEqual(self.run_m("--init"), 2)
        self.assertEqual(self.writes(), [])


class IfDue(Env):
    def state_done(self, mode, days):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "metrics-state.json"), "w") as f:
            json.dump({"done": {mode: days}}, f)

    def test_not_due_makes_no_call_and_no_log(self):
        self.state_done("dry-run", [f"2026-10-{d}" for d in range(14, 21)])
        self.assertEqual(self.run_m("--if-due"), 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.log(), [])

    def test_runs_the_oldest_due_day_only(self):
        self.state_done("dry-run", [f"2026-10-{d}" for d in range(16, 21)])
        self.assertEqual(self.run_m("--if-due"), 0)
        self.assertEqual([e["day"] for e in self.log() if e["event"] == "metrics_computed"], ["2026-10-14"])
        self.assertEqual(self.run_m("--if-due"), 0)
        self.assertEqual([e["day"] for e in self.log() if e["event"] == "metrics_computed"], ["2026-10-14", "2026-10-15"])
        with open(os.path.join(self.state, "metrics", "dry-run", "daily", "2026-10-14.json")) as f:
            self.assertIsNone(json.load(f)["queue_depth"], "a back-filled day has no live snapshot")

    def test_modes_are_tracked_apart(self):
        self.state_done("dry-run", [f"2026-10-{d}" for d in range(14, 21)])
        self.assertEqual(self.run_m("--if-due", METRICS_MODE="apply"), 0)
        self.assertTrue(self.writes(), "a day done in dry-run is still published in apply mode")


class BoxLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.record = os.path.join(self.tmp, "calls")
        self.orch_py = os.path.join(self.tmp, "fake_orchestrate.py")
        self.metrics_py = os.path.join(self.tmp, "fake_metrics.py")
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.config)
        self.env = dict(os.environ, ORCHESTRATE_STATE_DIR=os.path.join(self.tmp, "state"), ORCHESTRATE_CONFIG_DIR=self.config,
                        ORCHESTRATE_PY=self.orch_py, METRICS_PY=self.metrics_py, ORCHESTRATE_BACKOFF_BASE="0",
                        ORCHESTRATE_INTERVAL="0", ORCHESTRATE_LOOP_ITERATIONS="3", FAKE_RECORD=self.record,
                        ORCHESTRATE_AUTO_UPDATE="0")  # never fetch or move this dev checkout (vws-deploy#490)
        self.env.pop("METRICS_ENABLED", None)

    def fake(self, path, name, rc):
        with open(path, "w") as f:
            f.write("import os, sys\n"
                    f"open(os.environ['FAKE_RECORD'], 'a').write('{name} ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                    f"sys.exit(0 if '--notify-failure' in sys.argv else {rc})\n")

    def loop(self, enabled, orch_rc, metrics_rc):
        self.fake(self.orch_py, "orch", orch_rc)
        self.fake(self.metrics_py, "metrics", metrics_rc)
        with open(os.path.join(self.config, "env"), "w") as f:
            f.write(f"METRICS_ENABLED={enabled}\n")
        subprocess.run([os.path.join(BOX, "orchestrate-loop.sh")], env=self.env, check=True, timeout=30)
        with open(self.record) as f:
            return f.read().splitlines()

    def test_disabled_by_default(self):
        lines = self.loop(0, 0, 0)
        self.assertEqual(lines, ["orch --once"] * 3)

    def test_failing_metrics_never_counts_as_a_failed_tick(self):
        lines = self.loop(1, 0, 1)
        self.assertEqual(lines, ["orch --once", "metrics --if-due"] * 3)
        with open(os.path.join(self.env["ORCHESTRATE_STATE_DIR"], "log", "metrics.log")) as f:
            self.assertIn("(not a failed tick)", f.read())

    def test_failure_streak_ignores_metrics(self):
        lines = self.loop(1, 1, 1)
        notes = [ln.split()[3] for ln in lines if "--notify-failure" in ln]
        self.assertEqual(notes, ["1", "2", "3"])
        self.assertEqual(sum(ln == "metrics --if-due" for ln in lines), 3)


class Static(unittest.TestCase):
    def test_direct_http_only_for_the_alert_webhook(self):
        # vws-deploy#416: the alert webhook is the only HTTP call that does not go through gh.
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        for word in ("requests", "git push", "heads/main", "urlopen("):
            self.assertNotIn(word, text)
        self.assertTrue(text.isascii())
        allowed = re.search(r"^class _NoRedirect.*?^def read_tokens", text, re.M | re.S).group(0)
        rest = text.replace(allowed, "")
        self.assertEqual([line for line in rest.splitlines() if "urllib" in line and not line.startswith("import ")], [])

    def test_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
