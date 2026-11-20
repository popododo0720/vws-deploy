#!/usr/bin/env bash
# Install the pinned otelcol-contrib and etcd binaries the guards and tests use.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/scripts/versions.env"

dest=${VWS_TOOL_DIR:-/tmp/bin}
mkdir -p "$dest"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fetch() {
  local url=$1 sha=$2 file=$3
  curl -fsSL -o "$file" "$url"
  echo "${sha}  ${file}" | sha256sum -c -
}

fetch "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/otelcol-contrib_${OTELCOL_VERSION}_linux_amd64.tar.gz" \
  "$OTELCOL_SHA256" "$work/otelcol.tgz"
tar -xzf "$work/otelcol.tgz" -C "$work" otelcol-contrib
install -m 755 "$work/otelcol-contrib" "$dest/otelcol-contrib"

fetch "https://github.com/etcd-io/etcd/releases/download/${ETCD_VERSION}/etcd-${ETCD_VERSION}-linux-amd64.tar.gz" \
  "$ETCD_SHA256" "$work/etcd.tgz"
tar -xzf "$work/etcd.tgz" -C "$work"
for bin in etcd etcdctl etcdutl; do
  install -m 755 "$work/etcd-${ETCD_VERSION}-linux-amd64/${bin}" "$dest/${bin}"
done

echo "installed otelcol-contrib and etcd in ${dest}"
