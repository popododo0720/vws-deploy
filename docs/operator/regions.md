# Registering regions and AWS accounts

This guide takes an operator from an empty global layer to a registered Proxmox
region and AWS account. Registration lives in the admin API's registry, not in a
config file. The console's admin pages and the `vws` CLI are both clients of that
API, so a region you add with one shows up in the other.

The order is:

1. Write the bootstrap `/etc/vws/global.yaml` on the global layer.
2. Create the read-only Proxmox user and token.
3. Set up the region daemon VIP on the Proxmox nodes.
4. Register the region (console or CLI) and test the connection.
5. Create the AWS role with an ExternalId and register the AWS account.

## Who can do this

The admin API serves only an account's root principal. Anyone else gets
`PermissionDenied` ("administrator required"), and the console's Regions page
shows "Access denied" instead of the list. Each operation is authorized under its
own `vws:Admin*` action:

| Action | Used by |
| --- | --- |
| `vws:AdminListRegions` | Regions page, `vws region list/show/health` |
| `vws:AdminRegisterRegion` | Register region, `vws region add` |
| `vws:AdminUpdateRegion` | Edit region, `vws region edit` |
| `vws:AdminRemoveRegion` | Delete region, `vws region remove` |
| `vws:AdminListAwsAccounts` | AWS account list, `vws aws-account list` |
| `vws:AdminRegisterAwsAccount` | `vws aws-account add` |
| `vws:AdminUpdateAwsAccount` | `vws aws-account edit` |
| `vws:AdminRemoveAwsAccount` | `vws aws-account remove` |
| `vws:AdminMapProject` | project to region or account mapping |
| `vws:AdminTestConnection` | Test connection, `vws region test`, `vws aws-account test` |

When the CLI gets a 403, it prints the missing action and the requestId and
exits 3.

## Secrets are write-only

The Proxmox token secret and the AWS ExternalId go in, and never come back out.

- The AWS ExternalId is stored envelope-encrypted: the API seals it with
  AES-256-GCM under the global key-encryption key (`credentials.kek_file`),
  bound to the AWS account ID, so a sealed value copied to another record
  doesn't open.
- The Proxmox token secret isn't kept by the global layer at all. The registry
  records only that it's set and its fingerprint; the region daemon on the
  Proxmox nodes holds the secret, under its own region KEK
  (`/etc/vws/credentials/region-kek`, see
  [packaging/vws-region/README.md](../../packaging/vws-region/README.md)).
- Reads return only metadata: whether the secret is set and a fingerprint (the
  first 4 bytes of its SHA-256, in hex). The CLI prints it as
  `set · <fingerprint>`, or `not set`.
- The CLI refuses secrets on the command line. `--token-secret` and
  `--external-id` are usage errors; pass `--token-secret-file PATH` or
  `--external-id-file PATH`, or `-` to read stdin. Trailing newlines are
  stripped.
- `global.yaml` never holds a secret inline. It names files by absolute path.

## Step 1: bootstrap global.yaml

