# Rehearsal on a real Proxmox cluster

Status: not run. This is the checklist for vws-deploy#851. Nothing here has been run against a real Proxmox
cluster, and no step of the install runbook was either: `vws install` is tested against a fake SSH target and a
disposable Debian container only. Use a lab cluster you may power off. Never use a production cluster.

Write the result of each step in the table at the end: the command, what you saw, and for the timed steps the
seconds. An unchecked item is an open finding, not a pass.

## Before you start

- A Proxmox cluster of three nodes with quorum, a lab network, and a Debian 12 host for the global layer
  (`docs/operator/vm-install.md`), built from the packages of one `packaging/build.sh` run.
- A PVE user and token as in [regions.md](regions.md#step-2-the-read-only-proxmox-token). Add the write
  permissions of [permissions.md](permissions.md) only for the instance-creation step.
- The inventory of [vm-install.md](vm-install.md#4-the-region-layer) with the instance on the global host
  (and, if you test the other placement, a second one on a separate host).
- A clock or a script that records seconds.

## Checklist

1. Install: `vws install` ends with `registration unregistered` for the instance, in one run. A second run
   reports every step `unchanged` except that row.
2. Certificate: `openssl x509 -noout -ext subjectAltName -in /etc/vws-region/<region>/region.crt` on the instance
   host shows the host address, 127.0.0.1 and the region name. The `regionapi` step of item 3 passes, which
   shows that the global layer trusts the certificate.
3. Register: `vws region add` with the address of every node, the cluster CA and the token. `vws region test`
   passes all five steps. `vws region health` shows the API node.
4. Create: launch one instance in the region and wait for it to run. Record the time. Remove it.
5. Failover: create another instance and, while it is being created, power off the node that `vws region
   health` names as the API node. Then check:
   - `vws region health` passes again. Record the seconds from power-off to the first pass.
   - The creation in flight finishes: one VM, not two, and not left half made. Record the seconds.
   - The instance is not created twice after the node returns.
6. Node reach: the PVE node certificates contain the addresses you gave in `--pve-api-url`; a task lookup and
   a console relay work when the daemon is attached to a node other than the one that runs the VM.
7. Restart: `systemctl restart vws-region@<region>` leaves the region ready with no new registration.
8. Move or recreate (optional): follow [regions.md](regions.md#move-or-recreate-the-daemon) and confirm that the
   old token no longer works against PVE.
9. Clean up: remove the test instances, the test token and the instance on the lab host.

The assumptions of item 6 are unverified; a failure is a finding for vws-console-api, not a reason to change
the runbook silently.

## Record

| Item | Command or action | Result | Seconds | Date and who |
| --- | --- | --- | --- | --- |
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |
| 6 | | | | |
| 7 | | | | |
| 8 | | | | |
| 9 | | | | |
