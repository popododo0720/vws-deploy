# Installing the platform bundle

These are the components that must be in the cluster before the VWS control plane goes up. The design is https://github.com/popododo0720/vws-deploy/issues/2. This repository applies nothing to a cluster. The procedure below is the order in which a person runs things on a cluster; it was not run on this box (unverified). `make check` only does the render and schema checks.

## Versions

Versions and the sha256 of downloaded files live only in `scripts/versions.env`. Do not write version numbers in this document or other files. `scripts/guard.sh bundle` checks this.

| Component | Variable | How it is installed |
| --- | --- | --- |
| Multus | `MULTUS_VERSION` | Upstream thick daemonset manifest. The image is rewritten to the pinned release tag and digest because the upstream manifest points at the moving `snapshot-thick` tag (#202). The `kube-multus` memory is changed to request 100Mi and limit 300Mi (the upstream 50Mi was OOMKilled on node reboots, #88) |
| Kube-OVN | `KUBEOVN_VERSION` | Helm chart `kube-ovn`, values `platform/kube-ovn.values.yaml` |
| Rook operator | `ROOK_VERSION` | Helm chart `rook-ceph`, values `platform/rook-ceph.values.yaml` |
| Rook Ceph cluster | `ROOK_VERSION` | Helm chart `rook-ceph-cluster`, values `platform/rook-ceph-cluster.values.yaml` |
| KubeVirt | `KUBEVIRT_VERSION` | Upstream operator manifest, CR `platform/kubevirt-cr.yaml`. `spec.infra.nodePlacement.nodeSelector: kubernetes.io/os: linux` keeps virt-api, virt-controller and virt-exportproxy off a control-plane-only placement. With the default placement, draining the only control-plane node of a cluster stalls on the PDBs (#168) |
| CDI | `CDI_VERSION` | Upstream operator manifest, CR `platform/cdi-cr.yaml` |
| csi-driver-nfs | `CSI_DRIVER_NFS_VERSION` | Helm chart `csi-driver-nfs` (kubernetes-csi), values `platform/csi-driver-nfs.values.yaml`. It creates no StorageClass, VolumeSnapshotClass, snapshot controller or CRDs. `35-csi-driver-nfs.yaml` is always in the bundle, but `vws apply` installs it only with `spec.storage.driver: nfs` (#170) |

`scripts/platform.sh` renders the above into `build/platform/` and checks it with kubeconform. Downloaded manifests are checked against their sha256. The number in each file name the script creates is the partial order of the installation order below.

## Installation order

The startup order is etcd, API, CNI, CSI, KubeVirt, VWS. Do not start a step before the previous one is Ready.

1. **etcd and the API server.** The Kubernetes distribution provides them. Production is Talos (#8) and the local lab is k3s (#7). Backup and restore follow the runbook from #6.
2. **CNI.** Start without the distribution's default CNI, or use Kube-OVN as the default CNI. Install Kube-OVN first (`20-kube-ovn.yaml`), then Multus (`10-multus.yaml`). VMs connect through Multus NetworkAttachmentDefinitions (provider kube-ovn).
3. **CSI.** The Rook operator (`30-rook-ceph-operator.yaml`), then the Ceph cluster (`31-rook-ceph-cluster.yaml`). The default StorageClass is Ceph RBD.
   With `spec.storage.driver: nfs`, install csi-driver-nfs (`35-csi-driver-nfs.yaml`) and StorageClass `vws-nfs` (`36-vws-nfs-storageclass.yaml`, which `vws apply` builds from the CR). The NFS export must already exist and be mountable from every node.
4. **KubeVirt and CDI.** Install the CRs (`41-`, `43-`) after the operators (`40-`, `42-`) are Ready.
5. **VWS.** Install `charts/vws`. Controller rights come from each service's `rbac` in the chart.

## Storage class names

The names the Rook Ceph cluster chart creates are matched with vws-storage's settings. The bundle values files do not override the names, so they are the chart defaults. The bundle names are not changed; vws-storage's defaults follow the bundle.

| Target | Name the bundle creates | vws-storage side |
| --- | --- | --- |
| StorageClass | `ceph-block` | the value in the volume type table (`kube.Classes`) |
| VolumeSnapshotClass | `ceph-block` | the `snapshotClass` argument of `kube.NewBlock` |

With `spec.storage.driver: nfs`, `scripts/vws.d/lib/platform.py` creates StorageClass `vws-nfs` (provisioner `nfs.csi.k8s.io`, parameters `server` and `share` from the CR's `spec.storage.nfs`, `reclaimPolicy: Delete`, `allowVolumeExpansion: true`). There is no VolumeSnapshotClass (no snapshots). vws-storage runs with `-kube-volume-mode=Filesystem` and creates `disk.img` through a CDI blank DataVolume (vws-storage#73). With any other driver, neither csi-driver-nfs nor `vws-nfs` is installed. Provisioning on a real NAS was not checked (needs checking, vws-test).

Both names must match the names the bundle creates. `scripts/guard.sh bundle` checks that each name appears exactly once in `build/platform/31-rook-ceph-cluster.yaml` (created by `scripts/platform.sh`), that the provisioner and driver match, and that this document lists the same names. PVC binding and VolumeSnapshot creation on a real cluster were not checked (needs checking, T1 or above).

## CDI feature gates

`platform/cdi-cr.yaml` turns on `HonorWaitForFirstConsumer` in `spec.config.featureGates`. This gate is opt-in and is off when absent from the CR. When on, CDI does not create worker pods on a `WaitForFirstConsumer` StorageClass until the VM is scheduled. The default Ceph class is `Immediate`, so the gate does nothing there. The local lab's default k3s class (`local-path`) is `WaitForFirstConsumer`, where it matters. The gate name and field path were checked against the source, docs and CRD of the pinned CDI. `scripts/guard.sh bundle` checks that this CR's fields exist in the bundle CRD schema. Whether DataVolumes work on a real cluster was not checked (needs checking, T1 or above).

## Required host kernel modules

The node OS must provide them. The code in this repository installs nothing on the hosts.

| Module | Used by |
| --- | --- |
| `rbd` | Ceph RBD CSI attaches volumes to nodes |
| `openvswitch` | the Kube-OVN data path |
| `geneve` | the default Kube-OVN tunnel (`TUNNEL_TYPE` in `platform/kube-ovn.values.yaml`) |
| `nfs` | with `spec.storage.driver: nfs`, csi-driver-nfs mounts the export on nodes (NFS client) |
| `kvm` (`kvm_intel` or `kvm_amd`) | KubeVirt VM acceleration. Without it `useEmulation` in `platform/kubevirt-cr.yaml` must be on, and it is very slow |

## What a real cluster needed (vws-test, #51)

Confirmed on k3s v1.35.9 + external Ceph (ceph-csi-rbd), 3 nodes. Other distributions are unverified.

- **A LoadBalancer implementation (#86).** The chart's `external: true` services are `type: LoadBalancer`. On k3s with servicelb off, EXTERNAL-IP stays pending and `helm --wait --atomic` in `vws apply` waits until the timeout. Install an implementation such as MetalLB (L2, IPAddressPool and L2Advertisement) first, or turn off the service's `external`.
- **LB addresses in a lab on OpenStack (#86).** OVN does not answer ARP for addresses it does not know, so the LB address was added as an extra fixed IP on the advertising node's port. With an extra fixed IP, cloud-init DHCP picked a different address and the node address changed, so pin the node address with a static netplan and turn off cloud-init network configuration.
- **Block device ownership (#87).** For the CDI importer to write to a Block mode PVC, containerd needs `device_ownership_from_security_context = true`. On k3s (containerd v2, config v3) put it in the drop-in `/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/10-device-ownership.toml`, not in `config-v3.toml.tmpl`, and restart k3s.

  ```toml
  version = 3
  [plugins."io.containerd.cri.v1.runtime"]
    device_ownership_from_security_context = true
  ```
- **Rebooting krbd nodes (#87).** Simply rebooting a node with mapped RBDs took the network down first, and shutdown hung on `libceph ... error -101`. Move the RBD pods off with `kubectl drain` before rebooting. When it hung, a hard reboot from the hypervisor was needed.
  Reboot procedure (#116, #150). If any step exits non-zero, stop there and do not reboot. ① `vws verify-reboot --snapshot FILE`: emits `WARN krbd` for each node with an attached RBD volume (read only). If any list cannot be read it exits 3 and leaves no file. ② Empty each warned node with `kubectl drain NODE --ignore-daemonsets --delete-emptydir-data`. Stop if the drain fails. ③ `vws verify-reboot --drained NODE [--timeout 5m]`: 0 only when the node is cordoned and no `rbd.csi.ceph.com` VolumeAttachment remains. It re-reads until `--timeout` (5m by default) and prints the remaining PV names; it fails (1) only when the detach is still incomplete at the timeout (3 when unreadable). Go on only on 0. ④ Reboot. ⑤ `kubectl uncordon NODE`. ⑥ `vws verify-reboot --check FILE`.

## Known limits

- Only the render and kubeconform were checked. Real cluster installation, startup order and upgrades are unverified. Measurements happen in the T2 lab.
- kubeconform skips CRD-based resources (CephCluster, KubeVirt, CDI and others) because their schemas are not in the default catalog. Only their syntax is checked.
- `KUBERNETES_VERSION` (the kubeconform schemas) and `K3S_VERSION` must be the same Kubernetes minor, and `scripts/guard.sh bundle` checks this. That minor was picked inside the intersection of the documented support ranges (KubeVirt, Rook, Kube-OVN). No support range was found in the docs for CDI and Multus (needs checking). Behaviour on a real cluster was not checked either.

## Bundle adjusted to the node networks (#127)

`vws apply` does not use the bundle as is; it uses a copy that `scripts/vws.d/lib/platform.py` adjusts to the CR. Without `spec.network.nodes` the copy is byte-identical. With it, only the following change (design #125).

| Target | Value |
| --- | --- |
| kube-ovn-cni `--iface` | the host interface of the tunnel network (`<nic>` or `<nic>.<vlan>`). management when there is no tunnel |
| kube-ovn-cni `--mtu` | tunnel `mtu` − 100 (geneve overhead) |
| CephCluster `spec.network` | `provider: host`, `addressRanges.public`/`cluster` (only when storage is set) |

If the bundle's kube-ovn-cni does not have exactly one empty `--iface=` argument (the chart changed), the exit code is 2. `ovn-external-gw-config` is not created (#164). vws-test evidence 53: with this ConfigMap on, kube-ovn-controller deletes the underlay bridge `br-vwsul` and requires a Subnet named `external`. EIP/SNAT for user VPCs combined with `extraExternalSubnets` did not need it. vws-network re-applies the gateway node label `ovn.kubernetes.io/external-gw` through its reconcile (owned by vws-network, RBAC in [rbac.md](rbac.md)). A ConfigMap created by an earlier apply (field manager `vws`) is deleted by `vws apply` before the unchanged decision (#171). A ConfigMap of the same name that vws did not write is not deleted; it is a WARN. Needs checking: whether deleting the ConfigMap alone makes kube-ovn restore `br-vwsul`, or whether the node label or `br-external` also need cleanup, was not measured on vws-test. Needs checking: the impact of changing `--iface`/`--mtu`/the Ceph network in operation (kube-ovn-cni restart, mon redeploy). Not measured on a real cluster.

### OVN gateway_mtu (#237)

When `spec.network.nodes.external` is set (subnet `vws-external`, MTU `external.mtu`, else 1500), or, without it, when the external subnet is given only through the vws-network flag `spec.services.network.flags.kube-external-subnet` (that subnet, MTU 1500, #246), `platform.py` adds `22-vws-ovn-gateway-mtu.yaml`. If the flag value is not a subnet name (DNS label), the exit code is 2. With neither, the file is not created. Deployment `vws-ovn-gateway-mtu` in kube-system (1 replica) looks in the OVN NB every 30 seconds for logical_router_ports whose names end in `-<external subnet>` (Kube-OVN names the VPC port of `extraExternalSubnets` `<vpc>-<subnet>`; the external subnet is `vws-external` for `nodes.external`, else the flag value) and runs `ovn-nbctl set` only when `options:gateway_mtu` differs from the external link MTU (`nodes.external.mtu`, else 1500). When they match it writes nothing, so it is idempotent, and new VPCs are picked up on the next cycle. Why: VPC VMs use 9001 (#236), but the bundle's Kube-OVN does not set `gateway_mtu` and has no field for it, so the VPC router does not send ICMP errors for large DF packets headed to the 1500 external network (PMTUD fails, vws-network `docs/mtu.md`). OVN answers an oversized IPv4 packet with ICMP type 3 code 4 and, when the port has an IPv6 address, an oversized IPv6 packet with ICMPv6 type 2 (Packet Too Big), so the one option covers both families. vws-network uses only the Kubernetes API and cannot write to the NB.

- Connection: the `ovn-nb` Service (kube-system, TCP 6641; the bundle has `ENABLE_SSL=false`) through service link environment variables. No Kubernetes token or RBAC (`automountServiceAccountToken: false`).
- Image: `docker.io/kubeovn/kube-ovn:<KUBEOVN_VERSION>` (`scripts/versions.env`), already on the nodes, so no new image is pulled. runAsNonRoot 65534, read-only root, no capabilities.
- Alternatives and why they were dropped: a Job runs once and misses later VPCs. Putting it in vws-network would give the service NB access and break the repository boundary (Kubernetes API only). TCP MSS clamping fixes only TCP, leaves UDP/ICMP PMTUD broken, and has no standard mechanism on OVN logical routers. If a Kube-OVN upgrade solves it, delete this Deployment.
- Needs checking: not measured on vws-test. Whether gateway_mtu actually emits ICMP needs-frag (the distributed gateway port condition), whether `ovn-nbctl` runs as non-root, whether the Service follows NB leader changes, and whether the IPv6 Packet Too Big path (#246) works end to end. The flag-only path is created since #246: without `nodes.external`, the Deployment renders on `spec.services.network.flags.kube-external-subnet` with MTU 1500.
