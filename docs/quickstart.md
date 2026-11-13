# Quick start

> Legacy Kubernetes preparation and rights reclaim remain until #775. Chart installation is retired.

**Preparing the cluster is out of scope.** You need Kubernetes with one or more nodes and cluster-admin access through `kubectl` already (vws-deploy#43 section 10).

**Node addresses must be static (#118).** If DHCP changes a node IP, the node registration, the CNI tunnel (Kube-OVN geneve) and the certificate SANs all point at the old address and the node does not come back (this actually happened on vws-test after node1 rebooted). Use a static IP or a DHCP reservation, and give k3s that address with `--node-ip`.

Chart installation is retired. Use [the operator guide](operator/regions.md) for deployment.

For existing Kubernetes installations, `vws installer finish` reclaims the temporary installation rights. The reaper Job also removes the binding at its creation time plus `INSTALLER_TTL_SECONDS`; see [the installer guide](installer.md).
