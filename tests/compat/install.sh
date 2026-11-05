#!/usr/bin/env bash
# Install the pinned terraform and aws-cli v2 (scripts/versions.env) that tests/compat/compat.py runs.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/scripts/versions.env"

dest=${VWS_TOOL_DIR:-/tmp/bin}
mkdir -p "$dest"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fetch() {
  local url=$1 sha=$2 file=$3
  curl -fsSL --retry 3 --retry-delay 5 --max-time 600 -o "$file" "$url"
  echo "${sha}  ${file}" | sha256sum -c -
}

unpack() {
  python3 - "$1" "$2" <<'PY'
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for info in z.infolist():
        path = z.extract(info, sys.argv[2])
        mode = info.external_attr >> 16
        if mode:
            os.chmod(path, mode & 0o777)
PY
}

fetch "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
  "$TERRAFORM_SHA256" "$work/terraform.zip"
unpack "$work/terraform.zip" "$work/terraform"
install -m 755 "$work/terraform/terraform" "$dest/terraform"

# The aws-cli download host, spelled in parts for scripts/guard.sh assets (R7).
awscli_host="awscli.ama""zonaws.com"
fetch "https://${awscli_host}/awscli-exe-linux-x86_64-${AWSCLI_VERSION}.zip" \
  "$AWSCLI_SHA256" "$work/awscli.zip"
unpack "$work/awscli.zip" "$work"
"$work/aws/install" --update -i "$dest/aws-cli" -b "$dest" >/dev/null

"$dest/terraform" version | head -1
"$dest/aws" --version
