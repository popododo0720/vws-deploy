#!/usr/bin/env bash
# Remove the test VM, KubeVirt, CDI and k3s. Missing objects are not errors.
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if command -v k3s >/dev/null 2>&1 || [[ ${LAB_DRY_RUN:-0} == 1 ]]; then
  kc delete vm lab-vm --ignore-not-found --wait=true
  kc delete -f "$LAB_ROOT/platform/cdi-cr.yaml" --ignore-not-found --wait=true
  kc delete -f "$LAB_ROOT/platform/kubevirt-cr.yaml" --ignore-not-found --wait=true
fi
if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
  run sudo /usr/local/bin/k3s-uninstall.sh
fi
run rm -rf "$LAB_CACHE"
