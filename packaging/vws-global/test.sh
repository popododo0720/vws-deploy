#!/bin/sh
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH='' cd -- "$src/../.." && pwd)
mkdir -p "$root/build"
tmp=$(mktemp -d "$root/build/global-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
shellcheck -x -P "$src" "$src/"*.sh "$src/check-ready" "$src/run-console-api" \
    "$src/postinst" "$src/prerm" "$src/postrm"
python3 "$src/test-package.py" "$src" "$tmp"
lintian --fail-on error "$tmp/out/vws-global_0.0.0-1_amd64.deb"
echo "global package checks passed"
