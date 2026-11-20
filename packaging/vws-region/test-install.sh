#!/bin/sh
# Run only inside a disposable Debian container, with no /etc/pve and no sudo.
set -eu
if [ "$#" -ne 1 ]; then
    echo "usage: $0 PACKAGE.deb" >&2
    exit 2
fi
test ! -e /etc/pve
if command -v sudo >/dev/null; then
    echo "run this test in a container without sudo" >&2
    exit 2
fi
apt-get install -y --no-install-recommends "$1"
test ! -e /etc/pve
if command -v sudo >/dev/null || command -v keepalived >/dev/null; then
    echo "the package pulled in sudo or keepalived" >&2
    exit 1
fi
getent passwd vws-region
for path in /usr/bin/vws-region /usr/lib/vws/run-region /usr/lib/vws/check-region-ready \
    /usr/lib/systemd/system/vws-region@.service \
    /usr/share/vws-region/examples/region.yaml /usr/share/vws-region/examples/region.env; do
    test -f "$path"
done
test -d /etc/vws-region
for path in /usr/lib/vws/vws-region-helper /usr/lib/systemd/system/vws-region.service \
    /etc/sudoers.d/vws-region /etc/vws/credentials /usr/share/vws-region/examples/keepalived.conf; do
    test ! -e "$path"
done
if dpkg-query -W -f='${Depends}' vws-region | grep -Eq '(^|[ ,])(sudo|keepalived)([ ,(]|$)'; then
    echo "package depends on sudo or keepalived" >&2
    exit 1
fi
unit=/usr/lib/systemd/system/vws-region@.service
grep -q '^StateDirectory=vws-region/%i$' "$unit"
grep -q '^StateDirectoryMode=0700$' "$unit"
grep -q -- '-region %i -state-dir /var/lib/vws-region/%i' "$unit"
# An operator's instance files are not package files and survive every step below.
install -d -m 0700 /etc/vws-region/pve-seoul-1
cp /usr/share/vws-region/examples/region.env /etc/vws-region/pve-seoul-1.env
# A sudoers rule left by the per-node release is removed on configure.
install -d /etc/sudoers.d
printf '# stale\n' > /etc/sudoers.d/vws-region
/var/lib/dpkg/info/vws-region.postinst configure
test ! -e /etc/sudoers.d/vws-region
/var/lib/dpkg/info/vws-region.postinst configure
apt-get install -y --reinstall --no-install-recommends "$1"
test ! -e /etc/pve
apt-get remove -y vws-region
test ! -e /usr/bin/vws-region
test ! -e /usr/lib/systemd/system/vws-region@.service
test -f /etc/vws-region/pve-seoul-1.env
getent passwd vws-region
apt-get purge -y vws-region
test -f /etc/vws-region/pve-seoul-1.env
test -d /etc/vws-region/pve-seoul-1
test ! -e /etc/pve
echo "install, configure rerun, reinstall, remove and purge work without PVE, sudo or keepalived"
