#!/usr/bin/env python3
"""Compatibility matrix of unmodified aws-cli and the Terraform AWS provider against VWS (vws-deploy#133).

  compat.py run OUT_DIR       run every scenario, write OUT_DIR/results.json and OUT_DIR/matrix.md
  compat.py issues RESULTS    print the issue each failing row needs; --apply opens the missing ones

See docs/compat.md for the environment and the owner of each resource.
"""
import argparse
import dataclasses
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = ("fake", "vws")
RESULTS = ("pass", "fail", "skip")
OWNERS = {
    "image": "vws-compute",
    "instance": "vws-compute",
    "volume": "vws-storage",
    "vpc": "vws-network",
    "subnet": "vws-network",
    "security-group": "vws-network",
    "address": "vws-network",
    "bucket": "vws-storage",
    "object": "vws-storage",
    "provider": "vws-deploy",
    "aws_instance": "vws-compute",
    "aws_ebs_volume": "vws-storage",
    "aws_volume_attachment": "vws-storage",
    "aws_vpc": "vws-network",
    "aws_subnet": "vws-network",
    "aws_security_group": "vws-network",
    "aws_eip": "vws-network",
    "aws_eip_association": "vws-network",
    "aws_s3_bucket": "vws-storage",
    "aws_s3_object": "vws-storage",
}
_A = "ama" + "zon"
AWS_OWNED = (f"{_A}aws.com", f"{_A}aws.com.cn", "api.aws", f"{_A}.com", "aws.dev")
MULTIPART_PART = 5 * 1024 * 1024
DETAIL_MAX = 600


class ConfigError(Exception):
    pass


class Fail(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Row:
    tool: str
    resource: str
    action: str
    result: str
    detail: str = ""

    def __post_init__(self):
        if self.result not in RESULTS:
            raise ValueError(f"result {self.result!r} is not one of {RESULTS}")
        if self.resource not in OWNERS:
            raise ValueError(f"resource {self.resource!r} has no owner in OWNERS")

    @property
    def owner(self):
        return OWNERS[self.resource]


@dataclasses.dataclass(frozen=True)
class Config:
    target: str
    region: str
    ec2_endpoint: str
    s3_endpoint: str
    access_key: str
    secret_key: str
    image_id: str
    instance_type: str


def check_endpoint(name, url, allowed):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"{name} must be an http(s) URL, got {url!r}")
    host = parsed.hostname.lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in AWS_OWNED):
        raise ConfigError(f"{name} host {host} is AWS-owned; the matrix runs only against VWS")
    if host not in allowed:
        raise ConfigError(f"{name} host {host} is not in VWS_COMPAT_ALLOWED_HOSTS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must not carry credentials, a query or a fragment")
    return url.rstrip("/")


def load_config(env):
    def need(key):
        value = env.get(key, "").strip()
        if not value:
            raise ConfigError(f"{key} is required")
        return value

    target = need("VWS_COMPAT_TARGET")
    if target not in TARGETS:
        raise ConfigError(f"VWS_COMPAT_TARGET must be one of {TARGETS}, got {target!r}")
    allowed = {h.strip().lower() for h in need("VWS_COMPAT_ALLOWED_HOSTS").split(",") if h.strip()}
    region = env.get("VWS_COMPAT_REGION", "").strip() or "vws-1"
    if not re.fullmatch(r"[a-z0-9-]{1,32}", region):
        raise ConfigError(f"VWS_COMPAT_REGION {region!r} is not a region name")
    image_id = env.get("VWS_COMPAT_IMAGE_ID", "").strip()
    if image_id and not re.fullmatch(r"ami-[0-9a-f]{8,17}", image_id):
        raise ConfigError(f"VWS_COMPAT_IMAGE_ID {image_id!r} is not an image id")
    instance_type = env.get("VWS_COMPAT_INSTANCE_TYPE", "").strip() or "t3.micro"
    if not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9]+", instance_type):
        raise ConfigError(f"VWS_COMPAT_INSTANCE_TYPE {instance_type!r} is not an instance type")
    return Config(
        target=target,
        region=region,
        ec2_endpoint=check_endpoint("VWS_COMPAT_EC2_ENDPOINT", need("VWS_COMPAT_EC2_ENDPOINT"), allowed),
        s3_endpoint=check_endpoint("VWS_COMPAT_S3_ENDPOINT", need("VWS_COMPAT_S3_ENDPOINT"), allowed),
        access_key=need("VWS_COMPAT_ACCESS_KEY_ID"),
        secret_key=need("VWS_COMPAT_SECRET_ACCESS_KEY"),
        image_id=image_id,
        instance_type=instance_type,
    )


