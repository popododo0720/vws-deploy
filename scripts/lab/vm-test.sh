#!/usr/bin/env bash
# Start one test VM, wait for Running, restart k3s, and wait for Running again.
# This is the node-restart recovery check of the lab. A second run reuses the VM.
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

cat > "$LAB_CACHE/lab-vm.yaml" <<YAML
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: lab-vm
  namespace: default
spec:
  runStrategy: Always
  template:
    spec:
      domain:
        devices:
          disks:
            - name: root
              disk:
                bus: virtio
        resources:
          requests:
            memory: 128Mi
      volumes:
        - name: root
          containerDisk:
            image: quay.io/kubevirt/cirros-container-disk-demo:${KUBEVIRT_VERSION}
YAML

kc apply -f "$LAB_CACHE/lab-vm.yaml"
kc wait vmi lab-vm --for=jsonpath='{.status.phase}'=Running --timeout=600s
run sudo systemctl restart k3s
kc wait --for=condition=Ready node --all --timeout=180s
kc wait vmi lab-vm --for=jsonpath='{.status.phase}'=Running --timeout=600s
echo "lab-vm is Running after the k3s restart"
