# Quick start

**Preparing the cluster is out of scope.** You need Kubernetes with one or more nodes and cluster-admin access through `kubectl` already (vws-deploy#43 section 10).

**Node addresses must be static (#118).** If DHCP changes a node IP, the node registration, the CNI tunnel (Kube-OVN geneve) and the certificate SANs all point at the old address and the node does not come back (this actually happened on vws-test after node1 rebooted). Use a static IP or a DHCP reservation, and give k3s that address with `--node-ip`. The `node-ip` check of `vws preflight` compares a k3s node's `--node-ip` (annotation `k3s.io/node-args`) with its InternalIP and warns when they differ or it is missing. Other distributions are not checked.

1. Build `build/bootstrap.yaml` with `scripts/bootstrap.sh` (the VWSCluster CRD, namespace `vws`, the installer ServiceAccount and temporary rights, the installer Deployment and ClusterIP Service, and the reaper Job). The installer image is not published yet: the digest in `VWS_INSTALLER_IMAGE` is all zeros. Until it is published this procedure does not work (needs checking).
2. `kubectl apply -f build/bootstrap.yaml`
3. Interactive: `kubectl -n vws exec -it deploy/vws-installer -- vws init` (installer API TUI, #142). Connection details for the web wizard: `kubectl -n vws exec deploy/vws-installer -- vws init --print-url`. Unattended: `kubectl -n vws exec -i deploy/vws-installer -- vws init --from /dev/stdin < my-vwscluster.yaml` (examples in `examples/vwscluster/`).
4. `kubectl -n vws exec deploy/vws-installer -- vws apply --unattended`. A second run prints `unchanged`. Steps 3 and 4 in one go: `kubectl -n vws exec -i deploy/vws-installer -- sh -c 'cat > /tmp/cr.yaml && vws apply --unattended -f /tmp/cr.yaml' < my-vwscluster.yaml` (#144).
5. `kubectl -n vws exec deploy/vws-installer -- vws installer finish`. It also confirms that the temporary cluster-admin is gone. On failure it exits 1; then run `kubectl delete clusterrolebinding vws-installer-temp` yourself.

If you forget finish, the `vws-installer-reaper` Job deletes the binding at its creation time + `INSTALLER_TTL_SECONDS` (4 hours). Details are in `docs/installer.md`.