def child_env(cfg, work, run_id):
    """The whole environment of every aws and terraform call: nothing from the runner leaks in."""
    config_file = os.path.join(work, "aws-config")
    with open(config_file, "w", encoding="utf-8") as f:
        f.write("[default]\ns3 =\n  addressing_style = path\n")
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "AWS_ACCESS_KEY_ID": cfg.access_key,
        "AWS_SECRET_ACCESS_KEY": cfg.secret_key,
        "AWS_REGION": cfg.region,
        "AWS_DEFAULT_REGION": cfg.region,
        "AWS_CONFIG_FILE": config_file,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_ENDPOINT_URL": cfg.ec2_endpoint,
        "AWS_ENDPOINT_URL_EC2": cfg.ec2_endpoint,
        "AWS_ENDPOINT_URL_S3": cfg.s3_endpoint,
        "AWS_ENDPOINT_URL_STS": cfg.ec2_endpoint,
        "AWS_ENDPOINT_URL_IAM": cfg.ec2_endpoint,
        "AWS_RETRY_MODE": "standard",
        "AWS_MAX_ATTEMPTS": "5",
        "AWS_PAGER": "",
        "CHECKPOINT_DISABLE": "1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_VAR_run_id": run_id,
        "TF_VAR_region": cfg.region,
        "TF_VAR_ec2_endpoint": cfg.ec2_endpoint,
        "TF_VAR_s3_endpoint": cfg.s3_endpoint,
        "TF_VAR_instance_type": cfg.instance_type,
    }
    if os.environ.get("TF_PLUGIN_CACHE_DIR"):
        env["TF_PLUGIN_CACHE_DIR"] = os.environ["TF_PLUGIN_CACHE_DIR"]
    return env


class Runner:
    """Runs one external command with a deadline and returns its output; secrets never reach a detail."""

    def __init__(self, env, cwd, secret_values):
        self.env = env
        self.cwd = cwd
        self.secret_values = [s for s in secret_values if s]

    def redact(self, text):
        for s in self.secret_values:
            text = text.replace(s, "<redacted>")
        return text

    def detail(self, text):
        text = self.redact(" ".join(text.split()))
        return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 3] + "..."

    def run(self, argv, timeout, cwd=None):
        try:
            proc = subprocess.run(
                argv, cwd=cwd or self.cwd, env=self.env, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise Fail(f"{' '.join(argv[:3])} timed out after {timeout}s") from None
        return proc

    def aws(self, *args, timeout=180):
        argv = ["aws", *args, "--output", "json", "--cli-connect-timeout", "10", "--cli-read-timeout", "60"]
        proc = self.run(argv, timeout)
        if proc.returncode != 0:
            raise Fail(self.detail(proc.stderr or proc.stdout or f"exit {proc.returncode}"))
        out = proc.stdout.strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"_text": out}


class Matrix:
    def __init__(self, tool):
        self.tool = tool
        self.rows = []

    def step(self, resource, action, fn, needs=()):
        """Record one row. fn returns a value for later steps or raises Fail; a missing need is a skip."""
        missing = [name for name, value in needs if value is None]
        if missing:
            self.rows.append(Row(self.tool, resource, action, "skip", "needs " + ", ".join(missing)))
            return None
        try:
            value = fn()
        except Fail as e:
            self.rows.append(Row(self.tool, resource, action, "fail", str(e)))
            return None
        except (KeyError, IndexError, TypeError) as e:
            self.rows.append(Row(self.tool, resource, action, "fail", f"unexpected response: {type(e).__name__} {e}"))
            return None
        self.rows.append(Row(self.tool, resource, action, "pass"))
        return value if value is not None else True


