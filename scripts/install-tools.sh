#!/usr/bin/env bash
# Install the pinned helm, kubeconform, otelcol-contrib and etcd binaries used by make check and CI.
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

fetch "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
  "$HELM_SHA256" "$work/helm.tgz"
tar -xzf "$work/helm.tgz" -C "$work"
install -m 755 "$work/linux-amd64/helm" "$dest/helm"

fetch "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" \
  "$KUBECONFORM_SHA256" "$work/kubeconform.tgz"
tar -xzf "$work/kubeconform.tgz" -C "$work"
install -m 755 "$work/kubeconform" "$dest/kubeconform"

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

echo "installed helm, kubeconform, otelcol-contrib and etcd in ${dest}"
