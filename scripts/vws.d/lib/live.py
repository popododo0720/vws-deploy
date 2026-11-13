#!/usr/bin/env python3
"""Read-only gate and L0 cleanup for the single-node verification cluster."""
import json
import os
import re
import signal
import ssl
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from admin import Parser, fail, read_file, redact
from live_origin import NoRedirect, Refused, origin

ALLOWLIST = ("pve-test",)


class HTTPFailure(Refused):
    def __init__(self, status):
        self.status = status
        super().__init__("API HTTP " + str(status))


class Client:
    def __init__(self, a):
        token_file = os.environ.get("VWS_API_TOKEN_FILE", "")
        credential_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not token_file or token_file == "-" or not credential_dir:
            raise Refused("console token file and systemd pve-token credential are required")
        self.console = read_file(token_file)
        self.token = read_file(str(Path(credential_dir) / "pve-token"))
        if (not self.token.startswith("vws@pve!live=") or not self.token.partition("=")[2]
                or any(not s or any(ord(c) <= 32 or ord(c) >= 127 for c in s)
                       for s in (self.console, self.token))):
            raise Refused("invalid console or runner credential")
        self.secrets = (self.console, self.token, self.token.partition("=")[2])
        self.base = origin(os.environ.get("VWS_API_URL", "").rstrip("/"))
        self.pve = origin(os.environ.get("VWS_LIVE_PVE_URL", "").rstrip("/"))
        self.opener = build_opener(ProxyHandler({}), NoRedirect(),
                                  HTTPSHandler(context=ssl.create_default_context(cafile=a.pve_ca_file)))
        self.console_opener = build_opener(ProxyHandler({}), NoRedirect())
        self.a, self.steps = a, []
        if a.operation not in ("gate", "identity"):
            _, self.node = json.loads((Path(a.results) / ".gate.json").read_text())

    def request(self, path, method="GET", console=False, deadline=None):
        base = self.base if console else self.pve + "/api2/json"
        auth = "Bearer " + self.console if console else "PVEAPIToken" + "=" + self.token
        for attempt in range(3 if method == "GET" else 1):
            timeout = min(20, deadline - time.monotonic()) if deadline else 20
            if timeout <= 0:
                raise Refused("task deadline exceeded")
            try:
                opener = self.console_opener if console else self.opener
                with opener.open(Request(base + path, headers={"Authorization": auth},
                                         method=method), timeout=timeout) as response:
                    if method == "GET" and response.status != 200:
                        raise HTTPFailure(response.status)
                    data = json.load(response)
                return data if console else data["data"]
            except HTTPError as e:
                if method != "GET" or e.code < 500 or attempt == 2:
                    raise HTTPFailure(e.code) from None
            except (URLError, TimeoutError, ConnectionError):
                if method != "GET" or attempt == 2:
                    raise Refused("API transport failed") from None
            delay = attempt + 1
            if deadline and time.monotonic() + delay >= deadline:
                raise Refused("task deadline exceeded")
            time.sleep(delay)

    def emit(self, name, passed, message=""):
        step = redact(dict(name=name, result="pass" if passed else "fail", message=message), self.secrets)
        self.steps.append(step)
        print(redact(f"{'OK' if passed else 'FAIL'} {name} {message}", self.secrets), flush=True)

    def save(self, name):
        data = dict(result="fail" if any(s["result"] == "fail" for s in self.steps) else "pass",
                    steps=self.steps)
        (Path(self.a.results) / (name + ".json")).write_text(json.dumps(data) + "\n")

    def check_gate(self):
        if not re.fullmatch(r"pve-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+", self.a.region) or len(self.a.region) > 63:
            raise Refused("invalid region name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.a.snippets_storage):
            raise Refused("invalid snippets storage")
        cap = self.request("/api/v1/regions/" + self.a.region + "/capabilities", console=True)
        if not isinstance(cap, dict) or cap.get("kind") != "proxmox":
            raise Refused("capability kind must be proxmox")
        cluster = cap.get("cluster")
        if not isinstance(cluster, dict) or cluster.get("name") not in ALLOWLIST:
            raise Refused("cluster name is not allowlisted")
        features = cap.get("features")
        if not isinstance(features, dict) or features.get("liveTests") is not True:
            raise Refused("features.liveTests must be true (vws-console-api#943)")
        health = self.request("/api/v1/admin/regions/" + self.a.region + "/health", console=True)
        if not isinstance(health, dict) or not isinstance(health.get("nodes"), list) or len(health["nodes"]) != 1:
            raise Refused("health must contain exactly one node")
        self.node = health["nodes"][0]["name"]
        if not isinstance(self.node, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.node):
            raise Refused("invalid health node")
        region = self.request("/api/v1/admin/regions/" + self.a.region, console=True)
        reported, pinned = urlsplit(origin(region["pveApiUrl"])), urlsplit(self.pve)
        ports = {"https": 443, "http": 80}
        if (reported.scheme, reported.hostname, reported.port or ports[reported.scheme]) != (
                pinned.scheme, pinned.hostname, pinned.port or ports[pinned.scheme]):
            raise Refused("console pveApiUrl does not match VWS_LIVE_PVE_URL")
        pool = self.request("/pools/vws-live")
        if not isinstance(pool, dict):
            raise Refused("invalid pool response")
        state = json.dumps([self.pve, self.node])
        if redact(state, self.secrets) != state:
            raise Refused("credential in gate response")
        return state

    def gate(self):
        (Path(self.a.results) / ".gate.json").write_text(self.check_gate())
        self.emit("gate", True)

    def identity(self):
        self.check_gate()
        serverid = self.request("/nodes/" + quote(self.node, safe="") + "/subscription").get("serverid")
        if (not isinstance(serverid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", serverid)
                or redact(serverid, self.secrets) != serverid):
            raise Refused("invalid PVE server ID")
        print(serverid)

    def resources(self):
        data = self.request("/cluster/resources?type=vm")
        if not isinstance(data, list) or any(not isinstance(v, dict) for v in data):
            raise Refused("invalid cluster resources")
        return data

    @staticmethod
    def candidate(v):
        return v.get("type") == "qemu" and v.get("pool") == "vws-live" and not v.get("template")

    @staticmethod
    def vm_path(v):
        if isinstance(v["vmid"], bool) or not re.fullmatch(r"[1-9][0-9]*", str(v["vmid"])):
            raise Refused("invalid VM identifier")
        return "/nodes/" + quote(v["node"], safe="") + "/qemu/" + str(int(v["vmid"]))

    @staticmethod
    def config_uuid(config):
        match = re.search(r"(?:^|,)uuid=([^,]+)", config.get("smbios1", ""))
        return str(uuid.UUID(match[1])) if match else None

    def wait(self, node, upid):
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            raise Refused("mutation returned no task identifier")
        deadline = time.monotonic() + 120
        while True:
            task = self.request("/nodes/" + quote(node, safe="") + "/tasks/" +
                                quote(upid, safe="") + "/status", deadline=deadline)
            if task["status"] == "stopped":
                if task.get("exitstatus") != "OK":
                    raise Refused("task exitstatus was not OK")
                return
            if task["status"] != "running" or time.monotonic() + 1 >= deadline:
                raise Refused("task did not complete within 120 seconds")
            time.sleep(1)

    def item(self, name, action):
        try:
            action()
            self.emit(name, True)
        except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.emit(name, False, str(e) if isinstance(e, Refused) else "invalid sweep data")

    def delete_snippet(self, v, ident):
        volid = self.a.snippets_storage + ":snippets/vws-" + ident + "-user.yaml"
        path = ("/nodes/" + quote(v["node"], safe="") + "/storage/" +
                quote(self.a.snippets_storage, safe="") + "/content")
        try:
            task = self.request(path + "/" + quote(volid, safe=""), "DELETE")
        except HTTPFailure as e:
            if e.status != 404 and (e.status != 500 or any(
                    s["volid"] == volid for s in self.request(path + "?content=snippets"))):
                raise
        else:
            if task is not None:
                self.wait(v["node"], task)
        self.emit("sweep/snippet/" + volid, True)

    def delete_vm(self, v):
        path = self.vm_path(v)
        try:
            ident = self.config_uuid(self.request(path + "/config"))
            if ident:
                self.delete_snippet(v, ident)
            if v.get("status") == "running":
                self.wait(v["node"], self.request(path + "/status/stop", "POST"))
            self.wait(v["node"], self.request(path + "?purge=1&destroy-unreferenced-disks=1", "DELETE"))
        except HTTPFailure as e:
            if e.status not in (404, 500) or any(
                    x.get("vmid") == v["vmid"] for x in self.resources()):
                raise

    def sweep(self):
        resources = self.resources()
        for v in resources:
            if self.candidate(v) and any(re.fullmatch(r"vws\.live\..+", tag)
                                         for tag in v.get("tags", "").split(";")):
                self.item("sweep/vm/" + str(v["vmid"]), lambda v=v: self.delete_vm(v))
        for v in self.resources():
            if self.candidate(v):
                snapshots = self.request(self.vm_path(v) + "/snapshot")
                for snapshot in snapshots:
                    if snapshot["name"] != "current":
                        path = self.vm_path(v) + "/snapshot/" + quote(snapshot["name"], safe="")
                        self.item("sweep/snapshot/" + str(v["vmid"]) + "/" + snapshot["name"],
                                  lambda path=path, v=v: self.wait(v["node"], self.request(path, "DELETE")))

    def slots(self):
        vms = [v for v in self.resources() if self.candidate(v) and
               "vws.holder" not in v.get("tags", "").split(";")]
        passed = (not self.a.fail_at and len(vms) + self.a.need <= 2 and
                  all(0 <= int(v["maxmem"]) <= 1024 ** 3 for v in vms))
        self.emit("slots", passed, "two VM / 1 GiB limit" if not passed else "")


def main():
    p = Parser(allow_abbrev=False)
    p.add_argument("operation", choices=("gate", "identity", "sweep", "slots", "results"))
    p.add_argument("--region", required=True)
    p.add_argument("--results")
    p.add_argument("--snippets-storage", default="local")
    p.add_argument("--pve-ca-file")
    p.add_argument("--fail-at", choices=("slots",))
    p.add_argument("--phase", choices=("l0-start", "l0-end"))
    p.add_argument("--need", type=int, default=0)
    p.add_argument("--exit", type=int, default=0)
    a = p.parse_args()
    if (a.need < 0 or a.operation == "sweep" and not a.phase
            or a.operation != "identity" and not a.results):
        p.error("")
    if a.operation == "results":
        steps = []
        for name in ("l0-start", "slots", "l0-end"):
            path = Path(a.results) / (name + ".json")
            if path.exists():
                data = json.loads(path.read_text())
                steps.append(dict(name=name, result=data["result"], steps=data["steps"]))
        result = "fail" if a.exit or any(s["result"] == "fail" for s in steps) else "pass"
        (Path(a.results) / "results.json").write_text(json.dumps(dict(result=result, steps=steps)) + "\n")
        return int(result == "fail" and not a.exit)
    c = Client(a)
    try:
        getattr(c, a.operation)()
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        message = str(e) if isinstance(e, Refused) else "invalid API response"
        if a.operation == "identity":
            fail(redact(message, c.secrets), 3)
        c.emit(a.operation, False, message)
    except SystemExit:
        if a.operation != "identity":
            c.emit(a.operation, False, "interrupted")
        raise
    finally:
        if a.operation not in ("gate", "identity"):
            c.save(a.phase if a.operation == "sweep" else "slots")
    return (3 if a.operation == "gate" else 1) if any(s["result"] == "fail" for s in c.steps) else 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        sys.exit(main())
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError):
        fail("nightly-live: invalid environment or API data", 3)
