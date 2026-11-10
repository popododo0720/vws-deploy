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

| # | Scenario | Tier | Action | Expected | Check | Recovery time | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Controller killed | T1 | `kubectl -n vws delete pod -l app.kubernetes.io/name=compute --force --grace-period=0` | A new pod starts and converges again through list and watch. Instance state is unchanged | `kubectl -n vws rollout status deployment/compute`, then compare `kubectl get vm -A` before and after | not measured | unverified |
| 2 | Instance recovery after restart | T1 | Reboot a node | VMIs of VMs with `runStrategy: Always` come back and `Halted` VMs stay as they are | `kubectl get vmi -A -w` | not measured | unverified |
| 3 | Same ClientToken is idempotent | T0, T1 | Call RunInstances twice with the same `ClientToken` | The same instance ID comes back and there is one VM object | `kubectl get vm -A -o name \| wc -l` is 1 | not measured | unverified |
| 4 | One etcd member lost | T1 (k3s embedded etcd, 3 servers) | Power off one control-plane node | Quorum holds and reads and writes continue | `kubectl create configmap probe-$RANDOM -n vws --from-literal=a=b` succeeds | not measured | unverified |
| 5 | Restore after quorum loss | T1, T2 with network and storage | Power off two or more control-plane nodes and restore from a snapshot | Writes are blocked, then after the restore the API comes up and the objects from the snapshot time are back. EC2 Query write calls return `ServiceUnavailable` in between | the restore procedure above, `kubectl get vm -A`, the error code of `aws ec2 run-instances` | not measured | unverified |
| 6 | Running VMs survive quorum loss | T2 | Block etcd and the API completely and ping a VM | Running VMs and the OVS data path keep working (assumed by design) | `ping` to the VM and `ovs-vsctl show` | not measured | unverified, T2 only |
| 7 | Restart after a full power cut | T1 (without network and storage), full run on T2 | Before powering off, `vws verify-reboot --snapshot before.json` (do not power off on a non-zero exit, #150), then power every node off and on at once | Everything comes up in the startup order and VMs return to their declared state | `vws verify-reboot --check before.json --timeout 20m` (gate order and durations, `docs/vws-cli.md`), the checks in the startup order table above | not measured | unverified |
| 8 | Worker node power-off with running VMs | T2 | [Scenario 8](#8-worker-node-power-off-with-running-vms) | After the out-of-service taint and the Ceph blocklist, the VM comes up on another node with its volumes attached, and the API reports the instance `running` again | [Scenario 8](#8-worker-node-power-off-with-running-vms) | not measured | unverified, T2 only |
| 9 | One Ceph OSD lost | T2 | [Scenario 9](#9-one-ceph-osd-lost) | Volumes stay readable and writable; Ceph rebalances | [Scenario 9](#9-one-ceph-osd-lost) | not measured | unverified, T2 only |
| 10 | Ceph MON quorum lost | T2 | [Scenario 10](#10-ceph-mon-quorum-lost) | Running VMs keep their mapped volumes; new volume and snapshot calls fail with a typed unavailable error | [Scenario 10](#10-ceph-mon-quorum-lost) | not measured | unverified, T2 only |
| 11 | Ceph fully unavailable | T2 | [Scenario 11](#11-ceph-fully-unavailable) | VM disk I/O blocks; storage calls fail with a typed unavailable error, never a 500 or a hang | [Scenario 11](#11-ceph-fully-unavailable) | not measured | unverified, T2 only |
| 12 | Kubernetes API unavailable for 5 minutes | T1, T2 | [Scenario 12](#12-kubernetes-api-unavailable-for-5-minutes) | VWS pods stay up, calls return a typed unavailable error, and service resumes without a restart | [Scenario 12](#12-kubernetes-api-unavailable-for-5-minutes) | not measured | unverified |
| 13 | Kube-OVN controller pod lost | T2 | [Scenario 13](#13-kube-ovn-controller-pod-lost) | Running VMs keep their network; network writes wait or fail typed until the controller is back; vws-network restores the gateway label | [Scenario 13](#13-kube-ovn-controller-pod-lost) | not measured | unverified, T2 only |

## Failure scenarios with VMs, Ceph and the Kubernetes API

Scenarios 8 to 13 need the HA platform profile (`platform/rook-ceph-cluster.ha.values.yaml`: three mons, at least three OSD nodes) and at least two worker nodes. `ceph` commands run in the Rook toolbox: `kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph <args>`. Start every scenario from a healthy cluster (the startup order table above all green) and write down the time of the trigger and the time recovery is confirmed; the difference is the recovery time in the table.

A typed unavailable error is what a caller gets while a backend is down: gRPC status `UNAVAILABLE` (or `DEADLINE_EXCEEDED`), the EC2 Query error code `ServiceUnavailable` with HTTP 503, and a 503 from vws-console-api. A 500 `InternalError`, a timeout with no response, or a crash is a failed run. Liveness (`/healthz`) checks no dependency, so a backend outage must not restart a VWS pod; readiness (`/readyz`) may fail, and then the Service drops the pod from its endpoints, so record whether the caller saw a typed error or a refused connection.

Before each scenario, save a baseline: `kubectl -n vws get pods -o wide`, `kubectl get vm,vmi -A -o wide`, `aws ec2 describe-instances` and `aws ec2 describe-volumes` against the VWS endpoint, and `ceph status`.

### 8. Worker node power-off with running VMs

- Trigger: start one instance with an attached RBD data volume, find its node with `kubectl get vmi -A -o wide`, then power that node off at the hypervisor or BMC (not `shutdown`, which drains).
- Expected VWS behaviour: the node goes `NotReady`. The API keeps answering; `describe-instances` shows the instance as `running` or `pending`, never an error. KubeVirt does not move a VMI with an RWO RBD volume until the node is fenced. After the operator fences the node, the VM starts on another node, the volume is attached there, and `describe-instances` shows `running` with the same instance ID and volume attachment.
- Check: `kubectl get node`, `kubectl get vmi -A -o wide` (new node), `aws ec2 describe-volumes --volume-ids <id>` (attachment state `attached`), `ceph osd blocklist ls` (the dead node's client is listed), and the data written before the trigger is still there inside the VM.
- Recovery: confirm the node is really off, then `kubectl taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute`. When the VM runs elsewhere and the node is back on, remove the taint with `kubectl taint node <node> node.kubernetes.io/out-of-service-`.

### 9. One Ceph OSD lost

- Trigger: stop one OSD: `kubectl -n rook-ceph scale deploy rook-ceph-osd-<n> --replicas=0`, or power off one storage disk's node when the node holds a single OSD.
- Expected VWS behaviour: `ceph status` shows `HEALTH_WARN` with degraded placement groups; I/O continues because pools keep more than one copy. Every VWS call succeeds, and volume creation, attach and snapshots work.
- Check: `ceph status`, `ceph osd tree` (one OSD `down`), `aws ec2 create-volume` then `attach-volume` succeed, and a write inside a running VM succeeds.
- Recovery: scale the OSD back to 1 (or power the node on) and wait for `ceph status` to show `HEALTH_OK` with all placement groups `active+clean`.

### 10. Ceph MON quorum lost

- Trigger: stop two of the three mons: `kubectl -n rook-ceph scale deploy rook-ceph-mon-a rook-ceph-mon-b --replicas=0`. Also scale `deploy/rook-ceph-operator` to 0 first so it does not replace them.
- Expected VWS behaviour: `ceph status` hangs or times out. Running VMs keep their already mapped volumes for as long as the OSDs serve I/O (assumed: clients keep their cached maps). vws-storage calls that need Ceph (create, delete and snapshot a volume) return a typed unavailable error within their deadline. Calls that read only Kubernetes objects keep working.
- Check: `timeout 30 ceph status` fails, `aws ec2 create-volume` returns `ServiceUnavailable` within 60 seconds, `aws ec2 describe-volumes` succeeds, and `kubectl -n vws get pods` shows no restarts.
- Recovery: scale the mons back to 1, then the operator back to 1, and wait for `ceph quorum_status` to list three mons and `ceph status` to show `HEALTH_OK`.

### 11. Ceph fully unavailable

- Trigger: stop the operator, every mon and every OSD (scale each deployment to 0).
- Expected VWS behaviour: disk I/O inside running VMs blocks (the guest sees hung I/O, not a reset). Every vws-storage call that needs Ceph returns a typed unavailable error within its deadline. vws-compute calls that need a new volume (run-instances with a block device) fail with a typed unavailable error, and the instance is not left half created. No VWS pod restarts.
- Check: `aws ec2 create-volume` and `aws ec2 run-instances` return `ServiceUnavailable`; `kubectl -n vws get pods` restart counts are unchanged; `kubectl get vm -A` has no new object from the failed run-instances.
- Recovery: scale the mons, then the OSDs, then the operator back up. Wait for `HEALTH_OK`, then check that the blocked I/O in each VM finished (`dmesg` in the guest has no I/O error) and retry the failed calls with the same `ClientToken`.

### 12. Kubernetes API unavailable for 5 minutes

- Trigger: on a T2 lab, stop the kube-apiserver on every control-plane node for 5 minutes (Talos runs kube-apiserver as a static pod, so block port 6443 on the control-plane nodes from the worker network instead; k3s: `systemctl stop k3s` on every server, which also stops that node's kubelet, so use servers that run no VWS pods). Write down the start and end times.
- Expected VWS behaviour: VWS pods stay `Running` and are not restarted (liveness does not depend on the API, and the kubelet keeps running pods without the API). Every call that reads or writes Kubernetes state returns a typed unavailable error within its deadline. Running VMs keep running. When the API is back, each service resumes through list and watch without a restart, and a call retried with the same `ClientToken` gives one resource, not two.
- Check: during the outage, `aws ec2 describe-instances` and `aws ec2 run-instances` return `ServiceUnavailable`; vws-iam calls return `ServiceUnavailable`; the console shows a service-unavailable message. After the outage, `kubectl -n vws get pods` shows the same restart counts as the baseline, and the same calls succeed within 2 minutes.
- Recovery: start the API again. No VWS action is expected; if a service does not recover without a restart, that is a finding (open an issue in its repo).

### 13. Kube-OVN controller pod lost

- Trigger: `kubectl -n kube-system delete pod -l app=kube-ovn-controller --force --grace-period=0`, and for a longer outage `kubectl -n kube-system scale deploy kube-ovn-controller --replicas=0` for 5 minutes.
- Expected VWS behaviour: running VMs keep their network (the OVS data path keeps the existing flows). vws-network calls that create subnets, security groups or elastic IPs either wait for the controller and finish when it is back, or fail with a typed unavailable error; none returns a 500. After the controller restarts it resets the gateway node label `ovn.kubernetes.io/external-gw` to `false`, and vws-network sets it back within `-kube-gateway-recheck` (default 30s, `docs/node-network.md`).
- Check: `ping` from and to a VM during the outage, `aws ec2 create-security-group` during the outage, `kubectl get node -L ovn.kubernetes.io/external-gw` after the restart, and SNAT from a VM to an outside address works again.
- Recovery: scale the controller back to its replica count and wait for `kubectl -n kube-system rollout status deploy/kube-ovn-controller`.

## Recording results

For each scenario run, add one line under this table with the date, tier, commands run, the actual result and the measured recovery time, change the status in the table above to "verified (date)", and fill in the recovery time column. A scenario that cannot be run is marked "blocked: <reason>". If the result differs from the expectation, leave the status alone and open an issue.
