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
# An upgrade from the per-node release, whose old prerm leaves vws-region.service running.
# A stand-in systemctl plays a booted systemd: the unit is active until a stop takes effect.
legacy=$(mktemp -d)
install -d "$legacy/DEBIAN" "$legacy/usr/lib/systemd/system"
printf '[Service]\nExecStart=/bin/true\n' > "$legacy/usr/lib/systemd/system/vws-region.service"
printf 'Package: vws-region\nVersion: 0.0.0-0\nArchitecture: amd64\nMaintainer: VWS maintainers <maintainers@vws.cloud>\nDescription: per-node release\n' \
    > "$legacy/DEBIAN/control"
dpkg-deb --root-owner-group --build "$legacy" "$legacy.deb"
state=$(mktemp -d)
cat > /usr/local/bin/systemctl <<EOF
#!/bin/sh
echo "\$*" >> $state/calls
case "\$1" in
    disable) [ -e $state/stuck ] || rm -f $state/active ;;
    show)
        [ ! -e $state/query-fails ] || exit 1
        if [ -e $state/active ]; then echo active; else echo inactive; fi
        ;;
esac
EOF
chmod 0755 /usr/local/bin/systemctl
install -d /run/systemd/system
dpkg -i "$legacy.deb"
# A stop that does not take effect aborts the upgrade and keeps the legacy package.
touch "$state/active" "$state/stuck"
if dpkg -i "$1" 2> "$state/err"; then
    echo "upgrade continued while the per-node vws-region.service was active" >&2
    exit 1
fi
grep -q 'vws-region.service is still active after the stop (state: active)' "$state/err"
test "$(dpkg-query -W -f='${Version} ${db:Status-Abbrev}' vws-region)" = '0.0.0-0 ii '
test -f /usr/lib/systemd/system/vws-region.service
# A stop whose result systemd cannot report aborts the upgrade too.
rm "$state/stuck"
touch "$state/query-fails"
if dpkg -i "$1" 2> "$state/err"; then
    echo "upgrade continued without a confirmed stop of the per-node vws-region.service" >&2
    exit 1
fi
grep -q 'could not confirm that the per-node vws-region.service stopped (state: unknown)' "$state/err"
test "$(dpkg-query -W -f='${Version} ${db:Status-Abbrev}' vws-region)" = '0.0.0-0 ii '
test -f /usr/lib/systemd/system/vws-region.service
# A stop that takes effect lets the upgrade finish and removes the old unit.
rm "$state/query-fails"
touch "$state/active"
dpkg -i "$1"
grep -qx 'disable --now vws-region.service' "$state/calls"
test ! -e "$state/active"
test ! -e /usr/lib/systemd/system/vws-region.service
test -f /usr/lib/systemd/system/vws-region@.service
rm -rf /usr/local/bin/systemctl /run/systemd/system "$state" "$legacy" "$legacy.deb"
echo "install, configure rerun, reinstall, remove, purge and the per-node upgrade work without PVE, sudo or keepalived"
