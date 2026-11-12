#!/bin/sh
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH='' cd -- "$src/../.." && pwd)
mkdir -p "$root/build"
tmp=$(mktemp -d "$root/build/etcd-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
export TMPDIR="$tmp"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
printf '#!/bin/sh\nexit 0\n' > "$tmp/stub"
chmod 0755 "$tmp/stub"
"$src/build.sh" 0.0.0-1 "$tmp/stub" "$tmp"
deb=$tmp/vws-etcd_0.0.0-1_amd64.deb
dpkg-deb --info "$deb"
dpkg-deb --contents "$deb"
lintian --fail-on error "$deb"
shellcheck -x -P "$root" "$src/build.sh" "$src/run" "$src/check-ready" \
    "$src/check-credentials" "$src/postinst" "$src/prerm" "$src/postrm" \
    "$src/test.sh" "$src/test-install.sh" "$src/vws-etcd-config" "$src/vws-etcd-tls" \
    "$src/vws-etcd-snapshot" "$src/vws-etcd-defrag" "$src/vws-etcd-check" "$src/members.sh"
if "$src/build.sh" bad_version "$tmp/stub" "$tmp"; then
    echo "invalid version accepted" >&2
    exit 1
fi
if "$src/build.sh" 0.0.0-1 "$tmp/missing" "$tmp"; then
    echo "missing binary accepted" >&2
    exit 1
fi
mkdir -p "$tmp/guard/scripts" "$tmp/guard/packaging/vws-etcd"
cp "$root/scripts/guard.sh" "$tmp/guard/scripts/"
cp "$src/"*.service "$src/"*.timer "$tmp/guard/packaging/vws-etcd/"
git -C "$tmp/guard" init -q
git -C "$tmp/guard" add packaging
bash "$tmp/guard/scripts/guard.sh" secrets
for secret in 'Environment=ETCD_TOKEN=canary' 'EnvironmentFile=/etc/vws/etcd.conf'; do
    cp "$src/vws-etcd.service" "$tmp/guard/packaging/vws-etcd/"
    printf '\n%s\n' "$secret" >> "$tmp/guard/packaging/vws-etcd/vws-etcd.service"
    if bash "$tmp/guard/scripts/guard.sh" secrets; then
        echo "unsafe environment directive accepted" >&2
        exit 1
    fi
done
dpkg-deb -x "$deb" "$tmp/unpacked"
python3 "$src/test-runtime.py" "$src" "$tmp"
echo "etcd package checks passed"
