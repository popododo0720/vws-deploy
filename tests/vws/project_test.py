#!/usr/bin/env python3
"""Project commands against the console API's replacement PUT contract."""
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "123456789012"
PATH = "/api/v1/admin/projects/" + PROJECT
TOKEN = "project-console-canary"
POOL = {"region": "pve-test-1", "pool": "vws-live"}
ACCOUNT = "234567890123"
RECORD = {"project": PROJECT,
          "pools": [{"region": "pve-test-1", "pool": "old"},
                    {"region": "pve-other-1", "pool": "keep"}],
          "awsAccounts": ["345678901234"]}
POOL_ARGS = ["--region", POOL["region"], "--pool", POOL["pool"]]
ACCOUNT_ARGS = ["--aws-account", ACCOUNT]


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_request(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request = (self.command, self.path, json.loads(data) if data else None,
                   self.headers.get("If-Match"), self.headers.get("Authorization"),
                   self.headers.get("Content-Type"))
        self.server.requests.append(request)
        status, result, etag = self.server.responses.pop(0) if self.server.responses else (500, {}, None)
        if status == 0:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        raw = result if isinstance(result, bytes) else (json.dumps(result) + "\n").encode()
        self.send_response(status)
        if etag:
            self.send_header("eTaG", etag)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_PUT = handle_request


class ProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="vws-project-test-")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        ready = threading.Event()

        def serve():
            ready.set()
            cls.server.serve_forever()

        cls.thread = threading.Thread(target=serve)
        cls.thread.start()
        ready.wait()
        cls.env = dict(os.environ, VWS_API_URL=f"http://127.0.0.1:{cls.server.server_port}",
                       VWS_API_TOKEN=TOKEN, TMPDIR=cls.work.name)
        for key in ("VWS_API_TOKEN_FILE", "VWS_DRY_RUN", "VWS_OUTPUT", "VWS_YES", "VWS_COMMAND_DIR"):
            cls.env.pop(key, None)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.work.cleanup()

    def setUp(self):
        self.server.requests = []
        self.server.responses = []

    def expect(self, result=RECORD, status=200, etag='"7"'):
        self.server.responses.append((status, result, etag))

    def run_cli(self, args, code=0, env=None):
        result = subprocess.run(["bash", os.environ.get("VWS_ADMIN_TEST_BIN", str(ROOT / "scripts/vws")),
                                 "project"] + args, env=env or self.env, capture_output=True,
                                text=True, timeout=75, check=False)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertFalse(self.server.responses, "expected API response was not consumed")
        return result

    def assert_calls(self, methods, body=None, etag='"7"'):
        self.assertEqual([v[0] for v in self.server.requests], methods)
        for method, path, data, match, token, content_type in self.server.requests:
            self.assertEqual(path, PATH)
            self.assertEqual(token, "Bearer " + TOKEN)
            self.assertEqual(content_type, "application/json")
            self.assertEqual(data, body if method == "PUT" else None)
            self.assertEqual(match, etag if method == "PUT" else None)

    def test_show(self):
        for mode in ("text", "json"):
            with self.subTest(mode=mode):
                self.setUp()
                self.expect()
                result = self.run_cli(["show", PROJECT, "-o", mode])
                self.assert_calls(["GET"])
                if mode == "json":
                    self.assertEqual(result.stdout, json.dumps(RECORD) + "\n")
                else:
                    for value in (PROJECT, "pve-other-1", "keep", "345678901234"):
                        self.assertIn(value, result.stdout)

    def test_map_and_unmap(self):
        for operation in ("map", "unmap"):
            for args in (POOL_ARGS, ACCOUNT_ARGS):
                with self.subTest(operation=operation, args=args):
                    self.setUp()
                    current = copy.deepcopy(RECORD)
                    if operation == "unmap":
                        current["pools"][0] = POOL
                        current["awsAccounts"].append(ACCOUNT)
                    body = {k: copy.deepcopy(current[k]) for k in ("pools", "awsAccounts")}
                    if args == POOL_ARGS:
                        if operation == "map":
                            body["pools"][0] = POOL
                        else:
                            body["pools"].remove(POOL)
                    elif operation == "map":
                        body["awsAccounts"].append(ACCOUNT)
                    else:
                        body["awsAccounts"].remove(ACCOUNT)
                    updated = dict(body, project=PROJECT)
                    self.expect(current)
                    self.expect(updated, etag='"8"')
                    result = self.run_cli([operation, PROJECT] + args + ["--yes", "-o", "json"])
                    self.assert_calls(["GET", "PUT"], body)
                    self.assertEqual(json.loads(result.stdout), updated)

    def test_add_pool_to_empty_mapping(self):
        self.expect(dict(project=PROJECT, pools=[], awsAccounts=[]), etag='"0"')
        body = {"pools": [POOL], "awsAccounts": []}
        self.expect(dict(body, project=PROJECT), etag='"1"')
        self.run_cli(["map", PROJECT] + POOL_ARGS)
        self.assert_calls(["GET", "PUT"], body, etag='"0"')

    def test_absent_unmap_has_no_put(self):
        for args in (POOL_ARGS, ACCOUNT_ARGS):
            with self.subTest(args=args):
                self.setUp()
                self.expect()
                result = self.run_cli(["unmap", PROJECT] + args + ["--yes"], code=1)
                self.assert_calls(["GET"])
                self.assertIn("entry not found", result.stderr)

    def test_map_is_idempotent_across_fresh_clients(self):
        for args in (POOL_ARGS, ACCOUNT_ARGS):
            current = copy.deepcopy(RECORD)
            current["pools"][0] = POOL
            current["awsAccounts"].append(ACCOUNT)
            body = {k: current[k] for k in ("pools", "awsAccounts")}
            for _ in range(2):
                self.setUp()
                self.expect(current)
                self.expect(current)
                self.run_cli(["map", PROJECT] + args)
                self.assert_calls(["GET", "PUT"], body)

    def test_conflicts_are_not_retried(self):
        body = {"pools": RECORD["pools"], "awsAccounts": RECORD["awsAccounts"] + [ACCOUNT]}
        for status in (409, 412, 503):
            with self.subTest(status=status):
                self.setUp()
                self.expect()
                self.expect({"error": {"code": "Conflict" if status != 503 else "Unavailable",
                                       "message": "mapping changed"}, "requestId": "req-write"},
                            status=status)
                result = self.run_cli(["map", PROJECT] + ACCOUNT_ARGS, code=3 if status == 503 else 1)
                self.assert_calls(["GET", "PUT"], body)
                self.assertIn("req-write", result.stderr)

    def test_fresh_client_after_uncertain_write(self):
        body = {"pools": RECORD["pools"], "awsAccounts": RECORD["awsAccounts"] + [ACCOUNT]}
        self.expect()
        self.expect(status=0)
        result = self.run_cli(["map", PROJECT] + ACCOUNT_ARGS, code=3)
        self.assert_calls(["GET", "PUT"], body)
        self.assertIn("transport failed", result.stderr)
        self.setUp()
        current = dict(body, project=PROJECT)
        self.expect(current, etag='"8"')
        self.expect(current, etag='"8"')
        self.run_cli(["map", PROJECT] + ACCOUNT_ARGS)
        self.assert_calls(["GET", "PUT"], {k: current[k] for k in ("pools", "awsAccounts")}, etag='"8"')

    def test_missing_etag_or_invalid_get_stops_put(self):
        for value, etag in ((RECORD, None), (dict(RECORD, pools=None), '"7"'),
                            (dict(RECORD, awsAccounts={}), '"7"'),
                            (dict(RECORD, pools=[{}]), '"7"'),
                            (dict(RECORD, project="234567890123"), '"7"')):
            with self.subTest(value=value, etag=etag):
                self.setUp()
                self.expect(value, etag=etag)
                self.run_cli(["map", PROJECT] + ACCOUNT_ARGS, code=3)
                self.assert_calls(["GET"])

    def test_forbidden_action_for_every_step(self):
        for operation in ("show", "map", "unmap"):
            for step in ("GET", "PUT") if operation != "show" else ("GET",):
                for payload in ({"error": {"code": "PermissionDenied", "message": TOKEN},
                                 "requestId": "req-403"}, b"<html>Forbidden</html>"):
                    with self.subTest(operation=operation, step=step, payload=payload):
                        self.setUp()
                        if step == "PUT":
                            self.expect(dict(RECORD, awsAccounts=RECORD["awsAccounts"] + [ACCOUNT]))
                        self.expect(payload, status=403)
                        args = [operation, PROJECT] + ([] if operation == "show" else ACCOUNT_ARGS + ["--yes"])
                        result = self.run_cli(args, code=3)
                        self.assertEqual([v[0] for v in self.server.requests],
                                         ["GET", "PUT"] if step == "PUT" else ["GET"])
                        self.assertIn("missing permission vws:AdminMapProject", result.stderr)
                        self.assertIn("requestId: req-403" if isinstance(payload, dict)
                                      else "requestId: unknown", result.stderr)

    def test_invalid_input_sends_no_requests(self):
        for args in (["show", "../bad"], ["show", PROJECT, "--aws-account", ACCOUNT],
                     ["map", PROJECT], ["map", PROJECT, "--region", "pve-test-1"],
                     ["map", PROJECT, "--pool", "x"], ["map", PROJECT, "--aws-account", "short"],
                     ["map", PROJECT] + POOL_ARGS + ACCOUNT_ARGS,
                     ["map", PROJECT, "--region", "pve-test-1", "--pool", "../bad"],
                     ["map", PROJECT, "--region", "bad", "--pool", "good"],
                     ["map", PROJECT, "--region", "pve-test-1", "--pool", "a" * 65],
                     ["unmap", PROJECT] + ACCOUNT_ARGS):
            with self.subTest(args=args):
                self.run_cli(args, code=2)
                self.assert_calls([])

    def test_dry_run_and_help(self):
        for operation in ("show", "map", "unmap"):
            args = [operation, PROJECT] + ([] if operation == "show" else ACCOUNT_ARGS + ["--yes"])
            result = self.run_cli(args + ["--dry-run"], env=dict(self.env, VWS_API_TOKEN=""))
            lines = ["DRYRUN GET " + PATH]
            if operation != "show":
                lines.append("DRYRUN PUT " + PATH)
            self.assertEqual(result.stdout.splitlines(), lines)
            self.assert_calls([])
        result = subprocess.run(["bash", str(ROOT / "scripts/vws"), "help"], env=self.env,
                                text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("project", result.stdout)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProjectTests))
    print(f"{result.testsRun} cases, {len(result.failures) + len(result.errors)} failed")
    sys.exit(not result.wasSuccessful())
