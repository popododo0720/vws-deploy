#!/usr/bin/env python3
"""Start the real vws-aws-proxy with the flags the packaged unit passes and probe it like check-ready.

usage: test-proxy-flags.py SRC_DIR WORK_DIR VWS_AWS_PROXY_BINARY

The proxy reads its account and project snapshots, its source credential and its endpoint
templates before it listens, so a missing or renamed flag stops it before the socket exists. This
test runs the pinned binary with the unit's ExecStart, the package's empty snapshots and a fake
source credential, and expects the 403 that check-ready expects. It calls no AWS endpoint.
"""
import http.client
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import time

src, work, binary = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]) / "proxy-flags", pathlib.Path(sys.argv[3]).resolve()
etc, run, creds = work / "etc", work / "run", work / "credentials"
for path in (etc, run, creds):
    path.mkdir(parents=True, mode=0o700)
for name in ("aws-accounts.json", "projects.json"):
    (etc / name).write_text("[]\n")
(creds / "aws-source").write_text('{"AccessKey":"AKIAPROXYFLAGSTEST","Secret":"proxy-flags-test-secret"}\n')

unit = (src / "vws-aws-proxy.service").read_text()
args = shlex.split(re.search(r"^ExecStart=(.*)$", unit, re.M)[1])
assert args[0] == "/usr/bin/vws-aws-proxy", args
sock = run / "aws-proxy.sock"
args = [a.replace("/run/vws-aws-proxy/aws-proxy.sock", str(sock)).replace("/etc/vws", str(etc)) for a in args[1:]]
built = (src / "build.sh").read_text()
for name in ("aws-accounts.json", "projects.json"):
    if f"/etc/vws/{name}" in unit:
        assert f'"$stage/etc/vws/{name}"' in built, f"the unit reads /etc/vws/{name}, which the package does not ship"

env = {"PATH": os.environ["PATH"], "CREDENTIALS_DIRECTORY": str(creds)}


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(1)
        self.sock.connect(str(sock))


def start(argv):
    """Run the proxy and return (status of GET / on its socket or None, output)."""
    proc = subprocess.Popen([str(binary), *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    status = None
    try:
        for _ in range(20):
            if proc.poll() is not None:
                break
            if sock.exists():
                conn = UnixConnection("localhost", timeout=1)
                try:
                    conn.request("GET", "/")
                    status = conn.getresponse().status
                    break
                except OSError:
                    pass
                finally:
                    conn.close()
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
        output = proc.communicate(timeout=10)[0]
        sock.unlink(missing_ok=True)
    return status, output


status, output = start(args)
assert status == 403, f"the pinned vws-aws-proxy does not serve with the unit's flags (status {status}): {output}"
# The check can fail: the same binary without the project snapshot stops before it listens.
drop = args.index("-projects")
status, output = start(args[:drop] + args[drop + 2:])
assert status is None and "read project mappings" in output, f"the probe cannot tell a missing flag: {status} {output}"
print("the pinned vws-aws-proxy serves with the unit's flags and the packaged snapshots")
