#!/bin/sh
set -eu
umask 022
if [ "$#" -ne 3 ]; then
    echo "usage: $0 VERSION ETCD_BINARY OUTPUT_DIRECTORY" >&2
    exit 2
fi
version=$1
binary=$2
out=$3
dpkg --validate-version "$version"
if [ ! -f "$binary" ] || [ ! -x "$binary" ]; then
    echo "not an executable file: $binary" >&2
    exit 2
fi
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$out"
stage=$(mktemp -d "$out/.vws-etcd.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
chmod 0755 "$stage"
install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/vws/etcd" \
    "$stage/usr/lib/systemd/system" "$stage/etc/vws" "$stage/usr/share/doc/vws-etcd"
install -m 0755 "$binary" "$stage/usr/bin/vws-etcd"
for script in vws-etcd-config vws-etcd-tls vws-etcd-snapshot vws-etcd-defrag \
    vws-etcd-check run check-ready check-credentials; do
    install -m 0755 "$src/$script" "$stage/usr/lib/vws/etcd/"
done
install -m 0644 "$src/members.sh" "$stage/usr/lib/vws/etcd/"
install -m 0644 "$src/"*.service "$src/"*.timer "$stage/usr/lib/systemd/system/"
install -m 0644 "$src/etcd.conf" "$stage/etc/vws/"
install -m 0644 "$src/README.md" "$src/copyright" "$stage/usr/share/doc/vws-etcd/"
gzip -n -9 -c > "$stage/usr/share/doc/vws-etcd/changelog.Debian.gz" <<EOF
vws-etcd ($version) unstable; urgency=medium

  * Package the TLS member and snapshot and defrag timers.

 -- VWS maintainers <maintainers@vws.cloud>  Thu, 12 Nov 2026 00:00:00 +0000
EOF
for script in postinst prerm postrm; do
    install -m 0755 "$src/$script" "$stage/DEBIAN/$script"
done
cat > "$stage/DEBIAN/control" <<EOF
Package: vws-etcd
Version: $version
Section: admin
Priority: optional
Architecture: amd64
Maintainer: VWS maintainers <maintainers@vws.cloud>
Depends: adduser, curl, etcd-client, jq, openssl, init-system-helpers (>= 1.65.2), systemd (>= 252)
Homepage: https://github.com/popododo0720/vws-deploy
Description: TLS etcd member for VWS IAM and the admin registry
 Runs a certificate-authenticated etcd member with bounded readiness checks.
 Includes opt-in hourly snapshot and weekly defragmentation timers.
EOF
printf '/etc/vws/etcd.conf\n' > "$stage/DEBIAN/conffiles"
dpkg-deb --root-owner-group --build "$stage" "$out/vws-etcd_${version}_amd64.deb"
