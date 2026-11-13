#!/usr/bin/env python3
"""Gated live steps and cleanup for the single-node verification cluster."""
import hashlib
import ipaddress
import json
import os
import re
import signal
import ssl
import subprocess
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
        self.secrets = (self.console, self.token, self.token.partition("=")[2],
                        os.environ.get("VWS_VERIFY_SECRET_ACCESS_KEY", ""),
                        os.environ.get("VWS_VERIFY_ACCESS_KEY_ID", ""))
        self.base = origin(os.environ.get("VWS_API_URL", "").rstrip("/"))
        self.pve = origin(os.environ.get("VWS_LIVE_PVE_URL", "").rstrip("/"))
        self.opener = build_opener(ProxyHandler({}), NoRedirect(),
                                  HTTPSHandler(context=ssl.create_default_context(cafile=a.pve_ca_file)))
        self.console_opener = build_opener(ProxyHandler({}), NoRedirect())
        self.a, self.steps, self.instances = a, [], set()
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
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", self.a.run_id):
            raise Refused("invalid run ID")
        if os.environ.get("VWS_VERIFY_INSTANCE_TYPE", "vws.small") != "vws.small":
            raise Refused("only vws.small is permitted")
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
                self.remember(ident)
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

    def slots(self, need=None):
        vms = [v for v in self.resources() if self.candidate(v) and
               "vws.holder" not in v.get("tags", "").split(";")]
        passed = (self.a.fail_at != "slots" and len(vms) + (self.a.need if need is None else need) <= 2 and
                  all(0 <= int(v["maxmem"]) <= 1024 ** 3 for v in vms))
        self.emit("slots", passed, "two VM / 1 GiB limit" if not passed else "")
        return passed

    def ec2(self, operation, **parameters):
        if operation == "run-instances":
            if (parameters.get("InstanceType") != "vws.small"
                    or os.environ.get("VWS_VERIFY_INSTANCE_TYPE", "vws.small") != "vws.small"
                    or parameters.get("MinCount") != 1 or parameters.get("MaxCount") != 1):
                raise Refused("only one vws.small per create is permitted")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", self.a.run_id):
                raise Refused("invalid run ID")
            if not any(t.get("Key") == "vws-live-run" and t.get("Value") == self.a.run_id
                       for spec in parameters.get("TagSpecifications", []) if spec.get("ResourceType") == "instance"
                       for t in spec.get("Tags", [])):
                raise Refused("create requires the live run tag")
            if not self.slots(1):
                raise Refused("create refused by slot guard")
        required = ("EC2_ENDPOINT", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "IMAGE_ID", "KEY_NAME")
        if any(not os.environ.get("VWS_VERIFY_" + key) for key in required):
            raise Refused("EC2 endpoint, access keys, image and existing key name are required")
        endpoint = os.environ["VWS_VERIFY_EC2_ENDPOINT"]
        u = urlsplit(endpoint)
        origin(endpoint.removesuffix(u.path))
        domains = ("ama" + "zonaws.com", "ama" + "zonaws.com.cn", "api.aws")
        if any(u.hostname.rstrip(".").lower() == d or u.hostname.rstrip(".").lower().endswith("." + d)
               for d in domains):
            raise Refused("AWS-owned EC2 endpoint refused")
        env = dict(PATH=os.environ.get("PATH", ""), HOME=self.a.results,
                   AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                   AWS_EC2_METADATA_DISABLED="true", AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="3",
                   AWS_PAGER="", AWS_ENDPOINT_URL_EC2=endpoint, AWS_DEFAULT_REGION=self.a.region,
                   AWS_ACCESS_KEY_ID=os.environ["VWS_VERIFY_ACCESS_KEY_ID"],
                   AWS_SECRET_ACCESS_KEY=os.environ["VWS_VERIFY_SECRET_ACCESS_KEY"])
        args = ["aws", "--cli-connect-timeout", "10", "--cli-read-timeout", "20",
                "ec2", operation, "--output", "json", "--cli-input-json", json.dumps(parameters)]
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, _ = process.communicate(timeout=90)
            if process.returncode:
                raise Refused("EC2 " + operation + " failed")
            return json.loads(out)
        except subprocess.TimeoutExpired:
            raise Refused("EC2 " + operation + " timed out") from None
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    @staticmethod
    def instance_id(config):
        ident = Client.config_uuid(config)
        if not ident:
            return None
        value = "\0".join(("vws-pve-instance-v1", "pve-test", ident))
        return "i-" + hashlib.sha256(value.encode()).hexdigest()[:17]

    def create(self, token):
        tags = [{"Key": "vws-live-run", "Value": self.a.run_id}, {"Key": "live-long", "Value": "x" * 128}]
        args = dict(ImageId=os.environ.get("VWS_VERIFY_IMAGE_ID", ""), InstanceType="vws.small",
                    MinCount=1, MaxCount=1, ClientToken=token,
                    KeyName=os.environ.get("VWS_VERIFY_KEY_NAME", ""),
                    UserData="I2Nsb3VkLWNvbmZpZwo=", TagSpecifications=[dict(ResourceType="instance", Tags=tags)])
        if os.environ.get("VWS_VERIFY_SUBNET_ID"):
            args["SubnetId"] = os.environ["VWS_VERIFY_SUBNET_ID"]
        ident = self.ec2("run-instances", **args)["Instances"][0]["InstanceId"]
        if not re.fullmatch(r"i-[0-9a-f]{17}", ident):
            raise Refused("invalid instance ID")
        self.instances.add(ident)
        self.until(lambda: self.verify_instance(ident, tags), "launch did not become visible")
        return ident

    def remember(self, ident):
        manifest = Path(self.a.results) / ".run-vms.json"
        known = set(json.loads(manifest.read_text())) if manifest.exists() else set()
        manifest.write_text(json.dumps(sorted(known | {ident})))

    def until(self, action, message):
        deadline = time.monotonic() + 120
        while not action():
            if time.monotonic() + 1 >= deadline:
                raise Refused(message)
            time.sleep(1)

    def verify_instance(self, ident, tags):
        data = self.ec2("describe-instances", InstanceIds=[ident])
        instances = [i for r in data["Reservations"] for i in r["Instances"]]
        if not instances or instances[0]["State"]["Name"] == "pending":
            return False
        instance = instances[0]
        ipaddress.IPv4Address(instance["PrivateIpAddress"])
        if any(t not in instance["Tags"] for t in tags):
            raise Refused("DescribeInstances tags mismatch")
        for v in self.resources():
            if self.candidate(v):
                config = self.request(self.vm_path(v) + "/config")
                if self.instance_id(config) == ident:
                    self.remember(self.config_uuid(config))
                    if "vws.live." + self.a.run_id not in config.get("tags", "").split(";"):
                        raise Refused("PVE launch tag missing")
                    return True
        return False

    def run_vms(self):
        found = []
        for v in self.resources():
            if self.candidate(v):
                config = self.request(self.vm_path(v) + "/config")
                if ("vws.live." + self.a.run_id in config.get("tags", "").split(";")
                        or self.instance_id(config) in self.instances):
                    found.append(v)
        return found

    def live_step(self):
        try:
            token = self.a.run_id + "-" + self.a.operation
            first = self.create(token)
            before = {v["vmid"] for v in self.resources() if self.candidate(v)}
            if self.a.operation == "l2":
                if self.create(token) != first or before != {
                        v["vmid"] for v in self.resources() if self.candidate(v)}:
                    raise Refused("ClientToken retry created a second VM")
            else:
                second = self.create(token + "-second")
                if first == second or len(self.run_vms()) != 2:
                    raise Refused("L7 requires two distinct VMs")
            if self.a.fail_at == self.a.operation:
                raise Refused("injected step failure")
            if self.a.operation == "l7":
                self.ec2("terminate-instances", InstanceIds=sorted(self.instances))
                self.until(lambda: not self.run_vms(), "terminated VMs still exist")
                data = self.ec2("describe-instances")
                if any(i["InstanceId"] in self.instances and i["State"]["Name"] != "terminated"
                       for r in data["Reservations"] for i in r["Instances"]):
                    raise Refused("terminated instances still listed")
            self.emit(self.a.operation, True)
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            for v in self.run_vms():
                self.item("cleanup/" + str(v["vmid"]), lambda v=v: self.delete_vm(v))

    def final(self):
        for v in self.resources():
            if v.get("type") != "qemu" or v.get("pool") != "vws-live":
                continue
            if not v.get("template"):
                config = self.request(self.vm_path(v) + "/config")
                if "vws.holder" not in v.get("tags", "").split(";"):
                    raise Refused("pool contains a non-holder VM")
                if any(re.fullmatch(r"(?:unused|scsi|virtio|sata|ide)[0-9]+", k) for k in config):
                    raise Refused("holder contains leftover volumes")
            if any(s["name"] != "current" for s in self.request(self.vm_path(v) + "/snapshot")):
                raise Refused("pool contains snapshots")
        manifest = Path(self.a.results) / ".run-vms.json"
        known = json.loads(manifest.read_text()) if manifest.exists() else []
        snippets = self.request("/nodes/" + quote(self.node, safe="") + "/storage/" +
                                quote(self.a.snippets_storage, safe="") + "/content?content=snippets")
        if any(s["volid"] == self.a.snippets_storage + ":snippets/vws-" + ident + "-user.yaml"
               for s in snippets for ident in known):
            raise Refused("run snippets remain")
        self.emit("final", True)