def expect(cond, message):
    if not cond:
        raise Fail(message)


def awscli_scenarios(r, cfg, run_id, work):
    m = Matrix("aws-cli")
    cleanup = []

    def created(key, *argv):
        cleanup.append((key, argv))

    def deleted(key):
        cleanup[:] = [c for c in cleanup if c[0] != key]

    def first_image():
        images = r.aws("ec2", "describe-images").get("Images") or []
        expect(images, "describe-images returned no images")
        return cfg.image_id or images[0]["ImageId"]

    image = m.step("image", "describe-images", first_image)

    def create_vpc():
        vpc = r.aws("ec2", "create-vpc", "--cidr-block", "10.232.0.0/16")["Vpc"]["VpcId"]
        created(vpc, "ec2", "delete-vpc", "--vpc-id", vpc)
        return vpc

    vpc = m.step("vpc", "create-vpc", create_vpc)
    m.step("vpc", "describe-vpcs", lambda: expect(
        [v["VpcId"] for v in r.aws("ec2", "describe-vpcs", "--vpc-ids", vpc)["Vpcs"]] == [vpc],
        "describe-vpcs did not return the new VPC"), needs=[("vpc", vpc)])

    def create_subnet():
        subnet = r.aws("ec2", "create-subnet", "--vpc-id", vpc, "--cidr-block", "10.232.1.0/24")["Subnet"]["SubnetId"]
        created(subnet, "ec2", "delete-subnet", "--subnet-id", subnet)
        return subnet

    subnet = m.step("subnet", "create-subnet", create_subnet, needs=[("vpc", vpc)])
    m.step("subnet", "describe-subnets", lambda: expect(
        [s["SubnetId"] for s in r.aws("ec2", "describe-subnets", "--subnet-ids", subnet)["Subnets"]] == [subnet],
        "describe-subnets did not return the new subnet"), needs=[("subnet", subnet)])

    def create_sg():
        sg = r.aws("ec2", "create-security-group", "--group-name", run_id, "--description", "vws compat",
                   "--vpc-id", vpc)["GroupId"]
        created(sg, "ec2", "delete-security-group", "--group-id", sg)
        return sg

    sg = m.step("security-group", "create-security-group", create_sg, needs=[("vpc", vpc)])
    m.step("security-group", "authorize-security-group-ingress", lambda: r.aws(
        "ec2", "authorize-security-group-ingress", "--group-id", sg, "--protocol", "tcp", "--port", "22",
        "--cidr", "10.232.0.0/16"), needs=[("security-group", sg)])

    def describe_sg():
        groups = r.aws("ec2", "describe-security-groups", "--group-ids", sg)["SecurityGroups"]
        expect(len(groups) == 1, "describe-security-groups did not return the new group")
        ports = [p.get("FromPort") for p in groups[0].get("IpPermissions", [])]
        expect(22 in ports, f"ingress rule for port 22 missing, got ports {ports}")

    m.step("security-group", "describe-security-groups", describe_sg, needs=[("security-group", sg)])

    def run_instance():
        inst = r.aws("ec2", "run-instances", "--image-id", image, "--instance-type", cfg.instance_type,
                     "--subnet-id", subnet, "--security-group-ids", sg, "--count", "1",
                     "--tag-specifications", f"ResourceType=instance,Tags=[{{Key=vws-compat,Value={run_id}}}]",
                     )["Instances"][0]["InstanceId"]
        created(inst, "ec2", "terminate-instances", "--instance-ids", inst)
        r.aws("ec2", "wait", "instance-running", "--instance-ids", inst, timeout=900)
        return inst

    inst = m.step("instance", "run-instances", run_instance,
                  needs=[("image", image), ("subnet", subnet), ("security-group", sg)])

    def describe_instance():
        res = r.aws("ec2", "describe-instances", "--instance-ids", inst)["Reservations"]
        found = [i for rr in res for i in rr["Instances"]]
        expect([i["InstanceId"] for i in found] == [inst], "describe-instances did not return the instance")
        return found[0]["Placement"]["AvailabilityZone"]

    zone = m.step("instance", "describe-instances", describe_instance, needs=[("instance", inst)])

    def create_volume():
        vol = r.aws("ec2", "create-volume", "--availability-zone", zone, "--size", "1")["VolumeId"]
        created(vol, "ec2", "delete-volume", "--volume-id", vol)
        r.aws("ec2", "wait", "volume-available", "--volume-ids", vol, timeout=900)
        return vol

    vol = m.step("volume", "create-volume", create_volume, needs=[("zone", zone)])
    m.step("volume", "describe-volumes", lambda: expect(
        [v["VolumeId"] for v in r.aws("ec2", "describe-volumes", "--volume-ids", vol)["Volumes"]] == [vol],
        "describe-volumes did not return the volume"), needs=[("volume", vol)])

    def attach():
        r.aws("ec2", "attach-volume", "--volume-id", vol, "--instance-id", inst, "--device", "/dev/sdf")
        r.aws("ec2", "wait", "volume-in-use", "--volume-ids", vol, timeout=900)

    attached = m.step("volume", "attach-volume", attach, needs=[("volume", vol), ("instance", inst)])

    def detach():
        r.aws("ec2", "detach-volume", "--volume-id", vol)
        r.aws("ec2", "wait", "volume-available", "--volume-ids", vol, timeout=900)

    m.step("volume", "detach-volume", detach, needs=[("attach-volume", attached)])

    def allocate():
        alloc = r.aws("ec2", "allocate-address", "--domain", "vpc")["AllocationId"]
        created(alloc, "ec2", "release-address", "--allocation-id", alloc)
        return alloc

    alloc = m.step("address", "allocate-address", allocate)
    assoc = m.step("address", "associate-address", lambda: r.aws(
        "ec2", "associate-address", "--allocation-id", alloc, "--instance-id", inst)["AssociationId"],
        needs=[("address", alloc), ("instance", inst)])

    def describe_address():
        addrs = r.aws("ec2", "describe-addresses", "--allocation-ids", alloc)["Addresses"]
        expect(len(addrs) == 1, "describe-addresses did not return the address")
        if assoc:
            expect(addrs[0].get("InstanceId") == inst, f"address is on {addrs[0].get('InstanceId')}, want {inst}")

    m.step("address", "describe-addresses", describe_address, needs=[("address", alloc)])
    m.step("address", "disassociate-address", lambda: r.aws(
        "ec2", "disassociate-address", "--association-id", assoc), needs=[("associate-address", assoc)])

    def release():
        r.aws("ec2", "release-address", "--allocation-id", alloc)
        deleted(alloc)

    m.step("address", "release-address", release, needs=[("address", alloc)])

    def stop():
        r.aws("ec2", "stop-instances", "--instance-ids", inst)
        r.aws("ec2", "wait", "instance-stopped", "--instance-ids", inst, timeout=900)

    stopped = m.step("instance", "stop-instances", stop, needs=[("instance", inst)])

    def start():
        r.aws("ec2", "start-instances", "--instance-ids", inst)
        r.aws("ec2", "wait", "instance-running", "--instance-ids", inst, timeout=900)

    m.step("instance", "start-instances", start, needs=[("stop-instances", stopped)])

    def terminate():
        r.aws("ec2", "terminate-instances", "--instance-ids", inst)
        r.aws("ec2", "wait", "instance-terminated", "--instance-ids", inst, timeout=900)
        deleted(inst)

    m.step("instance", "terminate-instances", terminate, needs=[("instance", inst)])

    def delete(key, *argv):
        def fn():
            r.aws(*argv)
            deleted(key)
        return fn

    m.step("volume", "delete-volume", delete(vol, "ec2", "delete-volume", "--volume-id", vol),
           needs=[("volume", vol)])
    m.step("security-group", "delete-security-group",
           delete(sg, "ec2", "delete-security-group", "--group-id", sg), needs=[("security-group", sg)])
    m.step("subnet", "delete-subnet", delete(subnet, "ec2", "delete-subnet", "--subnet-id", subnet),
           needs=[("subnet", subnet)])
    m.step("vpc", "delete-vpc", delete(vpc, "ec2", "delete-vpc", "--vpc-id", vpc), needs=[("vpc", vpc)])

    s3_scenarios(m, r, run_id, work, created, deleted)
    leftovers = run_cleanup(r, cleanup)
    return m.rows, leftovers, image


