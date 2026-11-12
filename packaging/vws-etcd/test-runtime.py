#!/usr/bin/env python3
"""Check installed helper layout, launcher boundaries and mutual-TLS readiness."""
import http.server
import fcntl
import os
import pathlib
import ssl
import subprocess
import sys
import threading

src, tmp = map(pathlib.Path, sys.argv[1:])
helpers = tmp / "unpacked/usr/lib/vws/etcd"
conf = tmp / "etcd.conf"
runtime = tmp / "config.yaml"
binary = tmp / "member"
binary.write_text('#!/bin/sh\n[ "$1" = --config-file ] && [ -s "$2" ]\n')
binary.chmod(0o755)
launcher = helpers / "run"
launcher.write_text(launcher.read_text().replace("/etc/vws/etcd.conf", str(conf))
                    .replace("/run/vws-etcd/config.yaml", str(runtime))
                    .replace("/var/lib/vws-etcd/maintenance.lock", str(tmp / "maintenance.lock"))
                    .replace("--wait 1800", "--wait 1")
                    .replace("/usr/bin/vws-etcd", str(binary)))
tls = tmp / "tls"
subprocess.run([str(helpers / "vws-etcd-tls"), str(tls), "local=127.0.0.1"],
               check=True, timeout=20)
env = {**os.environ, "CREDENTIALS_DIRECTORY": str(tls / "local")}
for setting in ("NAME=\nLIST=\n", "NAME=local\nLIST=local=999.0.0.1\n",
                "NAME=local\nLIST=local=127.0.0.1\nBOGUS=1\n",
                f"NAME=$(touch {tmp}/injected)\nLIST=local=127.0.0.1\n"):
    conf.write_text(setting)
    result = subprocess.run([str(launcher), "member"], env=env, timeout=5)
    assert result.returncode != 0, setting
assert not (tmp / "injected").exists()
conf.write_text("NAME=local\nLIST=local=127.0.0.1\n")
for _ in range(2):
    subprocess.run([str(launcher), "member"], env=env, check=True, timeout=5)
    assert f"key-file: {tls}/local/member.key" in runtime.read_text()
assert runtime.stat().st_mode & 0o777 == 0o600
for action in ("snapshot", "defrag"):
    helper = helpers / f"vws-etcd-{action}"
    helper.write_text(
        '#!/bin/sh\nset -eu\n'
        '[ "$ETCDCTL_ENDPOINTS" = https://127.0.0.1:2379 ]\n'
        '[ "$ETCDCTL_CACERT" = "$CREDENTIALS_DIRECTORY/ca.crt" ]\n'
        '[ "$ETCDCTL_CERT" = "$CREDENTIALS_DIRECTORY/client.crt" ]\n'
        '[ "$ETCDCTL_KEY" = "$CREDENTIALS_DIRECTORY/client.key" ]\n'
        '[ "$ETCDCTL_DIAL_TIMEOUT" = 5s ]\n'
        f'[ "$ETCDCTL_COMMAND_TIMEOUT" = {"60s" if action == "snapshot" else "10m"} ]\n'
        + ('[ "$1" = /var/lib/vws-etcd/snapshots ] && [ "$2" = 48 ]\n'
           if action == "snapshot" else '[ "$#" = 0 ]\n'))
    subprocess.run([str(launcher), action], env=env, check=True, timeout=5)
    with (tmp / "maintenance.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        blocked = subprocess.run([str(launcher), action], env=env, timeout=5)
        assert blocked.returncode != 0
    subprocess.run([str(launcher), action], env=env, check=True, timeout=5)


class Health(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        assert self.path == "/health"
        body = b'{"health":"true"}'
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 2379), Health)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(tls / "local/member.crt", tls / "local/member.key")
context.load_verify_locations(tls / "ca.crt")
context.verify_mode = ssl.CERT_REQUIRED
server.socket = context.wrap_socket(server.socket, server_side=True)
thread = threading.Thread(target=server.serve_forever)
thread.start()
try:
    for status in (200, 503, 200):
        Health.status = status
        result = subprocess.run([str(helpers / "check-ready")], env=env, timeout=5)
        assert (result.returncode == 0) == (status == 200), status
    result = subprocess.run([str(helpers / "check-ready")],
                            env={**env, "CREDENTIALS_DIRECTORY": str(tls / "client")},
                            timeout=5)
    assert result.returncode != 0
finally:
    server.shutdown()
    thread.join()
    server.server_close()
assert subprocess.run([str(helpers / "check-ready")], env=env, timeout=5).returncode != 0

for action in ("snapshot", "defrag"):
    unit = (src / f"vws-etcd-{action}.service").read_text().splitlines()
    for name in ("ca.crt", "client.crt", "client.key"):
        assert f"LoadCredential={name}:/etc/vws/credentials/{name}" in unit
    assert "ExecStartPre=+/usr/lib/vws/etcd/check-credentials client" in unit
    assert f"ExecStart=/usr/lib/vws/etcd/run {action}" in unit
    assert f"TimeoutStartSec={'32min' if action == 'snapshot' else '60min'}" in unit
    timer = (src / f"vws-etcd-{action}.timer").read_text().splitlines()
    assert "Persistent=true" in timer
    assert f"Unit=vws-etcd-{action}.service" in timer
    assert f"OnCalendar={'hourly' if action == 'snapshot' else 'weekly'}" in timer
unit = (src / "vws-etcd.service").read_text().splitlines()
for name in ("ca.crt", "member.crt", "member.key"):
    assert f"LoadCredential={name}:/etc/vws/credentials/{name}" in unit
for directive in ("User=vws-etcd", "Group=vws-etcd", "RuntimeDirectoryMode=0700",
                  "Restart=on-failure", "ExecStart=/usr/lib/vws/etcd/run member",
                  "ExecStartPre=+/usr/lib/vws/etcd/check-credentials member"):
    assert directive in unit
for path in src.glob("*.service"):
    assert not any(line.startswith(("Environment=", "EnvironmentFile="))
                   for line in path.read_text().splitlines())
print("installed helpers, config rejection, restart and mutual-TLS readiness passed")