def main():
    p = Parser(allow_abbrev=False)
    p.add_argument("operation", choices=("gate", "identity", "sweep", "slots", "l2", "l7", "final", "results"))
    p.add_argument("--region", required=True)
    p.add_argument("--results")
    p.add_argument("--snippets-storage", default="local")
    p.add_argument("--pve-ca-file")
    p.add_argument("--run-id", default=uuid.uuid4().hex)
    p.add_argument("--fail-at", choices=("slots", "l2", "l7"))
    p.add_argument("--phase", choices=("l0-start", "l0-end"))
    p.add_argument("--need", type=int, default=0)
    p.add_argument("--exit", type=int, default=0)
    a = p.parse_args()
    if (a.need < 0 or a.operation == "sweep" and not a.phase
            or a.operation != "identity" and not a.results):
        p.error("")
    if a.operation == "results":
        steps = []
        for name in ("l0-start", "slots", "l2", "l7", "l0-end", "final"):
            path = Path(a.results) / (name + ".json")
            if path.exists():
                data = json.loads(path.read_text())
                steps.append(dict(name=name, result=data["result"], steps=data["steps"]))
        result = "fail" if a.exit or any(s["result"] == "fail" for s in steps) else "pass"
        (Path(a.results) / "results.json").write_text(json.dumps(dict(result=result, steps=steps)) + "\n")
        return int(result == "fail" and not a.exit)
    c = Client(a)
    try:
        if a.operation in ("l2", "l7"):
            c.live_step()
        else:
            getattr(c, a.operation)()
    except Exception as e:
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
            c.save(a.phase if a.operation == "sweep" else a.operation)
    return (3 if a.operation == "gate" else 1) if any(s["result"] == "fail" for s in c.steps) else 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        sys.exit(main())
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError):
        fail("nightly-live: invalid environment or API data", 3)
