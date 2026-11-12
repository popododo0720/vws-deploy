#!/usr/bin/env python3
"""Console admin API client. Credentials never enter subprocess arguments."""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def fail(message, code=2):
    print("vws: " + message, file=sys.stderr)
    raise SystemExit(code)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostic echoes untrusted argument values.
        fail("invalid admin command arguments; see docs/vws-cli.md")


def read_file(path):
    try:
        return (sys.stdin.read() if path == "-" else Path(path).read_text()).rstrip("\r\n")
    except (OSError, UnicodeError):
        fail("cannot read input file", 3)


def parse(kind, argv):
    for arg in argv:
        if arg.split("=", 1)[0] in ("--token-secret", "--external-id"):
            fail("secrets require --token-secret-file or --external-id-file")
    p = Parser(prog="vws " + kind, allow_abbrev=False)
    commands = ["add", "edit", "remove", "list"]
    if kind == "region":
        commands += ["show", "test", "health"]
    p.add_argument("operation", choices=commands)
    p.add_argument("name", nargs="?")
    fields = ({"--cluster-name": "clusterName", "--pve-api-url": "pveApiUrl",
               "--token-id": "tokenId", "--region-endpoint": "regionEndpoint",
               "--zones": "zones", "--ca-pem-file": "caPem"}
              if kind == "region" else
              {"--role-arn": "roleArn", "--allowed-regions": "allowedRegions"})
    secret_flag = "--token-secret-file" if kind == "region" else "--external-id-file"
    for flag, field in fields.items():
        p.add_argument(flag, dest=field)
    p.add_argument(secret_flag, dest="secret_file")
    a = p.parse_args(argv)
    if (a.operation == "list") != (a.name is None):
        p.error("")
    if a.name:
        pattern = r"pve-[a-z0-9]+(-[a-z0-9]+)*-[0-9]+" if kind == "region" else r"[0-9]{12}"
        if not re.fullmatch(pattern, a.name) or len(a.name) > 63:
            fail("invalid region name or AWS account ID")
    body = {field: getattr(a, field) for field in fields.values()
            if getattr(a, field) is not None}
    if a.operation not in ("add", "edit") and body:
        fail("registration fields are accepted only by add and edit")
    if a.secret_file and a.operation not in ("add", "edit", "test"):
        fail("secret file is not accepted by this operation")
    if a.operation == "add":
        required = (["clusterName", "pveApiUrl", "tokenId", "regionEndpoint"]
                    if kind == "region" else ["roleArn", "allowedRegions"])
        if not a.secret_file or any(field not in body for field in required):
            fail("add requires registration fields and a secret file; see docs/vws-cli.md")
        body["name" if kind == "region" else "awsAccountId"] = a.name
        if kind == "region":
            body.setdefault("zones", [])
    if "zones" in body and isinstance(body["zones"], str):
        try:
            body["zones"] = json.loads(body["zones"])
        except ValueError:
            fail("--zones requires a JSON array")
        if not isinstance(body["zones"], list):
            fail("--zones requires a JSON array")
    if "allowedRegions" in body:
        body["allowedRegions"] = body["allowedRegions"].split(",")
    if "caPem" in body:
        if body["caPem"] == "-" and a.secret_file == "-":
            fail("only one input file may use stdin")
        body["caPem"] = read_file(body["caPem"])
    if a.secret_file:
        body["tokenSecret" if kind == "region" else "externalId"] = read_file(a.secret_file)
    if a.operation == "edit" and not body:
        fail("edit requires at least one field")
    return a, body


def redact(value, credentials):
    if isinstance(value, str):
        for secret in credentials:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    elif isinstance(value, list):
        return [redact(item, credentials) for item in value]
    elif isinstance(value, dict):
        return {redact(key, credentials): redact(item, credentials) for key, item in value.items()}
    return value


def request(base, path, method, body, action, credentials, directory, etag=None):
    headers = Path(directory) / "request.headers"
    with headers.open("w", encoding="utf-8") as f:
        headers.chmod(0o600)
        f.write("Authorization: Bearer " + credentials[0] + "\nContent-Type: application/json\n")
        if etag:
            f.write("If-Match: " + etag + "\n")
    response = Path(directory) / "response"
    received = Path(directory) / "response.headers"
    args = ["curl", "-q", "--silent", "--show-error", "--max-time", "20",
            "--connect-timeout", "5", "--proto", "=http,https", "--request", method,
            "--header", "@" + str(headers), "--dump-header", str(received),
            "--output", str(response), "--write-out", "%{http_code}"]
    if method == "GET":
        args += ["--retry", "2", "--retry-max-time", "40"]
    if body is not None:
        args += ["--data-binary", "@-"]
    args += ["--url", base + path]
    env = {k: v for k, v in os.environ.items() if k not in ("VWS_API_TOKEN", "VWS_API_TOKEN_FILE")}
    try:
        result = subprocess.run(args, input=json.dumps(body).encode() if body is not None else b"",
                                capture_output=True, timeout=65, env=env, check=False)
    except subprocess.TimeoutExpired:
        fail("admin API request deadline exceeded", 3)
    if result.returncode:
        fail("admin API transport failed (curl exit " + str(result.returncode) + ")", 3)
    raw = response.read_bytes()
    try:
        status = int(result.stdout)
        data = json.loads(raw) if raw else None
    except (ValueError, UnicodeError):
        fail("admin API returned an invalid response", 3)
    if status >= 400:
        error = data.get("error") if isinstance(data, dict) else None
        request_id = data.get("requestId", "unknown") if isinstance(data, dict) else "unknown"
        if not isinstance(request_id, str) or not request_id:
            request_id = "unknown"
        if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)):
            fail(redact(f"unexpected API error response (HTTP {status}, requestId {request_id})", credentials), 3)
        if status == 403:
            fail(redact(f"missing permission vws:{action} (requestId: {request_id})", credentials), 3)
        message = f"{error['code']}: {error['message']} (requestId: {request_id})"
        fail(redact(message, credentials), 2 if status == 400 else 3 if status == 401 or status >= 500 else 1)
    matches = re.findall(r'^etag:\s*("[^"\r\n]+")\s*$', received.read_text(), re.I | re.M)
    return raw, data, matches[-1] if matches else None


