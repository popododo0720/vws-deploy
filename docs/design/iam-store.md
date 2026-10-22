# vws-iam state on the kube store (#369)

## Problem

#369 found that iam had no state volume (only the `/tmp` emptyDir). The real cause was that the chart passed no `-namespace`, so vws-iam kept users in process memory: each replica had its own copy and a restart lost it (vws-iam#110).

## Chosen

- vws-iam already has a durable store: one custom resource per object (`internal/store/kubestore`), uncached reads, compare-and-swap writes. The chart passes `-namespace` as `{namespace: release}` (R14), ships the CRDs as `charts/vws/crds/iam.yaml` (a copy of vws-iam `deploy/crds/iam.yaml`), and grants one Role in the release namespace on exactly those resources (R11). `services.iam.rbac` also mounts the API token.
- `replicas: 2`, a normal stateless Deployment.
- No image change. The pinned `sha-74a29dc` has the same store and CRDs behind `-namespace`.

## Rejected

- A PVC or emptyDir for iam (the original #369 AC): vws-iam writes no files, so a volume would hold nothing, and a ReadWriteOnce volume cannot be shared by two replicas.
- Env `POD_NAMESPACE` from `metadata.namespace` (the vws-iam#110 fallback): values say flags only (R14), and the pinned image ignores that env, so two replicas would split state in memory until the image moves. `-namespace` works with both images.
- vws-iam owning its CRDs outside the chart: a second install path for one service.

## Limits

- Helm installs `crds/` only on the first install. An existing release needs `iam.yaml` applied once before iam is enabled; `vws apply` does not do that yet.
- Store writes retry without backoff (vws-iam#51).