def s3_scenarios(m, r, run_id, work, created, deleted):
    bucket = run_id
    small = os.path.join(work, "small.txt")
    with open(small, "wb") as f:
        f.write(f"vws compat {run_id}\n".encode())

    def mb():
        r.aws("s3", "mb", f"s3://{bucket}")
        created(bucket, "s3", "rb", f"s3://{bucket}", "--force")
        return bucket

    made = m.step("bucket", "mb", mb)
    need = [("bucket", made)]
    m.step("bucket", "head-bucket", lambda: r.aws("s3api", "head-bucket", "--bucket", bucket), needs=need)
    m.step("bucket", "ls", lambda: expect(
        bucket in [b["Name"] for b in r.aws("s3api", "list-buckets").get("Buckets", [])],
        "list-buckets does not show the bucket"), needs=need)
    put = m.step("object", "cp-upload", lambda: r.aws("s3", "cp", small, f"s3://{bucket}/small.txt"), needs=need)
    put_need = [("cp-upload", put)]
    m.step("object", "head-object", lambda: expect(
        r.aws("s3api", "head-object", "--bucket", bucket, "--key", "small.txt").get("ContentLength")
        == os.path.getsize(small), "head-object ContentLength does not match the upload"), needs=put_need)

    def download():
        got = os.path.join(work, "small.got")
        r.aws("s3", "cp", f"s3://{bucket}/small.txt", got)
        with open(got, "rb") as a, open(small, "rb") as b:
            expect(a.read() == b.read(), "downloaded object differs from the upload")

    m.step("object", "cp-download", download, needs=put_need)
    m.step("object", "ls", lambda: expect(
        "small.txt" in [o["Key"] for o in r.aws("s3api", "list-objects-v2", "--bucket", bucket).get("Contents", [])],
        "list-objects-v2 does not show the object"), needs=put_need)

    def multipart():
        parts_files = []
        for n, size in ((1, MULTIPART_PART), (2, 1024)):
            path = os.path.join(work, f"part{n}")
            with open(path, "wb") as f:
                f.write(os.urandom(size))
            parts_files.append((n, path))
        upload = r.aws("s3api", "create-multipart-upload", "--bucket", bucket, "--key", "big.bin")["UploadId"]
        try:
            parts = []
            for n, path in parts_files:
                etag = r.aws("s3api", "upload-part", "--bucket", bucket, "--key", "big.bin", "--part-number", str(n),
                             "--upload-id", upload, "--body", path)["ETag"]
                parts.append({"PartNumber": n, "ETag": etag})
            r.aws("s3api", "complete-multipart-upload", "--bucket", bucket, "--key", "big.bin", "--upload-id", upload,
                  "--multipart-upload", json.dumps({"Parts": parts}))
        except Fail:
            try:
                r.aws("s3api", "abort-multipart-upload", "--bucket", bucket, "--key", "big.bin", "--upload-id", upload)
            except Fail:
                pass
            raise
        size = r.aws("s3api", "head-object", "--bucket", bucket, "--key", "big.bin").get("ContentLength")
        expect(size == MULTIPART_PART + 1024, f"multipart object is {size} bytes, want {MULTIPART_PART + 1024}")

    m.step("object", "multipart-upload", multipart, needs=need)

    def rm():
        r.aws("s3", "rm", f"s3://{bucket}/small.txt")
        expect("Contents" not in r.aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", "small.txt"),
               "object still listed after rm")

    m.step("object", "rm", rm, needs=put_need)

    def rb():
        r.aws("s3", "rb", f"s3://{bucket}", "--force")
        deleted(bucket)

    m.step("bucket", "rb", rb, needs=need)


