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

`LIST` is the same everywhere: `NAME=IPV4[:PEERPORT],...` with an odd member count, for example
`global-1=10.0.0.11,global-2=10.0.0.12,witness=10.0.0.21`. Each member must connect to its peers from the
IP in `LIST`, because the peers check that IP against the certificate.

Operator backups and restores go through `vws backup --etcd` and `vws restore --etcd` (docs/vws-cli.md).
A snapshot holds IAM and the admin registry, not VM disks or Proxmox data. Copy snapshots off the member;
a snapshot that exists only on the member is not a backup.

Not here yet: the `vws-etcd.service` member unit and the backup timer unit, which wait for the R10 rewrite
(vws-deploy#763), and the `.deb` that installs these files.
