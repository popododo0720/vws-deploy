# vws-region

One `vws-region@<region>` instance serves one Proxmox cluster. Install the package on
the global host (the default), on a VM inside the cluster or on a separate machine. It
does not go on a Proxmox node, and it needs no PVE, `/etc/pve`, sudo or keepalived. The
daemon reaches the cluster API at the addresses and CA it receives in the region
registration; it keeps its state in `registration.json` under its own state directory.

The package installs `/usr/bin/vws-region`, `/usr/lib/vws/run-region`, the readiness
probe `/usr/lib/vws/check-region-ready`, the template unit `vws-region@.service`, the
examples `region.yaml` and `region.env` under `/usr/share/vws-region/examples/`, and an
empty `/etc/vws-region/`. It does not modify VMs, storage or a PVE cluster.

## Build and install

Build the Linux amd64 binary from the matching vws-console-api release, then:

```sh
packaging/vws-region/build.sh 1.0.0-7 ./vws-region ./build
sudo apt-get install ./build/vws-region_1.0.0-7_amd64.deb
```

The script packages the supplied executable; it does not compile or download it. CI uses
an executable shell stub to test packaging, not daemon behavior. Run
`packaging/vws-region/test.sh` for package lint, the two-instance test with a stub daemon
and the unit checks. `test-install.sh` runs only in a disposable Debian container.

## One instance per cluster

The instance name is the region name, for example `pve-seoul-1`. For each instance
provide, as root (`vws install` writes all of these from the `regions:` list of its inventory and
issues the certificate from the internal CA; see `docs/operator/vm-install.md`):

| Path | Content |
| --- | --- |
| `/etc/vws-region/<region>.env` | `VWS_REGION_ADDR` (mTLS API listen address) and `VWS_REGION_OPS_ADDR` (health and metrics, keep it on loopback). Optional `VWS_REGION_TLS_CERT` and `VWS_REGION_TLS_CA`. |
| `/etc/vws-region/<region>/region.yaml` | The node-local configuration; see the example. |
| `/etc/vws-region/<region>/region.crt` | Server certificate for the address the global host uses. |
| `/etc/vws-region/<region>/region.key` | Its private key, root-only. |
| `/etc/vws-region/<region>/global-ca.pem` | CA of the global client certificate. |

`run-region` reads the `.env` file as plain `KEY=VALUE` lines; it never sources it, rejects
unknown keys and unsafe values, and the file holds no secret. The certificate and CA paths
default to the directory above. The private key reaches the daemon only through
`LoadCredential=region-key:`, never through a flag or `Environment=`. Two instances on one
host need different `VWS_REGION_ADDR` and `VWS_REGION_OPS_ADDR` ports. The ops address of
the global API is `127.0.0.1:8083`, so do not use that one.

Modes: `/etc/vws-region/<region>/`, `region.yaml`, `region.crt` and `global-ca.pem` must be
readable by the `vws-region` account (directory 0755, files 0644, root-owned). Only
`region.key` is secret: root-owned 0600; systemd reads it as root and hands it to the daemon.

The package does not start or enable any instance. `vws install` does, and reports `unregistered`
for an instance that has no registration yet. By hand, run:

```sh
sudo systemctl enable --now vws-region@pve-seoul-1.service
sudo /usr/lib/vws/check-region-ready pve-seoul-1
```

The probe reads `VWS_REGION_OPS_ADDR` from the instance's `.env` and asks `/readyz`
through curl. An instance that is not registered yet is not ready.

## State and removal

The unit declares `StateDirectory=vws-region/%i` with `StateDirectoryMode=0700`, and the
daemon runs with `-region %i -state-dir /var/lib/vws-region/%i`. The directory holds
`registration.json` (mode 0600), which includes the PVE token secret. The daemon refuses a
directory that is not private. The package never creates, changes or deletes state and
operator files.

Upgrading from the per-node release: the new package stops and disables the old
`vws-region.service` (`preinst`), no longer ships that unit, and removes the old
`/etc/sudoers.d/vws-region` rule. It leaves `/etc/vws/region.yaml` and
`/etc/vws/credentials/region-kek` for you to move or delete; nothing reads them.

`apt remove vws-region` stops all `vws-region@*` instances and removes the package files.
It keeps `/etc/vws-region`, `/var/lib/vws-region` and the `vws-region` system account, also
on purge. Before you move or delete an instance, delete its token in PVE (Datacenter,
Permissions, API Tokens), because the old host still holds the secret.
