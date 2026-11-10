# vws-etcd: what it holds and how it is run (#478)

## Problem

Kubernetes is retired. etcd no longer holds compute, network or storage desired state: Proxmox and AWS
are the source of truth for instances, volumes and networks. The one exception to "VWS stores no state"
(OWNER-DESIGN.md principle 1, extended by design.md 3.2.6) is IAM plus the operator registrations, and
both live in one etcd cluster, vws-etcd (`packaging/vws-etcd/README.md`). The old runbook assumed every
VWS resource was in etcd; this record scales the rules to the real footprint.

Related work: vws-iam#450 (the `store/etcdstore` backend), vws-iam#451 (`-store etcd` with TLS from
systemd credentials), vws-deploy#765 (the vws-etcd package: TLS, member config, snapshots) and
vws-console-api#752 (the admin registry store).

## What may be written

vws-etcd serves vws-iam and the admin registry of vws-console-api, and nothing else. There is no other
Kubernetes etcd to share it with, so the old "dedicated or shared with Kubernetes" question is gone.

| Prefix | Writer | Holds |
| --- | --- | --- |
| `/vws/iam/` | vws-iam (`-etcd-prefix`, default `/vws/iam`) | one key per IAM object, `<kind>/<folded key>`: users, groups, roles, policies, access keys, MFA devices, STS state, project bindings, key pairs, console sessions |
| `/vws/admin/` | vws-console-api `internal/admin` | operator registrations: `regions/<name>`, `aws-accounts/<id>`, project mappings |

- No instance, volume, network, image or tag state is written, not even as a cache. A cache stays in
  process memory and never decides ownership or recovery.
- No secret in the clear. PVE token secrets are never in etcd (design.md 3.2.6); `external_id` and IAM
  secrets are sealed with the IAM KEK before the write.
- Every write is a compare-and-swap on the stored version, the key's `mod_revision`: a create is a
  `Txn` that requires `create_revision == 0`, an update or delete a `Txn` that requires the
  `mod_revision` read, and a write conditioned on a second object compares both in the same `Txn`.
  A blind `Put` is a bug. vws-iam `internal/store/etcdstore/etcdstore.go` does this; the admin store of
  vws-console-api#752 is built the same way.
- One value is at most **64 KiB** (65536 bytes). The largest AWS IAM documents are a role's inline
  policies (10,240 characters together) and a managed policy version (6,144 characters), from "IAM and
  AWS STS quotas" in the AWS IAM User Guide (`reference_iam-quotas.html`); with up to 4 bytes per
  character and JSON escaping such an object stays well under 64 KiB, and etcd's own request limit
  (1.5 MiB) is far above it.

`packaging/vws-etcd/vws-etcd-check [MAX_BYTES]` reads the cluster and fails on any key outside the two
prefixes and any value over the limit, naming the key and its size and never printing a value
(`tests/vws/etcd.test.sh`). Run it after an upgrade of vws-iam or vws-console-api and from the weekly
maintenance below.

## Sizing

The data is small. A rough upper bound for one deployment: 10,000 users with two access keys and a few
policies each, 1,000 roles, a few hundred registrations and the live console sessions come to tens of
MiB. etcd keeps the default 2 GiB backend quota; reaching it would mean a writer is broken, not that the
cluster needs to grow.

| Profile | Members | Each member |
| --- | --- | --- |
| `lab`, `small` | 1 | shares its VM; 1 GiB of RAM for etcd |
| `ha` | 3: two global VMs and a witness (may be a PVE node) | 2 vCPU, 2 GiB RAM, 20 GiB of SSD for the data directory; WAL fsync p99 under 10 ms |

Three members survive the loss of one. Five would only add write latency at this size.

## Maintenance

- **Compaction**: every member compacts its history hourly (`auto-compaction-mode: periodic`,
  `auto-compaction-retention: "1h"` in `vws-etcd-config`).
- **Defrag**: compaction frees pages but does not shrink the file. `vws-etcd-defrag` runs weekly from
  one global VM: it starts only when every member is healthy, defragments one member at a time,
  followers first and the leader last, and stops at the first member that is not healthy afterwards.
  Also run it when the DB size is more than twice the size in use.
- **Snapshots**: `vws-etcd-snapshot DIR 48` runs hourly on each global VM (`ha`) or on the one member
  (`lab`, `small`), so a member keeps two days of snapshots. Each new snapshot is then copied off the
  member host to a backup server or object storage outside the etcd members, kept there 30 days, and
  encrypted at rest. A snapshot only on the member is not a backup. An hourly schedule bounds what a
  restore loses to one hour of IAM changes (a key created in that hour is gone after the restore).
- **Restore**: `vws restore --etcd` on every member with the same snapshot (`docs/etcd-runbook.md`).

The timer units for snapshots and defrag wait for the R10 rewrite (vws-deploy#763), like the member unit.

## Alerts

Thresholds for the rules the global layer's metrics pipeline will load. They are scaled to the
IAM/registry-only workload; the rules file itself is a later part of #478.

| Alert | Metric | Warn | Critical |
| --- | --- | --- | --- |
| DB size | `etcd_mvcc_db_total_size_in_bytes` | over 256 MiB for 15 min | over 1 GiB |
| fsync latency | p99 of `etcd_disk_wal_fsync_duration_seconds` | over 10 ms for 10 min | over 50 ms for 10 min |
| No leader | `etcd_server_has_leader` | - | 0 for 1 min on any member |
| Leader changes | `increase(etcd_server_leader_changes_seen_total[1h])` | over 3 | over 10 |
| Snapshot age | newest `vws-etcd-*.db` off the member | over 2 h | over 6 h |

## Rejected

- A separate etcd for the admin registry: two clusters to back up and alert on for a few hundred keys.
- Enforcing the size limit with etcd `max-request-bytes`: a conditional write carries two objects in
  one request, so a request limit cannot state a per-object limit.
  The writers keep their own limit and `vws-etcd-check` finds what got past them.
- Five members: more write latency, no gain at this size.

## Limits

- `vws-etcd-check` reads every key in one request. That is fine at tens of MiB; a cluster near its
  quota is already an alert.
- vws-iam and vws-console-api use the one vws-etcd client certificate; etcd auth per prefix (a user
  per writer) is not set up yet.
- The restore drill is run once the nested PVE lab exists (vws-deploy#765, vws-deploy#767) and is
  recorded in `docs/etcd-runbook.md` with the time to restore and the object counts before and after.
