#!/usr/bin/env bash
# Install KubeVirt and CDI from the pinned operator manifests, then wait until both are available.
# KubeVirt runs with useEmulation when kvm-check.sh says emulation. A second run changes nothing.
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

fetch "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-operator.yaml" \
  "$KUBEVIRT_OPERATOR_SHA256" "$LAB_CACHE/kubevirt-operator.yaml"
fetch "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-operator.yaml" \
  "$CDI_OPERATOR_SHA256" "$LAB_CACHE/cdi-operator.yaml"

mode=$("$LAB_ROOT/scripts/lab/kvm-check.sh")
echo "virtualization mode: $mode"
if [[ $mode == emulation ]]; then
  sed 's/useEmulation: false/useEmulation: true/' "$LAB_ROOT/platform/kubevirt-cr.yaml" > "$LAB_CACHE/kubevirt-cr.yaml"
else
  cp "$LAB_ROOT/platform/kubevirt-cr.yaml" "$LAB_CACHE/kubevirt-cr.yaml"
fi

kc apply -f "$LAB_CACHE/kubevirt-operator.yaml"
kc apply -f "$LAB_CACHE/kubevirt-cr.yaml"
kc apply -f "$LAB_CACHE/cdi-operator.yaml"
kc apply -f "$LAB_ROOT/platform/cdi-cr.yaml"
kc -n kubevirt wait kubevirt kubevirt --for=condition=Available --timeout=600s
kc wait cdi cdi --for=condition=Available --timeout=600s
