# etcd backup, restore and recovery tests

The design is https://github.com/popododo0720/vws-deploy/issues/2. The desired state lives in one place, etcd. Every procedure and scenario in this document is **unverified**; no cluster was brought up on this box. Once one has actually been run, change the status on its line.

Production runs Talos and the local lab runs k3s (#8, #7). Commands are given for both. `<cp-ip>` is a control-plane node address.

## Snapshots

| Item | Talos (production) | k3s (lab) |
| --- | --- | --- |
| Create | `talosctl -n <cp-ip> etcd snapshot ./etcd-$(date +%Y%m%d-%H%M).db` | `k3s etcd-snapshot save --name vws` |
| Schedule | cron on an operations host outside the nodes runs the command above every 6 hours | `--etcd-snapshot-schedule-cron "0 */6 * * *"`, `--etcd-snapshot-retention 28` |
| Copy off the nodes | `rclone copy` from the operations host to object storage or another server | the `--etcd-s3` options or `rclone copy` |
| List | `ls -l` for file size and time | `k3s etcd-snapshot ls` |

- A snapshot that exists only on the nodes is not a backup. Count it as a success only after confirming a copy exists off the nodes.
- Snapshots contain Secrets. Encrypt the copy destination and restrict access to it.
- Check: confirm a snapshot file was refreshed in the last 24 hours with `find <dir> -name '*.db' -mmin -1440 | grep -q .`.

## Restore

Restore only when etcd lost quorum or the data is corrupt. A restore discards every change after the snapshot. If a VM was created after the snapshot, only its Kubernetes objects disappear and its Ceph volume stays. Clean up orphan volumes after the restore.

### Talos

1. Reset etcd on every control-plane node. `talosctl -n <cp-ip> reset --graceful=false --reboot --system-labels-to-wipe=EPHEMERAL`
2. Bootstrap one control-plane node from the snapshot. `talosctl -n <cp-ip> bootstrap --recover-from=./etcd.db`
3. Confirm the other control-plane nodes join. `talosctl -n <cp-ip> etcd members`

### k3s

1. Stop k3s on every server. `systemctl stop k3s`
2. Restore on one server. `k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>`
3. Start k3s on that server and check `kubectl get nodes`.
4. On the other servers, delete `/var/lib/rancher/k3s/server/db`, then start them so they join.

## Startup order

After a power recovery or a restore the order is etcd, API, CNI (Kube-OVN), CSI (Rook), KubeVirt, VWS. Each step moves on only after the previous one is Ready. Operators converge on their own; a person enforces the order only for etcd and the API.

| Step | Check | Expected |
| --- | --- | --- |
| etcd | `talosctl -n <cp-ip> etcd status` or `k3s kubectl get --raw /readyz/etcd` | every member healthy, one leader |
| API | `kubectl get --raw /readyz` | `ok` |
| CNI | `kubectl -n kube-system get pods -l app=kube-ovn-cni` | all Ready |
| CSI | `kubectl -n rook-ceph get cephcluster` | `HEALTH_OK` |
| KubeVirt | `kubectl -n kubevirt get kubevirt kubevirt -o jsonpath='{.status.phase}'` | `Deployed` |
| VWS | `kubectl -n vws get pods` | all Ready |

## Recovery test scenarios

The tiers follow the design document: T0 is the box (no cluster), T1 is the k3s lab, T2 is a lab of three or more VMs with a normal kernel. Scenarios that need Kube-OVN and Ceph cannot run on the box and are **T2 lab only**. Every scenario is currently **unverified**.

| # | Scenario | Tier | Action | Expected | Check | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Controller killed | T1 | `kubectl -n vws delete pod -l app.kubernetes.io/name=compute --force --grace-period=0` | A new pod starts and converges again through list and watch. Instance state is unchanged | `kubectl -n vws rollout status statefulset/compute`, then compare `kubectl get vm -A` before and after | unverified |
| 2 | Instance recovery after restart | T1 | Reboot a node | VMIs of VMs with `runStrategy: Always` come back and `Halted` VMs stay as they are | `kubectl get vmi -A -w` | unverified |
| 3 | Same ClientToken is idempotent | T0, T1 | Call RunInstances twice with the same `ClientToken` | The same instance ID comes back and there is one VM object | `kubectl get vm -A -o name \| wc -l` is 1 | unverified |
| 4 | One etcd member lost | T1 (k3s embedded etcd, 3 servers) | Power off one control-plane node | Quorum holds and reads and writes continue | `kubectl create configmap probe-$RANDOM -n vws --from-literal=a=b` succeeds | unverified |
| 5 | Restore after quorum loss | T1, T2 with network and storage | Power off two or more control-plane nodes and restore from a snapshot | Writes are blocked, then after the restore the API comes up and the objects from the snapshot time are back. EC2 Query write calls return `ServiceUnavailable` in between | the restore procedure above, `kubectl get vm -A`, the error code of `aws ec2 run-instances` | unverified |
| 6 | Running VMs survive quorum loss | T2 | Block etcd and the API completely and ping a VM | Running VMs and the OVS data path keep working (assumed by design) | `ping` to the VM and `ovs-vsctl show` | unverified, T2 only |
| 7 | Restart after a full power cut | T1 (without network and storage), full run on T2 | Before powering off, `vws verify-reboot --snapshot before.json` (do not power off on a non-zero exit, #150), then power every node off and on at once | Everything comes up in the startup order and VMs return to their declared state | `vws verify-reboot --check before.json --timeout 20m` (gate order and durations, `docs/vws-cli.md`), the checks in the startup order table above | unverified |
| 8 | RWO volume failover on node failure | T2 | Power off the node a VM runs on | After the out-of-service taint and the Ceph blocklist, the VM comes up on another node | `kubectl get vmi -A -o wide`, `ceph osd blocklist ls` | unverified, T2 only |

## Recording results

For each scenario run, add one line under this table with the date, tier, commands run and the actual result, and change the status in the table above to "verified (date)". If the result differs from the expectation, leave the status alone and open an issue.
