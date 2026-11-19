# Installing VWS on a VM or bare metal

VWS installs as three systemd packages on Debian 12 amd64. The target is a VM on Proxmox or a
bare-metal machine; the steps are the same. The default is one host with one etcd member. The
three-member `ha` layout is described in `packaging/vws-etcd/README.md`; `vws install` does not
automate it yet (vws-deploy#776).

Nothing on this page has been run on a deployed host yet. The build was run end to end on a
workstation, and the install steps run against a fake SSH target in `tests/vws/install_test.py` and
a disposable Debian container in CI.

## 1. Build the packages

On a build host with git, Go, curl, xz and dpkg-deb:

```sh
packaging/build.sh 1.0.0-1
```

It checks out vws-console-api, vws-iam and vws-console at the commits in `scripts/versions.env`,
builds the Go binaries with `-trimpath`, builds the console with the pinned Node archive, takes etcd
from the pinned release archive, and writes `build/vws-etcd_*.deb`, `build/vws-global_*.deb` and
`build/vws-region_*.deb`. Downloads are checked against the checksums in `scripts/versions.env`.
`build/src` keeps the checkouts and downloads, so a rerun fetches nothing that is still valid.

## 2. Prepare the inputs

Write `hosts.yaml` and `global.yaml` as in [`vws install`](../vws-cli.md#vws-install). Create an
empty credentials directory on the operator host and add what only you can supply:

| File | From |
| --- | --- |
| `console-token`, `vws-access-key`, `vws-secret-key` | the console-api token and VWS access key (`packaging/vws-global/README.md`) |
| `aws-source` | the AWS identity the proxy assumes roles from (`packaging/vws-global/README.md`) |
| `region.key` | the region certificate key (`packaging/vws-region/README.md`) |

`vws install` creates the rest when it is absent: `region-kek`, `admin-kek`, `iam-kek/`, the etcd CA
with the member and client certificates, and the internal CA with the vws-iam server and console-api
client certificates. A file that exists is never rewritten, so a rerun keeps every key. The CA keys
stay in `etcd-tls/` and `internal-tls/` inside the credentials directory and are never copied to the
target. Back this directory up and keep it private: losing a KEK makes the sealed IAM and registry
secrets in etcd unreadable.

Limits of the generated set:

- vws-iam and console-api share one etcd client certificate. Supply `api-etcd*.pem` and
  `iam-etcd*.pem` yourself to separate them.
- Leaf certificates last 825 days and the CAs 10 years. To rotate, delete the leaf files and their
  directory under `etcd-tls/` or `internal-tls/`, then run `vws install` again.
- A set you supply in part is not completed; the missing file is reported instead.

## 3. Install

```sh
scripts/vws install --profile lab --inventory hosts.yaml --packages build --credentials credentials
```

The command copies the inputs (secrets 0600 under `/etc/vws/credentials`, directory 0700), installs
the packages, then enables and starts `vws-etcd`, its snapshot and defrag timers, `vws-iam`,
`vws-aws-proxy`, `vws-console-api`, `vws-console`, `vws-region` and keepalived, waiting for each
readiness probe. A rerun reports every step `unchanged`. After a failure, fix the cause and run it
again.

Then register regions and AWS accounts: [regions.md](regions.md).

`vws-aws-proxy` reads accounts from the static `/etc/vws/aws-accounts.json`, not from etcd. After you
register or change an AWS account, export the records to that file and restart the proxy
(`packaging/vws-global/README.md`).

## Not for production: the in-memory store

`vws-iam -store memory` and `vws-console-api -admin-store memory` keep users, access keys and
registrations in process memory and lose them on restart. They exist for tests. The packaged units
use etcd; do not change them to the memory store on a host you depend on.

## Failover check (keepalived VIP)

Run after an install or a keepalived change. On one host this checks that an unready daemon gives up
the VIP; with several nodes it also checks the move.

1. `keepalived --config-test`, then `ip address show dev vmbr0`: exactly one ready node holds the VIP.
2. `sudo /usr/lib/vws/check-region-ready` succeeds on every node.
3. Stop `vws-region` on the VIP owner. Within 10 seconds it loses the VIP. With several nodes, one
   ready peer gains it and a request through the VIP succeeds.
4. Start `vws-region` again. The VIP returns only after two successful probes.

The full procedure, with the mTLS request and the all-nodes-down case, is in
[packaging/vws-region/README.md](../../packaging/vws-region/README.md#vip-configuration-and-failover-test).

## etcd backup and restore drill

Run this after the first install and then every quarter. It restores into a scratch directory, so
the running member is not touched.

```sh
export ETCDCTL_ENDPOINTS=https://<host-ip>:2379
export ETCDCTL_CACERT=credentials/ca.crt ETCDCTL_CERT=credentials/client.crt ETCDCTL_KEY=credentials/client.key
etcdctl get --prefix --keys-only /vws/ | grep -c .
scripts/vws backup --etcd --out drill.db
scripts/vws restore --etcd drill.db --data-dir ./drill-data --name <host-name> --peer-url https://127.0.0.1:2380
```

The backup must end with `OK` and the restore must write `./drill-data`. Copy `drill.db` off the
host; the hourly snapshots in `/var/lib/vws-etcd/snapshots` are not a backup until they are copied
too. To restore for real, and to record the drill, follow
[the etcd runbook](../etcd-runbook.md#restore).
