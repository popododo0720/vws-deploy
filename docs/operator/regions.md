# Registering regions and AWS accounts

This guide takes an operator from an empty global layer to a registered Proxmox
region and AWS account. Registration lives in the admin API's registry, not in a
config file. The console's admin pages and the `vws` CLI are both clients of that
API, so a region you add with one shows up in the other.

The order is:

1. Write the bootstrap `/etc/vws/global.yaml` on the global layer.
2. Create the read-only Proxmox user and token.
3. Install one region daemon instance for the cluster (`vws install`).
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
  records only that it's set and its fingerprint; the region daemon of that
  cluster holds the secret in its state file,
  `/var/lib/vws-region/<region>/registration.json` (mode 0600, in a 0700
  directory only the daemon can use). See
  [packaging/vws-region/README.md](../../packaging/vws-region/README.md).
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

## Step 3: the region daemon instance

One `vws-region@<region>` instance serves one Proxmox cluster and is the only process that talks to it. It
does not run on a Proxmox node and needs no `/etc/pve`; it must reach the cluster's port 8006, and the
global host must reach the instance's port. You declare the instance in the `regions:` list of the
`vws install` inventory and run the command once; the inventory example, the files and the certificate are
in [vm-install.md](vm-install.md#4-the-region-layer).

```yaml
hosts:
  - name: global-1
    address: 192.0.2.1
    roles: [etcd, global, region]
    regions:
      - name: pve-seoul-1
        port: 8443
```

```sh
scripts/vws install --profile lab --inventory global/hosts.yaml --packages build --credentials credentials
```

The run ends with `global-1 vws-region@pve-seoul-1 registration unregistered`. The instance is running and
has no registration yet, which is correct. Go on with step 4; there is no second install run. The instance keeps
its state in `/var/lib/vws-region/pve-seoul-1`, and the region endpoint you register is `https://<address>:8443`
(or `https://127.0.0.1:8443` when the instance is on the global host). The package's README lists the files
and the readiness probe.

### Where the instance runs

The instance needs two things: to reach the cluster's nodes on port 8006, and to be reachable from the global
host on its port. The global layer's client certificate and the instance's server certificate both come from the internal CA.
There are three placements, and every one uses the same package.

| Placement | Inventory | When |
| --- | --- | --- |
| The global host (default) | the global host gets `region` and a `regions` entry | The cluster is on the network of the global host. No extra machine. Several clusters mean several entries with different ports. |
| A separate host | a host with `roles: [region]` | The cluster is remote, or you want the PVE token off the global host. A Debian 12 machine outside the cluster, or a VM inside it. |
| A VM inside the managed cluster | the same `[region]` host | You want PVE HA to restart it on another node when its node dies. Keep that VM out of every pool VWS manages, or VWS can stop its own daemon. |

Not supported: two instances for one cluster, and an instance on a Proxmox node.

On the `ha` profile (two global hosts; `vws install` does not automate it yet, vws-deploy#776) run the
instance of a cluster on one global host only, or on a separate host or VM. Two global hosts both running it
would be two writers to one cluster.

If the instance is down, only that region's API stops. VMs keep running, because VWS does not host them.
The unit restarts on failure and reads its state file, so it needs no registration again.

## Step 4: register the region

Before `region add`, the PVE pools and the bridge that `region.yaml` names must exist: with the packaged
default, the pools `vws-default` and `vws-templates` and the bridge `vmbr0`. The registration self-check
refuses a missing pool (`pool "vws-default": does not exist`) and leaves the daemon `unregistered`.

The same region, `pve-seoul-1`, registered both ways. The values are examples.

| Field | Console label | CLI option |
| --- | --- | --- |
| name | Region name | `NAME` argument |
| clusterName | Cluster name | `--cluster-name` |
| pveApiUrls | Proxmox API URLs | `--pve-api-url`, repeated, one per address you want tried (1 to 16) |
| regionEndpoint | Region endpoint | `--region-endpoint` |
| caPem | CA certificate | `--ca-pem-file` |
| tokenId | API token ID | `--token-id` |
| tokenSecret | API token secret | `--token-secret-file PATH\|-` |
| zones | Zone letter, Nodes | `--zones` JSON |

`clusterName` is the name `pvecm status` reports. The test fails if it doesn't
match what Proxmox returns from `/cluster/status`.

### On the console

1. Open **Regions** (`/admin/regions`) and choose **Register region**.
2. Fill in Region settings: Region name `pve-seoul-1`, Cluster name `seoul`,
   Proxmox API URL `https://pve1.example.org:8006` (add a field for each other node of the cluster; one
   address is enough for a load balancer you run in front of it), Region endpoint
   `https://127.0.0.1:8443`, and the CA certificate.
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
  --pve-api-url https://pve2.example.org:8006 \
  --token-id 'vws@pve!inventory' \
  --region-endpoint https://127.0.0.1:8443 \
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

### Before the first instance

A registered region serves reads; to create an instance it needs more:

1. A write token (`vws@pve!region` with the roles and ACLs in [permissions.md](permissions.md)) and
   `read_only: false` in `global/regions/<region>.yaml`, then `vws install` and
   `scripts/vws region edit <region> --token-id 'vws@pve!region' --token-secret-file <file>`.
2. Storage for the root volume type in `storages`. A RunInstances without a type launches on the `gp2`
   storage, or on the `gp3` storage when `storages` maps no `gp2` (vws-console-api#1081), so the packaged
   `region.yaml`, which maps only `gp3`, needs nothing more. A stated type without storage fails with
   `UnsupportedOperation: no configured storage for root volume type`. Do not map `gp2` to the same storage
   as `gp3`: a volume's type is read from its storage, so existing `gp3` volumes would then read back as
   `gp2` (vws-console-api#1082).
3. A project mapping to a pool that `region.yaml` `pools` lists:
   `scripts/vws project map <account> --region <region> --pool vws-default`. An unmapped account is bound
   to a pool named after it, and every EC2 call then fails with `scope requires a managed pool`.
4. A template in that project pool, with its root disk on `scsi0` (a launch refuses a template without
   one). DescribeImages lists only the templates in the caller's pool; RunInstances also clones a template
   in `templates.pool` or one tagged `templates.tag`, by its image ID.
5. A `SubnetId` in RunInstances: there is no default subnet.

A full clone (`linked_clones: false`) copies the whole template disk before the instance runs. In the
first rehearsal a 3 GiB template on a slow local-lvm disk took about 6 minutes (358 s) to reach
`running`; terminate took 2 seconds.

## Move or recreate the daemon

The PVE token secret lives only in the state file of the instance, and the global layer keeps only its
fingerprint. So the old host keeps a working secret until you delete the token in PVE. A changed
`--region-endpoint` never asks the old instance to forget its registration. `vws region remove` does
(vws-console-api #1074, in the pinned `GLOBAL_CONSOLE_API_REF`): the region is deleted only after
the instance confirms, and an unreachable instance leaves the region marked for deletion until you repeat
the remove. An API delete with `?force=true` still asks the instance but deletes the region even if that
fails; then delete its token in PVE by hand. A move keeps the region, so do every step below.

### Move the daemon

Move an instance to another host, for example from the global host to a separate host, keeping its region
name. One write-enabled daemon at a time may serve a cluster: a daemon with a working token and a registration
runs the launch finalizer on its own, and changing the global endpoint does not stop it. So the replacement
stays unregistered until the old instance can no longer write.

1. Install the replacement and leave it unregistered. In the inventory, take the entry out of the old host's
   `regions` and add it, with a port, to the new host (a `[region]` host). Delete `regions/<region>/` in the
   credentials directory so the certificate is reissued for the new address; `vws install` refuses the old
   one and tells you to. Run `vws install`: the new instance reports `registration unregistered`, holds no
   token and does nothing. Do not run `vws region edit` yet.
2. Create a new token for the cluster in PVE (step 2, with the permissions your region needs) and save it to
   a file. Do not reuse the old token ID.
3. Quiesce, stop and disable the old instance: stop new launches and wait for the launches in flight to
   finish, then run `systemctl disable --now vws-region@<region>` on the old host and check that
   `systemctl is-active vws-region@<region>` prints `inactive`.
4. If you cannot stop it (the host is down, unreachable or not yours any more), revoke its token first:
   delete the old token in PVE (Datacenter, Permissions, API Tokens, or `pveum user token remove
   vws@pve <old token name>`). Do this before step 5, even if the old host is gone, because the old host
   still holds the secret and would keep writing until the token is gone.
5. Only now register the replacement and verify it:
   `scripts/vws region edit <region> --region-endpoint https://<new-address>:8443 --token-id '<new id>'
   --token-secret-file <file>`, then `scripts/vws region test <region> --token-secret-file <file>` and
   `scripts/vws region health <region>`.
6. Remove what the old instance held: `/etc/vws-region/<region>.env`, `/etc/vws-region/<region>/` and
   `/var/lib/vws-region/<region>` on the old host. `vws install` never removes an instance for you.
7. Delete the old token in PVE if you have not (step 4 did it), so the old secret is dead in every case.

Keep the region name: the registry and the project mappings refer to it.

### Recreate the daemon

Recreate an instance when its host or its state directory is lost, or you rebuild it from scratch. Its state
file is gone, so it is a new, unregistered instance and the global layer has no secret to send it. As in a
move, never register it while an older instance of the same cluster can still write.

1. If the old host may still run, stop and disable its instance (`systemctl disable --now
   vws-region@<region>`); if you cannot, delete its token in PVE before going on.
2. If the host is new, change the address in the inventory and delete `regions/<region>/` as in the move.
3. Run `vws install`. The instance reports `registration unregistered`.
4. Create a new token in PVE (step 2), register it, and verify:
   `scripts/vws region edit <region> --region-endpoint https://<address>:<port> --token-id '<id>'
   --token-secret-file <file>`, then `vws region test` and `vws region health`. Always pass the
   instance's current endpoint: when `--region-endpoint` is omitted the global layer keeps the old one and
   sends the new token there, which leaves the replacement unregistered and can hand a fresh token to an
   old daemon you fenced by revoking its token. Delete the old token in PVE.

The VMs of the region are not touched: they live in the cluster, not in the daemon.

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

## Notes carried over from retired designs

The in-cluster backend designs (volume types, zone model, node networks, VM MTU, AMI and EBS
import) were deleted with the Kubernetes installer in #775. These points still apply to a Proxmox
region and an AWS account, and are kept here.

### Zones are sets of Proxmox nodes

A zone in step 4 is a letter plus the Proxmox nodes it holds (`--zones`). VWS cannot tell whether the
nodes of two zones have separate power or network, so list nodes by their real failure domain. If all
nodes share one, all zones share it: an outage of that domain takes every zone down, and a second
region (a second Proxmox cluster) is the only protection. On AWS a cross-zone volume attach fails with
`InvalidVolume.ZoneMismatch`; whether the Proxmox backend returns that code is not checked here
(needs checking).

### EBS volume types map to Proxmox storages

The EBS volume type a caller asks for is resolved through `storages` in the region's `region.yaml`
(for example `gp3: local-lvm`). Every storage named there needs the storage ACL from
[permissions.md](permissions.md). Changing the mapping means editing `region.yaml`; there is no
admin page for it.

### Host networking belongs to the operator

VWS does not configure host networking on a Proxmox node. The bridge named by `default_bridge`
(for example `vmbr0`) or the SDN vnet must exist, with its VLANs, addresses and MTU, before the region
is registered. Two networks on the same NIC and VLAN are one L2, so their CIDR and MTU must match, and
CIDRs of different networks must not overlap. On AWS a VM inside a VPC sees MTU 9001 while paths out
through the gateway are 1500; the bridge and NIC MTU on a Proxmox node must be at least the VM MTU
plus any tunnel overhead. This is not verified on Proxmox yet (needs checking).

### Moving AWS images and snapshots to a Proxmox region

- The bytes leave AWS on the operator's workstation with the operator's own AWS credentials
  (`aws ec2 export-image`, or `coldsnap download` for an EBS snapshot; an AMI's root device is an EBS
  snapshot). VWS stores no AWS credential for this step.
- Only HVM AMIs boot as a full KVM VM; a paravirtual AMI has no bootloader.
- An initramfs built only for the AWS host may lack the virtio drivers a Proxmox VM uses (virtio disk
  and NIC). Check that a stock AWS-published Linux AMI and an Ubuntu AMI boot unmodified before relying
  on it; this is not verified on Proxmox yet.

### The in-memory store is not for production

Where a VWS service offers an in-memory store (`memstore`), it loses all data on restart and is not
recommended for production. Use etcd (`docs/etcd-runbook.md`).
