#!/bin/sh
set -eu
umask 022
if [ "$#" -ne 4 ]; then
    echo "usage: $0 VERSION REGION_BINARY HELPER_BINARY OUTPUT_DIRECTORY" >&2
    exit 2
fi
version=$1
region=$2
helper=$3
out=$4
dpkg --validate-version "$version"
for binary in "$region" "$helper"; do
    if [ ! -f "$binary" ] || [ ! -x "$binary" ]; then
        echo "not an executable file: $binary" >&2
        exit 2
    fi
done
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$out"
stage=$(mktemp -d "$out/.vws-region.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
chmod 0755 "$stage"
install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/vws" \
    "$stage/usr/lib/systemd/system" "$stage/etc/vws" "$stage/etc/sudoers.d" \
    "$stage/usr/share/doc/vws-region/examples"
install -m 0755 "$region" "$stage/usr/bin/vws-region"
install -m 0755 "$helper" "$stage/usr/lib/vws/vws-region-helper"
install -m 0755 "$src/check-ready" "$stage/usr/lib/vws/check-region-ready"
install -m 0644 "$src/vws-region.service" "$stage/usr/lib/systemd/system/"
install -m 0644 "$src/region.yaml" "$stage/etc/vws/"
install -m 0440 "$src/sudoers" "$stage/etc/sudoers.d/vws-region"
install -m 0644 "$src/README.md" "$stage/usr/share/doc/vws-region/"
install -m 0644 "$src/keepalived.conf" "$stage/usr/share/doc/vws-region/examples/"
install -m 0644 "$src/copyright" "$stage/usr/share/doc/vws-region/copyright"
gzip -n -9 -c > "$stage/usr/share/doc/vws-region/changelog.Debian.gz" <<EOF
vws-region ($version) unstable; urgency=medium

  * Package the region daemon and its local readiness and helper policy.

 -- VWS maintainers <maintainers@vws.cloud>  Wed, 11 Nov 2026 00:00:00 +0000
EOF
for script in postinst prerm postrm; do
    install -m 0755 "$src/$script" "$stage/DEBIAN/$script"
done
cat > "$stage/DEBIAN/control" <<EOF
Package: vws-region
Version: $version
Section: admin
Priority: optional
Architecture: amd64
Maintainer: VWS maintainers <maintainers@vws.cloud>
Depends: adduser, curl, sudo (>= 1.9.10), init-system-helpers (>= 1.65.2), systemd (>= 252)
Recommends: keepalived
Homepage: https://github.com/popododo0720/vws-deploy
Description: Proxmox region API for VWS
 Runs the region daemon on each PVE node behind an operator-managed VIP.
 Includes a bounded readiness probe and a restricted privileged helper policy.
EOF
printf '/etc/vws/region.yaml\n/etc/sudoers.d/vws-region\n' > "$stage/DEBIAN/conffiles"
dpkg-deb --root-owner-group --build "$stage" "$out/vws-region_${version}_amd64.deb"
