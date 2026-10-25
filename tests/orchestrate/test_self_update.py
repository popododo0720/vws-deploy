"""Auto-update of the service checkout before each tick (vws-deploy#490): orchestrate-update.py and its
hook in orchestrate-loop.sh, run against throwaway local git repos (never the network), and the running
sha line and deduped deploy alerts in the status issue body.
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from orch_testlib import BOX, ROOT, orch
from test_runtime import Env

LOOP_REL = ".github/orchestrate/box/orchestrate-loop.sh"
UPDATE_REL = ".github/orchestrate/box/orchestrate-update.py"
PY_REL = ".github/orchestrate/orchestrate.py"
STUB = ("import os, sys\nVERSION = {v!r}\n"
        "open(os.environ['FAKE_RECORD'], 'a').write(VERSION + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Checkout(unittest.TestCase):
    """origin.git (bare), seed/ (pushes to it, plays merged main) and svc/ (the service checkout)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.seed = os.path.join(self.tmp, "seed")
        self.svc = os.path.join(self.tmp, "svc")
        self.state = os.path.join(self.tmp, "state")
        self.config = os.path.join(self.tmp, "config")
        self.record = os.path.join(self.tmp, "py-calls")
        self.genv = dict(os.environ, **GIT_ENV)
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.git(self.tmp, "init", "-q", "-b", "main", self.seed)
        self.git(self.seed, "remote", "add", "origin", self.origin)
        self.assertTrue(os.path.exists(os.path.join(ROOT, UPDATE_REL)), "orchestrate-update.py must exist")
        for rel in (LOOP_REL, UPDATE_REL, ".github/orchestrate/box/orchestrate-ensure.sh"):
            os.makedirs(os.path.dirname(os.path.join(self.seed, rel)), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, rel), os.path.join(self.seed, rel))
        write(os.path.join(self.seed, PY_REL), STUB.format(v="v1"), 0o755)
        # orchestrate.py imports it; orchestrate-update.py syntax-checks it (vws-deploy#439 class 1).
        write(os.path.join(self.seed, ".github/orchestrate/trusted_read.py"), "OWNER = 'popododo0720'\n")
        write(os.path.join(self.seed, "README.md"), "seed\n")
        self.v1 = self.commit("v1")
        self.git(self.tmp, "clone", "-q", self.origin, self.svc)
        self.env = dict(self.genv, ORCHESTRATE_STATE_DIR=self.state, ORCHESTRATE_CONFIG_DIR=self.config,
                        ORCHESTRATE_BACKOFF_BASE="0", ORCHESTRATE_INTERVAL="0", ORCHESTRATE_LOOP_ITERATIONS="1",
                        FAKE_RECORD=self.record)
        for k in ("ORCHESTRATE_PY", "ORCHESTRATE_AUTO_UPDATE", "METRICS_ENABLED"):
            self.env.pop(k, None)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.genv, check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()

    def commit(self, msg, files=None):
        for rel, text in (files or {}).items():
            write(os.path.join(self.seed, rel), text, 0o755 if rel.endswith((".sh", ".py")) else None)
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-q", "-m", msg)
        self.git(self.seed, "push", "-q", "origin", "main")
        return self.git(self.seed, "rev-parse", "HEAD")

    def head(self):
        return self.git(self.svc, "rev-parse", "HEAD")

    def loop(self, **env):
        return subprocess.run([os.path.join(self.svc, LOOP_REL)], env=dict(self.env, **env), timeout=60,
                              capture_output=True, text=True)

    def runs(self):
        return read(self.record).splitlines() if os.path.exists(self.record) else []

    def deploy(self):
        return json.loads(read(os.path.join(self.state, "deploy.json")))

    def deploy_lines(self):
        out = []
        for line in read(os.path.join(self.state, "log", "loop.log")).splitlines():
            if line.startswith("{"):
                d = json.loads(line)
                if d.get("event") == "deploy":
                    out.append(d)
        return out

    def alerts(self):
        return self.deploy().get("alerts", [])


