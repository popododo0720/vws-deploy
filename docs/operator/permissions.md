# Pool permissions for Proxmox administrators

In VWS a Proxmox pool is a project. The VWS console and API check VWS IAM
policies per project, and Proxmox UI admins get Proxmox ACLs on the same pool.
Both sides read one source, so a VM's pool decides who sees and controls it.

This guide shows the `pveum` commands for the VWS token user, its roles, one
project pool (`team-a`) and a human admin group. Command syntax follows the
`pveum` manual: https://pve.proxmox.com/pve-docs/pveum.1.html

The example names are `team-a` (project pool), `vws-images` (template pool),
`local-lvm` (VM storage), `backup-nfs` (backup storage) and zone `vwszone`
with vnet `vnet10`. Replace them with yours.

## The VWS user and its tokens

VWS uses one user, `vws@pve`, with no usable password and no console login.
It has three tokens, all with `privsep=1`, so a token's rights are the user's
rights intersected with the token's own ACLs. Tokens expire after one year.

```bash
pveum user add vws@pve --comment "VWS service user, no login"
EXPIRE=$(date -d '+1 year' +%s)
pveum user token add vws@pve inventory --privsep 1 --expire "$EXPIRE"
pveum user token add vws@pve region    --privsep 1 --expire "$EXPIRE"
pveum user token add vws@pve backup    --privsep 1 --expire "$EXPIRE"
```

`token add` prints the secret once. Enter it in the region's write-only
secret field (see [regions.md](regions.md)); never put it in a file.

| Token | Use |
| --- | --- |
| `vws@pve!inventory` | read-only regions |
| `vws@pve!region` | the region daemon's changes |
| `vws@pve!backup` | DR backups, kept apart from `region` |

## Roles

`PVEAuditor` is built in. Create the other roles once per cluster.
`VWSBackupOperator` is this guide's name for the backup role; the design
doesn't fix one.

```bash
pveum role add VWSRegionOperator --privs "VM.Audit VM.Allocate VM.Clone VM.PowerMgmt VM.Config.CPU VM.Config.Memory VM.Config.Disk VM.Config.Network VM.Config.Cloudinit VM.Config.Options VM.Config.HWType VM.Snapshot VM.Console VM.GuestAgent.Audit Pool.Audit"
pveum role add VWSTemplateReader --privs "VM.Audit VM.Clone"
pveum role add VWSStorageUser    --privs "Datastore.Audit Datastore.AllocateSpace"
pveum role add VWSNetUser        --privs "SDN.Use SDN.Audit"
pveum role add VWSBackupOperator --privs "VM.Backup Datastore.AllocateSpace"
```

## ACLs

With `privsep=1` the user `vws@pve` needs the same rights, or the
intersection is empty. Run every line below a second time with
`--users vws@pve` in place of `--tokens ...`.

Inventory token, read only on everything:

```bash
pveum acl modify / --tokens 'vws@pve!inventory' --roles PVEAuditor --propagate 1
```

Region token. VM rights go only on managed pools, never on `/` or `/vms`:

```bash
pveum acl modify /pool/team-a     --tokens 'vws@pve!region' --roles VWSRegionOperator --propagate 1
pveum acl modify /pool/vws-images --tokens 'vws@pve!region' --roles VWSTemplateReader --propagate 1
pveum acl modify /storage/local-lvm --tokens 'vws@pve!region' --roles VWSStorageUser --propagate 1
pveum acl modify /sdn/zones/vwszone/vnet10 --tokens 'vws@pve!region' --roles VWSNetUser --propagate 1
pveum acl modify / --tokens 'vws@pve!region' --roles PVEAuditor --propagate 1
```

Add the `/storage/...` line for every storage in `region.yaml` `storages` and
for the snippets storage. A local bridge uses
`/sdn/zones/localnetwork/<bridge>`.

Backup token, on the managed pool and the backup storage:

