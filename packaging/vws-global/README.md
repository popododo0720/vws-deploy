# vws-global

Build the Linux amd64 API and proxy from `GLOBAL_CONSOLE_API_REF`, IAM from
`GLOBAL_IAM_REF`, and the console from `GLOBAL_CONSOLE_REF` in
`scripts/versions.env`. These are one release set. Use that file's Node version
and verify the upstream linux-x64 tar.xz with `GLOBAL_NODE_ARCHIVE_SHA256`.
The builder independently verifies the supplied Node binary's SHA256 and version.
Include the archive's LICENSE (including bundled dependency notices).

Prepare the console's standalone output with its `public/` and `.next/static/`
directories copied into it, matching the console Dockerfile. Then run:

```sh
packaging/vws-global/build.sh 1.0.0-1 ./vws-console-api ./vws-aws-proxy \
  ./vws-iam ./node ./LICENSE ./standalone ./build
sudo apt-get install ./build/vws-global_1.0.0-1_amd64.deb
```

The builder packages supplied inputs without downloading or compiling. Go
binaries must be built for Debian 12 amd64 (prefer `CGO_ENABLED=0`). Installation
creates separate system users but does not enable or start services. Repeat
configuration preserves operator credentials and data; removal stops services,
and purge removes package conffiles only. IAM and admin records remain in etcd.

Provision root-owned credentials under `/etc/vws/credentials` (directory 0700,
files 0600). The units load private inputs only with systemd `LoadCredential`.
Public certificates and CAs below live in `/etc/vws` and must be readable by the
service users. No credentials are generated or supplied as defaults.

| Unit | Private credential inputs | Public files |
| --- | --- | --- |
| console-api | `admin-kek` (base64 of 32 bytes), `console-token`, `vws-access-key`, `vws-secret-key`, `api-etcd-key.pem`, `api-mtls-key.pem` | `etcd-ca.pem`, `api-etcd.pem`, `internal-ca.pem`, `api-mtls.pem` |
| aws-proxy | `aws-source` (JSON fields `AccessKey`, `Secret`, optional `SessionToken`) | none |
| iam | `iam-etcd-key.pem`, `iam-key.pem`, directory `iam-kek/` | `etcd-ca.pem`, `iam-etcd.pem`, `internal-ca.pem`, `iam.pem` |
| console | none | none |

`iam-kek/active` holds the active key id; each other file is named by key id and
contains base64 of 32 bytes. Systemd flattens the directory to `kek_active` and
`kek_<id>`, as IAM expects. Keep all KEKs identical across replicas and backups.
Use distinct etcd client certificates with permissions for `/vws/admin/` and
`/vws/iam/` respectively. Internal certificates must support mutual TLS and the
configured loopback server names. Provision etcd and bootstrap IAM separately.

Edit `/etc/vws/global.yaml` for bootstrap endpoints, credential paths and project.
The shipped absolute `/run/credentials/vws-console-api.service/` paths correspond
to systemd's default credentials directory. API listener settings currently use
the binary's matching loopback defaults; change its flags in a systemd override
if changing listeners. Region and account registration belongs to the admin API.
The pinned proxy still requires a metadata snapshot in `/etc/vws/aws-accounts.json`
(initially `[]`); export registered records there and restart it after changes.
For each account, add a proxy-only `LoadCredential=aws-external-id-<account-id>:...`
override. The current proxy does not consume encrypted external IDs from etcd.

The proxy uses a private Unix socket shared only with the API group. Startup
removes a stale socket, waits for HTTP, then grants group access. It has no
`/healthz` or `/readyz`: the local probe expects its 403 rejection of an empty
request, which cannot call AWS. Other units require `/readyz` status 200.
All probes make up to 12 one-second requests one second apart (at most 23 s,
inside `TimeoutStartSec=30`), so a daemon has time to bind, and fail closed.

```sh
sudo systemctl enable --now vws-iam vws-aws-proxy vws-console-api vws-console
journalctl -u vws-console-api -u vws-aws-proxy -u vws-iam -u vws-console
```

Adapt the shipped `examples/Caddyfile` domain names and TLS policy before enabling
Caddy. Only loopback listeners are exposed behind it; ops stays private. Console
readiness checks the API. Logs go to journald; API ops also exports `/metrics`.
Run `packaging/vws-global/test.sh` for packaging, credential and failure checks.
Tests use runtime fixtures, not production daemons. Debian service startup and
the pvefake API smoke test are left for part 2 of vws-deploy#766.
