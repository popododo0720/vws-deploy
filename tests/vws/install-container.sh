#!/usr/bin/env bash
# Installs the packages for real into the root filesystem of the machine it runs on, so it runs only as
# root in a disposable container: the Docker or Podman marker, or VWS_INSTALL_TEST_DISPOSABLE=1 for another
# throwaway machine. The checks come before any write.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
refuse() { echo "install-container.sh: refused: $1" >&2; exit 2; }
test "$(id -u)" -eq 0 || refuse "needs root in a disposable container"
if ! test -e /.dockerenv && ! test -e /run/.containerenv && test "${VWS_INSTALL_TEST_DISPOSABLE:-}" != 1; then
    refuse "this is not a disposable container (no /.dockerenv or /run/.containerenv); set VWS_INSTALL_TEST_DISPOSABLE=1 only on a throwaway machine"
fi
if grep -qs ' /etc/pve ' /proc/mounts; then refuse "/etc/pve is a mount: this is a real PVE node"; fi
for path in /etc/vws /etc/keepalived /var/lib/vws-install-fail; do
    if test -e "$path" || test -L "$path"; then refuse "$path exists; start from a clean container"; fi
done
daemons=
trap 'kill $daemons 2>/dev/null || true; rm -rf "${work:-}"; rm -f /var/lib/vws-install-fail' EXIT
work=$(mktemp -d "${TMPDIR:?}/install-target.XXXXXX")
python3 "$root/tests/vws/install_fixture.py" "$work"
export INSTALL_STATE="$work/state"
export PATH="$work/bin:$PATH"
cat > "$work/bin/ssh" <<'SH'
#!/bin/sh
set -eu
printf '%s\n' "$@" >> "$INSTALL_STATE/argv"
for command do :; done
exec sh -c "$command"
SH
cat > "$work/bin/systemctl" <<'SH'
#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$INSTALL_STATE/systemctl"
verb=$1
shift
case "$verb" in
    is-enabled|is-active)
        shift
        test -f "$INSTALL_STATE/$verb-$1"
        ;;
    enable) touch "$INSTALL_STATE/is-enabled-$1" ;;
    start) touch "$INSTALL_STATE/is-active-$1" ;;
    disable)
        shift
        rm -f "$INSTALL_STATE/is-enabled-$1" "$INSTALL_STATE/is-active-$1"
        ;;
    restart) test -f "$INSTALL_STATE/is-active-$1" ;;
    daemon-reload) ;;
    # show -p MainPID --value UNIT: the process a test started for the unit, else 0 as systemd says.
    show) cat "$INSTALL_STATE/mainpid-$4" 2>/dev/null || echo 0 ;;
    *) exit 1 ;;
esac
SH
# curl is not in the image: this one fetches the URL for real and prints the body, also of a 503.
cat > "$work/bin/curl" <<'PY'
#!/usr/bin/env python3
import os, subprocess, sys, urllib.error, urllib.request
try:
    body = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[-1], timeout=2).read()
except urllib.error.HTTPError as e:
    body = e.read()
except OSError:
    sys.exit(7)
sys.stdout.buffer.write(body)
# A test's hook for the moment between the answer and the next look at the listener.
hook = os.path.join(os.environ["INSTALL_STATE"], "after-answer")
if os.path.exists(hook):
    os.rename(hook, hook + ".ran")
    subprocess.run(["sh", hook + ".ran"], check=True)
PY
# A region daemon on 127.0.0.1:PORT that answers /readyz with 503 and BODY (default: not registered yet).
# With "v6only" it is a listener on [::]:PORT that takes no IPv4 connection.
cat > "$work/daemon.py" <<'PY'
import http.server, socket, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.end_headers()
        self.wfile.write((sys.argv[2] if len(sys.argv) > 2 else "unregistered").encode() + b"\n")
    def log_message(self, *args):
        pass
if sys.argv[2:] == ["v6only"]:
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind(("::", int(sys.argv[1])))
    s.listen()
    while True:
        s.accept()[0].close()
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
chmod +x "$work/bin/ssh" "$work/bin/systemctl" "$work/bin/curl"
# daemon PORT [BODY] starts a daemon on 127.0.0.1:PORT and sets $pid once it answers.
daemon() {
    python3 "$work/daemon.py" "$@" &
    pid=$!
    daemons="$daemons $pid"
    for _ in $(seq 50); do
        if curl "http://127.0.0.1:$1/readyz" 2>/dev/null | grep -qx "${2:-unregistered}"; then return; fi
        sleep 0.1
    done
    exit 1
}
global_units="vws-etcd vws-etcd-snapshot.timer vws-etcd-defrag.timer vws-iam vws-aws-proxy vws-console-api vws-console"

