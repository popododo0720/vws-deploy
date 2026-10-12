#!/usr/bin/env bash
# Render the pinned platform bundle into build/platform and validate it with kubeconform.
# Nothing is applied to a cluster.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
# shellcheck disable=SC1091
source scripts/versions.env

out=build/platform
mkdir -p "$out" .cache/platform .cache/kubeconform

fetch() {
  local url=$1 sha=$2 file=$3
  [[ -f $file ]] && echo "${sha}  ${file}" | sha256sum -c --status - && return 0
  curl -fsSL -o "$file" "$url"
  echo "${sha}  ${file}" | sha256sum -c -
}

fetch "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-operator.yaml" \
  "$KUBEVIRT_OPERATOR_SHA256" .cache/platform/kubevirt-operator.yaml
fetch "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-operator.yaml" \
  "$CDI_OPERATOR_SHA256" .cache/platform/cdi-operator.yaml
fetch "https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/${MULTUS_VERSION}/deployments/multus-daemonset-thick.yml" \
  "$MULTUS_SHA256" .cache/platform/multus.yaml

cp .cache/platform/multus.yaml "$out/10-multus.yaml"
helm template kube-ovn kube-ovn --repo https://kubeovn.github.io/kube-ovn/ --version "$KUBEOVN_VERSION" \
  --namespace kube-system -f platform/kube-ovn.values.yaml > "$out/20-kube-ovn.yaml"
helm template rook-ceph rook-ceph --repo https://charts.rook.io/release --version "$ROOK_VERSION" \
  --namespace rook-ceph -f platform/rook-ceph.values.yaml > "$out/30-rook-ceph-operator.yaml"
helm template rook-ceph-cluster rook-ceph-cluster --repo https://charts.rook.io/release --version "$ROOK_VERSION" \
  --namespace rook-ceph -f platform/rook-ceph-cluster.values.yaml > "$out/31-rook-ceph-cluster.yaml"
cp .cache/platform/kubevirt-operator.yaml "$out/40-kubevirt-operator.yaml"
cp platform/kubevirt-cr.yaml "$out/41-kubevirt-cr.yaml"
cp .cache/platform/cdi-operator.yaml "$out/42-cdi-operator.yaml"
cp platform/cdi-cr.yaml "$out/43-cdi-cr.yaml"

# CRs of the bundle have no schema in the default catalog, so they are skipped by kind.
kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version "$KUBERNETES_VERSION" \
  -cache .cache/kubeconform "$out"
