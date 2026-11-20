#!/bin/sh
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH='' cd -- "$src/../.." && pwd)
mkdir -p "$root/build"
tmp=$(mktemp -d "$root/build/region-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
python3 "$src/../check-ops-ports.py"
python3 "$src/../test-ops-ports.py" "$src/.." "$tmp"
printf '#!/bin/sh\nexit 0\n' > "$tmp/stub"
chmod 0755 "$tmp/stub"
"$src/build.sh" 0.0.0-1 "$tmp/stub" "$tmp"
deb=$tmp/vws-region_0.0.0-1_amd64.deb
dpkg-deb --info "$deb"
dpkg-deb --contents "$deb"
lintian --fail-on error "$deb"
shellcheck "$src/build.sh" "$src/check-ready" "$src/run-region" "$src/preinst" "$src/postinst" "$src/prerm" \
    "$src/postrm" "$src/test.sh" "$src/test-install.sh"
python3 - "$deb" <<'PY'
import re
import subprocess
import sys

deb = sys.argv[1]
files = subprocess.run(["dpkg-deb", "--contents", deb], capture_output=True, text=True, check=True).stdout
control = subprocess.run(["dpkg-deb", "--field", deb], capture_output=True, text=True, check=True).stdout
for banned in ("sudoers", "helper", "keepalived", "kek", "pve/"):
    assert banned not in files.lower(), (banned, files)
depends = re.search(r"^Depends: (.*)$", control, re.M)[1]
assert not re.search(r"\b(sudo|keepalived)\b", control), control
assert "vws-region@.service" in files and "run-region" in files and "check-region-ready" in files
assert "conffiles" not in subprocess.run(["dpkg-deb", "--ctrl-tarfile", deb], capture_output=True).stdout.decode("latin-1")
print("package has no sudoers, helper, keepalived, KEK or pmxcfs handling; depends:", depends)
PY
python3 "$src/test-instances.py" "$src" "$tmp"
python3 "$src/check-instance-types.py" "$src/region.yaml"
python3 "$src/test-instance-types.py" "$src/check-instance-types.py" "$src/region.yaml" "$tmp"
mkdir -p "$tmp/guard/scripts" "$tmp/guard/packaging/vws-region"
cp "$root/scripts/guard.sh" "$tmp/guard/scripts/"
git -C "$tmp/guard" init -q
cp "$src/vws-region@.service" "$tmp/guard/packaging/vws-region/"
git -C "$tmp/guard" add packaging
bash "$tmp/guard/scripts/guard.sh" secrets
for secret in 'Environment=REGION_TOKEN=canary' 'PVEAPIToken''=canary'; do
    cp "$src/vws-region@.service" "$tmp/guard/packaging/vws-region/"
    printf '\n%s\n' "$secret" >> "$tmp/guard/packaging/vws-region/vws-region@.service"
    if bash "$tmp/guard/scripts/guard.sh" secrets; then
        echo "secret in packaged unit accepted" >&2
        exit 1
    fi
done
if "$src/build.sh" bad_version "$tmp/stub" "$tmp"; then
    echo "invalid version accepted" >&2
    exit 1
fi
if "$src/build.sh" 0.0.0-1 "$tmp/missing" "$tmp"; then
    echo "missing binary accepted" >&2
    exit 1
fi
python3 - "$src" <<'PY'
import pathlib
import re
import sys

src = pathlib.Path(sys.argv[1])
unit = (src / "vws-region@.service").read_text().splitlines()
for directive in ("User=vws-region", "StateDirectory=vws-region/%i", "StateDirectoryMode=0700",
                  "LoadCredential=region-key:/etc/vws-region/%i/region.key",
                  "MemoryMax=512M", "CPUQuota=50%", "UMask=0077", "ProtectSystem=strict"):
    assert directive in unit, directive
exec_start = next(line for line in unit if line.startswith("ExecStart="))
for flag in ("-region %i", "-state-dir /var/lib/vws-region/%i"):
    assert flag in exec_start, flag
for banned in ("-registration-file", "-helper", "-pve-endpoint", "-pve-ca-file", "pve-cluster", "/etc/pve"):
    assert not any(banned in line for line in unit), banned
assert not any(line.startswith(("Environment=", "EnvironmentFile=")) and "TOKEN" in line.upper() for line in unit)
assert not list(src.rglob("*.service.d")), "unit drop-in"
env = (src / "region.env").read_text()
assert re.search(r"^VWS_REGION_ADDR=", env, re.M) and re.search(r"^VWS_REGION_OPS_ADDR=", env, re.M)
print("instance unit: private state directory, per-instance credential, no node-level flags")
PY
echo "region package checks passed"