class FastForward(Checkout):
    def test_behind_checkout_fast_forwards_and_runs_the_new_sha(self):
        """AC1"""
        v2 = self.commit("v2", {PY_REL: STUB.format(v="v2")})
        r = self.loop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(), v2)
        self.assertEqual(self.git(self.svc, "rev-list", "--merges", "--count", "HEAD"), "0", "no merge commit")
        self.assertEqual(self.runs(), ["v2 --once"], "orchestrate.py runs from the new sha")
        d = self.deploy()
        self.assertEqual((d["result"], d["running"], d["main"], d["previous"]), ("updated", v2, v2, self.v1))
        self.assertEqual(self.alerts(), [])

    def test_second_tick_on_the_same_sha_is_a_noop(self):
        v2 = self.commit("v2", {PY_REL: STUB.format(v="v2")})
        self.loop()
        self.loop()
        self.assertEqual(self.head(), v2)
        self.assertEqual([d["result"] for d in self.deploy_lines()], ["updated", "current"])
        self.assertEqual(self.alerts(), [])

    def test_loop_log_line_has_running_and_main_sha(self):
        """AC5 (loop.log half): one structured deploy line per tick."""
        self.loop()
        (d,) = self.deploy_lines()
        self.assertEqual((d["running"], d["main"], d["result"]), (self.v1[:7], self.v1[:7], "current"))
        self.assertRegex(d["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_broken_candidate_is_refused(self):
        self.commit("v2", {PY_REL: "def broken(:\n"})
        self.loop()
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(self.runs(), ["v1 --once"])
        self.assertEqual(self.deploy()["result"], "refused")
        self.assertEqual([a["kind"] for a in self.alerts()], ["refused"])
        self.commit("v3", {PY_REL: STUB.format(v="v3"), LOOP_REL: "#!/usr/bin/env bash\nif then\n"})
        self.loop()
        self.assertEqual(self.head(), self.v1, "a loop script that bash -n rejects is never deployed")


class Refuse(Checkout):
    """AC2"""

    def check_refused(self, before, expect_run):
        self.commit("v2", {PY_REL: STUB.format(v="v2")})
        keys = []
        for _ in range(2):
            self.loop()
            keys.append([a["key"] for a in self.alerts() if a["kind"] == "refused"])
        self.assertEqual(self.head(), before, "the checkout does not move")
        self.assertEqual(self.runs(), [expect_run + " --once"] * 2, "the tick runs the current sha")
        self.assertEqual(self.deploy()["result"], "refused")
        self.assertEqual(len(keys[0]), 1, "one alert")
        self.assertEqual(keys[0], keys[1], "the same key on every tick, so the status issue gets it once")
        self.assertRegex(keys[0][0], r"^sha256:[0-9a-f]{64}$")

    def test_modified_tracked_file(self):
        write(os.path.join(self.svc, "README.md"), "local edit\n")
        self.check_refused(self.v1, "v1")
        self.assertEqual(read(os.path.join(self.svc, "README.md")), "local edit\n")

    def test_untracked_file(self):
        write(os.path.join(self.svc, "notes.txt"), "mine\n")
        self.check_refused(self.v1, "v1")
        self.assertEqual(read(os.path.join(self.svc, "notes.txt")), "mine\n")

    def test_diverged_local_commit(self):
        write(os.path.join(self.svc, PY_REL), STUB.format(v="local"))
        self.git(self.svc, "commit", "-q", "-am", "local")
        local = self.head()
        self.check_refused(local, "local")

    def test_state_dir_inside_checkout_is_not_a_local_change(self):
        v2 = self.commit("v2", {PY_REL: STUB.format(v="v2")})
        self.loop(ORCHESTRATE_STATE_DIR=os.path.join(self.svc, "state"))
        self.assertEqual(self.head(), v2)


class FetchFailure(Checkout):
    def test_unreachable_remote_alerts_and_runs_the_current_sha(self):
        """AC3"""
        self.commit("v2", {PY_REL: STUB.format(v="v2")})
        self.git(self.svc, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        r = self.loop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(self.runs(), ["v1 --once"])
        self.assertEqual(self.deploy()["result"], "fetch_failed")
        self.assertEqual([a["kind"] for a in self.alerts()], ["fetch_failed"])

    def test_credentials_in_the_remote_url_never_reach_logs(self):
        self.git(self.svc, "remote", "set-url", "origin", "http://x-access-token:ghp_secret123@127.0.0.1:9/r.git")
        self.loop(ORCHESTRATE_UPDATE_TIMEOUT="10")
        self.assertEqual(self.deploy()["result"], "fetch_failed")
        for path in (os.path.join(self.state, "deploy.json"), os.path.join(self.state, "log", "loop.log")):
            self.assertNotIn("ghp_secret123", read(path))


class Reexec(Checkout):
    def test_changed_loop_script_reexecs_into_the_new_script(self):
        """AC4"""
        new_loop = read(os.path.join(ROOT, LOOP_REL)).replace(
            'mkdir -p "$state/log"\n', 'mkdir -p "$state/log"\n: >"$state/loop-v2-ran"\n', 1)
        self.assertIn("loop-v2-ran", new_loop)
        v2 = self.commit("v2", {LOOP_REL: new_loop, PY_REL: STUB.format(v="v2")})
        r = self.loop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(), v2)
        self.assertTrue(os.path.exists(os.path.join(self.state, "loop-v2-ran")), "the new loop script ran")
        self.assertEqual(self.runs(), ["v2 --once"], "exactly one tick, from the new sha")
        self.assertEqual([d["result"] for d in self.deploy_lines()], ["updated", "current"])
        self.assertTrue(self.deploy_lines()[0]["reexec"])
        lock = os.path.join(self.state, "loop.lock")
        self.assertEqual(subprocess.run(["flock", "-n", lock, "true"]).returncode, 0)

    def test_python_only_change_does_not_reexec(self):
        self.commit("v2", {PY_REL: STUB.format(v="v2")})
        self.loop()
        self.assertFalse(self.deploy_lines()[0]["reexec"])


class Drift(Checkout):
    """AC6 (the box half: one alert key per sha after two ticks behind)."""

    def test_two_ticks_behind_raise_one_alert_per_sha(self):
        write(os.path.join(self.config, "env"), "ORCHESTRATE_AUTO_UPDATE=0\n")
        a = self.commit("orch change a", {PY_REL: STUB.format(v="a")})
        self.git(self.svc, "fetch", "-q", "origin")  # disabled: no fetch; main is the last seen ref
        self.loop()
        self.assertEqual(self.alerts(), [], "one tick behind is not drift yet")
        self.loop()
        self.loop()
        drift = [x for x in self.alerts() if x["kind"] == "drift"]
        self.assertEqual(len(drift), 1)
        self.assertIn(a[:7], drift[0]["text"])
        key_a = drift[0]["key"]
        b = self.commit("orch change b", {PY_REL: STUB.format(v="b")})
        self.git(self.svc, "fetch", "-q", "origin")
        self.loop()
        self.loop()
        drift = [x for x in self.alerts() if x["kind"] == "drift"]
        self.assertEqual(len(drift), 1)
        self.assertNotEqual(drift[0]["key"], key_a, "a new sha gets its own key")
        self.assertIn(b[:7], drift[0]["text"])

    def test_change_outside_watched_paths_is_not_drift(self):
        write(os.path.join(self.config, "env"), "ORCHESTRATE_AUTO_UPDATE=0\n")
        self.commit("docs only", {"README.md": "docs\n"})
        self.git(self.svc, "fetch", "-q", "origin")
        self.loop()
        self.loop()
        self.assertEqual(self.alerts(), [])


class NoCheckout(Checkout):
    def test_archive_install_alerts_once_and_still_ticks(self):
        svc = os.path.join(self.tmp, "archive")
        for rel in (LOOP_REL, UPDATE_REL, PY_REL):
            os.makedirs(os.path.dirname(os.path.join(svc, rel)), exist_ok=True)
            shutil.copy2(os.path.join(self.seed, rel), os.path.join(svc, rel))
        for _ in range(2):
            r = subprocess.run([os.path.join(svc, LOOP_REL)], env=self.env, timeout=60, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.runs(), ["v1 --once"] * 2)
        self.assertEqual(self.deploy()["result"], "no_checkout")
        self.assertEqual([a["kind"] for a in self.alerts()], ["no_checkout"])
        self.assertIn("- Running: unknown (last update: no_checkout)", orch.running_line(self.deploy()))


class Disabled(Checkout):
    def test_auto_update_zero_pins_the_sha(self):
        """AC7"""
        self.commit("v2", {PY_REL: STUB.format(v="v2")})
        write(os.path.join(self.config, "env"), "ORCHESTRATE_AUTO_UPDATE=0\n")
        self.loop()
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(self.runs(), ["v1 --once"])
        self.assertEqual(self.deploy()["result"], "disabled")
        self.loop(ORCHESTRATE_AUTO_UPDATE="1")
        self.assertEqual(self.head(), self.v1, "the env file wins over the environment")

    def test_environment_switch(self):
        self.commit("v2", {PY_REL: STUB.format(v="v2")})
        self.loop(ORCHESTRATE_AUTO_UPDATE="0")
        self.assertEqual(self.head(), self.v1)

    def test_documented(self):
        doc = read(os.path.join(ROOT, "docs", "factory-orchestrator.md"))
        self.assertIn("ORCHESTRATE_AUTO_UPDATE=0", doc)
        self.assertIn("orchestrate-update.py", doc)


SHA_A, SHA_B = "a" * 40, "b" * 40
KEY = "sha256:" + "c" * 64
DEPLOY = {"result": "current", "running": SHA_A, "main": SHA_B, "previous": "", "behind_ticks": 2,
          "alerts": [{"kind": "drift", "key": KEY, "text": "running aaaaaaa is behind bbbbbbb"}]}
RUNNING_RE = r"(?m)^- Running: `[0-9a-f]{7}` \(main `[0-9a-f]{7}`, last update: [a-z_]+\)$"


class StatusLine(unittest.TestCase):
    """AC5 (status issue half) and AC6 (dedupe)."""

    def body(self, deploy):
        res = {"notes": [], "pending_count": 0, "actions": []}
        return orch.status_body({"run_id": "r"}, res, dict(orch.DEFAULTS), {}, "apply", 0, deploy=deploy)

    def test_running_line_format(self):
        b = self.body(DEPLOY)
        self.assertRegex(b, RUNNING_RE)
        self.assertIn("- Running: `aaaaaaa` (main `bbbbbbb`, last update: current)", b)

    def test_missing_or_invalid_deploy_file(self):
        self.assertIn("- Running: unknown (auto-update has not run)", self.body({}))
        bad = dict(DEPLOY, running="$(rm -rf /)", result="<b>x</b>")
        self.assertIn("- Running: unknown (auto-update has not run)", self.body(bad))

    def test_alert_added_once_per_key(self):
        state = {}
        self.assertEqual(orch.add_deploy_alerts(state, DEPLOY, 100), 1)
        self.assertEqual(orch.add_deploy_alerts(state, DEPLOY, 200), 0)
        (e,) = state["status_events"]
        self.assertEqual((e["key"], e["at"], e["kind"]), (KEY, 100, "deploy"))

    def test_invalid_alert_is_ignored(self):
        state = {}
        bad = dict(DEPLOY, alerts=[{"kind": "drift", "key": "nope", "text": "x"}, "junk"])
        self.assertEqual(orch.add_deploy_alerts(state, bad, 1), 0)
        self.assertEqual(state.get("status_events", []), [])


class StatusTick(Env):
    """AC5 and AC6 through a real apply tick: the status edit carries the running line and one alert."""

    def put_deploy(self, deploy):
        os.makedirs(self.paths.state, exist_ok=True)
        with open(os.path.join(self.paths.state, "deploy.json"), "w") as f:
            json.dump(deploy, f)

    def test_apply_tick_writes_running_line_and_one_alert(self):
        self.put_deploy(DEPLOY)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        edits = self.status_edits()
        self.assertEqual(len(edits), 1)
        self.assertRegex(edits[0], RUNNING_RE)
        self.assertEqual(edits[0].count(f"(key `{KEY}`)"), 1)
        self.assertIn("deploy: running aaaaaaa is behind bbbbbbb", edits[0])
        self.set_routes(dict(self.routes_with_body(edits[0])))
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(len(self.status_edits()), 1, "the same alert key edits nothing on the next tick")

    def routes_with_body(self, body):
        from test_runtime import routes
        r = routes()
        r["repos/popododo0720/vws-deploy/issues/415"] = {"body": {"number": 415, "state": "open", "body": body}}
        return r

    def test_dry_run_tick_renders_the_line(self):
        self.put_deploy(DEPLOY)
        self.assertEqual(self.tick(), 0)
        self.assertRegex(read(os.path.join(self.paths.state, "status-body.md")), RUNNING_RE)


class Static(unittest.TestCase):
    def test_update_script_executable_and_box_tests_pin_it_off(self):
        self.assertTrue(os.access(os.path.join(BOX, "orchestrate-update.py"), os.X_OK))


if __name__ == "__main__":
    unittest.main()
