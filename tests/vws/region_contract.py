#!/usr/bin/env python3
"""Run `vws region add` against a capture server and save the request body.

usage: region_contract.py WORK_DIR BODY_OUT

CI feeds BODY_OUT to region_contract_test.go.txt inside the pinned vws-console-api, whose own
validator then has to accept it. The CA is a real certificate, because the API parses it.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

root = Path(__file__).resolve().parents[2]
work, out = Path(sys.argv[1]), Path(sys.argv[2])
work.mkdir(parents=True, exist_ok=True)
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=pve-ca",
                "-keyout", str(work / "ca.key"), "-out", str(work / "ca.pem")],
               check=True, capture_output=True)
for name, text in (("token", "console-token\n"), ("secret", "pve-secret\n"),
                   ("firewall", "fw-secret\n"), ("backup", "bk-secret\n")):
    (work / name).write_text(text)
captured = []


class Capture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        captured.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        body = json.dumps({"lastError": None}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = HTTPServer(("127.0.0.1", 0), Capture)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = {**os.environ, "VWS_API_URL": f"http://127.0.0.1:{server.server_port}",
       "VWS_API_TOKEN_FILE": str(work / "token")}
result = subprocess.run([
    str(root / "scripts/vws"), "region", "add", "pve-contract-1", "--cluster-name", "contract",
    "--pve-api-url", "https://pve1.example.org:8006", "--pve-api-url", "https://pve2.example.org:8006",
    "--ca-pem-file", str(work / "ca.pem"), "--token-id", "root@pam!vws",
    "--region-endpoint", "https://region.example.org:8443",
    "--zones", '[{"letter":"a","nodes":["pve1","pve2"]}]', "--token-secret-file", str(work / "secret"),
    "--firewall-token-id", "fw@pve!fw", "--firewall-token-secret-file", str(work / "firewall"),
    "--backup-token-id", "bk@pve!bk", "--backup-token-secret-file", str(work / "backup"), "-o", "json"],
    env=env, capture_output=True, text=True, timeout=60)
assert result.returncode == 0, result.stderr
assert [path for path, _ in captured] == ["/api/v1/admin/regions"], captured
out.write_text(json.dumps(captured[0][1]))
print("captured the region add body")
