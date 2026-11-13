#!/usr/bin/env python3
"""Deterministic console/PVE boundary tests; no requests leave loopback."""
import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from live_stub import ACCESS, CONSOLE, EC2_SECRET, PVE, SECRET, TOKEN, Stub, ident

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/vws.d/lib"))
import live


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="live-test-", dir=os.environ.get("TMPDIR", ROOT))
        self.work = Path(self.temp.name)
        (self.work / "console").write_text(TOKEN)
        (self.work / "pve-token").write_text("vws@pve!live=" + SECRET)
        s = self.s = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        s.daemon_threads = True
        s.calls, s.errors, s.resource_reads = [], [], 0
        s.replies, s.uuids = {}, {}
        s.ec2_calls, s.tokens, s.instances, s.config_extra = [], {}, {}, {}
        s.next_vmid = 201
        s.ec2_block = s.no_ip = s.no_live_tag = s.keep_snippet = False
        s.arrived, s.release, ready = threading.Event(), threading.Event(), threading.Event()
        s.block = s.gone = s.task_fail = s.fail_once = False
        s.cap = {"kind": "proxmox", "cluster": {"name": "pve-test"}, "features": {"liveTests": True}}
        s.health = {"nodes": [{"name": "node1"}]}
        s.pve_url = self.base = f"http://127.0.0.1:{s.server_port}"
        s.pool_status = 200
        s.vms = [dict(type="qemu", pool="vws-live", template=0, node="node1",
                      vmid=n, status="running" if n == 101 else "stopped", maxmem=1024 ** 3,
                      tags={101: "other;vws.live.one", 102: "vws.live.two", 103: "vws.holder",
                            104: "vws.live.foreign", 105: "vws.live.template", 106: "xvws.live.bad"}[n])
                 for n in range(101, 107)]
        s.vms[3]["pool"], s.vms[4]["template"] = "production", 1
        s.vms[5]["pool"] = "production"
        s.snapshots = {n: ["current", "old"] for n in range(101, 107)}
        s.snapshots[105] = ["current"]
        s.snippets = [f"local:snippets/vws-{ident(n)}-user.yaml" for n in (101, 102, 103, 104, 999)]
        s.snippets += ["local:snippets/unrelated.yaml", "local:snippets/vws-invalid-user.yaml"]
        def serve():
            ready.set()
            s.serve_forever()
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.assertTrue(ready.wait(5))
        self.env = dict(os.environ, VWS_API_URL=self.base, VWS_LIVE_PVE_URL=self.base,
                        VWS_API_TOKEN_FILE=str(self.work / "console"),
                        CREDENTIALS_DIRECTORY=str(self.work), VWS_COMMAND_DIR=str(ROOT / "scripts/vws.d"))
        self.env.pop("VWS_API_TOKEN", None)
        (self.work / "aws").write_text("#!/usr/bin/env bash\nexec python3 " +
                                       str(ROOT / "tests/vws/live_aws.py") + ' "$@"\n')
        (self.work / "aws").chmod(0o700)
        self.env.update(PATH=str(self.work) + ":" + self.env["PATH"],
                        VWS_VERIFY_EC2_ENDPOINT=self.base + "/ec2", VWS_VERIFY_ACCESS_KEY_ID=ACCESS,
                        VWS_VERIFY_SECRET_ACCESS_KEY=EC2_SECRET, VWS_VERIFY_IMAGE_ID="ami-" + "a" * 17,
                        VWS_VERIFY_KEY_NAME="existing-live-key")
        self.results = self.work / "results"
        self.command = ["bash", str(ROOT / "scripts/vws"), "nightly-live",
                        "--region", "pve-test-1", "--results", str(self.results)]
        self.outputs = []

    def tearDown(self):
        self.s.release.set()
        self.s.shutdown()
        self.thread.join()
        self.s.server_close()
        self.assertEqual(self.s.errors, [])
        self.assertFalse(any("clone" in p or m == "POST" and
                             urlsplit(p).path.endswith("/qemu") for m, p in self.s.calls))
        text = "".join(self.outputs) + "".join(p.read_text() for p in self.results.glob("*.json"))
        for secret in (TOKEN, SECRET, "vws@pve!live=" + SECRET, ACCESS, EC2_SECRET):
            self.assertNotIn(secret, text)
        self.assertNotIn("Traceback", text)
        self.temp.cleanup()

    def run_cli(self, code=0, args=(), env=None):
        result = subprocess.run(self.command + list(args), env=env or self.env, capture_output=True,
                                text=True, timeout=60, check=False)
        self.outputs.append(result.stdout + result.stderr)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def assert_refused(self):
        self.run_cli(3)
        self.assertFalse(any(m != "GET" or "/cluster/resources" in p for m, p in self.s.calls))
        self.assertFalse((self.results / "results.json").exists())
        self.s.calls.clear()

    def test_gate_preconditions(self):
        good = copy.deepcopy(self.s.cap)
        for cap in ({**good, "kind": "aws"}, {**good, "cluster": {"name": "production"}},
                    {**good, "features": {"liveTests": False}}, {**good, "features": {}},
                    {k: v for k, v in good.items() if k != "features"},
                    {**good, "features": {"liveTests": 1}}):
            with self.subTest(cap=cap):
                self.s.cap = cap
                self.assert_refused()
        self.s.cap = good
        for nodes in ([], [{"name": "a"}, {"name": "b"}], {}, None):
            self.s.health = {"nodes": nodes}
            self.assert_refused()
        self.s.health = {"nodes": [{"name": "node1"}]}
        for status in (201, 404):
            self.s.pool_status = status
            self.assert_refused()

    def test_identity(self):
        self.command = ["python3", str(ROOT / "scripts/vws.d/lib/live.py"), "identity",
                        "--region", "pve-test-1"]
        for args in ((), ("--results", str(self.results))):
            result = self.run_cli(args=args)
            self.assertEqual(result.stdout, "TestServer826\n")
            self.assertEqual(result.stderr, "")
        self.assertFalse(self.results.exists())
        self.assertTrue(all(m == "GET" for m, _ in self.s.calls))
        self.assertIn(("GET", PVE + "/nodes/node1/subscription"), self.s.calls)

    def test_identity_refused(self):
        self.command = ["python3", str(ROOT / "scripts/vws.d/lib/live.py"), "identity",
                        "--region", "pve-test-1", "--results", str(self.results)]
        for payload in ({}, {"serverid": ""}, {"serverid": "bad-id"}, {"serverid": "A" * 65},
                        {"serverid": 123}, {"serverid": TOKEN}, []):
            with self.subTest(payload=payload):
                self.s.replies = {PVE + "/nodes/node1/subscription": (200, {"data": payload})}
                result = self.run_cli(3)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.results / ".gate.json").exists())
        self.s.replies = {}
        self.s.cap["cluster"]["name"] = "production"
        self.s.calls.clear()
        result = self.run_cli(3)
        self.assertIn("not allowlisted", result.stderr)
        self.assertFalse(any(p.endswith("/subscription") for _, p in self.s.calls))

    def test_malformed_gate(self):
        for cap in ([], None, b"not-json " + SECRET.encode(), {"kind": "proxmox"},
                    {"kind": "proxmox", "cluster": {"name": "pve-test"}, "features": []}):
            self.s.cap = cap
            self.assert_refused()

    def test_bad_urls(self):
        for url in ("http://remote.example.org", "https://user:pass@example.org", "file:///secret",
                    "https://example.org/path", "https://[bad", "https://example.org:bad"):
            self.s.pve_url = url
            self.assert_refused()
            self.run_cli(3, env=dict(self.env, VWS_API_URL=url))

    def test_bad_input_and_missing_credentials(self):
        self.run_cli(3, ["--region", "../bad"])
        self.run_cli(3, ["--snippets-storage", "../bad"])
        for env in (dict(self.env, CREDENTIALS_DIRECTORY=""), dict(self.env, VWS_API_TOKEN_FILE="-"),
                    dict(self.env, VWS_API_TOKEN_FILE=str(self.work / "absent"))):
            self.run_cli(3, env=env)
        (self.work / "pve-token").write_text("root@pam!admin=" + SECRET)
        self.assert_refused()

    def test_pinned_origin(self):
        self.s.pve_url = f"http://localhost:{self.s.server_port}"
        self.run_cli(3)
        self.assertFalse(any(p.startswith(PVE) for _, p in self.s.calls))
        self.s.pve_url = self.base
        for pin in (None, "", "http://remote.example.org", "https://[bad", "https://example.org/path"):
            self.s.calls.clear()
            env = dict(self.env)
            if pin is None:
                env.pop("VWS_LIVE_PVE_URL")
            else:
                env["VWS_LIVE_PVE_URL"] = pin
            self.run_cli(3, env=env)
            self.assertFalse(any(p.startswith(PVE) for _, p in self.s.calls))

    def test_gate_reply_shapes(self):
        for path, payload in ((CONSOLE + "/health", []), (CONSOLE, []),
                              (CONSOLE, {"pveApiUrl": []}), (PVE + "/pools/vws-live", {"data": []}),
                              (PVE + "/pools/vws-live", b"bad " + TOKEN.encode())):
            self.s.replies = {path: (200, payload)}
            self.assert_refused()
        self.s.replies = {CONSOLE: (302, {})}
        self.assert_refused()

    def test_happy_and_resume(self):
        self.run_cli()
        writes = [(m, p.removeprefix(PVE)) for m, p in self.s.calls if m != "GET"]
        expected = []
        for n in (101, 102):
            expected.append(("DELETE", f"/nodes/node1/storage/local/content/local:snippets/vws-{ident(n)}-user.yaml"))
            if n == 101:
                expected.append(("POST", "/nodes/node1/qemu/101/status/stop"))
            expected.append(("DELETE", f"/nodes/node1/qemu/{n}?purge=1&destroy-unreferenced-disks=1"))
        expected += [("DELETE", "/nodes/node1/qemu/103/snapshot/old")]
        self.assertEqual(writes[:len(expected)], expected)
        self.assertIn(f"local:snippets/vws-{ident(999)}-user.yaml", self.s.snippets)
        self.assertEqual([v["vmid"] for v in self.s.vms], [103, 104, 105, 106])
        self.assertEqual(json.loads((self.results / "results.json").read_text())["result"], "pass")
        self.s.calls.clear()
        self.run_cli()
        self.assertEqual(len([op for op, _ in self.s.ec2_calls if op == "run-instances"]), 8)
        self.assertTrue((self.work / "nightly-live.lock").exists())

    def test_already_gone(self):
        self.s.gone = True
        self.run_cli()

    def test_deleted_uuid_still_held(self):
        self.s.uuids[103] = ident(101)
        self.run_cli()
        self.assertNotIn(f"local:snippets/vws-{ident(101)}-user.yaml", self.s.snippets)

    def test_missing_snippet_and_retry(self):
        self.s.snippets.remove(f"local:snippets/vws-{ident(102)}-user.yaml")
        missing = PVE + f"/nodes/node1/storage/local/content/local:snippets/vws-{ident(102)}-user.yaml"
        self.s.replies[missing] = (500, {"data": None})
        self.s.fail_once = True
        self.run_cli(1)
        snippet = ("DELETE", PVE + f"/nodes/node1/storage/local/content/local:snippets/vws-{ident(101)}-user.yaml")
        deletes = [i for i, call in enumerate(self.s.calls) if call == snippet]
        vm = ("DELETE", PVE + "/nodes/node1/qemu/101?purge=1&destroy-unreferenced-disks=1")
        self.assertEqual(len(deletes), 2)
        self.assertLess(deletes[-1], self.s.calls.index(vm))
        self.s.calls.clear()
        self.run_cli()

    def test_failed_task(self):
        self.s.task_fail = True
        self.run_cli(1)
        self.assertEqual(json.loads((self.results / "results.json").read_text())["result"], "fail")
        self.assertTrue((self.results / "l0-end.json").exists())

    def test_fail_at(self):
        self.run_cli(1, ["--fail-at", "slots"])
        self.assertTrue((self.results / "l0-end.json").exists())

    def test_slot_limits(self):
        for memory, count in ((1024 ** 3 + 1, 1), (1024 ** 3, 3)):
            self.s.vms = [dict(self.s.vms[-1], pool="vws-live", vmid=200 + i, maxmem=memory) for i in range(count)]
            self.s.snapshots.update({v["vmid"]: ["current"] for v in self.s.vms})
            self.run_cli(1)
            self.assertIn("FAIL slots", self.outputs[-1])

    def test_slot_need(self):
        self.results.mkdir()
        (self.results / ".gate.json").write_text(json.dumps([f"http://localhost:{self.s.server_port}", "node1"]))
        small = [self.s.vms[2], self.s.vms[-1]]
        small[1]["pool"] = "vws-live"
        small[0]["maxmem"] = 2 * 1024 ** 3
        for vms, need, code in ((self.s.vms, 0, 1), (self.s.vms, 3, 1), (self.s.vms, -1, 2),
                                (small, 1, 0), (small, 2, 1)):
            self.s.vms = vms
            result = subprocess.run(["python3", str(ROOT / "scripts/vws.d/lib/live.py"), "slots",
                                     "--region", "pve-test-1", "--results", str(self.results),
                                     "--need", str(need)], env=self.env, capture_output=True, text=True, timeout=30)
            self.outputs.append(result.stdout + result.stderr)
            self.assertEqual(result.returncode, code)

    def test_cancel_and_concurrent(self):
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            self.s.block, self.s.resource_reads = True, 0
            self.s.arrived.clear()
            self.s.release.clear()
            p = subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(self.s.arrived.wait(15), "slots request was not reached")
                self.run_cli(3)
                os.kill(p.pid, sig)
            finally:
                try:
                    out, err = p.communicate(timeout=60)
                finally:
                    self.s.release.set()
                self.outputs.append(out + err)
            self.assertEqual(p.returncode, code, out + err)
            self.assertTrue((self.results / "l0-end.json").exists())
            self.assertEqual(json.loads((self.results / "results.json").read_text())["result"], "fail")

    def test_launch_contract_and_stale_results(self):
        self.results.mkdir()
        for name in ("l2", "l7", "final"):
            (self.results / (name + ".json")).write_text('{"result":"fail","steps":[]}')
        self.run_cli(args=["--run-id", "contract-772"])
        creates = [args for op, args in self.s.ec2_calls if op == "run-instances"]
        self.assertEqual(len(creates), 4)
        self.assertEqual(creates[0], creates[1])
        for args in creates:
            self.assertEqual((args["InstanceType"], args["MinCount"], args["MaxCount"]), ("vws.small", 1, 1))
            self.assertEqual(args["KeyName"], "existing-live-key")
            self.assertEqual(args["UserData"], "I2Nsb3VkLWNvbmZpZwo=")
            self.assertIn({"Key": "live-long", "Value": "x" * 128}, args["TagSpecifications"][0]["Tags"])
        data = json.loads((self.results / "results.json").read_text())
        self.assertEqual([s["name"] for s in data["steps"]],
                         ["l0-start", "slots", "l2", "l7", "l0-end", "final"])
        self.assertEqual(data["result"], "pass")
        self.assertFalse(any(v["vmid"] >= 201 for v in self.s.vms))

    def test_step_failure_cleanup_and_restart(self):
        for step in ("l2", "l7"):
            self.run_cli(1, ["--fail-at", step])
            data = json.loads((self.results / "results.json").read_text())
            self.assertEqual(data["result"], "fail")
            self.assertEqual(next(s["result"] for s in data["steps"] if s["name"] == step), "fail")
            self.assertFalse(any(v["vmid"] >= 201 for v in self.s.vms))
            self.run_cli()

    def test_launch_missing_ip_and_pve_tag(self):
        for flag in ("no_ip", "no_live_tag"):
            setattr(self.s, flag, True)
            self.run_cli(1)
            self.assertFalse(any(v["vmid"] >= 201 for v in self.s.vms))
            setattr(self.s, flag, False)

    def test_run_id_type_boundaries(self):
        self.run_cli(2, ["--run-id", ""])
        for runid in ("../bad", "-bad", "Upper", "a;b", "a" * 41):
            self.run_cli(3, ["--run-id", runid])
        self.run_cli(3, env=dict(self.env, VWS_VERIFY_INSTANCE_TYPE="vws.large"))
        self.assertEqual(self.s.calls, [])
        self.assertEqual(self.s.ec2_calls, [])
        for runid in ("0", "a" + "-" * 39):
            self.run_cli(args=["--run-id", runid])

    def test_create_guard_at_capacity(self):
        self.s.vms = [dict(self.s.vms[-1], pool="vws-live", vmid=200 + i) for i in range(2)]
        self.s.snapshots.update({v["vmid"]: ["current"] for v in self.s.vms})
        self.run_cli(1)
        self.assertEqual(self.s.ec2_calls, [])

    def test_step_exception_and_timeout_cleanup(self):
        self.run_cli()
        (self.results / ".gate.json").write_text(json.dumps([self.base, "node1"]))
        argv = ["live.py", "l2", "--region", "pve-test-1", "--results", str(self.results)]
        for error in (RuntimeError(SECRET), live.Refused("step deadline exceeded")):
            with patch.dict(os.environ, self.env), patch.object(sys, "argv", argv), \
                    patch.object(live.Client, "until", side_effect=error), patch.object(signal, "signal"):
                self.assertEqual(live.main(), 1)
            self.assertFalse(any(v["vmid"] >= 201 for v in self.s.vms))
            self.assertEqual(json.loads((self.results / "l2.json").read_text())["result"], "fail")

    def test_ec2_timeout_kills_subprocess(self):
        self.run_cli()
        (self.results / ".gate.json").write_text(json.dumps([self.base, "node1"]))
        args = live.Parser().parse_args([])
        args.operation, args.results, args.pve_ca_file = "l2", str(self.results), None
        args.region = "pve-test-1"
        with patch.dict(os.environ, self.env):
            client = live.Client(args)
            with patch.object(live.subprocess, "Popen") as spawn:
                process = spawn.return_value
                process.communicate.side_effect = [subprocess.TimeoutExpired("aws", 90), (b"", b"")]
                process.poll.return_value = None
                with self.assertRaisesRegex(live.Refused, "timed out"):
                    client.ec2("describe-instances")
                process.kill.assert_called_once()

    def test_final_detects_leftovers(self):
        for key in ("unused0", "scsi0"):
            self.s.config_extra = {103: {key: "local:vm-103-disk-1"}}
            self.run_cli(1)
            data = json.loads((self.results / "results.json").read_text())
            self.assertEqual(data["steps"][-1]["result"], "fail")
            self.assertEqual(data["result"], "fail")
        self.s.config_extra = {}
        self.s.snapshots[105] = ["current", "leftover"]
        self.run_cli(1)
        self.assertEqual(json.loads((self.results / "final.json").read_text())["result"], "fail")
        self.s.snapshots[105] = ["current"]
        self.s.keep_snippet = True
        self.run_cli(1)
        self.assertEqual(json.loads((self.results / "final.json").read_text())["result"], "fail")

    def test_signal_during_live_step_and_resume(self):
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            self.s.ec2_block = True
            self.s.arrived.clear()
            self.s.release.clear()
            p = subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(self.s.arrived.wait(15), "live describe event not reached")
                self.assertTrue(any(v["vmid"] >= 201 for v in self.s.vms))
                os.kill(p.pid, sig)
                out, err = p.communicate(timeout=60)
                self.outputs.append(out + err)
                self.assertEqual(p.returncode, code, out + err)
                self.assertFalse(any(v["vmid"] >= 201 for v in self.s.vms))
                self.assertEqual(json.loads((self.results / "l2.json").read_text())["result"], "fail")
            finally:
                self.s.ec2_block = False
                self.s.release.set()
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            self.run_cli()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LiveTests))
    failed = len(result.failures) + len(result.errors)
    print(f"{result.testsRun} cases, {failed} failed")
    raise SystemExit(int(failed != 0))