# A global host (Debian VM or bare metal, no PVE): etcd and the global services, nothing of the region.
# The VIP and keepalived keys of the per-node layout are refused.
cat > "$work/vip-hosts.yaml" <<'YAML'
profile: lab
hosts:
  - name: lab-1
    address: 192.0.2.1
    roles: [etcd, global]
vip:
  address: 192.0.2.10/24
YAML
if bash "$root/scripts/vws" install --profile lab --inventory "$work/vip-hosts.yaml" \
    --packages "$work/packages" --credentials "$work/credentials" >/dev/null 2>&1; then exit 1; fi
test ! -e /etc/vws
bash "$root/scripts/vws" install --profile lab --inventory "$work/hosts.yaml" \
    --packages "$work/packages" --credentials "$work/credentials" > "$work/global.out" 2> "$work/global.err"
for unit in $global_units; do
    test -f "$INSTALL_STATE/is-enabled-$unit"
    test -f "$INSTALL_STATE/is-active-$unit"
done
if grep -q -e keepalived -e vws-region "$INSTALL_STATE/systemctl" "$work/global.out"; then exit 1; fi
test ! -e /etc/keepalived
test ! -e /etc/vws/region.key
test ! -e /etc/vws/region.yaml
test ! -e /etc/vws/credentials/region-kek
test ! -e /usr/lib/vws/check-region-ready
test ! -e /etc/systemd/system/vws-lab-region.slice.d
args=(install --profile lab --inventory "$work/hosts.yaml" --packages "$work/packages"
      --credentials "$work/credentials")
snapshot() {
    find /etc/vws /usr/lib/vws -printf '%p %m %u %g %y\n' | sort
    find /etc/vws /usr/lib/vws -type f -exec sha256sum {} + | sort
    find /etc/systemd/system -name 50-vws-memory.conf -exec sha256sum {} + | sort
}
bash "$root/scripts/vws" "${args[@]}" > "$work/first.out" 2> "$work/first.err"
grep -q 'changed' "$work/first.out"
python3 - <<'PY'
from pathlib import Path
root = Path("/etc/systemd/system")
for group, mib, services in (
        ("global", 1024, ("vws-iam", "vws-aws-proxy", "vws-console-api", "vws-console")),
        ("etcd", 512, ("vws-etcd", "vws-etcd-snapshot", "vws-etcd-defrag"))):
    name = f"vws-lab-{group}.slice"
    config = root / (name + ".d") / "50-vws-memory.conf"
    assert config.read_text() == f"[Slice]\nMemoryAccounting=yes\nMemoryMax={mib}M\nMemorySwapMax=0\n"
    for service in services:
        config = root / (service + ".service.d") / "50-vws-memory.conf"
        assert config.read_text() == f"[Service]\nSlice={name}\n"
    assert config.stat().st_uid == 0 and config.stat().st_mode & 0o777 == 0o644
