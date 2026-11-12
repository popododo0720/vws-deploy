#!/usr/bin/env python3
"""Admin CLI tests at the HTTP boundary; every invocation starts a fresh client."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KIND = sys.argv[1]
MANUAL = "--manual" in sys.argv
SECRET = "canary-secret-768"
TOKEN = "console-canary-768"
NAME = "pve-test-1" if KIND == "region" else "123456789012"
BASE = "/api/v1/admin/" + ("regions" if KIND == "region" else "aws-accounts")
METADATA = {"set": True, "fingerprint": "abcdef12"}
REGION = dict(name="pve-test-1", clusterName="lab", pveApiUrl="https://pve.example.org:8006",
              tokenId="root@pam!vws", regionEndpoint="https://region.example.org",
              tokenSecret=METADATA, zones=[{"letter": "a", "nodes": ["node1"]}])
ACCOUNT = dict(awsAccountId="123456789012", roleArn="arn:aws:iam::123456789012:role/vws",
               externalId=METADATA, allowedRegions=["us-east-1"])
RECORD = REGION if KIND == "region" else ACCOUNT
SECRET_KEY = "tokenSecret" if KIND == "region" else "externalId"
SECRET_FLAG = "--token-secret-file" if KIND == "region" else "--external-id-file"
FIELDS = (["--cluster-name", "lab", "--pve-api-url", REGION["pveApiUrl"],
           "--token-id", REGION["tokenId"], "--region-endpoint", REGION["regionEndpoint"],
           "--zones", json.dumps(REGION["zones"])] if KIND == "region" else
          ["--role-arn", ACCOUNT["roleArn"], "--allowed-regions", "us-east-1"])
EXPECTED = []
REQUESTS = []
ERRORS = []
CASES = FAILED = 0


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_request(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(data) if data else None
        if self.path.endswith("/test") and KIND == "aws-account":
            if data != b"{}" or self.headers.get("Content-Type") != "application/json":
                ERRORS.append("saved test requires exactly {} and application/json")
        REQUESTS.append((self.command, self.path, body, self.headers.get("If-Match")))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            ERRORS.append("bearer token did not match")
        if not EXPECTED:
            ERRORS.append("unexpected extra API call")
            status, result, etag = 500, {}, None
        else:
            method, path, want, status, result, etag = EXPECTED.pop(0)
            if (self.command, self.path, body) != (method, path, want):
                ERRORS.append("method, path or body did not match")
            if self.command == "PATCH" and self.headers.get("If-Match") != '"7"':
                ERRORS.append("PATCH did not send the GET ETag")
        raw = result if isinstance(result, bytes) else b"" if result is None else (json.dumps(result) + "\n").encode()
        self.send_response(status)
        if status in (301, 302):
            self.send_header("Location", "/unexpected-follow")
        if etag:
            self.send_header("eTaG", etag)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PATCH = do_DELETE = handle_request


def check(name, condition):
    global CASES, FAILED
    CASES += 1
    if not condition:
        FAILED += 1
    print(("ok   " if condition else "FAIL ") + name)


def expected(method, path=BASE, body=None, status=200, result=RECORD, etag='"7"'):
    EXPECTED.append((method, path, body, status, result, etag))


def run(args, code=0, stdin="", trace=False, env=None):
    REQUESTS.clear()
    ERRORS.clear()
    command = ["bash"] + (["-x"] if trace else []) + [
        os.environ.get("VWS_ADMIN_TEST_BIN", str(ROOT / "scripts/vws")), KIND] + args
    result = subprocess.run(command, input=stdin, text=True, capture_output=True,
                            timeout=75, env=env or ENV, check=False)
    check(" ".join(args[:2]) + " exit " + str(code), result.returncode == code)
    check("exact API call sequence and body", not ERRORS and not EXPECTED)
    EXPECTED.clear()
    check("no secret in stdout or stderr", all(s not in result.stdout + result.stderr for s in (SECRET, TOKEN)))
    argv = (WORK / "curl.argv").read_text() if (WORK / "curl.argv").exists() else ""
    check("no credentials in curl argv", SECRET not in argv and TOKEN not in argv)
    if REQUESTS:
        curl_args = json.loads(argv)
        check("bounded curl deadline", "--max-time" in curl_args and "--connect-timeout" in curl_args)
        check("only GET retries", ("--retry" in curl_args) == (REQUESTS[-1][0] == "GET"))
    if MANUAL:
        print("$ scripts/vws " + KIND + " " + " ".join(args).replace(SECRET, "<rejected-canary>"))
        print(result.stdout + result.stderr, end="")
        print("exit:", result.returncode)
    return result


with tempfile.TemporaryDirectory(prefix="vws-admin-test-") as work:
    WORK = Path(work)
    secret_file = WORK / "secret"
    secret_file.write_text(SECRET + "\n")
    token_file = WORK / "token"
    token_file.write_text(TOKEN + "\n")
    # Inspect real curl argv without substituting a fake HTTP transport.
    real_curl = shutil.which("curl")
    shim = WORK / "curl"
    shim.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                    "Path(os.environ['CURL_ARGV']).write_text(json.dumps(sys.argv[1:]))\n"
                    "assert 'VWS_API_TOKEN' not in os.environ\n"
                    "header=Path(sys.argv[sys.argv.index('--header')+1][1:])\n"
                    "assert header.stat().st_mode & 0o077 == 0\n"
                    f"os.execv({real_curl!r}, [{real_curl!r}] + sys.argv[1:])\n")
    shim.chmod(0o700)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    ready = threading.Event()

    def serve():
        ready.set()
        server.serve_forever()

    thread = threading.Thread(target=serve)
    thread.start()
    ready.wait()
    ENV = dict(os.environ, VWS_API_URL=f"http://127.0.0.1:{server.server_port}",
               VWS_API_TOKEN=TOKEN, PATH=work + os.pathsep + os.environ["PATH"],
               CURL_ARGV=str(WORK / "curl.argv"))
    for key in ("VWS_API_TOKEN_FILE", "VWS_DRY_RUN", "VWS_OUTPUT", "VWS_COMMAND_DIR", "VWS_YES"):
        ENV.pop(key, None)
    try:
        create = dict(RECORD, **{SECRET_KEY: SECRET})
        expected("POST", body=create, status=201)
        result = run(["add", NAME] + FIELDS + [SECRET_FLAG, "-"], stdin=SECRET + "\n")
        check("text shows secret metadata", "set \u00b7 abcdef12" in result.stdout)
        if not MANUAL:
            expected("POST", body=create, status=201)
            run(["add", NAME] + FIELDS + [SECRET_FLAG, str(secret_file)], trace=True)
            expected("GET", result={"items": [RECORD]})
            result = run(["list"])
            check("text prints all console fields", all(str(RECORD[k]) in result.stdout for k in
                  (["name", "clusterName", "pveApiUrl", "regionEndpoint", "tokenId"]
                   if KIND == "region" else ["awsAccountId", "roleArn"])))
            expected("GET", result={"items": [RECORD]})
            result = run(["list", "-o", "json"], env=dict(ENV, VWS_API_TOKEN_FILE=str(token_file),
                                                       VWS_API_TOKEN="wrong-env-token"))
            check("JSON is exactly the API body", result.stdout == json.dumps({"items": [RECORD]}) + "\n")
            expected("GET", BASE + "/" + NAME)
            expected("PATCH", BASE + "/" + NAME, {SECRET_KEY: SECRET})
            run(["edit", NAME, SECRET_FLAG, "-"], stdin=SECRET)
            field, value = ("clusterName", "updated") if KIND == "region" else ("roleArn", ACCOUNT["roleArn"])
            flag = "--cluster-name" if KIND == "region" else "--role-arn"
            expected("GET", BASE + "/" + NAME)
            expected("PATCH", BASE + "/" + NAME, {field: value}, status=409,
                     result={"error": {"code": "Conflict", "message": "registration changed"}, "requestId": "req-409"})
            result = run(["edit", NAME, flag, value], code=1)
            check("conflict is reported with requestId", "Conflict: registration changed" in result.stderr and "req-409" in result.stderr)
            expected("GET", BASE + "/" + NAME, etag=None)
            run(["edit", NAME, flag, value], code=3)
            expected("DELETE", BASE + "/" + NAME, status=204, result=None)
            run(["remove", NAME, "--yes"])
            check("remove needs no If-Match", REQUESTS[0][3] is None if REQUESTS else False)
            run(["remove", NAME], code=2)
        for flag in ("--token-secret", "--external-id"):
            run(["add", NAME, flag, SECRET], code=2, trace=True)
            run(["add", NAME, flag + "=" + SECRET], code=2, trace=True)
        if KIND == "region":
            if not MANUAL:
                expected("GET", BASE + "/" + NAME)
                run(["show", NAME])
                health = dict(status="ok", quorate=True, leader="node1",
                              nodes=[dict(name="node1", online=True)], lastError=None)
                expected("GET", BASE + "/" + NAME + "/health", result=health)
                result = run(["health", NAME])
                check("health fields", all(v in result.stdout for v in ("QUORUM", "LEADER", "ONLINE NODES", "LAST ERROR", "node1")))
            steps = {"steps": [dict(name="tls", result="FAIL", message="TLS handshake failed"),
                               dict(name="version", result="SKIPPED", message="previous step failed")]}
            expected("POST", BASE + "/" + NAME + "/test", {}, result=steps)
            result = run(["test", NAME], code=1)
            check("test table on FAIL", all(v in result.stdout for v in ("STEP", "RESULT", "MESSAGE", "tls\tFAIL", "SKIPPED")))
            if not MANUAL:
                expected("POST", BASE + "/" + NAME + "/test", {SECRET_KEY: SECRET}, result=steps)
                result = run(["test", NAME, SECRET_FLAG, "-", "-o", "json"], code=1, stdin=SECRET)
                check("failed test JSON passes through", result.stdout == json.dumps(steps) + "\n")
                expected("POST", BASE + "/" + NAME + "/test", {},
                         result={"steps": [dict(name="regionapi", result="PASS", message="connected")]})
                run(["test", NAME])
        else:
            for outcome, code in (("PASS", 0), ("FAIL", 1), ("SKIPPED", 1)):
                steps = {"steps": [dict(name="assume_role", result=outcome, message="AssumeRole " + outcome),
                                   dict(name="get_caller_identity", result=outcome, message="identity " + outcome)]}
                for mode in ("text", "json"):
                    expected("POST", BASE + "/" + NAME + "/test", {}, result=steps)
                    result = run(["test", NAME, "-o", mode], code=code)
                    check("saved test table or exact JSON", result.stdout == json.dumps(steps) + "\n"
                          if mode == "json" else all(v in result.stdout for v in
                              ("STEP\tRESULT\tMESSAGE", "assume_role\t" + outcome, "get_caller_identity")))
            for mode in ("text", "json"):
                expected("POST", BASE + "/" + NAME + "/test", {},
                         result={"steps": [dict(name="assume_role", result="PASS", message=TOKEN)]})
                result = run(["test", NAME, "-o", mode])
                check("step bearer token is redacted", "[REDACTED]" in result.stdout)
            for invalid in ("12345678901", "1234567890123", "abcdefghijkl"):
                run(["test", invalid], code=2)
            run(["test", NAME, SECRET_FLAG, str(secret_file)], code=2)
            for status in (302, 304, 500):
                expected("POST", BASE + "/" + NAME + "/test", {}, status=status,
                         result={"error": {"code": "Unavailable", "message": "test unavailable"}, "requestId": "req-test"})
                run(["test", NAME], code=3)
        suffix = "Region" if KIND == "region" else "AwsAccount"
        ops = [("list", "AdminList" + suffix + "s"), ("add", "AdminRegister" + suffix),
               ("edit", "AdminUpdate" + suffix), ("remove", "AdminRemove" + suffix),
               ("test", "AdminList" + suffix + "s")]
        if KIND == "region":
            ops += [(op, "AdminListRegions") for op in ("show", "health")]
        for op, action in ops if not MANUAL else ops[-1:]:
            method = {"add": "POST", "remove": "DELETE", "test": "POST"}.get(op, "GET")
            path = BASE if op in ("list", "add") else BASE + "/" + NAME
            if op in ("test", "health"):
                path += "/" + op
            body = create if op == "add" else {} if op == "test" else None
            expected(method, path, body, status=403,
                     result={"error": {"code": "PermissionDenied", "message": "administrator required"}, "requestId": "req-403"})
            args = [op] + ([] if op == "list" else [NAME])
            if op == "add":
                args += FIELDS + [SECRET_FLAG, str(secret_file)]
            if op == "edit":
                args += [SECRET_FLAG, str(secret_file)]
            if op == "remove":
                args += ["--yes"]
            result = run(args, code=3)
            check("403 action and requestId", "missing permission vws:" + action in result.stderr and "req-403" in result.stderr)
        if not MANUAL:
            for status in (301, 302, 304):
                for payload in (None,) if status == 304 else (None, {"requestId": TOKEN}, b"<html>redirect</html>"):
                    for op in ("list", "remove"):
                        path = BASE if op == "list" else BASE + "/" + NAME
                        expected("GET" if op == "list" else "DELETE", path, status=status, result=payload)
                        result = run([op] + ([] if op == "list" else [NAME, "--yes"]), code=3)
                        check(f"{op} HTTP {status} {type(payload).__name__} is diagnosed",
                              f"unexpected HTTP {status}" in result.stderr)
                        if isinstance(payload, dict):
                            check("redirect requestId is redacted", "[REDACTED]" in result.stderr)
            for status, error, code in [
                    (400, None, 3),
                    (400, {"code": SECRET, "message": SECRET + TOKEN}, 2),
                    (403, {"code": "PermissionDenied", "message": SECRET + TOKEN}, 3)]:
                expected("POST", body=create, status=status,
                         result={"error": error, "requestId": SECRET + TOKEN})
                result = run(["add", NAME] + FIELDS + [SECRET_FLAG, "-"], code=code, stdin=SECRET, trace=True)
                check("echoed error credentials are redacted", "[REDACTED]" in result.stderr)
            for mode in ("text", "json"):
                echoed = dict(RECORD, **{SECRET_KEY: {"set": True, "fingerprint": SECRET + TOKEN}})
                expected("POST", body=create, status=201, result=echoed)
                result = run(["add", NAME] + FIELDS + [SECRET_FLAG, "-", "-o", mode], stdin=SECRET)
                check("echoed success credentials are redacted", "[REDACTED]" in result.stdout)
            expected("GET", BASE + "/" + NAME, status=403,
                     result={"error": {"code": "PermissionDenied", "message": SECRET}, "requestId": SECRET})
            run(["edit", NAME, SECRET_FLAG, "-"], code=3, stdin=SECRET)
            if KIND == "region":
                expected("POST", BASE + "/" + NAME + "/test", {SECRET_KEY: SECRET},
                         result={"steps": [dict(name=SECRET, result="FAIL", message=SECRET + TOKEN)]})
                run(["test", NAME, SECRET_FLAG, "-"], code=1, stdin=SECRET)
                for secret in ("FAIL", "tls"):
                    for mode in ("text", "json"):
                        expected("POST", BASE + "/" + NAME + "/test", {SECRET_KEY: secret},
                                 result={"steps": [dict(name="tls", result="FAIL", message="TLS handshake failed")]})
                        result = run(["test", NAME, SECRET_FLAG, "-", "-o", mode], code=1, stdin=secret)
                        check("protocol-collision secret stays private", secret not in result.stdout + result.stderr)
            expected("POST", body=dict(create, **{SECRET_KEY: "error"}), status=400,
                     result={"error": {"code": "InvalidArgument", "message": "invalid query"}, "requestId": "error"})
            result = run(["add", NAME] + FIELDS + [SECRET_FLAG, "-"], code=2, stdin="error")
            check("envelope-key collision stays private", "error" not in result.stdout + result.stderr)
            for envelope, request_id in [
                    ({"error": None, "requestId": "r"}, "r"),
                    ({"error": []}, "unknown"),
                    ({"error": {"code": [], "message": "bad"}, "requestId": []}, "unknown"),
                    ([], "unknown")]:
                expected("GET", status=400, result=envelope)
                result = run(["list"], code=3)
                check("malformed error envelope has a clean diagnostic",
                      f"unexpected API error response (HTTP 400, requestId {request_id})" in result.stderr
                      and "Traceback" not in result.stderr)
            expected("GET", status=400,
                     result={"error": {"code": "InvalidArgument", "message": "invalid query"}, "requestId": "r"})
            result = run(["list"], code=2)
            check("valid argument error retains its usage exit", "InvalidArgument: invalid query" in result.stderr)
            run(["list"], code=3, env=dict(ENV, VWS_API_TOKEN="bad\nheader"))
            run(["edit", "../bad", SECRET_FLAG, str(secret_file)], code=2)
            run(["add", NAME] + FIELDS + [SECRET_FLAG, str(WORK / "absent")], code=3)
            run(["list"], env=dict(ENV, VWS_DRY_RUN="1", VWS_API_TOKEN=""))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
print(f"{CASES} cases, {FAILED} failed")
sys.exit(int(FAILED != 0))
