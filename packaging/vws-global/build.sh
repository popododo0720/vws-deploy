#!/bin/sh
set -eu
umask 022
if [ "$#" -ne 8 ]; then
    echo "usage: $0 VERSION API_BINARY PROXY_BINARY IAM_BINARY NODE_BINARY NODE_LICENSE STANDALONE_DIR OUTPUT_DIR" >&2
    exit 2
fi
version=$1 api=$2 proxy=$3 iam=$4 node=$5 license=$6 console=$7 out=$8
dpkg --validate-version "$version"
for binary in "$api" "$proxy" "$iam" "$node"; do
    if [ ! -f "$binary" ] || [ ! -x "$binary" ]; then
        echo "not an executable file: $binary" >&2
        exit 2
    fi
done
for file in "$license" "$console/server.js" "$console/.next/static" "$console/public"; do
    if [ ! -e "$file" ]; then
        echo "missing runtime input: $file" >&2
        exit 2
    fi
done
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=../../scripts/versions.env
. "$src/../../scripts/versions.env"
printf '%s  %s\n' "$GLOBAL_NODE_BINARY_SHA256" "$node" | sha256sum -c -
if [ "$("$node" --version)" != "v$GLOBAL_NODE_VERSION" ]; then
    echo "Node runtime does not match scripts/versions.env" >&2
    exit 2
fi
mkdir -p "$out"
stage=$(mktemp -d "$out/.vws-global.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
chmod 0755 "$stage"
install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/vws/console" \
    "$stage/usr/lib/systemd/system" "$stage/etc/vws" "$stage/usr/share/doc/vws-global/examples"
install -m 0755 "$api" "$stage/usr/bin/vws-console-api"
install -m 0755 "$proxy" "$stage/usr/bin/vws-aws-proxy"
install -m 0755 "$iam" "$stage/usr/bin/vws-iam"
install -m 0755 "$node" "$stage/usr/lib/vws/node"
cp -R "$console/." "$stage/usr/lib/vws/console/"
install -m 0755 "$src/check-ready" "$stage/usr/lib/vws/check-global-ready"
install -m 0755 "$src/run-console-api" "$stage/usr/lib/vws/"
install -m 0644 "$src/"*.service "$stage/usr/lib/systemd/system/"
install -m 0644 "$src/global.yaml" "$stage/etc/vws/"
printf '[]\n' > "$stage/etc/vws/aws-accounts.json"
install -m 0644 "$src/README.md" "$src/copyright" "$stage/usr/share/doc/vws-global/"
install -m 0644 "$license" "$stage/usr/share/doc/vws-global/node-LICENSE"
install -m 0644 "$src/Caddyfile" "$stage/usr/share/doc/vws-global/examples/"
gzip -n -9 -c > "$stage/usr/share/doc/vws-global/changelog.Debian.gz" <<EOF
vws-global ($version) unstable; urgency=medium

  * Package the global API, AWS proxy, IAM and console.

 -- VWS maintainers <maintainers@vws.cloud>  Thu, 12 Nov 2026 00:00:00 +0000
EOF
for script in postinst prerm postrm; do
    install -m 0755 "$src/$script" "$stage/DEBIAN/$script"
done
cat > "$stage/DEBIAN/control" <<EOF
Package: vws-global
Version: $version
Section: admin
Priority: optional
Architecture: amd64
Maintainer: VWS maintainers <maintainers@vws.cloud>
Depends: adduser, curl, ca-certificates, libc6 (>= 2.36), libstdc++6, init-system-helpers (>= 1.65.2), systemd (>= 252)
Recommends: caddy
Homepage: https://github.com/popododo0720/vws-deploy
Description: Global API, identity and console services for VWS
 Runs four isolated systemd services with a bundled pinned Node runtime.
EOF
printf '/etc/vws/global.yaml\n/etc/vws/aws-accounts.json\n' > "$stage/DEBIAN/conffiles"
dpkg-deb --root-owner-group --build "$stage" "$out/vws-global_${version}_amd64.deb"