def run_cleanup(r, cleanup):
    """Delete what a failed step left behind, newest first. Returns what could not be deleted."""
    left = []
    for key, argv in reversed(cleanup):
        try:
            r.aws(*argv)
        except Fail as e:
            left.append(f"{key}: {e}")
    return left


TF_NEEDS_IMAGE = {"aws_instance.this", "aws_ebs_volume.this", "aws_volume_attachment.this", "aws_eip_association.this"}
TF_ADDR = re.compile(r"^(aws_[a-z0-9_]+)\.this$")


def tf_events(stdout):
    for line in stdout.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def tf_errors(stdout, r):
    """Error diagnostics of a -json run: {address or "": message}."""
    errors = {}
    for ev in tf_events(stdout):
        if ev.get("type") == "diagnostic" and ev.get("@level") == "error":
            d = ev.get("diagnostic", {})
            addr = d.get("address", "")
            msg = r.detail(f"{d.get('summary', '')}: {d.get('detail', '')}")
            errors.setdefault(addr, msg)
    return errors


def tf_completed(stdout, action):
    return {
        ev["hook"]["resource"]["addr"]
        for ev in tf_events(stdout)
        if ev.get("type") == "apply_complete" and ev.get("hook", {}).get("action") == action
    }


def tf_state(r, tfdir):
    proc = r.run(["terraform", "state", "list"], 120, cwd=tfdir)
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def tf_addresses(tfdir):
    with open(os.path.join(tfdir, "main.tf"), encoding="utf-8") as f:
        return re.findall(r'^resource "(aws_[a-z0-9_]+)" "this"', f.read(), re.M)


