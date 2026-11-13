#!/usr/bin/env bash
# Run only as root inside a disposable Debian container.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:?}/install-target.XXXXXX")
trap 'rm -rf "$work"; rm -f /var/lib/vws-install-fail' EXIT
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
    *) exit 1 ;;
esac
SH
chmod +x "$work/bin/ssh" "$work/bin/systemctl"
args=(install --profile lab --inventory "$work/hosts.yaml" --packages "$work/packages"
      --credentials "$work/credentials")
snapshot() {
    find /etc/vws /etc/keepalived /usr/lib/vws -printf '%p %m %u %g %y\n' | sort
    find /etc/vws /etc/keepalived /usr/lib/vws -type f -exec sha256sum {} + | sort
}
bash "$root/scripts/vws" "${args[@]}" > "$work/first.out" 2> "$work/first.err"
grep -q 'changed' "$work/first.out"
test -f "$INSTALL_STATE/is-enabled-keepalived"
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
grep -q '^restart vws-region$' "$INSTALL_STATE/systemctl"
test ! -f /var/lib/vws-install/pending
touch /var/lib/vws-install-fail
: > "$INSTALL_STATE/systemctl"
rc=0
bash "$root/scripts/vws" "${args[@]}" > "$work/fail.out" 2> "$work/fail.err" || rc=$?
test "$rc" -eq 1
grep -q 'lab-1: etcd readiness failed' "$work/fail.err"
test ! -f "$INSTALL_STATE/is-enabled-keepalived"
if grep -q '^enable --now keepalived$' "$INSTALL_STATE/systemctl"; then exit 1; fi
rm /var/lib/vws-install-fail
bash "$root/scripts/vws" "${args[@]}" > "$work/recovery.out" 2> "$work/recovery.err"
test -f "$INSTALL_STATE/is-enabled-keepalived"
python3 - "$work" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
paths = list(root.glob("*.out")) + list(root.glob("*.err")) + [root / "state/argv",
        root / "hosts.yaml", root / "global.yaml"]
paths += [p for directory in (Path("/etc/vws"), Path("/etc/keepalived"), Path("/usr/lib/vws"))
          for p in directory.rglob("*") if p.is_file()
          and "/credentials/" not in str(p) and p.name != "region.key"]
content = b"\n".join(p.read_bytes() for p in paths)
for p in (root / "credentials").rglob("*"):
    if p.is_file() and p.name not in ("etcd-ca.pem", "api-etcd.pem", "internal-ca.pem",
                                     "api-mtls.pem", "iam-etcd.pem", "iam.pem"):
        assert p.read_bytes().strip() not in content, p.name
credentials = Path("/etc/vws/credentials")
for p in [credentials, *credentials.rglob("*")]:
    assert p.stat().st_uid == 0 and p.stat().st_gid == 0
    assert p.stat().st_mode & 0o777 == (0o700 if p.is_dir() else 0o600), p
for p in Path("/etc/vws").iterdir():
    if p.is_file():
        assert p.stat().st_uid == 0 and p.stat().st_gid == 0
        assert p.stat().st_mode & 0o777 == (0o600 if p.name == "region.key" else 0o644), p
PY
echo "install, unchanged rerun, readiness failure, recovery and secret isolation passed"
