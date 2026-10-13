#!/usr/bin/env bash
# Render build/bootstrap.yaml: the VWSCluster CRD from charts/vws/crds (the API group stays written in
# one place) and bootstrap/bootstrap.yaml with the installer image and TTL from scripts/versions.env.
# Validate it with kubeconform. Nothing is applied to a cluster.
set -euo pipefail

ROOT=$(CDPATH="" cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
# shellcheck disable=SC1091
source scripts/versions.env

group=$(sed -n 's/^  group: //p' charts/vws/crds/vwsclusters.yaml)
[[ -n $group ]] || { echo "bootstrap: no spec.group in the CRD" >&2; exit 1; }
version=$(sed -n 's/^    - name: //p' charts/vws/crds/vwsclusters.yaml)
[[ $version =~ ^v[0-9a-z]+$ ]] || { echo "bootstrap: the CRD needs exactly one version, got '${version}'" >&2; exit 1; }
mkdir -p build .cache/kubeconform
{
  cat charts/vws/crds/vwsclusters.yaml
  echo '---'
  sed -e "s#INSTALLER_IMAGE#${VWS_INSTALLER_IMAGE}#g" \
    -e "s#REAPER_DEADLINE_SECONDS#$((INSTALLER_TTL_SECONDS + 600))#g" \
    -e "s#INSTALLER_TTL_SECONDS#${INSTALLER_TTL_SECONDS}#g" \
    -e "s#VWS_CRD_VERSION#${version}#g" \
    -e "s#VWS_API_GROUP#${group}#g" bootstrap/bootstrap.yaml
} > build/bootstrap.yaml
# The kubeconform catalog has no CustomResourceDefinition schema; scripts/guard.sh crd checks it.
kubeconform -strict -summary -skip CustomResourceDefinition -kubernetes-version "$KUBERNETES_VERSION" \
  -cache .cache/kubeconform build/bootstrap.yaml
