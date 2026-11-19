#!/bin/sh
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH='' cd -- "$src/../.." && pwd)
mkdir -p "$root/build"
tmp=$(mktemp -d "$root/build/global-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
shellcheck -x -P "$src" "$src/"*.sh "$src/../build.sh" "$src/check-ready" "$src/run-console-api" \
    "$src/postinst" "$src/prerm" "$src/postrm"
python3 "$src/test-package.py" "$src" "$tmp"
# The source build refuses a missing or invalid version before it creates or fetches anything.
for arguments in "" "bad_version $tmp/unused"; do
    # shellcheck disable=SC2086 # split into the argument list
    if "$src/../build.sh" $arguments >/dev/null 2>&1 || [ -e "$tmp/unused" ]; then
        echo "build.sh accepted: $arguments" >&2
        exit 1
    fi
done
lintian --fail-on error "$tmp/out/vws-global_0.0.0-1_amd64.deb"
echo "global package checks passed"
