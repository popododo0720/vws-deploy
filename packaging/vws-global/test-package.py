import hashlib
import http.server
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import threading

import yaml

src, tmp = map(Path, sys.argv[1:])


def run(*args, ok=True, env=None):
    result = subprocess.run(list(map(str, args)), env=env, check=False)
    assert (result.returncode == 0) == ok, args


fixture = tmp / "fixture"
package = fixture / "packaging/vws-global"
shutil.copytree(src, package)
(fixture / "scripts").mkdir()
versions = (src.parent.parent / "scripts/versions.env").read_text()
node = tmp / "node"
node_version = dict(line.split("=", 1) for line in versions.splitlines()
                    if line.startswith("GLOBAL_"))["GLOBAL_NODE_VERSION"]
node.write_text(f"#!/bin/sh\nprintf 'v{node_version}\\n'\n")
node.chmod(0o755)
stub = tmp / "stub"
stub.write_text("#!/bin/sh\nexit 0\n")
stub.chmod(0o755)
license_file = tmp / "LICENSE"
license_file.write_text("Test runtime fixture license\n")
console = tmp / "console"
(console / ".next/static").mkdir(parents=True)
(console / "public").mkdir()
(console / "server.js").write_text("// Runtime fixture.\n")
out = tmp / "out"
args = ["0.0.0-1", stub, stub, stub, node, license_file, console, out]
run(src / "build.sh", *args, ok=False)  # Production pin rejects the fixture.
lines = versions.splitlines()
lines = [f"GLOBAL_NODE_BINARY_SHA256={hashlib.sha256(node.read_bytes()).hexdigest()}"
         if line.startswith("GLOBAL_NODE_BINARY_SHA256=") else line for line in lines]
(fixture / "scripts/versions.env").write_text("\n".join(lines) + "\n")
for _ in range(2):
    run(package / "build.sh", *args)
for index, value in ((0, "bad_version"), (1, tmp / "missing"), (4, stub), (6, tmp / "missing")):
    bad = args.copy()
    bad[index] = value
    run(package / "build.sh", *bad, ok=False)
dest = tmp / "unpacked"
run("dpkg-deb", "--extract", out / "vws-global_0.0.0-1_amd64.deb", dest)
for unit, probe in (("console-api", "api"), ("aws-proxy", "proxy"),
                    ("iam", "iam"), ("console", "console")):
    text = (dest / f"usr/lib/systemd/system/vws-{unit}.service").read_text()
    assert f"User=vws-{unit}\n" in text
    assert f"ExecStartPost=/usr/lib/vws/check-global-ready {probe}\n" in text
    assert "Restart=on-failure\n" in text and "TimeoutStartSec=30\n" in text
config = yaml.safe_load((dest / "etc/vws/global.yaml").read_text())
assert set(config) == {"iam", "listeners", "credentials", "default_project"}
assert not ({"regions", "accounts", "aws_accounts"} & config.keys())
assert config["credentials"]["kek_file"].endswith("/vws-console-api.service/admin-kek")
assert yaml.safe_load((dest / "etc/vws/aws-accounts.json").read_text()) == []
for path in ("usr/bin/vws-console-api", "usr/bin/vws-aws-proxy", "usr/bin/vws-iam",
             "usr/lib/vws/node", "usr/lib/vws/console/server.js"):
    assert (dest / path).is_file(), path


class Ready(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


class UnixHTTP(socketserver.UnixStreamServer):
    pass


# Paths and ports match the installed units; a restarted server reuses the socket.
http.server.HTTPServer.allow_reuse_address = True
for name, port in (("api", 8083), ("iam", 9096), ("console", 3000), ("proxy", None)):
    socket = tmp / "proxy.sock"
    probe = tmp / f"check-{name}"
    probe.write_text((src / "check-ready").read_text().replace(
        "/run/vws-aws-proxy/aws-proxy.sock", str(socket)))
    probe.chmod(0o755)
    for restart in range(2):
        server = (UnixHTTP(str(socket), Ready) if port is None else
                  http.server.HTTPServer(("127.0.0.1", port), Ready))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for status in ((403, 200, 403) if port is None else (200, 503, 200)):
                Ready.status = status
                run(probe, name, ok=(status == (403 if port is None else 200)))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            if socket.exists():
                socket.unlink()
    run(probe, name, ok=False)
run(src / "check-ready", "unknown", ok=False)

# Run configuration twice against real filesystem permissions and fake OS commands.
bin_dir = tmp / "bin"
bin_dir.mkdir()
users = tmp / "users"
for name, content in {
    "getent": f'#!/bin/sh\n/usr/bin/grep -qx "$2" "{users}"\n',
    "adduser": f'#!/bin/sh\nfor user do :; done\nprintf "%s\\n" "$user" >> "{users}"\n',
}.items():
    command = bin_dir / name
    command.write_text(content)
    command.chmod(0o755)
users.touch()
creds = tmp / "credentials"
postinst = tmp / "postinst"
postinst.write_text((src / "postinst").read_text().replace("/etc/vws/credentials", str(creds))
                   .replace("-o root -g root", f"-o {os.getuid()} -g {os.getgid()}")
                   .replace("/run/systemd/system", str(tmp / "no-systemd")))
postinst.chmod(0o755)
env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
for _ in range(2):
    run(postinst, "configure", env=env)
assert len(users.read_text().splitlines()) == 4
assert creds.stat().st_mode & 0o777 == 0o700
creds.rmdir()
creds.symlink_to(tmp / "elsewhere")
run(postinst, "configure", env=env, ok=False)
print("package inputs, restart, fail-closed probes and repeated configure passed")
