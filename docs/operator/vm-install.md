# Installing VWS on a VM or bare metal

VWS installs as systemd packages on Debian 12 amd64. This page covers the global layer: one VM on
Proxmox or one bare-metal machine, with `vws-etcd` and `vws-global` (sections 2 and 3). It needs no PVE
on it. The region layer is in section 4.

The default is one global host with one etcd member. The three-member `ha` layout is described in
`packaging/vws-etcd/README.md`; `vws install` does not automate it yet (vws-deploy#776).

Nothing on this page has been run on a deployed host yet: no VM was booted and no PVE node was
installed. The build was run end to end on a workstation. The install steps run against a fake SSH
target in `tests/vws/install_test.py` and a disposable Debian container in CI, and
`tests/vws/etcd.test.sh` starts a real etcd with the generated certificates and connects with the
etcd URL and client certificates of the packaged units.

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

## 2. Prepare the global host inputs

Write `global/hosts.yaml` with `roles: [etcd, global]` and no `vip`, and `global/global.yaml`, as in
[`vws install`](../vws-cli.md#vws-install). Create an empty credentials directory on the operator
host and add what only you can supply:

| File | From |
| --- | --- |
| `console-token`, `vws-access-key`, `vws-secret-key` | the console-api token and VWS access key (`packaging/vws-global/README.md`) |
| `aws-source` | the AWS identity the proxy assumes roles from (`packaging/vws-global/README.md`) |

`vws install` creates the rest when it is absent: `admin-kek`, `iam-kek/`, the etcd CA with the
member and client certificates, and the internal CA with the vws-iam server and console-api client
certificates. A file that exists is never rewritten, so a rerun keeps every key. The CA keys stay in
`etcd-tls/` and `internal-tls/` inside the credentials directory and are never copied to the target.
Back this directory up and keep it private: losing a KEK makes the sealed IAM and registry secrets
in etcd unreadable.

etcd listens on the host address and on 127.0.0.1, and the generated member certificate names both.
`vws-iam` and `vws-console-api` connect to `https://127.0.0.1:2379`, so keep that entry in
`iam.etcd_endpoints` of `global.yaml`.

Limits of the generated set:

- vws-iam and console-api share one etcd client certificate. Supply `api-etcd*.pem` and
  `iam-etcd*.pem` yourself to separate them.
- Leaf certificates last 825 days and the CAs 10 years. To rotate, delete the leaf files and their
  directory under `etcd-tls/` or `internal-tls/`, then run `vws install` again.
- A set you supply in part is not completed; the missing file is reported instead.

## 3. Install the global host

```sh
scripts/vws install --profile lab --inventory global/hosts.yaml --packages build --credentials credentials
```

The command copies the inputs (secrets 0600 under `/etc/vws/credentials`, directory 0700), installs
`vws-etcd` and `vws-global`, then enables and starts `vws-etcd`, its snapshot and defrag timers,
`vws-iam`, `vws-aws-proxy`, `vws-console-api` and `vws-console`, waiting for each readiness probe.
It does not install or start `vws-region` or keepalived on this host. A rerun reports every step
`unchanged`. After a failure, fix the cause and run it again.

`vws-aws-proxy` reads accounts from the static `/etc/vws/aws-accounts.json`, not from etcd. After you
register or change an AWS account, export the records to that file and restart the proxy
(`packaging/vws-global/README.md`).

AWS accounts need nothing more: register them as in [regions.md](regions.md). A Proxmox cluster
needs the next section first.

## 4. The region layer

This page does not install `vws-region`. Installing one region per Proxmox cluster on the global host
is tracked in vws-deploy#849 (packaging) and #850 (install and runbook), and the rehearsal on a real
cluster in #851. Until then the region role keeps the behavior it has on main: `vws install` accepts
it only on the lab host with all three roles ([`vws install`](../vws-cli.md#vws-install)), and
`packaging/vws-region/README.md` describes the package.

## Not for production: the in-memory store

`vws-iam -store memory` and `vws-console-api -admin-store memory` keep users, access keys and
registrations in process memory and lose them on restart. They exist for tests. The packaged units
use etcd; do not change them to the memory store on a host you depend on.

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
