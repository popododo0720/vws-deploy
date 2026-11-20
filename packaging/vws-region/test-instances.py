#!/usr/bin/env python3
"""Run two vws-region instances on one host through run-region and check-ready.

The daemon is a stub that records its flags, writes a 0600 state file and serves /readyz
on its ops address, so the test covers the wrapper, the per-instance env file and the
isolation between instances, not the real daemon.
"""
import os
import pathlib
import socket
import stat
import subprocess
import sys
import time

src, work = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]) / "instances"
etc, state, bin_dir = work / "etc", work / "state", work / "bin"
for path in (etc, state, bin_dir):
    path.mkdir(parents=True)

daemon = bin_dir / "vws-region"
daemon.write_text('''#!/usr/bin/env python3
import http.server, os, pathlib, socket, sys
args = sys.argv[1:]
flags = dict(zip(args[::2], args[1::2]))
state = pathlib.Path(flags["-state-dir"])
state.mkdir(mode=0o700, parents=True, exist_ok=True)
(state / "argv").write_text("\\n".join(args) + "\\n")
fd = os.open(state / "registration.json", os.O_WRONLY | os.O_CREAT, 0o600)
os.write(fd, flags["-region"].encode())
host, _, port = flags["-region-addr"].rpartition(":")
api = socket.create_server((host or "127.0.0.1", int(port)))
class Ready(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/readyz" else 404)
        self.end_headers()
    def log_message(self, *_):
        pass
host, _, port = flags["-ops-addr"].rpartition(":")
http.server.HTTPServer((host, int(port)), Ready).serve_forever()
''')
daemon.chmod(0o755)

run_region = work / "run-region"
run_region.write_text((src / "run-region").read_text()
                      .replace("/etc/vws-region", str(etc)).replace("/usr/bin/vws-region", str(daemon)))
check = work / "check-ready"
check.write_text((src / "check-ready").read_text().replace("/etc/vws-region", str(etc)))
for path in (run_region, check):
    path.chmod(0o755)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def configure(name, addr, ops, extra=""):
    (etc / f"{name}.env").write_text(f"# {name}\nVWS_REGION_ADDR={addr}\nVWS_REGION_OPS_ADDR={ops}\n{extra}")
    credentials = work / "credentials" / name
    credentials.mkdir(parents=True, exist_ok=True)
    (credentials / "region-key").write_text("key\n")
    return credentials


def start(name, credentials):
    env = {**os.environ, "CREDENTIALS_DIRECTORY": str(credentials), "VWS_REGION_INSTANCE": name}
    return subprocess.Popen([str(run_region), "-region", name, "-state-dir", str(state / name)],
                            env=env, stderr=subprocess.PIPE, text=True)


def ready(name):
    return subprocess.run([str(check), name], capture_output=True, text=True, timeout=10).returncode == 0


def wait_ready(name, process):
    for _ in range(100):
        if process.poll() is not None:
            raise AssertionError(f"{name} exited: {process.stderr.read()}")
        if ready(name):
            return
        time.sleep(0.1)
    raise AssertionError(f"{name} never became ready")


first, second = "pve-seoul-1", "pve-busan-1"
ports = [free_port() for _ in range(4)]
processes = []
try:
    one = start(first, configure(first, f"127.0.0.1:{ports[0]}", f"127.0.0.1:{ports[1]}"))
    processes.append(one)
    two = start(second, configure(second, f"127.0.0.1:{ports[2]}", f"127.0.0.1:{ports[3]}"))
    processes.append(two)
    wait_ready(first, one)
    wait_ready(second, two)
    for name, process in ((first, one), (second, two)):
        assert process.poll() is None, name
        directory = state / name
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, name
        assert stat.S_IMODE((directory / "registration.json").stat().st_mode) == 0o600, name
        assert (directory / "registration.json").read_text() == name
        argv = (directory / "argv").read_text().split()
        assert argv[argv.index("-region") + 1] == name
        assert argv[argv.index("-state-dir") + 1] == str(directory)
        assert argv[argv.index("-tls-key-file") + 1] == str(work / "credentials" / name / "region-key")
        assert argv[argv.index("-tls-cert-file") + 1] == f"{etc}/{name}/region.crt"
    # Both run at once, and stopping one leaves the other ready.
    one.terminate()
    one.wait(timeout=10)
    assert not ready(first) and ready(second)
    # An instance on a taken port fails; it does not take over the other's state.
    clash = start("pve-clash-1", configure("pve-clash-1", f"127.0.0.1:{ports[2]}", f"127.0.0.1:{free_port()}"))
    processes.append(clash)
    assert clash.wait(timeout=10) != 0
    assert ready(second)
finally:
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def refused(name, text, message):
    credentials = configure(name, "127.0.0.1:1", "127.0.0.1:2")
    (etc / f"{name}.env").write_text(text)
    process = start(name, credentials)
    error = process.communicate(timeout=10)[1]
    assert process.returncode == 2 and message in error, (name, process.returncode, error)
    assert not (state / name).exists(), "refused instance created state"


refused("pve-bad-1", "VWS_REGION_ADDR=:1\n", "must set")
refused("pve-bad-2", "VWS_REGION_ADDR=:1\nVWS_REGION_OPS_ADDR=127.0.0.1:2\nPVE_API_TOKEN=x\n", "unknown key")
refused("pve-bad-3", "VWS_REGION_ADDR=:1;id\nVWS_REGION_OPS_ADDR=127.0.0.1:2\n", "bad value")
refused("pve-bad-4", "VWS_REGION_ADDR=$(id)\nVWS_REGION_OPS_ADDR=127.0.0.1:2\n", "bad value")
for name in ("../pve-x-1", "Pve-x-1", "pve-x", ""):
    result = subprocess.run([str(run_region)], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "CREDENTIALS_DIRECTORY": str(work), "VWS_REGION_INSTANCE": name})
    assert result.returncode != 0, name
    result = subprocess.run([str(check), name], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2, name
print("two instances run side by side with separate state; bad instance configs are refused")
