# vws-etcd

The etcd cluster that holds vws-iam and the admin registry (design.md 10.1.1). The `ha` profile runs three
members: two global VMs and a witness, which may be a PVE node. The `lab` and `small` profiles run one.
Every listener is https, and both the client and the peer port require a certificate from the cluster CA.
The peer port also requires CN `vws-etcd-member`, so a client certificate cannot join as a member.

| File | Runs on | Does |
| --- | --- | --- |
| `vws-etcd-tls OUTDIR LIST` | operator host | CA, one `ca.crt`/`member.crt`/`member.key` directory per member, one client directory; a rerun only adds new members |
| `vws-etcd-config NAME LIST` | each member | prints the etcd config file of member `NAME`; TLS files come from the systemd credentials of `vws-etcd.service` |
| `vws-etcd-snapshot DIR KEEP` | one member (backup timer) | saves `DIR/vws-etcd-<UTC>.db` and keeps the newest `KEEP` |
| `vws-etcd-defrag` | one global VM (weekly timer) | defragments every member one at a time, followers first and the leader last, only when all are healthy |
| `vws-etcd-check [MAX_BYTES]` | operator host | fails on a key outside `/vws/iam/` and `/vws/admin/` or a value over `MAX_BYTES` (default 65536) |

`LIST` is the same everywhere: `NAME=IPV4[:PEERPORT],...` with an odd member count, for example
`global-1=10.0.0.11,global-2=10.0.0.12,witness=10.0.0.21`. Each member must connect to its peers from the
IP in `LIST`, because the peers check that IP against the certificate.

Operator backups and restores go through `vws backup --etcd` and `vws restore --etcd` (docs/vws-cli.md).
A snapshot holds IAM and the admin registry, not VM disks or Proxmox data. Copy snapshots off the member;
a snapshot that exists only on the member is not a backup.

Schedules, sizing and alerts: `docs/design/etcd.md` and `docs/etcd-runbook.md`.

Build with `packaging/vws-etcd/build.sh VERSION ETCD_BINARY OUTPUT_DIR`. The executable must be an
amd64 etcd build compatible with the installed `etcd-client`. The package installs it as
`/usr/bin/vws-etcd`, the helpers together under `/usr/lib/vws/etcd/`, and five units under
`/usr/lib/systemd/system/`. Installation creates the system user `vws-etcd`, its data directory
`/var/lib/vws-etcd` (0700), and `/etc/vws/credentials` (root:root 0700). It enables and starts nothing.
Reinstall, removal and purge preserve the data and credentials; purge removes the conffile.

Before starting, edit the non-secret conffile `/etc/vws/etcd.conf`:

```ini
NAME=global-1
LIST=global-1=10.0.0.11,global-2=10.0.0.12,witness=10.0.0.21
```

These are literal values without quotes; no shell code is evaluated. The launcher validates them with
`vws-etcd-config`, writes `/run/vws-etcd/config.yaml` privately on every member start, and executes
etcd with that config. The data directory survives a restart; the generated config is disposable.

Provision these files as regular root:root 0600 files, without symlinks:

| Source path | LoadCredential name | Used by |
| --- | --- | --- |
| `/etc/vws/credentials/ca.crt` | `ca.crt` | member, snapshot, defrag |
| `/etc/vws/credentials/member.crt` | `member.crt` | member and readiness |
| `/etc/vws/credentials/member.key` | `member.key` | member and readiness |
| `/etc/vws/credentials/client.crt` | `client.crt` | snapshot and defrag |
| `/etc/vws/credentials/client.key` | `client.key` | snapshot and defrag |

Copy the member's files and the client certificate pair produced by `vws-etcd-tls`; keep the CA private
key on the operator host. Each unit checks the source permissions before running and reads TLS only
from its systemd credentials directory. Configure also refuses unsafe existing credentials and
symlinked credential or data directories. The CA and client credentials are shared with the local
etcd clients; do not overwrite an existing cluster CA when adding a member.

Start the member explicitly with `systemctl enable --now vws-etcd.service`. As root, check readiness
with `CREDENTIALS_DIRECTORY=/run/credentials/vws-etcd.service /usr/lib/vws/etcd/check-ready`.
It uses mutual TLS against the configured member's `/health`, with 1 second to connect and 2 seconds
total. Failed member processes restart after 5 seconds.

Enable `vws-etcd-snapshot.timer` explicitly on each global VM (or the one lab/small member). It runs
hourly and retains 48 snapshots in `/var/lib/vws-etcd/snapshots`. Enable `vws-etcd-defrag.timer` on
only one global VM; it runs weekly. Both timers catch missed schedules after a restart, with a
random delay. Calls have a 5 second dial timeout and 60 second command timeout; systemd also bounds
each maintenance run. Copy snapshots off the member as described above.

`packaging/vws-etcd/test.sh` checks package contents, lintian, shellcheck, rejected inputs, unit
credentials, and mutual-TLS readiness/recovery. CI runs `test-install.sh` in a disposable Debian
container for configure twice, reinstall, remove, purge and malicious credential paths. Its executable
stub isolates packaging and lifecycle behavior from the upstream etcd version; the TLS readiness
boundary uses a real HTTPS server. This does not verify a running etcd cluster or systemd boot.
