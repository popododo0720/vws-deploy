#!/usr/bin/env python3
"""Deterministic console/PVE boundary tests; no requests leave loopback."""
import copy
import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
TOKEN, SECRET = "console-canary-769", "pve-canary-769"
CONSOLE = "/api/v1/admin/regions/pve-test-1"
PVE = "/api2/json"


def ident(n):
    return str(uuid.UUID(int=n))


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_request(self):
        s = self.server
        path = unquote(self.path)
        s.calls.append((self.command, path))
        auth = "Bearer " + TOKEN if path.startswith("/api/v1/") else "PVEAPIToken" + "=vws@pve!live=" + SECRET
        if self.headers.get("Authorization") != auth:
            s.errors.append("authorization mismatch")
        if path.startswith(PVE) and self.headers.get("Host") != f"127.0.0.1:{s.server_port}":
            s.errors.append("PVE origin did not match the runner pin")
        status, data = 200, {}
        if path.endswith("/capabilities"):
            data = s.cap
        elif path == CONSOLE + "/health":
            data = s.health
        elif path == CONSOLE:
            data = {"pveApiUrl": s.pve_url}
        elif path == PVE + "/pools/vws-live":
            status, data = s.pool_status, {"data": {}}
        elif path == PVE + "/cluster/resources?type=vm":
            s.resource_reads += 1
            if s.block and s.resource_reads == 3:
                s.arrived.set()
                s.release.wait()
            data = {"data": copy.deepcopy(s.vms)}
        elif "/tasks/" in path:
            data = {"data": {"status": "stopped", "exitstatus": SECRET if s.task_fail or s.fail_once else "OK"}}
            s.fail_once = False
        elif "/storage/" in path:
            if self.command == "DELETE":
                volid = path.split("/content/", 1)[1]
                if volid in s.snippets:
                    s.snippets.remove(volid)
                else:
                    status = 404
                data = {"data": None}
            else:
                data = {"data": [{"volid": v} for v in s.snippets]}
        elif "/qemu/" in path:
            rest = path.split("/qemu/", 1)[1]
            vmid = int(rest.split("/", 1)[0].split("?", 1)[0])
            if rest.endswith("/config"):
                if s.gone and vmid == 101:
                    s.vms = [v for v in s.vms if v["vmid"] != vmid]
                    status, data = 404, {}
                else:
                    data = {"data": {"smbios1": "uuid=" + s.uuids.get(vmid, ident(vmid))}}
            elif rest.endswith("/snapshot"):
                data = {"data": [{"name": n} for n in s.snapshots[vmid]]}
            else:
                if self.command == "DELETE":
                    if "/snapshot/" in rest:
                        s.snapshots[vmid].remove(rest.split("/snapshot/", 1)[1])
                    else:
                        s.vms = [v for v in s.vms if v["vmid"] != vmid]
                data = {"data": "UPID:node1:task"}
        else:
            s.errors.append("unexpected path: " + path)
            status = 404
        status, data = s.replies.get(path, (status, data))
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_DELETE = handle_request


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="live-test-")
        self.work = Path(self.temp.name)
        (self.work / "console").write_text(TOKEN)
        (self.work / "pve-token").write_text("vws@pve!live=" + SECRET)
        s = self.s = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        s.daemon_threads = True
        s.calls, s.errors, s.resource_reads = [], [], 0
        s.replies, s.uuids = {}, {}
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
        s.snapshots = {n: ["current", "old"] for n in range(101, 107)}
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
        for secret in (TOKEN, SECRET, "vws@pve!live=" + SECRET):
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
        expected += [("DELETE", f"/nodes/node1/qemu/{n}/snapshot/old") for n in (103, 106)]
        self.assertEqual(writes, expected)
        self.assertIn(f"local:snippets/vws-{ident(999)}-user.yaml", self.s.snippets)
        self.assertEqual([v["vmid"] for v in self.s.vms], [103, 104, 105, 106])
        self.assertEqual(json.loads((self.results / "results.json").read_text())["result"], "pass")
        self.s.calls.clear()
        self.run_cli()
        self.assertTrue(all(m == "GET" for m, _ in self.s.calls))
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
            self.s.vms = [dict(self.s.vms[-1], vmid=200 + i, maxmem=memory) for i in range(count)]
            self.s.snapshots.update({v["vmid"]: ["current"] for v in self.s.vms})
            self.run_cli(1)
            self.assertIn("FAIL slots", self.outputs[-1])

    def test_slot_need(self):
        self.results.mkdir()
        (self.results / ".gate.json").write_text(json.dumps([f"http://localhost:{self.s.server_port}", "node1"]))
        small = [self.s.vms[2], self.s.vms[-1]]
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


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LiveTests))
    failed = len(result.failures) + len(result.errors)
    print(f"{result.testsRun} cases, {failed} failed")
    raise SystemExit(int(failed != 0))
