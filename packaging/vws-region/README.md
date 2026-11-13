# vws-region

Install the same release on every PVE node. This package does not install a PVE
cluster or modify VM definitions, snippets or volumes. It installs
`/usr/bin/vws-region`, `/usr/lib/vws/vws-region-helper`, the readiness probe,
systemd unit, sudoers policy and `/etc/vws/region.yaml`.

## Build and install

Build both Linux amd64 binaries from the matching vws-console-api release, then:

```sh
packaging/vws-region/build.sh 1.0.0-1 ./vws-region ./vws-region-helper ./build
sudo apt-get install ./build/vws-region_1.0.0-1_amd64.deb
```

The script packages supplied executables; it does not compile or download them.
CI uses executable shell stubs to test packaging, not daemon behavior. Production
inputs must be Linux amd64 binaries with their runtime dependencies available.
Run `packaging/vws-region/test.sh` for package lint, policy and readiness tests.
`test-install.sh` is destructive to its fake data and runs only in a disposable
Debian container.

The service is deliberately not started or enabled during installation: provision
the node settings and certificates first. Edit `/etc/vws/region.yaml` to name
existing pools, bridges, template selectors and storage. The example is read-only.
Registration is read from `/etc/pve/vws/registration.json`, overriding the old
binary default under `priv/`. Operator registration must match the binary release.

During configure, postinst creates the lease directory `/etc/pve/vws/` and the helper's private and lock
directories `/etc/pve/priv/vws/` and `/etc/pve/priv/lock/vws-region/` only when
`/proc/self/mounts` lists `/dev/fuse` mounted at `/etc/pve` with filesystem type
`fuse` (pmxcfs). A plain directory or any other mount is left unchanged. Repeated
configure runs preserve existing data, and a symlink at any of those three paths is refused.
The package never changes ownership or permissions on pmxcfs and retains the
directory on removal and purge.

Provision a root-owned, mode 0600, 32-byte raw KEK at
`/etc/vws/credentials/region-kek` identically on each node. Provision the region
certificate at `/etc/vws/region.crt`, its root-only key at `/etc/vws/region.key`,
and global client CA at `/etc/vws/global-ca.pem`. Certificates must cover the VIP
and use the appropriate region/global trust. The unit loads both private inputs
through systemd credentials; no secret is supplied in flags or Environment.
Restrict the source credential directory to root. Then run:

```sh
sudo systemctl enable --now vws-region.service
sudo /usr/lib/vws/check-region-ready
```

The mTLS API listens on port 8443. Ops listens only on `127.0.0.1:8085`.
The probe uses curl because console-api main has no `vws-region check` subcommand.
One failed request removes readiness; keepalived supplies bounded repeat probes.

## Helper permission boundary

Only root owns the installed helper and sudoers policy. `registration-write` and
`lease-write` have exact argument matches without arguments. `token-write <fp>`,
`token-read <fp>` and `token-delete <fp>` each require exactly one fingerprint:
the token's lowercase hex SHA-256, matching `^[0-9a-f]{64}$`. Argument-free token
verbs, uppercase fingerprints, wrong lengths and extra arguments are denied.
Named operations use anchored POSIX extended regexes over the complete
verb-and-name argument string, with no globs.
`lock` and `unlock` require a 1-64 character name starting with an ASCII letter
or digit and containing only ASCII letters, digits, underscores and hyphens.
Snippet operations require `vws-<lowercase-hex UUID>-user.yaml`, matching the
helper's exact grammar. Empty names, extra words and traversal are denied by
sudo before the helper runs. The helper independently validates names and bounds
stdin to 1 MiB; contents are passed there, never in arguments.

Argument regexes require sudo 1.9.10 or newer, declared in the package dependency.
Debian 12 bookworm (PVE 8) supplies sudo 1.9.13p3 and satisfies this minimum.
`test-install.sh` tests the installed policy with real Debian sudo, allowing
valid names and denying empty, extra, traversal, overlength and malformed input.

Sudo strips `CREDENTIALS_DIRECTORY`; the helper uses its fixed root-owned
`/etc/vws/credentials/region-kek` fallback and checks ownership and permissions.
Postinst ensures the credentials directory is root:root 0700 and refuses an
existing key unless it is a regular, non-symlink, root:root 0600, 32-byte file.
It never generates a key. Provision the same raw key securely on all nodes,
for example with `sudo install -o root -g root -m 0600 ./region-kek
/etc/vws/credentials/region-kek`, then remove the transport copy. Invoke with
`sudo -n /usr/lib/vws/vws-region-helper <operation> [argument]` from the credential-bearing
service context. The package grants no shell, arbitrary path or unrestricted
helper invocation. `visudo` checks syntax; `check-policy.py` separately rejects
valid but broadened grants. The daemon release must implement the corresponding
sudo invocation and credential contract; this package does not change API code.
The fingerprint token verbs require the helper release containing
vws-console-api#904.

## VIP configuration and failover test

Install keepalived on every node. Adapt
`/usr/share/doc/vws-region/examples/keepalived.conf` into the operator's
`/etc/keepalived/keepalived.conf`; preserve any existing VRRP instances. Set the
interface, VIP/prefix and an unused shared virtual router ID. Set distinct node
priorities. Allow VRRP protocol 112 between the nodes, and mTLS port 8443 from
the global layer. Do not expose ops outside loopback.

The example starts in BACKUP and `init_fail` prevents VIP ownership before a
successful probe. A zero script weight puts the instance in FAULT on failure,
instead of merely lowering priority and leaving an unready sole node eligible.
It uses a 2-second interval, one failure and two recovery successes.

On an isolated test PVE cluster, after registration and certificate provisioning:

1. Validate each adapted configuration with `keepalived --config-test` and start
   keepalived. Check readiness on every node and `ip address show dev vmbr0`;
   exactly one ready node must own the VIP.
2. Send an authenticated `/internal/v1/version` request through the VIP using
   curl with the global client certificate, key and region CA. Confirm success.
3. Stop `vws-region` on the VIP owner. Within 10 seconds confirm that it loses the
   VIP, one ready peer gains it, and the same mTLS request succeeds.
4. Restart that daemon; confirm readiness and repeat the stop on the new owner.
   Restart keepalived and confirm an unready node never gains the VIP at startup.
5. Make every node unready (stop each daemon). Confirm no node holds the VIP.
   Restore one daemon and confirm the VIP returns only after successful probes.

Record timestamps, node addresses and journal output for both services.
This procedure is deferred until a phase 1 test cluster exists.

`apt remove vws-region` stops the service and lets dpkg remove package files,
retaining conffiles, the system account and all operator data. `apt purge` also
removes the package's conffiles; maintainer scripts never delete PVE data or
operator credentials. Remove the operator's keepalived instance separately.
Leader-aware upgrades remain phase 2.