PY
snapshot > "$work/before"
bash "$root/scripts/vws" -o json "${args[@]}" > "$work/again.out" 2> "$work/again.err"
python3 - "$work/again.out" <<'PY'
import json, sys
steps = json.load(open(sys.argv[1]))
assert steps and all(s["status"] == "unchanged" for s in steps), steps
PY
snapshot > "$work/after"
cmp "$work/before" "$work/after"
dpkg --unpack "$work/packages/vws-global_0.0.0-1_all.deb" >/dev/null
bash "$root/scripts/vws" "${args[@]}" > "$work/configure.out" 2> "$work/configure.err"
grep -q 'vws-global package changed' "$work/configure.out"
test "$(dpkg-query -W -f='${db:Status-Abbrev}' vws-global)" = 'ii '
touch /var/lib/vws-install/pending
: > "$INSTALL_STATE/systemctl"
bash "$root/scripts/vws" "${args[@]}" > "$work/restart.out" 2> "$work/restart.err"
grep -q '^restart vws-etcd$' "$INSTALL_STATE/systemctl"
test ! -f /var/lib/vws-install/pending
touch /var/lib/vws-install-fail
: > "$INSTALL_STATE/systemctl"
rc=0
bash "$root/scripts/vws" "${args[@]}" > "$work/fail.out" 2> "$work/fail.err" || rc=$?
test "$rc" -eq 1
grep -q 'lab-1: etcd readiness failed' "$work/fail.err"
rm /var/lib/vws-install-fail
bash "$root/scripts/vws" "${args[@]}" > "$work/recovery.out" 2> "$work/recovery.err"
# One region instance on this host: one run installs it and reports it unregistered. The certificate comes
# from the internal CA that vws install keeps, so this run generates that set instead of using the supplied one.
cp -a "$work/credentials" "$work/region-credentials"
rm -f "$work"/region-credentials/{internal-ca.pem,iam.pem,iam-key.pem,api-mtls.pem,api-mtls-key.pem}
cat > "$work/region-hosts.yaml" <<'YAML'
profile: lab
hosts:
  - name: lab-1
    address: 192.0.2.1
    roles: [etcd, global, region]
    regions:
      - name: pve-seoul-1
        port: 8443
YAML
region_args=(install --profile lab --inventory "$work/region-hosts.yaml" --packages "$work/packages"
             --credentials "$work/region-credentials")
# The instance's own daemon, the unit's main process, holds its ops port.
daemon 9443
own=$pid
echo "$own" > "$INSTALL_STATE/mainpid-vws-region@pve-seoul-1.service"
bash "$root/scripts/vws" "${region_args[@]}" > "$work/region.out" 2> "$work/region.err"
grep -q '^lab-1 vws-region@pve-seoul-1 registration unregistered$' "$work/region.out"
test -f "$INSTALL_STATE/is-enabled-vws-region@pve-seoul-1"
test -f "$INSTALL_STATE/is-active-vws-region@pve-seoul-1"
test -f /etc/systemd/system/vws-lab-region.slice.d/50-vws-memory.conf
test "$(cat /etc/vws-region/pve-seoul-1.env)" = "$(printf '%s\n' '# Written by vws install; one instance per Proxmox cluster, no secret here.' \
    VWS_REGION_ADDR=:8443 VWS_REGION_OPS_ADDR=127.0.0.1:9443)"
test "$(stat -c %u:%g:%a /etc/vws-region/pve-seoul-1 /etc/vws-region/pve-seoul-1/region.key /etc/vws-region/pve-seoul-1/region.crt \
    /etc/vws-region/pve-seoul-1/region.yaml /etc/vws-region/pve-seoul-1/global-ca.pem | tr '\n' ' ')" = \
    '0:0:755 0:0:600 0:0:644 0:0:644 0:0:644 '
cmp /etc/vws-region/pve-seoul-1/global-ca.pem /etc/vws/internal-ca.pem
openssl verify -CAfile /etc/vws/internal-ca.pem -verify_ip 192.0.2.1 -verify_ip 127.0.0.1 \
    -verify_hostname pve-seoul-1 /etc/vws-region/pve-seoul-1/region.crt >/dev/null
