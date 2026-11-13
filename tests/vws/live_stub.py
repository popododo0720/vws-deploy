#!/usr/bin/env python3
"""Shared loopback console/PVE wire fixture."""
import copy
import hashlib
import json
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote

TOKEN, SECRET = "console-canary-769", "pve-canary-769"
ACCESS, EC2_SECRET = "live-access-canary", "live-secret-canary"
CONSOLE = "/api/v1/admin/regions/pve-test-1"
PVE = "/api2/json"


def ident(n):
    return str(uuid.UUID(int=n))


def instance_id(n):
    return "i-" + hashlib.sha256(("\0".join(("vws-pve-instance-v1", "pve-test", ident(n)))).encode()).hexdigest()[:17]


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_request(self):
        s = self.server
        path = unquote(self.path)
        s.calls.append((self.command, path))
        auth = ("EC2 " + ACCESS + ":" + EC2_SECRET if path == "/ec2" else
                "Bearer " + TOKEN if path.startswith("/api/v1/") else "PVEAPIToken" + "=vws@pve!live=" + SECRET)
        if self.headers.get("Authorization") != auth:
            s.errors.append("authorization mismatch")
        if path.startswith(PVE) and self.headers.get("Host") != f"127.0.0.1:{s.server_port}":
            s.errors.append("PVE origin did not match the runner pin")
        status, data = 200, {}
        if path == "/ec2":
            operation, args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            s.ec2_calls.append((operation, args))
            if operation == "run-instances":
                if s.calls[-2] != ("GET", PVE + "/cluster/resources?type=vm"):
                    s.errors.append("create had no immediate slot assertion")
                if sum(v.get("pool") == "vws-live" and not v.get("template") and
                       "vws.holder" not in v.get("tags", "").split(";") for v in s.vms) >= 2:
                    s.errors.append("create exceeded the two VM boundary")
                if args["ClientToken"] not in s.tokens:
                    n = s.next_vmid
                    s.next_vmid += 1
                    run = args["TagSpecifications"][0]["Tags"][0]["Value"]
                    s.vms.append(dict(type="qemu", pool="vws-live", node="node1", vmid=n,
                                      status="running", maxmem=1024 ** 3, tags="vws.live." + run))
                    s.snapshots[n] = ["current"]
                    s.snippets.append(f"local:snippets/vws-{ident(n)}-user.yaml")
                    s.tokens[args["ClientToken"]] = instance_id(n)
                    s.instances[instance_id(n)] = dict(InstanceId=instance_id(n), State={"Name": "running"},
                        PrivateIpAddress="192.168.1.10", Tags=args["TagSpecifications"][0]["Tags"])
                data = {"Instances": [s.instances[s.tokens[args["ClientToken"]]]]}
            elif operation == "describe-instances":
                if s.ec2_block:
                    s.arrived.set()
                    s.release.wait()
                data = {"Reservations": [{"Instances": [v for k, v in s.instances.items()
                        if k in args.get("InstanceIds", s.instances) and any(
                            instance_id(vm["vmid"]) == k for vm in s.vms)]}]}
                if s.no_ip:
                    data = copy.deepcopy(data)
                    for i in data["Reservations"][0]["Instances"]:
                        i.pop("PrivateIpAddress", None)
            elif operation == "terminate-instances":
                removed = [v for v in s.vms if instance_id(v["vmid"]) in args["InstanceIds"]]
                s.vms = [v for v in s.vms if v not in removed]
                for v in removed:
                    snippet = f"local:snippets/vws-{ident(v['vmid'])}-user.yaml"
                    if not s.keep_snippet:
                        s.snippets.remove(snippet)
                data = {}
            else:
                s.errors.append("unexpected EC2 operation")
                status = 400
        elif path.endswith("/capabilities"):
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
                    if not s.keep_snippet:
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
                    tags = next((v["tags"] for v in s.vms if v["vmid"] == vmid), "")
                    data = {"data": {"smbios1": "uuid=" + s.uuids.get(vmid, ident(vmid)),
                                     "tags": "" if s.no_live_tag and vmid >= 201 else tags,
                                     **s.config_extra.get(vmid, {})}}
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
