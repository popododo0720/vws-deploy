#!/bin/sh
set -eu
umask 022
if [ "$#" -ne 3 ]; then
    echo "usage: $0 VERSION REGION_BINARY OUTPUT_DIRECTORY" >&2
    exit 2
fi
version=$1
region=$2
out=$3
dpkg --validate-version "$version"
if [ ! -f "$region" ] || [ ! -x "$region" ]; then
    echo "not an executable file: $region" >&2
    exit 2
fi
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$out"
stage=$(mktemp -d "$out/.vws-region.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
chmod 0755 "$stage"
install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/vws" \
    "$stage/usr/lib/systemd/system" "$stage/etc/vws-region" \
    "$stage/usr/share/doc/vws-region/examples"
install -m 0755 "$region" "$stage/usr/bin/vws-region"
install -m 0755 "$src/run-region" "$stage/usr/lib/vws/run-region"
install -m 0755 "$src/check-ready" "$stage/usr/lib/vws/check-region-ready"
install -m 0644 "$src/vws-region@.service" "$stage/usr/lib/systemd/system/"
install -m 0644 "$src/region.yaml" "$src/region.env" "$stage/usr/share/doc/vws-region/examples/"
install -m 0644 "$src/README.md" "$stage/usr/share/doc/vws-region/"
install -m 0644 "$src/copyright" "$stage/usr/share/doc/vws-region/copyright"
gzip -n -9 -c > "$stage/usr/share/doc/vws-region/changelog.Debian.gz" <<EOF
vws-region ($version) unstable; urgency=medium

  * Package the region daemon and its instance unit and readiness probe.

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
Depends: adduser, curl, init-system-helpers (>= 1.65.2), systemd (>= 252)
Homepage: https://github.com/popododo0720/vws-deploy
Description: Proxmox region API for VWS
 Runs one region daemon per PVE cluster as the vws-region@<region> unit.
 Each instance keeps its state under /var/lib/vws-region/<region> and needs
 no PVE node or privileged helper. Includes a bounded readiness probe.
EOF
dpkg-deb --root-owner-group --build "$stage" "$out/vws-region_${version}_amd64.deb"
