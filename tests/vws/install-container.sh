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
trap 'rm -rf "${work:-}"; rm -f /var/lib/vws-install-fail' EXIT
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
    enable)
        shift
        touch "$INSTALL_STATE/is-enabled-$1" "$INSTALL_STATE/is-active-$1"
        ;;
    disable)
        shift
        rm -f "$INSTALL_STATE/is-enabled-$1" "$INSTALL_STATE/is-active-$1"
        ;;
    restart) test -f "$INSTALL_STATE/is-active-$1" ;;
    daemon-reload) ;;
    *) exit 1 ;;
esac
SH
chmod +x "$work/bin/ssh" "$work/bin/systemctl"
global_units="vws-etcd vws-etcd-snapshot.timer vws-etcd-defrag.timer vws-iam vws-aws-proxy vws-console-api vws-console"

# A global host (Debian VM or bare metal, no PVE): etcd and the global services, nothing of the region.
# The region role and VIP of the per-node layout are refused until #850.
sed -e 's/^    roles: .*/    roles: [etcd, global, region]/' "$work/hosts.yaml" > "$work/region-hosts.yaml"
if bash "$root/scripts/vws" install --profile lab --inventory "$work/region-hosts.yaml" \
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
python3 - "$work" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
paths = list(root.glob("*.out")) + list(root.glob("*.err")) + [root / "state/argv",
        root / "hosts.yaml", root / "global.yaml"]
paths += [p for directory in (Path("/etc/vws"), Path("/usr/lib/vws"))
          for p in directory.rglob("*") if p.is_file()
          and "/credentials/" not in str(p)]
content = b"\n".join(p.read_bytes() for p in paths)
for p in (root / "credentials").rglob("*"):
    if p.is_file() and p.name not in ("etcd-ca.pem", "api-etcd.pem", "internal-ca.pem",
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
echo "install, unchanged rerun, readiness failure, recovery and secret isolation passed"