```bash
pveum acl modify /pool/team-a      --tokens 'vws@pve!backup' --roles VWSBackupOperator --propagate 1
pveum acl modify /storage/backup-nfs --tokens 'vws@pve!backup' --roles VWSBackupOperator --propagate 1
```

Each new project pool gets the `/pool/<pool>` lines for `region` and `backup`.
Then map it to the project with `vws project map PROJECT --region REGION --pool team-a`
([vws-cli.md](../vws-cli.md)).

## Human admins on the same pool

A Proxmox UI admin team gets its rights through a group on the same pool:

```bash
pveum group add team-a-admins --comment "Proxmox admins of project team-a"
pveum user modify alice@pve --groups team-a-admins
pveum acl modify /pool/team-a --groups team-a-admins --roles PVEVMAdmin --propagate 1
```

VWS shows the project every VM in the pool, including VMs made in the Proxmox
UI. The only exception is volume holder VMs (tag `vws.holder`), which are
hidden from the instance list.

## Pool.Allocate is a permission change

Moving a VM into or out of a pool changes which VWS project sees and controls
it, starting at the next read. Nothing in VWS has to be updated, and VWS never
undoes the move. A VM taken out of every pool falls to the admin's default
project.

So treat `Pool.Allocate` like `Permissions.Modify`:

- Give it only to trusted admins. `PVEVMAdmin` doesn't include it.
- VWS tokens never hold it. VWS never calls `PUT /pools`; the only way it puts
  a VM in a pool is the `pool` parameter of a clone it makes.
- The region daemon refuses to start if its token has `Pool.Allocate`,
  `Permissions.Modify`, `Sys.Modify` or `Datastore.Allocate`, or VM rights
  outside its managed pools (vws-console-api
  `internal/backend/proxmox/selfcheck.go`).

### What to watch

Pool membership changes are `PUT` calls under `/api2/json/pools` on the
Proxmox API. They don't create a task, so they aren't in the task log. Watch:

- `/var/log/pveproxy/access.log` on every node for `PUT` requests to
  `/api2/json/pools`, with the user that made them.
- Changes to `/etc/pve/user.cfg`, which holds pool membership and ACLs.
  `pveum pool list` and `pveum acl list` show the current state.

The VWS design says the region daemon will record pool moves it sees in
`/cluster/resources` in the VWS audit log. That isn't implemented yet, so the
Proxmox records above are the signal today.

## Privileges VWS never gets

| Privilege | Why |
| --- | --- |
| `Permissions.Modify` | VWS never changes permissions |
| `Pool.Allocate` | a pool move is an admin's permission change |
| `Sys.Modify` | no node or cluster configuration |
| `Datastore.Allocate` | no storage deletion or deletion of arbitrary volumes |
| `VM.Migrate` | moving VMs between nodes is PVE HA's or an admin's job |
| `VM.Snapshot.Rollback` | VWS doesn't offer rollback |
| `VM.GuestAgent.Unrestricted` | no command execution inside guests |

VWS also never uses a `root@pam` token or `skiplock`.

## What Proxmox ACLs can't express

The VWS IAM policy is the first boundary and the Proxmox ACL the second.
Proxmox privileges are coarse and attach to a path; these AWS IAM features
have no Proxmox equivalent:

- Policies per API action (`ec2:StopInstances` versus `ec2:StartInstances`
  are both `VM.PowerMgmt`).
- Condition keys, such as source IP, time or MFA.
- Resource tags in policies.
- Explicit deny statements for one action. The built-in `NoAccess` role on
  a path blocks every privilege there, not a single action.
- Permission boundaries and session policies.

A Proxmox admin with `PVEVMAdmin` on `team-a` can do anything to its VMs that
the role allows, whatever the project's VWS IAM policies say.

## Checking

```bash
pveum user token permissions vws@pve region
pvesh get /access/permissions --userid 'vws@pve!region'
```

Neither should list `Pool.Allocate`, `Permissions.Modify`, `Sys.Modify` or
`Datastore.Allocate`, and VM privileges should appear only under
`/pool/...` paths.
