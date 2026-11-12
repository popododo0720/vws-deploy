#!/bin/sh
# Run only inside a disposable Debian container.
set -eu
if [ "$#" -ne 1 ]; then
    echo "usage: $0 PACKAGE.deb" >&2
    exit 2
fi
mkdir -p /etc/pve/nodes/fake/qemu-server /var/lib/vz/images/100 /var/lib/vz/snippets
printf 'cores: 2\n' > /etc/pve/nodes/fake/qemu-server/100.conf
printf 'volume sentinel\n' > /var/lib/vz/images/100/disk.raw
printf 'snippet sentinel\n' > /var/lib/vz/snippets/user.yaml
snapshot() {
    find /etc/pve /var/lib/vz -printf '%p %m %u %g %y\n' | sort
    find /etc/pve /var/lib/vz -type f -exec sha256sum {} + | sort
}
before=$(snapshot)
apt-get install -y --no-install-recommends "$1"
test ! -e /etc/pve/vws
tmp=$(mktemp -d "$(dirname -- "$1")/region-install.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
printf '/dev/fuse /etc/pve fuse rw 0 0\n' > "$tmp/mounts"
VWS_REGION_TEST=1 VWS_REGION_TEST_MOUNTS="$tmp/mounts" \
    /var/lib/dpkg/info/vws-region.postinst configure
test -d /etc/pve/vws
printf 'lease sentinel\n' > /etc/pve/vws/lease.json
lease_before=$(snapshot)
VWS_REGION_TEST=1 VWS_REGION_TEST_MOUNTS="$tmp/mounts" \
    /var/lib/dpkg/info/vws-region.postinst configure
test "$lease_before" = "$(snapshot)"
rm /etc/pve/vws/lease.json
rmdir /etc/pve/vws
ln -s "$tmp" /etc/pve/vws
if VWS_REGION_TEST=1 VWS_REGION_TEST_MOUNTS="$tmp/mounts" \
    /var/lib/dpkg/info/vws-region.postinst configure 2>"$tmp/error"; then
    echo "symlinked lease directory accepted" >&2
    exit 1
fi
grep -q 'lease directory /etc/pve/vws must not be a symlink' "$tmp/error"
rm /etc/pve/vws
test "$before" = "$(snapshot)"
getent passwd vws-region
test "$(stat -c '%u:%g:%a' /etc/vws/credentials)" = 0:0:700
test ! -e /etc/vws/credentials/region-kek
test "$(stat -c %a /etc/sudoers.d/vws-region)" = 440
visudo -c -f /etc/sudoers.d/vws-region
sudo_version=$(dpkg-query -W -f='${Version}' sudo)
dpkg --compare-versions "$sudo_version" ge 1.9.10
printf 'Debian sudo version: %s\n' "$sudo_version"
python3 - <<'PY'
import subprocess

helper = "/usr/lib/vws/vws-region-helper"
fixed = ("registration-write", "token-write", "token-read", "lease-write")
cases = [(True, (verb,)) for verb in fixed]
cases += [(False, (verb, "extra")) for verb in fixed]
cases += [(False, ()), (False, ("unknown",))]
for verb in ("lock", "unlock"):
    cases += [(True, (verb, name)) for name in ("a", "A0_-", "a" * 64)]
    cases += [(False, (verb, name)) for name in
              ("", "../escape", "valid extra", "valid/name", "-bad", "_bad",
               "a" * 65, "valid\n", "\u00e9")]
    cases += [(False, (verb,)), (False, (verb, "valid", "extra"))]
snippet = "vws-01234567-89ab-cdef-0123-456789abcdef-user.yaml"
for verb in ("snippet-write", "snippet-delete"):
    cases.append((True, (verb, snippet)))
    cases += [(False, (verb, name)) for name in
              ("", "../" + snippet, "wrong.yaml", snippet.upper(),
               snippet.replace(".yaml", "Xyaml"), snippet + " extra", snippet + "\n")]
    cases += [(False, (verb,)), (False, (verb, snippet, "extra"))]
for allowed, args in cases:
    result = subprocess.run(
        ["runuser", "-u", "vws-region", "--", "sudo", "-n", "-l", helper, *args],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode in (0, 1), (args, result.stderr)
    assert (result.returncode == 0) == allowed, (args, result.stdout, result.stderr)
    print(f"sudo {'ALLOW' if allowed else 'DENY'} {args!r}")
print(f"real Debian sudo authorization: {len(cases)} cases passed")
PY
/var/lib/dpkg/info/vws-region.postinst configure
printf 'unsafe' > /etc/vws/credentials/region-kek
chmod 0644 /etc/vws/credentials/region-kek
if /var/lib/dpkg/info/vws-region.postinst configure; then
    echo "unsafe credential accepted" >&2
    exit 1
fi
rm /etc/vws/credentials/region-kek
apt-get install -y --reinstall --no-install-recommends "$1"
apt-get remove -y vws-region
test ! -e /usr/bin/vws-region
test ! -e /usr/lib/vws/vws-region-helper
test ! -e /usr/lib/systemd/system/vws-region.service
test "$before" = "$(snapshot)"
test -f /etc/vws/region.yaml
apt-get purge -y vws-region
test "$before" = "$(snapshot)"
echo "install, configure rerun, reinstall, remove and purge preserve PVE data"
