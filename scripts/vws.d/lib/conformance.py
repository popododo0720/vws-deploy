"""External pve-test runner; credentials stay in the child environment."""

import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit


_A = "ama" + "zon"
AWS_OWNED = (f"{_A}aws.com", f"{_A}aws.com.cn", "api.aws", "aws.dev", f"{_A}.com")
# vws-conformance eafa1698a00b5b8b3009d11992b14869dfd0f4ec, internal/engine/read_only.go.
# Operation names cannot detect Describe cases that create fixtures.
READ_ONLY = set("""
EC2-DI-101 EC2-DI-102 EC2-DIA-101 EC2-DIMG-001 EC2-DIMG-101
EC2-KP-103 EC2-EIP-103 EC2-GAP-003 EC2-GAP-004 EC2-GAP-008
EBS-VOL-104 EBS-SNAP-102 EBS-GAP-002
VPC-VPC-104 VPC-SUB-103 VPC-SG-104 VPC-GAP-004 VPC-GAP-005 VPC-GAP-007
AUTH-001 AUTH-002 AUTH-003 AUTH-004 AUTH-005 AUTH-006 AUTH-007
AUTH-008 AUTH-011 AUTH-013
""".split())


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def environment():
    env = os.environ.copy()
    for name, credential in (("ACCESS_KEY_ID", "conf-access-key-id"),
                             ("SECRET_ACCESS_KEY", "conf-secret-access-key")):
        key = "VWS_CONF_" + name
        if not env.get(key) and env.get("CREDENTIALS_DIRECTORY"):
            env[key] = (Path(env["CREDENTIALS_DIRECTORY"]) / credential).read_text().strip()
    for name in ("ALLOWED_HOSTS", "EC2_ENDPOINT", "IAM_ENDPOINT", "REGION",
                 "ACCOUNT_ID", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        if not env.get("VWS_CONF_" + name):
            raise ValueError("needs VWS_CONF_" + name)
    allowed = {h.strip().lower().rstrip(".") for h in env["VWS_CONF_ALLOWED_HOSTS"].split(",")}
    for name in ("EC2_ENDPOINT", "IAM_ENDPOINT", "S3_ENDPOINT"):
        value = env.get("VWS_CONF_" + name)
        if not value:
            continue
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if (url.scheme not in ("http", "https") or not host or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError("invalid VWS_CONF_" + name)
        if any(host == suffix or host.endswith("." + suffix) for suffix in AWS_OWNED):
            raise ValueError("AWS-owned VWS_CONF_" + name + " refused")
        if host not in allowed:
            raise ValueError("VWS_CONF_" + name + " host outside VWS_CONF_ALLOWED_HOSTS")
    env["VWS_CONF_TARGET"] = "pve-test"
    env["VWS_CONF_ALLOWED_ACCOUNTS"] = env["VWS_CONF_ACCOUNT_ID"]
    return env


def check_reports(reports):
    files = list(reports.glob("*/report.json"))
    if not files:
        raise ValueError("no conformance report; zero creating cases unverified")
    count = 0
    for file in files:
        data = json.loads(file.read_text())
        if data["target"]["name"] != "pve-test" or not data["results"]:
            raise ValueError("report must contain pve-test cases")
        for row in data["results"]:
            if row["id"] not in READ_ONLY:
                raise ValueError("creating or unknown case in conformance report")
            count += 1
    return count


class Interrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def interrupt(signum, _frame):
    raise Interrupted(signum)


def stop(process):
    if process is None:
        return
    # The Go launcher and suite share this group; stopping only the launcher leaves the suite alive.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def archive(results, secrets):
    for file in results.rglob("*"):
        if file.is_symlink():
            raise ValueError("symlink in conformance output refused")
        if file.is_file():
            content = file.read_bytes()
            for secret in secrets:
                content = content.replace(secret, b"[redacted]")
            file.write_bytes(content)
    with tarfile.open(results / "reports.tar.gz", "w:gz") as out:
        out.add(results / "reports", arcname="reports")


def main():
    checkout, directory, limit = sys.argv[1:]
    try:
        env = environment()
        script = Path(checkout).resolve() / "scripts/pve-test.sh"
        if not checkout or not script.is_file():
            raise ValueError("needs a reviewed conformance checkout (--conformance or VWS_CONF_DIR)")
        results = Path(directory).resolve()
        os.umask(0o077)
        results.mkdir(parents=True)
    except (OSError, ValueError):
        print("FAIL  conformance  runner configuration refused; check checkout, endpoints, credentials and unused results path",
              file=sys.stderr)
        return 3
    reports = results / "reports"
    reports.mkdir()
    env["VWS_CONF_OUT"] = str(reports)
    values = [v for k, v in env.items() if v and any(
        word in k for word in ("SECRET", "TOKEN", "PASSWORD", "ACCESS_KEY", "PRIVATE"))]
    secrets = sorted({encoded for value in values for encoded in (
        value.encode(), json.dumps(value)[1:-1].encode())}, key=len, reverse=True)
    started, process, rc, count = utc(), None, 0, None
    message = "zero creating cases"
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        with (results / "run.log").open("wb") as log:
            process = subprocess.Popen(["sh", str(script)], env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
            rc = process.wait(timeout=int(limit))
            if rc < 0:
                rc = 128 - rc
    except subprocess.TimeoutExpired:
        rc, message = 124, "conformance timed out"
    except Interrupted as error:
        rc, message = 128 + error.signum, "conformance interrupted"
    except OSError:
        rc, message = 3, "cannot start conformance"
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop(process)
        try:
            count = check_reports(reports)
        except (OSError, ValueError, KeyError, TypeError):
            message = "creating or unknown case, invalid or missing conformance report; zero creating cases unverified"
            rc = rc or 1
        try:
            (results / "summary.json").write_text(json.dumps({
                "target": "pve-test", "exit": rc, "startedAt": started, "finishedAt": utc(),
                "readOnlyCases": count, "creatingCases": 0 if count is not None else None,
                "message": message,
            }) + "\n")
            archive(results, secrets)
        except (OSError, ValueError, tarfile.TarError):
            print("FAIL  conformance  cannot archive reports", file=sys.stderr)
            return rc or 1
    print(("OK" if rc == 0 else "FAIL") + "  conformance  " + message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
