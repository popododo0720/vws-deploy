# vws-etcd runbook

vws-etcd holds vws-iam and the admin registry, nothing else (`docs/design/etcd.md`). Instances, volumes
and networks live in Proxmox and AWS, so a restore never touches them. The members, TLS and config come
from `packaging/vws-etcd/` (`packaging/vws-etcd/README.md`).

Every command below runs with the vws-etcd client certificate in the etcdctl variables:

```
export ETCDCTL_ENDPOINTS=https://<member-ip>:2379
export ETCDCTL_CACERT=<tls>/client/ca.crt ETCDCTL_CERT=<tls>/client/client.crt ETCDCTL_KEY=<tls>/client/client.key
```

The snapshot, restore, defrag and check commands run against real etcd processes in
`tests/vws/etcd.test.sh`; replacing a member is not tested. Nothing here has been run on a deployed
cluster yet; the restore drill waits for the nested PVE lab (vws-deploy#765, vws-deploy#767).

## Health

| Check | Command | Expected |
| --- | --- | --- |
| members | `etcdctl member list -w table` | every member `started`, the profile's count (1 or 3) |
| health | `etcdctl endpoint health --cluster` | every member `is healthy` |
| leader and size | `etcdctl endpoint status --cluster -w table` | one `IS LEADER true`; DB size under 256 MiB |
| contents | `packaging/vws-etcd/vws-etcd-check` | exit 0: only `/vws/iam/` and `/vws/admin/` keys, none over 64 KiB |
| alarms | `etcdctl alarm list` | empty |

## Snapshots

- Schedule: `packaging/vws-etcd/vws-etcd-snapshot /var/backups/vws-etcd 48` hourly on each global VM
  (`ha`) or on the one member (`lab`, `small`). It writes `vws-etcd-<UTC>.db` (0600) and keeps the
  newest 48.
- Off-node copy: after each run, copy the new file to a backup server or object storage that is not an
  etcd member (`rclone copy` or `rsync`), keep 30 days there, encrypted at rest. A snapshot only on the
  member is not a backup. Snapshots hold sealed IAM secrets and password hashes; restrict access.
- One-off: `vws backup --etcd --out <file>` (`docs/vws-cli.md`).
- Check: the newest off-node copy is under 2 hours old, for example
  `find <copy-dir> -name 'vws-etcd-*.db' -mmin -120 | grep -q .`, and it verifies with
  `etcdutl snapshot status <file> -w table`.

## Defrag

Compaction runs hourly on every member and frees pages, but the file only shrinks after a defrag.

- Schedule: weekly, at a quiet hour, from one global VM: `packaging/vws-etcd/vws-etcd-defrag`. Also run
  it when `endpoint status` shows a DB size more than twice the size in use.
- What it does: exits 1 and touches nothing unless every member is healthy and there is one leader;
  then defragments one member at a time, followers first and the leader last, printing the size before
  and after; stops at the first member that is not healthy after its defrag.
- A member blocks reads and writes while it defragments (seconds at this size). With three members the
  other two keep quorum. On a one-member profile the store is unavailable for that time, so pick a
  quiet hour.
- After an exit 1: fix the unhealthy member (Health above), then run it again. A rerun defragments
  every member again, which is safe.

## Restore

Restore only when quorum is lost for good or the data is corrupt. A restore discards every IAM and
registry change after the snapshot: access keys, users and registrations made since then are gone and
must be made again. VMs, volumes and networks are not affected.

1. Stop vws-iam and vws-console-api on every global VM, then stop etcd on every member.
2. On every member, move the old data directory aside (`mv /var/lib/vws-etcd /var/lib/vws-etcd.old`).
3. On every member, with the same snapshot and the full member list:
   `vws restore --etcd <snapshot> --data-dir /var/lib/vws-etcd --name <member> --peer-url https://<member-ip>:2380 --initial-cluster <name>=https://<ip>:2380,...`
4. Start etcd on every member, check Health above, and run `vws-etcd-check`.
5. Start vws-iam and vws-console-api, and check that a known user can sign in.

Record each drill below: date, profile, snapshot age, time from step 1 to step 5, and the key counts
under `/vws/iam/` and `/vws/admin/` before and after (`etcdctl get --prefix --keys-only /vws/iam/ | grep -c .`).

| Date | Profile | Snapshot age | Time to restore | Keys before | Keys after | Result |
| --- | --- | --- | --- | --- | --- | --- |

## One member lost

With three members the cluster keeps quorum. Replace the member: `etcdctl member remove <id>`, make its
TLS with `vws-etcd-tls` (a rerun adds only new members), `etcdctl member add <name> --peer-urls https://<ip>:2380`,
then start it on an empty data directory with its `vws-etcd-config` output changed to
`initial-cluster-state: existing`.
