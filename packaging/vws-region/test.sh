#!/bin/sh
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH='' cd -- "$src/../.." && pwd)
mkdir -p "$root/build"
tmp=$(mktemp -d "$root/build/region-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
python3 "$src/../check-ops-ports.py"
python3 "$src/../test-ops-ports.py" "$src/.." "$tmp"
python3 "$src/test-postinst.py" "$src/postinst" "$tmp"
printf '#!/bin/sh\nexit 0\n' > "$tmp/stub"
chmod 0755 "$tmp/stub"
"$src/build.sh" 0.0.0-1 "$tmp/stub" "$tmp/stub" "$tmp"
deb=$tmp/vws-region_0.0.0-1_amd64.deb
dpkg-deb --info "$deb"
dpkg-deb --contents "$deb"
lintian --fail-on error "$deb"
shellcheck "$src/build.sh" "$src/check-ready" "$src/postinst" "$src/prerm" \
    "$src/postrm" "$src/test.sh" "$src/test-install.sh"
/usr/sbin/visudo -c -f "$src/sudoers"
python3 "$src/check-policy.py" "$src/sudoers"
python3 "$src/test-policy.py" "$src/check-policy.py" "$src/sudoers" "$tmp"
printf 'vws-region ALL=(root) NOPASSWD: ALL\n' > "$tmp/broad-sudoers"
/usr/sbin/visudo -c -f "$tmp/broad-sudoers"
if python3 "$src/check-policy.py" "$tmp/broad-sudoers"; then
    echo "broadened sudoers accepted" >&2
    exit 1
fi
for arguments in 'lock *' 'lock [a-z]+' '^lock [a-z]+' 'lock [a-z]+$'; do
    printf 'vws-region ALL=(root) NOPASSWD: /usr/lib/vws/vws-region-helper %s\n' \
        "$arguments" > "$tmp/pattern-sudoers"
    if python3 "$src/check-policy.py" "$tmp/pattern-sudoers"; then
        echo "unsafe argument pattern accepted: $arguments" >&2
        exit 1
    fi
done
mkdir -p "$tmp/guard/scripts" "$tmp/guard/packaging/vws-region"
cp "$root/scripts/guard.sh" "$tmp/guard/scripts/"
git -C "$tmp/guard" init -q
cp "$src/vws-region.service" "$tmp/guard/packaging/vws-region/"
git -C "$tmp/guard" add packaging
bash "$tmp/guard/scripts/guard.sh" secrets
for secret in 'Environment=REGION_TOKEN=canary' 'PVEAPIToken''=canary'; do
    cp "$src/vws-region.service" "$tmp/guard/packaging/vws-region/"
    printf '\n%s\n' "$secret" >> "$tmp/guard/packaging/vws-region/vws-region.service"
    if bash "$tmp/guard/scripts/guard.sh" secrets; then
        echo "secret in packaged unit accepted" >&2
        exit 1
    fi
done
if "$src/build.sh" bad_version "$tmp/stub" "$tmp/stub" "$tmp"; then
    echo "invalid version accepted" >&2
    exit 1
fi
if "$src/build.sh" 0.0.0-1 "$tmp/missing" "$tmp/stub" "$tmp"; then
    echo "missing binary accepted" >&2
    exit 1
fi
python3 - "$src/check-ready" <<'PY'
import http.server
import pathlib
import subprocess
import sys
import threading

class Ready(http.server.BaseHTTPRequestHandler):
    status = 200
    def do_GET(self):
        assert self.path == "/readyz"
        self.send_response(self.status)
        self.end_headers()
    def log_message(self, *_args):
        pass

http.server.HTTPServer.allow_reuse_address = True
server = http.server.HTTPServer(("127.0.0.1", 8085), Ready)
thread = threading.Thread(target=server.serve_forever)
thread.start()
try:
    for status in (200, 503, 200):
        Ready.status = status
        result = subprocess.run([str(pathlib.Path(sys.argv[1]))], check=False)
        assert (result.returncode == 0) == (status == 200), status
finally:
    server.shutdown()
    thread.join()
    server.server_close()
assert subprocess.run([sys.argv[1]], check=False).returncode != 0
server = http.server.HTTPServer(("127.0.0.1", 8083), Ready)
thread = threading.Thread(target=server.serve_forever)
thread.start()
try:
    assert subprocess.run([sys.argv[1]], check=False).returncode != 0
finally:
    server.shutdown()
    thread.join()
    server.server_close()
print("global API readiness on 8083 cannot satisfy region readiness")
print("readiness: ready, unready, recovered and stopped OK")
PY
python3 - "$src" <<'PY'
import pathlib
import sys

src = pathlib.Path(sys.argv[1])
unit = (src / "vws-region.service").read_text().splitlines()
for directive in ("User=vws-region", "SupplementaryGroups=www-data",
                  "MemoryMax=512M", "CPUQuota=50%",
                  "LoadCredential=region-kek:/etc/vws/credentials/region-kek"):
    assert directive in unit, directive
assert not any(line.startswith(("Environment=", "EnvironmentFile=")) for line in unit)
vrrp = (src / "keepalived.conf").read_text()
for directive in ("weight 0", "init_fail", "vws_region_ready",
                  'script "/usr/lib/vws/check-region-ready"'):
    assert directive in vrrp, directive
print("unit limits, credentials and fail-closed VRRP policy OK")
PY
echo "region package checks passed"
