# Admin page for the EBS volume type mapping (#391 AC3, #614)

## Problem

#391 AC3 and #614 AC1-AC3 ask for a VWS admin page, outside vws-console, that lists and edits
`spec.storage.volumeTypes` and applies a change without restarting the cluster. Today the mapping
reaches vws-storage only one way: `vws apply` renders it into repeated `-kube-volume-type` flags
(`scripts/vws.d/lib/render.py` `volume_type_flags`, #611) and runs `vws_helm upgrade`, which rolls
the storage Deployment.

Two facts constrain where the page can live and how it applies a change:

- After `vws installer finish`, no in-cluster identity can run `helm upgrade`. The operator rights
  have no Secrets (Helm keeps releases in Secrets), no RBAC and no cluster writes
  (`docs/installer.md`, R20). A page that "runs vws apply" would need those rights back.
- The installer pod is a one-shot wizard with a TTL, a reaper and reclaimed rights (R20). Keeping
  it alive as a long-lived page contradicts that design.

## Chosen (proposal, pending the lead's answer on #614)

A new small control service `admin` in `charts/vws/values.yaml` `services` (R5), and vws-storage
reads the mapping at run time instead of only from flags.

1. The chart renders one ConfigMap `vws-volume-types` in the release namespace from
   `spec.storage.volumeTypes` (same data as the flags today). vws-storage gets a flag naming it
   and re-reads it on change (watch or a short-TTL cached get). The `-kube-volume-type` flags stay
   as the start-up value until vws-storage ships the reader, then go away (one wave, no shim).
2. `admin` serves the page on a ClusterIP Service only (no Ingress, no NodePort). Access is
   `kubectl port-forward`, so the operator's kube credentials are the auth boundary, the same as
   `vws` CLI commands.
3. Saving validates like `vws_check_volume_types` (every StorageClass and VolumeSnapshotClass
   exists, volumeMode is Block or Filesystem, nfs refuses a mapping), then writes both the
   VWSCluster `spec.storage.volumeTypes` and the ConfigMap. vws-storage picks up the change with no
   pod restart. The next `vws apply` renders the same ConfigMap from the CR, so the two converge
   and a re-save is a no-op (idempotent).
4. RBAC of `admin` (R11, only through `services.admin.rbac`): get and patch `vwsclusters` and
   get and update `configmaps` in the release namespace; get and list `storageclasses` and
   `volumesnapshotclasses`. No Secrets, no Deployments, no RBAC.

## Rejected

- Post-install mode of `vws-installer` (option b on #614): needs the installer pod to outlive
  `vws installer finish`, which R20 and the reaper exist to prevent.
- `admin` patches the storage Deployment args: needs `patch deployments`, which also lets it change
  the image and the service account (an escalation path), and the live Deployment drifts from the
  Helm release until the next apply.
- `admin` edits only the CR and the operator runs `vws apply`: does not meet "saving triggers a
  live apply" (#614 AC2).
- vws-storage reads the VWSCluster CR directly: couples a data-plane service to the deployment
  CRD and its API group; a ConfigMap keeps vws-storage install-agnostic.

## Open items

- `services.*.rbac` has no `resourceNames` today (`charts/vws/values.schema.json`), so the
  ConfigMap rule would cover every ConfigMap in the release namespace. Adding `resourceNames` to
  the schema and R11 is a prerequisite PR.
- The operator Role has no `pods/portforward`; the port-forward step needs the user's own
  credentials or a new narrow grant.
- vws-storage needs an issue for the run-time reader (flag, reload, metric and log on reload).

## Order of PRs

1. vws-storage: run-time reader of the ConfigMap (separate repo issue).
2. vws-deploy chart: `resourceNames` in `services.*.rbac` (schema, guard, failing-first test).
3. vws-deploy chart: render `vws-volume-types` ConfigMap and the vws-storage flag.
4. `admin` service (code and image) and its `services.admin` entry with probes, PDB and RBAC (R4).
5. Docs: `docs/vwscluster.md` and `docs/vws-cli.md` for the admin page.