`/etc/vws/global.yaml` holds only the bootstrap settings of the global layer.
The parser refuses top-level keys such as `regions`, `proxmox`, `clusters`,
`aws` or `aws_accounts` ("regions and AWS accounts are registered through the
admin API, not global.yaml"). It also refuses any key that looks like a secret
(`secret`, `password`, `token`, `kek`, `key`) unless the key ends in `_file`.

```yaml
iam:
  etcd_endpoints:
    - https://10.0.0.21:2379
    - https://10.0.0.22:2379
    - https://10.0.0.23:2379
  tls:
    ca_file: /etc/vws/etcd/ca.pem
    cert_file: /etc/vws/etcd/client.pem
    key_file: /etc/vws/etcd/client-key.pem
listeners:
  console: 0.0.0.0:8443
credentials:
  kek_file: /run/credentials/vws-console-api.service/kek
  mtls:
    ca_file: /etc/vws/mtls/ca.pem
    cert_file: /etc/vws/mtls/client.pem
    key_file: /etc/vws/mtls/client-key.pem
default_project: default
```

The rules the parser checks:

- `iam.etcd_endpoints` is required, and every entry is `https://host:port` with
  no user info or path.
- `iam.tls.ca_file` and `credentials.kek_file` are required.
- Every `*_file` is an absolute, clean path. A `cert_file` and its `key_file`
  are set together or not at all.
- `listeners.console`, `ec2`, `s3`, `iam` and `ops` are optional `host:port`
  values. An empty one keeps the service's flag default.
- `default_project` follows the Proxmox pool ID rules: up to 64 characters,
  starting with a letter or digit, then letters, digits, `-`, `_` or `.`.
- One YAML document, at most 64 KiB, and no unknown keys.

The connection test needs `credentials.kek_file`. Without it the test endpoint
answers `Unavailable` ("admin tests require credentials.kek_file").

## Step 2: the read-only Proxmox token

VWS reads Proxmox with one API token, `vws@pve!inventory`, holding exactly the
built-in `PVEAuditor` role on `/`. Run this on one Proxmox node as root:

```bash
pveum user add vws@pve --comment "VWS inventory (read-only)"
pveum acl modify / --users vws@pve --roles PVEAuditor
pveum user token add vws@pve inventory --privsep 1 \
  --expire "$(date -d '+1 year' +%s)" --comment "VWS inventory, rotate yearly"
pveum acl modify / --tokens 'vws@pve!inventory' --roles PVEAuditor
```

With `--privsep 1` the token's rights are the intersection of its own ACL and
the user's, which is why both lines grant `PVEAuditor`. `--expire` takes a Unix
epoch; the `date` call sets it one year out. `pveum user token add` prints the
token value once. Save it to a file readable only by you (for example
`~/vws/pve-token`), because Proxmox won't show it again.

Check what the token can do:

```bash
pveum user token permissions vws@pve inventory
```

Every privilege listed should end in `.Audit`. Don't grant anything else: the
connection test fails any privilege that doesn't end in `.Audit`.

Before the expiry date, create a new token value and send it with
`vws region edit NAME --token-secret-file PATH` (or the API token secret field
on the Edit page).

## Step 3: the region daemon VIP

The region daemon (`vws-region` package) runs on every Proxmox node. keepalived
holds one VIP and moves it to a node whose daemon is ready. That VIP is the
region endpoint you register in step 4.

The package ships an example at
`/usr/share/doc/vws-region/examples/keepalived.conf`. Its health script,
`/usr/lib/vws/check-region-ready`, asks `http://127.0.0.1:8085/readyz` with a
2-second limit. The instance starts in BACKUP, and `init_fail` keeps a node from
taking the VIP before it's ready. Set the interface, the VIP and prefix, a
shared unused `virtual_router_id`, and a distinct priority per node, then run
`keepalived --config-test` before starting it. The full procedure, with the
failover test, is in
[packaging/vws-region/README.md](../../packaging/vws-region/README.md#vip-configuration-and-failover-test).

The region's TLS certificate must cover the VIP address or name.

## Step 4: register the region

The same region, `pve-seoul-1`, registered both ways. The values are examples.

| Field | Console label | CLI option |
| --- | --- | --- |
| name | Region name | `NAME` argument |
| clusterName | Cluster name | `--cluster-name` |
| pveApiUrl | Proxmox API URL | `--pve-api-url` |
| regionEndpoint | Region endpoint | `--region-endpoint` |
| caPem | CA certificate (optional) | `--ca-pem-file` |
| tokenId | API token ID | `--token-id` |
| tokenSecret | API token secret | `--token-secret-file PATH\|-` |
| zones | Zone letter, Nodes | `--zones` JSON |

`clusterName` is the name `pvecm status` reports. The test fails if it doesn't
match what Proxmox returns from `/cluster/status`.

### On the console

1. Open **Regions** (`/admin/regions`) and choose **Register region**.
2. Fill in Region settings: Region name `pve-seoul-1`, Cluster name `seoul`,
   Proxmox API URL `https://pve1.example.org:8006`, Region endpoint
   `https://10.1.0.10:8443`, and the CA certificate if Proxmox uses a private CA.
3. Under API token, set API token ID `vws@pve!inventory` and paste the token
   value into API token secret.
4. Add an Availability Zone: Zone letter `a`, Nodes `pve1,pve2,pve3`.
5. Save. Then open the region's Edit page, enter the API token secret again and
   choose **Test connection**. The test runs against the saved registration,
   so save other changes first.

The result appears as a Step, Result, Message table.

### With the CLI

```bash
export VWS_API_URL=https://console.example.org
export VWS_API_TOKEN_FILE="$HOME/.config/vws/console-token"
scripts/vws region add pve-seoul-1 --cluster-name seoul \
  --pve-api-url https://pve1.example.org:8006 \
  --token-id 'vws@pve!inventory' \
  --region-endpoint https://10.1.0.10:8443 \
  --ca-pem-file ~/vws/pve-ca.pem \
  --zones '[{"letter":"a","nodes":["pve1","pve2","pve3"]}]' \
  --token-secret-file ~/vws/pve-token
scripts/vws region test pve-seoul-1 --token-secret-file ~/vws/pve-token
scripts/vws region health pve-seoul-1
```

`VWS_API_URL` must be HTTPS (HTTP only on loopback). `--dry-run` prints the
method and path without sending anything. Writes and tests are never retried.

### Test connection results

The test runs five steps in order: `tls`, `version`, `cluster_name`,
`privileges` and `regionapi`. After a FAIL, later steps are `SKIPPED` with
"previous step failed". `vws region test` exits 1 when any step is FAIL.

With the `PVEAuditor` token from step 2:

```text
STEP	RESULT	MESSAGE
tls	PASS	PVE TLS certificate verified
version	PASS	PVE version read succeeded
cluster_name	PASS	cluster_name accepted
privileges	PASS	token privileges are read-only
regionapi	PASS	regionapi mTLS status read succeeded
```

With an over-privileged token, for example one that also holds `VM.Allocate`:

```text
STEP	RESULT	MESSAGE
tls	PASS	PVE TLS certificate verified
version	PASS	PVE version read succeeded
cluster_name	PASS	cluster_name accepted
privileges	FAIL	token holds a privilege outside PVEAuditor read-only access
regionapi	SKIPPED	previous step failed
```

The command exits 1 on that run. Remove the extra ACL entries and test again.

Without `--token-secret-file`, the four Proxmox steps are `SKIPPED` with
"tokenSecret is needed; only the region daemon stores it", and only
`regionapi` runs. The global layer doesn't keep the token secret, so it can
only test Proxmox with a secret you supply for that one call.

## Step 5: the AWS account

VWS reaches AWS by assuming one role per AWS account, always with an
ExternalId. The role's permissions are decided by the AWS account owner; VWS
IAM is the first boundary and the role policy is the second.

### Create the role

In the AWS account, create an IAM role whose trust policy lets the principal of
the `vws-aws-proxy` source credentials assume it, and only with your
ExternalId:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "<ARN of the vws-aws-proxy source principal>" },
      "Action": "sts:AssumeRole",
      "Condition": { "StringEquals": { "sts:ExternalId": "<your ExternalId>" } }
    }
  ]
}
```

The ExternalId must be 2 to 1224 characters of letters, digits and
`_+=,.@:/-`. Keep it in a file (for example `~/vws/aws-external-id`).

### Register and test it

```bash
scripts/vws aws-account add 111122223333 \
  --role-arn arn:aws:iam::111122223333:role/vws-access \
  --allowed-regions us-east-1,eu-west-1 \
  --external-id-file ~/vws/aws-external-id
scripts/vws aws-account test 111122223333
```

The ID is the 12-digit account ID, and the role ARN must name a role in that
same account. `vws aws-account test` uses the saved registration and runs two
steps:

```text
STEP	RESULT	MESSAGE
assume_role	PASS	AssumeRole succeeded
get_caller_identity	PASS	AWS account identity matched
```

On failure the messages are fixed: `AssumeRole failed` (then
`get_caller_identity` is `SKIPPED`), or `AWS account identity check failed`.
They never carry AWS error text. The command exits 1 unless both steps PASS.