find /etc/vws-region -printf '%p %m %u %g %y\n' | sort > "$work/region-before"
bash "$root/scripts/vws" -o json "${region_args[@]}" > "$work/region-again.out" 2> "$work/region-again.err"
python3 - "$work/region-again.out" <<'PY'
import json, sys
steps = json.load(open(sys.argv[1]))
rest = [s for s in steps if s["status"] != "unchanged"]
assert rest == [{"host": "lab-1", "step": "vws-region@pve-seoul-1 registration", "status": "unregistered"}], rest
PY
find /etc/vws-region -printf '%p %m %u %g %y\n' | sort > "$work/region-after"
cmp "$work/region-before" "$work/region-after"
# Another process's IPv6-only listener on the same port takes no IPv4 connection: the answer is still the unit's.
python3 "$work/daemon.py" 9443 v6only &
v6=$!
daemons="$daemons $v6"
for _ in $(seq 50); do grep -q ':24E3 00000000000000000000000000000000:0000 0A' /proc/net/tcp6 && break; sleep 0.1; done
grep -q ':24E3 00000000000000000000000000000000:0000 0A' /proc/net/tcp6
bash "$root/scripts/vws" "${region_args[@]}" > "$work/v6only.out" 2> "$work/v6only.err"
grep -q '^lab-1 vws-region@pve-seoul-1 registration unregistered$' "$work/v6only.out"
kill "$v6"
# A stale daemon answers, and the unit's own daemon, which is not registered but not answering that either,
# takes the port before the probe looks at the listener again: that answer is not the unit's, and the unit's
# own never says unregistered, so the run fails.
kill "$own"
wait "$own" 2>/dev/null || true
daemon 9443
stale=$pid
cat > "$INSTALL_STATE/after-answer" <<SH
kill $stale
while kill -0 $stale 2>/dev/null; do sleep 0.05; done
python3 "$work/daemon.py" 9443 starting >/dev/null 2>&1 &
echo \$! > "$INSTALL_STATE/mainpid-vws-region@pve-seoul-1.service"
echo \$! > "$INSTALL_STATE/turnover-pid"
until curl http://127.0.0.1:9443/readyz 2>/dev/null | grep -qx starting; do sleep 0.05; done
SH
echo "$stale" > "$INSTALL_STATE/mainpid-vws-region@pve-seoul-1.service"
rc=0
bash "$root/scripts/vws" "${region_args[@]}" > "$work/turnover.out" 2> "$work/turnover.err" || rc=$?
daemons="$daemons $(cat "$INSTALL_STATE/turnover-pid")"
test -f "$INSTALL_STATE/after-answer.ran"
test "$rc" -eq 1
grep -qx 'vws: lab-1: vws-region@pve-seoul-1 readiness failed' "$work/turnover.err"
if grep -q 'registration' "$work/turnover.out"; then exit 1; fi
kill "$(cat "$INSTALL_STATE/turnover-pid")"
wait "$(cat "$INSTALL_STATE/turnover-pid")" 2>/dev/null || true
# A stale daemon, not the unit's main process, holds the reused ops port and answers unregistered as well:
# the run fails and names the port instead of reporting the instance unregistered.
daemon 9443
sleep 600 &
daemons="$daemons $!"
echo "$!" > "$INSTALL_STATE/mainpid-vws-region@pve-seoul-1.service"
rc=0
bash "$root/scripts/vws" "${region_args[@]}" > "$work/stale.out" 2> "$work/stale.err" || rc=$?
test "$rc" -eq 1
grep -qx 'vws: lab-1: vws-region@pve-seoul-1 readiness failed: 127.0.0.1:9443 is answered by a process other than the main process of vws-region@pve-seoul-1.service' "$work/stale.err"
if grep -q 'registration' "$work/stale.out"; then exit 1; fi
python3 - "$work" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
paths = list(root.glob("*.out")) + list(root.glob("*.err")) + [root / "state/argv",
        root / "hosts.yaml", root / "global.yaml"]
paths += [p for directory in (Path("/etc/vws"), Path("/usr/lib/vws"))
          for p in directory.rglob("*") if p.is_file()
          and "/credentials/" not in str(p)]
# Instance files: the key is meant to be there; the configuration and environment must hold no secret.
paths += [p for p in Path("/etc/vws-region").rglob("*") if p.is_file() and p.suffix in (".env", ".yaml")]
content = b"\n".join(p.read_bytes() for p in paths)
for p in [*(root / "credentials").rglob("*"), *(root / "region-credentials").rglob("*")]:
    if p.is_file() and p.name != ".lock" and p.name not in ("etcd-ca.pem", "api-etcd.pem", "internal-ca.pem",
                                     "api-mtls.pem", "iam-etcd.pem", "iam.pem",
                                     "ca.crt", "member.crt", "client.crt"):
        assert p.read_bytes().strip() not in content, p.name
credentials = Path("/etc/vws/credentials")
for p in [credentials, *credentials.rglob("*")]:
    assert p.stat().st_uid == 0 and p.stat().st_gid == 0
    assert p.stat().st_mode & 0o777 == (0o700 if p.is_dir() else 0o600), p
for p in Path("/etc/vws").iterdir():
    if p.is_file():
        assert p.stat().st_uid == 0 and p.stat().st_gid == 0
        assert p.stat().st_mode & 0o777 == 0o644, p
PY
echo "install, unchanged rerun, readiness failure, recovery, region instance, IPv6-only neighbour, listener turnover, stale ops port and secret isolation passed"
