#!/usr/bin/env python3
"""Start the real vws-region binary with the flags the unit's ExecStart and run-region pass.

usage: test-flags.py SRC_DIR WORK_DIR VWS_REGION_BINARY

The daemon rejects an unknown flag with "invalid flags". A stub daemon cannot show that, so this
test runs the pinned binary. It does not need a PVE cluster: the daemon may fail later (no
registration, no certificate), but it must get past flag parsing.
"""
import os
import pathlib
import re
import shlex
import subprocess
import sys

src, work, binary = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]) / "flags", pathlib.Path(sys.argv[3]).resolve()
etc, state, creds = work / "etc", work / "state", work / "credentials"
name = "pve-flags-1"
for path in (etc / name, creds):
    path.mkdir(parents=True)
(etc / f"{name}.env").write_text("VWS_REGION_ADDR=127.0.0.1:0\nVWS_REGION_OPS_ADDR=127.0.0.1:0\n")
(etc / name / "region.yaml").write_text((src / "region.yaml").read_text())
(creds / "region-key").write_text("not a key\n")

unit = (src / "vws-region@.service").read_text()
line = re.search(r"^ExecStart=(.*)$", unit, re.M)[1].replace("%i", name)
args = shlex.split(line)
assert args[0] == "/usr/lib/vws/run-region", args
args = [a.replace("/var/lib/vws-region", str(state)).replace("/etc/vws-region", str(etc)) for a in args[1:]]
wrapper = work / "run-region"
wrapper.write_text((src / "run-region").read_text()
                   .replace("/etc/vws-region", str(etc)).replace("/usr/bin/vws-region", str(binary)))
wrapper.chmod(0o755)
env = {**os.environ, "CREDENTIALS_DIRECTORY": str(creds), "VWS_REGION_INSTANCE": name}
try:
    result = subprocess.run([str(wrapper), *args], capture_output=True, text=True, timeout=30, env=env)
    output = result.stderr + result.stdout
except subprocess.TimeoutExpired as error:
    output = (error.stderr or b"").decode() + (error.stdout or b"").decode()
assert "invalid flags" not in output, f"the pinned vws-region rejects the unit's flags: {output}"
assert "-region %i -state-dir" in unit
# The check can fail: the same binary refuses an unknown flag.
bad = subprocess.run([str(binary), "-no-such-flag"], capture_output=True, text=True, timeout=30)
assert "invalid flags" in bad.stderr + bad.stdout, "the flag check cannot tell a bad flag from a good one"
print("the pinned vws-region accepts the unit and run-region flags")
