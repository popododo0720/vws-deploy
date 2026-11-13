#!/usr/bin/env python3
"""Shared loopback console/PVE wire fixture."""
import copy
import json
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote

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
        elif path == PVE + "/nodes/node1/subscription":
            data = {"data": {"serverid": "TestServer826"}}
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
            return

    do_GET = do_POST = do_DELETE = handle_request
