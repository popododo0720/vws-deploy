#!/usr/bin/env bash
# Print "kvm" when root can read and write /dev/kvm, else "emulation".
# KubeVirt gets useEmulation only in the second case.
set -euo pipefail

if sudo test -r /dev/kvm && sudo test -w /dev/kvm; then
  echo kvm
else
  echo emulation
fi