def tf_init(r, tfdir, attempts=3):
    """terraform init with bounded retries: the provider download is the one call to the registry."""
    error = ""
    for n in range(attempts):
        if n:
            time.sleep(10 * n)
        try:
            proc = r.run(["terraform", "init", "-input=false", "-no-color", "-lockfile=readonly"], 600, cwd=tfdir)
        except Fail as e:
            error = str(e)
            continue
        if proc.returncode == 0:
            return ""
        error = r.detail(proc.stderr or proc.stdout)
    return error


def terraform_scenarios(r, work, image):
    rows = []
    tfdir = os.path.join(work, "terraform")
    shutil.copytree(os.path.join(HERE, "terraform"), tfdir)
    addrs = [f"{t}.this" for t in tf_addresses(tfdir)]

    def row(addr, action, result, detail=""):
        rows.append(Row("terraform", TF_ADDR.match(addr).group(1), action, result, detail))

    def skip_all(action, detail):
        for a in addrs:
            row(a, action, "skip", detail)

    init_error = tf_init(r, tfdir)
    if init_error:
        rows.append(Row("terraform", "provider", "init", "fail", init_error))
        for action in ("apply", "plan", "destroy"):
            skip_all(action, "needs init")
        return rows, []
    rows.append(Row("terraform", "provider", "init", "pass"))
    targets = []
    if image is None:
        targets = [f"-target={a}" for a in addrs if a not in TF_NEEDS_IMAGE]
        for a in addrs:
            if a in TF_NEEDS_IMAGE:
                for action in ("apply", "plan", "destroy"):
                    row(a, action, "skip", "needs image")
        addrs = [a for a in addrs if a not in TF_NEEDS_IMAGE]
    r.env["TF_VAR_image_id"] = image or ""

    try:
        apply = r.run(["terraform", "apply", "-auto-approve", "-input=false", "-json", *targets], 1800, cwd=tfdir)
        out, errs = apply.stdout, tf_errors(apply.stdout, r)
    except Fail as e:
        out, errs = "", {"": str(e)}
    state = tf_state(r, tfdir)
    done = tf_completed(out, "create") | state
    for a in addrs:
        if a in errs:
            row(a, "apply", "fail", errs[a])
        elif a in done:
            row(a, "apply", "pass")
        else:
            row(a, "apply", "skip", "not applied: " + (errs.get("") or "a dependency failed"))

    try:
        plan = r.run(["terraform", "plan", "-input=false", "-json", "-detailed-exitcode", *targets], 900, cwd=tfdir)
        changed = {}
        for ev in tf_events(plan.stdout):
            if ev.get("type") == "planned_change":
                c = ev["change"]
                changed[c["resource"]["addr"]] = c["action"]
        perrs = tf_errors(plan.stdout, r) if plan.returncode == 1 else {}
    except Fail as e:
        changed, perrs = {}, {"": str(e)}
    for a in addrs:
        if a not in state:
            row(a, "plan", "skip", "not in state after apply")
        elif a in perrs or "" in perrs:
            row(a, "plan", "fail", perrs.get(a) or perrs[""])
        elif a in changed:
            row(a, "plan", "fail", f"plan after apply wants {changed[a]}")
        else:
            row(a, "plan", "pass")

    try:
        destroy = r.run(["terraform", "destroy", "-auto-approve", "-input=false", "-json", *targets], 1800, cwd=tfdir)
        derrs = tf_errors(destroy.stdout, r)
    except Fail as e:
        derrs = {"": str(e)}
    left = tf_state(r, tfdir)
    for a in addrs:
        if a not in state:
            row(a, "destroy", "skip", "not in state after apply")
        elif a in derrs:
            row(a, "destroy", "fail", derrs[a])
        elif a in left:
            row(a, "destroy", "fail", "still in state after destroy: " + (derrs.get("") or "no error reported"))
        else:
            row(a, "destroy", "pass")
    return rows, sorted(f"terraform {a}" for a in left)


