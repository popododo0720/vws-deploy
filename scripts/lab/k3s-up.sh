#!/usr/bin/env bash
# Install k3s at K3S_VERSION and wait for the node. A second run changes nothing.
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [[ ${LAB_DRY_RUN:-0} != 1 ]] && k3s --version 2>/dev/null | grep -q -F "$K3S_VERSION"; then
  echo "k3s $K3S_VERSION already installed"
else
  fetch "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION/+/%2B}/install.sh" "$K3S_INSTALL_SHA256" "$LAB_CACHE/k3s-install.sh"
  run sudo env INSTALL_K3S_VERSION="$K3S_VERSION" sh "$LAB_CACHE/k3s-install.sh"
fi
kc wait --for=condition=Ready node --all --timeout=180s
