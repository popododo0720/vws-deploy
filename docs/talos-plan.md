# Talos bootstrap and upgrade plan

The production Kubernetes distribution is Talos. The local lab is k3s; RKE2 is not used. The design is https://github.com/popododo0720/vws-deploy/issues/2.

This document is a plan. Nothing was applied to a cluster, and this box has neither Talos tools nor normal-kernel VMs, so no procedure was run. Every procedure and example configuration is therefore **unverified**. Measurements happen in the T2 lab (three or more normal-kernel VMs). Below, "needs checking" means the content is recalled from documentation and must be confirmed in the lab.

## No units of ours on the hosts

Talos has no SSH, no shell and no way to add systemd units. Nodes are managed only through the machine configuration and the `talosctl` machine API. This repository has no `.service` or `.target` files, and `scripts/guard.sh layout` (R10) checks that. So Talos does not conflict with this principle. Two conditions must hold: system extensions are used only to provide kernel modules, never extension services (daemons on the host), and node state changes only through the machine configuration.

## Layout

- Three control-plane nodes and N workers. etcd is stacked on the control plane; losing one of the three keeps quorum.
- No workloads on control-plane nodes (`allowSchedulingOnControlPlanes: false`).
- The API server endpoint is one VIP shared by the control plane. Example: `docs/talos/controlplane-patch.yaml`.
- The default CNI is off (`cluster.network.cni.name: none`). Kube-OVN is the CNI. Nodes are NotReady until the CNI is up, so Kube-OVN must go on right after the bootstrap.
- Generating configuration: `talosctl gen config vws https://<vip>:6443 --config-patch @docs/talos/controlplane-patch.yaml --output-types controlplane`, and for workers `--config-patch @docs/talos/worker-patch.yaml --output-types worker`. On each node, `talosctl apply-config --insecure -n <ip> --file <file>`.
- Bootstrap: `talosctl bootstrap -n <cp-ip>` once on one control-plane node, then `talosctl kubeconfig`.
- Security: `talosconfig` and the generated secrets (`secrets.yaml`) never go into the repository.

## Startup order

etcd, API, CNI (Kube-OVN), CSI (Rook), KubeVirt, VWS. Talos brings up etcd and the API; the later steps follow the order and checks in `docs/platform-bundle.md`. The Rook, KubeVirt and Kube-OVN namespaces need privileged pods. Talos's default pod security level may block them, so whether the namespaces need a privileged label must be checked in the lab (needs checking).

## Kernel modules and extensions

The required modules are `rbd`, `openvswitch`, `geneve` and `kvm`. The reasons are in `docs/platform-bundle.md`.

| Module | How it is provided | Status |
| --- | --- | --- |
| `kvm` | Recalled as built into the Talos kernel. VM acceleration needs `/dev/kvm` | needs checking |
| `geneve` | machine configuration `machine.kernel.modules` | needs checking |
| `openvswitch` | machine configuration `machine.kernel.modules`. If the default kernel lacks it, an installer image with a system extension (Image Factory schematic) is needed | needs checking |
| `rbd` | machine configuration `machine.kernel.modules`, else a system extension | needs checking |

The first thing to do in the lab is to see whether the modules actually load. Check with `talosctl -n <ip> read /proc/modules`; if one is missing, `talosctl upgrade --image` with an image that includes the extension. The installer image reference is reused on upgrades, so record the schematic ID in both the docs and the configuration.

## Upgrades

The order is Talos OS, Kubernetes, platform bundle. Change one thing at a time and run the startup-order checks after each step.

1. **Talos OS.** One node at a time, `talosctl upgrade -n <ip> --image <installer-image>:<version>`. Workers first, then the control plane one node at a time. After each control-plane node, check with `talosctl etcd status` that quorum is back. Take an etcd snapshot before starting (`docs/etcd-runbook.md`).
2. **Kubernetes.** `talosctl -n <cp-ip> upgrade-k8s --to <version>`. Use `--dry-run` first. Check the Kubernetes version range each bundle component requires in that project's docs before changing `scripts/versions.env`.
3. **Platform bundle.** Change the versions in `scripts/versions.env`, pass `make check` (render and kubeconform), then upgrade the components one at a time in the startup order. Rook goes operator first, then the Ceph cluster, and moves on only after Ceph is `HEALTH_OK`.

Rollback.

- Talos OS is recalled as using dual boot partitions. A failed boot returns to the previous image; manually it is `talosctl rollback -n <ip>` (needs checking).
- Kubernetes rollback is recalled as unsupported. So an etcd snapshot is needed before step 2, and on failure use the restore procedure in `docs/etcd-runbook.md` (needs checking).
- For the platform bundle, revert `scripts/versions.env` to the previous values and upgrade again. An operator whose CRDs changed may be hard to roll back, so take a snapshot before upgrading.

## Reboots, power cuts and the etcd runbook

- Worker reboot: Talos brings up the kubelet and KubeVirt recreates VMs with `runStrategy: Always`.
- Full power cut: the control-plane nodes recover etcd, the API comes up, and the rest follows the startup order. Same as scenario 7.
- etcd data loss: snapshot restore follows the Talos section of `docs/etcd-runbook.md`. Run `talosctl etcd snapshot` regularly from an operations host outside the nodes.
- The recovery test scenarios are in `docs/etcd-runbook.md`; measurements on Talos happen in the T2 lab.

## How the local lab (k3s) differs

| Item | k3s local lab (T1) | Talos production |
| --- | --- | --- |
| Node access | SSH, shell, `systemctl` | machine API (`talosctl`) only. No SSH |
| OS configuration | host packages, `modprobe` | machine configuration and system extensions |
| Kernel modules | loads modules from the host kernel | only what the machine configuration and image provide |
| etcd | embedded etcd or SQLite | etcd on three control-plane nodes |
| Snapshots | `k3s etcd-snapshot` | `talosctl etcd snapshot` |
| Restore | `--cluster-reset-restore-path` | `talosctl bootstrap --recover-from` |
| CNI | flannel (default) | Kube-OVN. Default CNI off |
| Storage | local-path | Rook Ceph RBD |
| Upgrades | re-run the install script | `talosctl upgrade`, `upgrade-k8s` |
| Pod security | close to allow-all by default | has a default pod security level, so namespaces may need configuration (needs checking) |
| Verification tier | the box (part of T1) and the lab | T2 lab |

## First things to check in the lab

1. Whether the four modules load, and whether extensions are needed.
2. The pod security settings the Kube-OVN and Rook namespaces need.
3. Whether the control-plane VIP works during the bootstrap without a CNI.
4. Whether snapshot restore works with `talosctl bootstrap --recover-from` (scenario 5).
5. Whether the control plane keeps quorum node by node during an OS upgrade.