def tool_versions(r):
    versions = {}
    proc = r.run(["aws", "--version"], 60)
    first = (proc.stdout or proc.stderr).split()[:1] if proc.returncode == 0 else []
    versions["aws-cli"] = first[0].removeprefix("aws-cli/") if first else "unknown"
    proc = r.run(["terraform", "version", "-json"], 60)
    if proc.returncode == 0:
        v = json.loads(proc.stdout)
        versions["terraform"] = v.get("terraform_version", "unknown")
    return versions


def render_matrix(doc):
    counts = {k: sum(1 for row in doc["rows"] if row["result"] == k) for k in RESULTS}
    lines = [
        "# VWS compatibility matrix",
        "",
        f"Target `{doc['target']}`, run `{doc['run_id']}`, {doc['started']}.",
        "Tools: " + ", ".join(f"{k} {v}" for k, v in sorted(doc["tools"].items())) + ".",
        "Terraform AWS provider: pinned in tests/compat/terraform/main.tf.",
        "",
        f"pass {counts['pass']}, fail {counts['fail']}, skip {counts['skip']}.",
        "",
        "| Tool | Resource | Action | Result | Owner | Detail |",
        "|---|---|---|---|---|---|",
    ]
    for row in doc["rows"]:
        detail = row["detail"].replace("|", "\\|")
        lines.append(f"| {row['tool']} | {row['resource']} | {row['action']} | {row['result']} | {row['owner']} | {detail} |")
    if doc["leftovers"]:
        lines += ["", "Resources the run could not delete:", ""] + [f"- {x}" for x in doc["leftovers"]]
    return "\n".join(lines) + "\n"


