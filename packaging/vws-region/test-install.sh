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
getent passwd vws-region
test "$(stat -c '%u:%g:%a' /etc/vws/credentials)" = 0:0:700
test ! -e /etc/vws/credentials/region-kek
test "$(stat -c %a /etc/sudoers.d/vws-region)" = 440
visudo -c -f /etc/sudoers.d/vws-region
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
