# Installing VWS on a VM or bare metal

VWS installs as systemd packages on Debian 12 amd64. This page covers the global layer: one VM on
Proxmox or one bare-metal machine, with `vws-etcd` and `vws-global` (sections 2 and 3). It needs no PVE
on it. The region layer, one `vws-region` instance per Proxmox cluster, is in section 4.

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
packaging/build.sh 1.0.0-5 build-1.0.0-5
```

It checks out vws-console-api, vws-iam and vws-console at the commits in `scripts/versions.env`,
builds the Go binaries with `-trimpath`, builds the console with the pinned Node archive, takes etcd
from the pinned release archive, and writes `build-1.0.0-5/vws-etcd_*.deb`, `build-1.0.0-5/vws-global_*.deb` and
`build-1.0.0-5/vws-region_*.deb`. Use a new output directory for each version: `vws install` takes exactly one
package per role from `--packages`. Downloads are checked against the checksums in `scripts/versions.env`.
`build-1.0.0-5/src` keeps the checkouts and downloads, so a rerun fetches nothing that is still valid.

## 2. Prepare the global host inputs

Write `global/hosts.yaml` with `roles: [etcd, global]` (add `region` and a `regions` list in section 4), and
`global/global.yaml`, as in [`vws install`](../vws-cli.md#vws-install). Create an empty credentials directory on the operator
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
- Leaf certificates last 825 days and the CAs 10 years. For an install with retained generated state
  (the CA key in `etcd-tls/` or `internal-tls/`), rotate by deleting the leaf files and their directory
  under that state directory, then run `vws install` again. For TLS you supply, reissue the leaves under
  your own CA and replace each certificate and key together.
- A set you supply in part is not completed; the missing file is reported instead. Every file that is
  present must equal the kept `etcd-tls/` or `internal-tls/` file of the same name, and a certificate whose
  key does not match is refused; both exit 2 before any SSH call.
- If the host address in `hosts.yaml` changes, the member certificate no longer names it and the
  command refuses to continue. With retained generated state (`etcd-tls/ca.key` and a `ca.crt` equal to
  `etcd-tls/ca.crt`) and every other credential still equal to its `etcd-tls/` counterpart, reissue it
  from the same CA: delete `member.crt`, `member.key` and `etcd-tls/<name>/` in the credentials
  directory, then run `vws install` again. The CA, the client certificates and every other key stay as
  they are. If you supplied a credential that differs from `etcd-tls/` (for example a separate
  `api-etcd.pem` and `api-etcd-key.pem` signed by the retained CA), deleting files does not help: the
  rerun exits 2 because that file differs from the retained state. Reissue the member pair under the
  same CA with the new address and `127.0.0.1` in its subject alternative names, replace `member.crt`
  and `member.key`, and keep the separate client credentials. With a set under your own CA, reissue the
  member certificate under that CA the same way and replace `member.crt` and `member.key` together.

## 3. Install the global host

```sh
scripts/vws install --profile lab --inventory global/hosts.yaml --packages build-1.0.0-5 --credentials credentials
```

The command copies the inputs (secrets 0600 under `/etc/vws/credentials`, directory 0700), installs
`vws-etcd` and `vws-global`, then enables and starts `vws-etcd`, its snapshot and defrag timers,
`vws-iam`, `vws-aws-proxy`, `vws-console-api` and `vws-console`, waiting for each readiness probe.
With `roles: [etcd, global]` it installs no `vws-region`. A rerun reports every step
`unchanged`. After a failure, fix the cause and run it again.

`vws-aws-proxy` reads accounts and project mappings from the static `/etc/vws/aws-accounts.json` and
`/etc/vws/projects.json` (both initially `[]`), not from etcd. After you register or change an AWS
account or a project mapping, export the records to those files and restart the proxy
(`packaging/vws-global/README.md`).

AWS accounts need nothing more: register them as in [regions.md](regions.md). A Proxmox cluster
needs the next section first.

## 4. The region layer

A `vws-region@<region>` instance is the only process that talks to a Proxmox cluster, and one instance
serves one cluster. It needs no PVE, `/etc/pve` or sudo. It runs on the global host, on another Debian
host, or in a VM of the cluster, never on a Proxmox node ([regions.md](regions.md#where-the-instance-runs)
compares the placements). `vws install` installs the instances in the same run as the global host.

Add the instances to the inventory of section 2, one entry per cluster:

```yaml
profile: lab
hosts:
  - name: global-1
    address: 192.0.2.1
    roles: [etcd, global, region]       # the instance runs on the global host
    regions:
      - name: pve-seoul-1
        port: 8443
  - name: region-1                      # optional: a separate host for another cluster
    address: 192.0.2.20
    roles: [region]
    regions:
      - name: pve-busan-1
        port: 8443
```

For each instance, put your own configuration in `global/regions/<region>.yaml` (pools, storages, bridges;
the example `packaging/vws-region/region.yaml` is the default and has `read_only: true`). Every run writes
that file, or the default, to `/etc/vws-region/<region>/region.yaml`, so an edit made on the host is
replaced by the next run: a region set to `read_only: false` there goes back to `true` and then fails its
registration self-check (`privileges`) with a write token. Change `global/regions/<region>.yaml` instead. The credentials
directory gets `regions/<region>/region.crt` and `region.key`, issued from the internal CA that section 2
keeps. The certificate covers the host address, 127.0.0.1 and the region name, so the region endpoint of the
registration can use the host address, or 127.0.0.1 when the global layer runs on the same host. `global-ca.pem` of each instance is `internal-ca.pem`.
If you supplied your own internal TLS set, there is no CA key to issue from: supply
`regions/<region>/region.crt` and `region.key` yourself, issued under `internal-ca.pem`. Regionapi trusts
that CA for the global client too, so the certificate, generated or supplied, must be a server-only leaf:
`extendedKeyUsage` exactly `serverAuth` (no `clientAuth`, no `anyExtendedKeyUsage`, not missing),
`basicConstraints` `CA:FALSE`, and the host address, 127.0.0.1 and the region name as subject alternative
names. An etcd member certificate has `clientAuth` and is refused; so is any other certificate that fails
this, before any SSH call.
The host of an instance needs the build from section 1 (`vws-region_*.deb` in `--packages`).

```sh
scripts/vws install --profile lab --inventory global/hosts.yaml --packages build-1.0.0-5 --credentials credentials
```

One run is enough. It installs the package, writes the files (`.env` and `region.yaml` 0644, key 0600 root),
starts every instance and prints, for a new one:

```text
global-1 vws-region@pve-seoul-1 registration unregistered
```

`unregistered` is the end state of the install: the instance runs and waits for its registration. Register
it with `vws region add` ([regions.md](regions.md#step-4-register-the-region)); you do not run `vws install`
again for that. The PVE token secret goes to the instance only through that registration, into
`/var/lib/vws-region/<region>/registration.json` (0600, in a directory only the daemon can use). The
inventory and the credentials directory hold no PVE token.

Reruns keep every key and report `unchanged`; an instance whose own files changed is restarted. A
changed host address fails with the reissue steps (delete `regions/<region>/`); two instances with one
name, or sharing a port on one host, are refused before SSH. `vws install` never removes an instance that
you delete from the inventory: see [moving](regions.md#move-the-daemon). Nothing in this section has been
run against a real Proxmox cluster; the rehearsal checklist is [region-rehearsal.md](region-rehearsal.md).

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
