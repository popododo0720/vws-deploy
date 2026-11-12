#!/bin/sh
# Run only inside a disposable Debian container.
set -eu
if [ "$#" -ne 1 ]; then
    echo "usage: $0 PACKAGE.deb" >&2
    exit 2
fi
apt-get install -y --no-install-recommends "$1"
getent passwd vws-etcd
test "$(stat -c '%U:%G:%a' /var/lib/vws-etcd)" = vws-etcd:vws-etcd:700
test "$(stat -c '%u:%g:%a' /etc/vws/credentials)" = 0:0:700
printf 'data sentinel\n' > /var/lib/vws-etcd/sentinel
printf '# operator configuration\n' >> /etc/vws/etcd.conf
snapshot() {
    sha256sum /var/lib/vws-etcd/sentinel /etc/vws/etcd.conf
}
before=$(snapshot)
for attempt in 1 2; do
    echo "configure attempt $attempt"
    /var/lib/dpkg/info/vws-etcd.postinst configure
    test "$before" = "$(snapshot)"
done
test -z "$(find /etc/systemd/system -name '*vws-etcd*' -print)"
rmdir /etc/vws/credentials
ln -s /var/lib/vws-etcd /etc/vws/credentials
if /var/lib/dpkg/info/vws-etcd.postinst configure; then
    echo "symlinked credentials directory accepted" >&2
    exit 1
fi
rm /etc/vws/credentials
/var/lib/dpkg/info/vws-etcd.postinst configure
for name in ca.crt member.crt member.key client.crt client.key; do
    printf 'credential sentinel\n' > "/etc/vws/credentials/$name"
    chmod 0600 "/etc/vws/credentials/$name"
done
/usr/lib/vws/etcd/check-credentials member
/usr/lib/vws/etcd/check-credentials client
chmod 0644 /etc/vws/credentials/member.key
if /usr/lib/vws/etcd/check-credentials member; then
    echo "unsafe member credential accepted" >&2
    exit 1
fi
if /var/lib/dpkg/info/vws-etcd.postinst configure; then
    echo "unsafe credential accepted on configure" >&2
    exit 1
fi
chmod 0600 /etc/vws/credentials/member.key
apt-get install -y --reinstall --no-install-recommends "$1"
test "$before" = "$(snapshot)"
test -z "$(find /etc/systemd/system -name '*vws-etcd*' -print)"
apt-get remove -y vws-etcd
test ! -e /usr/bin/vws-etcd
test ! -e /usr/lib/vws/etcd/run
test ! -e /usr/lib/systemd/system/vws-etcd.service
test "$before" = "$(snapshot)"
apt-get purge -y vws-etcd
test ! -e /etc/vws/etcd.conf
test "$(cat /var/lib/vws-etcd/sentinel)" = 'data sentinel'
test -f /etc/vws/credentials/member.key
echo "configure twice, symlink rejection, reinstall, remove and purge passed"