def text_output(kind, operation, data, credentials):
    def emit(line):
        print(redact(line, credentials))

    if operation == "test":
        emit("STEP\tRESULT\tMESSAGE")
        for step in data["steps"]:
            emit("\t".join(str(step[k]) for k in ("name", "result", "message")))
    elif operation == "health":
        emit("STATUS\tQUORUM\tLEADER\tONLINE NODES\tLAST ERROR")
        nodes = ",".join(n["name"] for n in data["nodes"] if n["online"])
        emit(f"{data['status']}\t{data['quorate']}\t{data['leader'] or '-'}\t"
              f"{nodes or '-'}\t{(data.get('lastError') or {}).get('message', '-')}")
    elif data is not None:
        fields = (["name", "clusterName", "pveApiUrl", "regionEndpoint", "tokenId", "tokenSecret", "zones"]
                  if kind == "region" else ["awsAccountId", "roleArn", "externalId", "allowedRegions"])
        emit("\t".join(fields))
        for item in data["items"] or [] if operation == "list" else [data]:
            values = []
            for field in fields:
                value = item[field]
                if field in ("tokenSecret", "externalId"):
                    value = "set \u00b7 " + value["fingerprint"] if value["set"] else "not set"
                elif isinstance(value, list):
                    value = json.dumps(redact(value, credentials), separators=(",", ":"))
                values.append(str(value))
            emit("\t".join(values))


def main():
    kind = sys.argv[1]
    a, body = parse(kind, sys.argv[2:])
    collection = "/api/v1/admin/" + ("regions" if kind == "region" else "aws-accounts")
    path = collection if a.operation in ("add", "list") else collection + "/" + a.name
    method = {"add": "POST", "edit": "PATCH", "remove": "DELETE", "test": "POST"}.get(a.operation, "GET")
    if a.operation in ("test", "health"):
        path += "/" + a.operation
    if a.operation == "remove" and not os.environ.get("VWS_YES"):
        fail("remove needs --yes")
    if os.environ.get("VWS_DRY_RUN"):
        print(f"DRYRUN {method} {path}")
        return 0
    base = os.environ.get("VWS_API_URL", "").rstrip("/")
    url = urlsplit(base)
    if (not url.hostname or url.username or url.password or url.query or url.fragment or url.path
            or (url.scheme != "https" and not (url.scheme == "http" and
                url.hostname in ("127.0.0.1", "::1", "localhost")))):
        fail("VWS_API_URL must be an HTTPS origin (HTTP allowed on loopback)", 3)
    token_file = os.environ.get("VWS_API_TOKEN_FILE")
    if token_file == "-":
        fail("VWS_API_TOKEN_FILE must name a file", 3)
    token = read_file(token_file) if token_file else os.environ.get("VWS_API_TOKEN", "")
    if not token or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
        fail("a single-line console token is required through VWS_API_TOKEN_FILE or VWS_API_TOKEN", 3)
    suffix = "Region" if kind == "region" else "AwsAccount"
    action = {"add": "AdminRegister" + suffix, "edit": "AdminUpdate" + suffix,
              "remove": "AdminRemove" + suffix}.get(a.operation, "AdminList" + suffix + "s")
    credentials = (token, body.get("tokenSecret"), body.get("externalId"))
    with tempfile.TemporaryDirectory(prefix="vws-admin-") as directory:
        etag = None
        if a.operation == "edit":
            _, _, etag = request(base, path, "GET", None, action, credentials, directory)
            if not etag:
                fail("admin API GET returned no ETag; edit was not sent", 3)
        raw, data, _ = request(base, path, method, body if method in ("POST", "PATCH") else None,
                               action, credentials, directory, etag)
        if os.environ.get("VWS_OUTPUT", "text") == "json":
            safe = redact(data, credentials)
            sys.stdout.buffer.write(raw if safe == data else (json.dumps(safe) + "\n").encode())
        else:
            text_output(kind, a.operation, data, credentials)
        return int(a.operation == "test" and any(s["result"] == "FAIL" for s in data["steps"]))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        sys.exit(main())
    except (OSError, UnicodeError):
        fail("admin client could not read or execute a required file", 3)
    except (ValueError, KeyError, TypeError):
        fail("admin API returned invalid data", 3)
