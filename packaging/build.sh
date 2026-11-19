#!/bin/sh
# Usage: packaging/build.sh VERSION [OUTPUT_DIR]
# Builds the release set pinned in scripts/versions.env from source and packages vws-etcd, vws-global
# and vws-region as OUTPUT_DIR/*.deb (default build/). Needs git, go, curl, sha256sum, tar, xz and
# dpkg-deb. The console is built with the pinned Node archive, not the Node on PATH.
# OUTPUT_DIR/src keeps the checkouts and downloads. A checkout already at its pinned commit and a
# download that still matches its checksum are reused, so a rerun fetches nothing; the Go and npm
# caches must then hold the dependencies. VWS_GIT_BASE names another origin for the three source
# repositories, for example a local mirror.
set -eu
umask 022
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: $0 VERSION [OUTPUT_DIR]" >&2
    exit 2
fi
version=$1
dpkg --validate-version "$version"
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=../../scripts/versions.env
. "$src/../scripts/versions.env"
mkdir -p "${2:-$src/../build}"
out=$(CDPATH='' cd -- "${2:-$src/../build}" && pwd)
work=$out/src
mkdir -p "$work/bin"

# checkout REPOSITORY COMMIT: $work/REPOSITORY holds exactly the tracked files of COMMIT.
checkout() {
    if [ "$(git -C "$work/$1" rev-parse --verify -q HEAD 2>/dev/null)" != "$2" ] ||
        [ "$(git -C "$work/$1" rev-parse --show-toplevel)" != "$work/$1" ]; then
        rm -rf "${work:?}/$1"
        git init -q "$work/$1"
        git -C "$work/$1" fetch -q --depth 1 "${VWS_GIT_BASE:-https://github.com/popododo0720}/$1.git" "$2"
    fi
    git -C "$work/$1" -c advice.detachedHead=false checkout -q --force --detach "$2"
}

# fetch URL SHA256 FILE: FILE has that checksum, downloaded only when it does not already.
fetch() {
    if [ ! -f "$3" ] || ! printf '%s  %s\n' "$2" "$3" | sha256sum -c --status -; then
        curl -fsSL --retry 3 -o "$3" "$1"
        printf '%s  %s\n' "$2" "$3" | sha256sum -c -
    fi
}

# gobuild REPOSITORY COMMAND: a static linux/amd64 $work/bin/COMMAND from REPOSITORY/cmd/COMMAND.
gobuild() {
    (cd "$work/$1" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o "$work/bin/$2" "./cmd/$2")
}

checkout vws-console-api "$GLOBAL_CONSOLE_API_REF"
checkout vws-iam "$GLOBAL_IAM_REF"
checkout vws-console "$GLOBAL_CONSOLE_REF"
for command in vws-console-api vws-aws-proxy vws-region vws-region-helper; do
    gobuild vws-console-api "$command"
done
gobuild vws-iam vws-iam

node=node-v$GLOBAL_NODE_VERSION-linux-x64
fetch "https://nodejs.org/dist/v$GLOBAL_NODE_VERSION/$node.tar.xz" "$GLOBAL_NODE_ARCHIVE_SHA256" "$work/$node.tar.xz"
rm -rf "${work:?}/$node" "$work/standalone"
tar -xJf "$work/$node.tar.xz" -C "$work"
(
    cd "$work/vws-console"
    PATH=$work/$node/bin:$PATH
    npm ci --no-audit --no-fund
    NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 npm run build
    cp -R .next/standalone "$work/standalone"
    cp -R public "$work/standalone/public"
    cp -R .next/static "$work/standalone/.next/static"
)

etcd=etcd-$ETCD_VERSION-linux-amd64
fetch "https://github.com/etcd-io/etcd/releases/download/$ETCD_VERSION/$etcd.tar.gz" "$ETCD_SHA256" "$work/$etcd.tar.gz"
tar -xzf "$work/$etcd.tar.gz" -C "$work" "$etcd/etcd"

"$src/vws-etcd/build.sh" "$version" "$work/$etcd/etcd" "$out"
"$src/vws-global/build.sh" "$version" "$work/bin/vws-console-api" "$work/bin/vws-aws-proxy" \
    "$work/bin/vws-iam" "$work/$node/bin/node" "$work/$node/LICENSE" "$work/standalone" "$out"
"$src/vws-region/build.sh" "$version" "$work/bin/vws-region" "$work/bin/vws-region-helper" "$out"