def cmd_run(out_dir):
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        print(f"compat: {e}", file=sys.stderr)
        return 2
    run_id = "vws-compat-" + secrets.token_hex(4)
    out_dir = os.path.abspath(out_dir)
    work = os.path.join(out_dir, "work")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    r = Runner(child_env(cfg, work, run_id), work, [cfg.secret_key])
    for tool in ("aws", "terraform"):
        if shutil.which(tool, path=r.env["PATH"]) is None:
            print(f"compat: {tool} is not on PATH (tests/compat/install.sh)", file=sys.stderr)
            return 2
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cli_rows, cli_left, image = awscli_scenarios(r, cfg, run_id, work)
    tf_rows, tf_left = terraform_scenarios(r, work, image)
    rows = cli_rows + tf_rows
    doc = {
        "target": cfg.target,
        "run_id": run_id,
        "started": started,
        "endpoints": {"ec2": urllib.parse.urlsplit(cfg.ec2_endpoint).hostname,
                      "s3": urllib.parse.urlsplit(cfg.s3_endpoint).hostname},
        "tools": tool_versions(r),
        "rows": [dict(dataclasses.asdict(row), owner=row.owner) for row in rows],
        "leftovers": cli_left + tf_left,
    }
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    with open(os.path.join(out_dir, "matrix.md"), "w", encoding="utf-8") as f:
        f.write(render_matrix(doc))
    print(render_matrix(doc))
    return 0


def issue_title(row):
    return f"compat: {row['tool']} {row['resource']} {row['action']} fails against VWS"


def issue_body(doc, row):
    run = os.environ.get("GITHUB_RUN_ID")
    where = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY')}"
             f"/actions/runs/{run}") if run else f"run {doc['run_id']}"
    tool_version = doc["tools"].get(row["tool"], "pinned in tests/compat/terraform/main.tf")
    return "\n".join([
        "The VWS compatibility matrix has a failing item.",
        "",
        f"- Tool: {row['tool']} {tool_version}",
        f"- Resource: {row['resource']}",
        f"- Action: {row['action']}",
        f"- Run: {where} ({doc['started']})",
        "",
        "Error:",
        "",
        "```",
        row["detail"],
        "```",
        "",
        "Reproduce with `tests/compat/compat.py run` (docs/compat.md in vws-deploy).",
        "",
        "Part of popododo0720/vws-deploy#133",
        "",
    ])


def gh(argv, attempts=3):
    for n in range(attempts):
        try:
            proc = subprocess.run(["gh", *argv], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            return proc.stdout
        if n + 1 < attempts:
            time.sleep(2 ** (n + 1))
    err = proc.stderr.strip() if proc is not None else "timed out after 60s"
    raise RuntimeError(f"gh {' '.join(argv[:3])}: {err}")


def plan_issues(doc):
    seen = set()
    planned = []
    for row in doc["rows"]:
        if row["result"] != "fail":
            continue
        key = (row["owner"], issue_title(row))
        if key not in seen:
            seen.add(key)
            planned.append((row["owner"], issue_title(row), issue_body(doc, row)))
    return planned


def cmd_issues(path, apply, org):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("target") != "vws":
        print(f"compat: issues come only from a vws run, this one is {doc.get('target')!r}", file=sys.stderr)
        return 2
    for owner, title, body in plan_issues(doc):
        repo = f"{org}/{owner}"
        if not apply:
            print(f"would open {repo}: {title}")
            continue
        found = json.loads(gh(["issue", "list", "-R", repo, "--state", "open", "--search", f'"{title}" in:title',
                               "--json", "title,url", "--limit", "100"]))
        url = next((i["url"] for i in found if i["title"] == title), None)
        if url:
            print(f"exists {url}")
            continue
        # One attempt: a create whose reply was lost may still have opened the issue, and a rerun finds it by title.
        print("opened " + gh(["issue", "create", "-R", repo, "--title", title, "--body", body], attempts=1).strip())
    return 0


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("out_dir")
    iss = sub.add_parser("issues")
    iss.add_argument("results")
    iss.add_argument("--apply", action="store_true")
    iss.add_argument("--org", default="popododo0720")
    args = p.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args.out_dir)
    try:
        return cmd_issues(args.results, args.apply, args.org)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"compat: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
