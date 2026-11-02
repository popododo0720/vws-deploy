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
2. `admin` listens on `127.0.0.1` only and has no Service, Ingress or NodePort. A ClusterIP
   Service would not be a boundary: every pod in the cluster can reach it, and the chart renders a
   NetworkPolicy only for services with `allowFrom`
   (`charts/vws/templates/control-plane.yaml`). `kubectl port-forward` reaches the pod loopback,
   so the operator's kube credentials (`pods/portforward` on the admin pod) are the auth boundary,
   the same as `vws` CLI commands. The chart also renders a deny-all-ingress NetworkPolicy for
   `admin` as defense in depth. If a later need puts `admin` behind a Service, it must first add
   in-app auth like vws-installer (TLS plus a one-time access code, `docs/installer-api.md`).
3. Saving validates with the same rules `vws apply` uses today: every StorageClass and
   VolumeSnapshotClass exists (`scripts/vws.d/lib/checks.sh` `vws_check_volume_types`), volumeMode
   is Block or Filesystem (the CRD enum in `charts/vws/crds/vwsclusters.yaml`), and driver nfs
   refuses a mapping (`scripts/vws.d/lib/render.py`). It then writes both the
   VWSCluster `spec.storage.volumeTypes` and the ConfigMap. vws-storage picks up the change with no
   pod restart. The next `vws apply` renders the same ConfigMap from the CR, so the two converge
   and a re-save is a no-op (idempotent). Field ownership: `admin` writes the CR with server-side
   apply under its own field manager (`vws-admin`) that owns only `spec.storage.volumeTypes`.
   `vws init` and `vws apply` write the CR as field manager `vws`, so PR 4 must define and test
   how the two managers hand off that field (conflict, force, or `vws` dropping it).
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
  the schema and R11 is a prerequisite PR. Once it lands, `admin` limits `vwsclusters` get and
  patch to `resourceNames: [vws]` and `configmaps` get and update to `[vws-volume-types]`.
- The operator Role has no `pods/portforward`; the port-forward step needs the user's own
  credentials or a new narrow grant.
- vws-storage needs an issue for the run-time reader (flag, reload, metric and log on reload).
  Its RBAC is `configmaps` get and watch with `resourceNames: [vws-volume-types]` in the release
  namespace, through `services.storage.rbac`.

## Order of PRs

1. vws-storage: run-time reader of the ConfigMap (separate repo issue).
2. vws-deploy chart: `resourceNames` in `services.*.rbac` (schema, guard, failing-first test).
3. vws-deploy chart: render `vws-volume-types` ConfigMap and the vws-storage flag.
4. `admin` service (code and image) and its `services.admin` entry with probes, PDB and RBAC (R4).
   Acceptance check: `admin` binds `127.0.0.1` only, the chart renders no Service for it and a
   deny-all-ingress NetworkPolicy, and a test proves another pod cannot reach the page. Kubelet
   httpGet probes target the pod IP, so probes use a separate port that serves only `/healthz`
   and `/readyz` (or exec probes); the page and its write endpoints stay on loopback.
5. Docs: `docs/vwscluster.md` and `docs/vws-cli.md` for the admin page.
