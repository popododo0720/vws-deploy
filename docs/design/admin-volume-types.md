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

## PR 3 as built (#391)

PR 1 (the vws-storage reader) has not landed, so PR 3 ships only the ConfigMap. It departs from
step 1 above in three ways:

- The ConfigMap has no value of its own. The template builds it from
  `services.storage.repeatedFlags.kube-volume-type`, the list `vws render` already fills from
  `spec.storage.volumeTypes` (#611). The flags and the ConfigMap cannot drift, and R5 and R14 keep
  one source.
- The data has one key per mapped type, and the value is the flag value after `TYPE=`:
  `gp3: ceph-block/Block/ceph-block` for `-kube-volume-type=gp3=ceph-block/Block/ceph-block`.
  vws-storage can parse it with its flag parser. An unmapped type has no key.
- The ConfigMap renders whenever storage renders, with `data: {}` when nothing is mapped. `admin`
  gets `update` and no `create`, because R11 cannot narrow `create` by name. So the object must exist
  before the first save.

The vws-storage flag that names the ConfigMap and its `configmaps` get and watch rule wait for
PR 1. Before then, vws-storage rejects an unknown flag at start, and the rule would grant a right
that no code calls (guardrail 4). `scripts/check_render.py` fails a render whose
`vws-volume-types` data differs from the storage `-kube-volume-type` args, or that has no such
ConfigMap while storage renders.

## PR 4a as built (#391)

PR 4 is split. This part is only the page code, `vws-installer admin` in `installer/`. It is not
wired into the chart yet. It departs from the steps above in four ways:

- `admin` is a subcommand of the vws-installer binary, not a new image. The installer image
  already has kubectl, and a second image would need its own build, publish and digest pin.
  Rejected: a separate `cmd/vws-admin` binary and image, which repeats `installer-image.yml` and
  `installer-publish.yml` for one page.
- A save writes with `kubectl patch --type=json --field-manager=vws-admin`, not server-side apply.
  With apply, unmapping a type that field manager `vws` also owns would leave the type in place.
  The patch replaces `spec.storage.volumeTypes` whole and starts with a `test` of
  `metadata.resourceVersion`, so a save over a CR that changed since the page loaded fails and
  writes nothing. The ConfigMap gets one `add` of `/data`. So the RBAC is `patch` on both objects,
  not `update` on the ConfigMap.
- The page refuses a mapping that adds the first `Filesystem` type. That type needs the
  datavolumes list rule that only `vws apply` renders (`render.py` `volume_type_flags`).
- The page checks `Host` (localhost or a loopback IP only, against DNS rebinding) and uses
  `http.CrossOriginProtection`, because a browser page from any site can post a form to a
  port-forwarded localhost port. `-listen` must be a loopback IP. /healthz and /readyz come from
  `obs.Setup` on `VWS_HEALTH_ADDR`, a separate port, as step 4 asks.

The CR is written before the ConfigMap. If the ConfigMap write fails, a re-save or the next
`vws apply` (which reads the CR from the cluster) renders the same data, so the two converge.

Still open after PR 4a:

- PR 4b: `services.admin` in values.yaml (the installer image, probes on the health port, PDB,
  RBAC `get`/`patch` `vwsclusters` `[vws]` and `configmaps` `[vws-volume-types]`, `list`
  `storageclasses` and `volumesnapshotclasses`), no Service, and the deny-all-ingress
  NetworkPolicy that still lets the kubelet reach the health port.
- `vws init` and `vws reconfigure` save the CR with server-side apply as `vws` and no
  `--force-conflicts` (`applyrun.sh` `vws_save_spec`). After an admin save, a reconfigure that
  changes a type the page wrote can hit a field conflict. PR 4b or a CLI PR must settle that.
- PR 1 (the vws-storage reader). Until it lands, vws-storage reads only its flags, so a save
  reaches it at the next `vws apply`, which renders the flags from the CR and rolls the pod. The
  page says so after a save.
- PR 5 (docs).

## PR 4b as built (#391)

PR 4b wires the page into the chart as `services.admin`. It departs from the design in these ways:

- `enabled: false`. The installer package on GHCR is private (an anonymous token request answers
  "authentication required" on 2026-11-09), so a default pod would sit in ImagePullBackOff. iam is
  off for the same reason. The default render is byte-identical to the one before this PR.
- `imageName: installer` and `command: [/opt/vws/bin/vws-installer, admin]`. The page is a
  subcommand of the installer image, whose entrypoint is `scripts/vws`, so the chart needs a
  repository name other than the service name and a `command`. `args` stay exactly the flags (R14).
  `imageTag` pins `sha-d543fa640b164ec436a3cda9c6217a18e00ddc4b` (index digest
  `sha256:fed9ac13593da64b234740643f6faf1249fe09e222e02536ab6369ba75ab2bad`, installer-publish run
  42644159420). Rejected: a second image for the page, as in PR 4a.
- `loopback: true` is a schema key, not an `allowFrom` variant. It requires `healthPort` and
  `-listen` on 127.0.0.1, and refuses `external`, `allowFrom`, `scrapeMetrics`, `httpProbes`,
  `state` and `portEnv`. It renders no Service. Rejected: a `-peers` Service, which `state` would
  need and loopback forbids.
- The `http` containerPort stays, because R15 makes it the service port and `kubectl port-forward`
  targets it. Nothing but the pod loopback reaches it.
- The NetworkPolicy has one ingress rule: the health port, no `from`. Kubelet probes come from the
  node, so the health port must stay open to every source (#650: a deny-all policy must not block
  the probe port). The page port needs no rule: it is bound to 127.0.0.1.
- The `vwsclusters` rule names its group as `{crd: vwsclusters}`. The template and
  `scripts/check_render.py` read `spec.group` from `charts/vws/crds/vwsclusters.yaml`, because R18
  allows that group to be written only there. The CRD header no longer says no service reads it.
- RBAC is what the page calls: `get` and `patch` on VWSCluster `vws`, `get` and `patch` on
  ConfigMap `vws-volume-types` (the page never reads it, but `kubectl patch` gets the object before
  it sends the patch), `list` on storageclasses and
  volumesnapshotclasses (`kubectl get KIND -o jsonpath` is a list). The design said `update` on the
  ConfigMap and get/list on the classes; PR 4a replaced the first and the code needs only list.
- The leader-election Lease Role is still rendered, as for every service with `rbac`. The page does
  not use it, and it grants a Lease named `admin` only.
- `scripts/guard.sh config` renders admin enabled and runs `check_render.py` on it. Its self-test
  breaks the render in ten ways (a Service under any name, no policy, a policy with a `from`, on the
  page port or with a second rule, no command, another command, another image repository, another
  Role group) and a non-loopback `-listen`.

Still open before `admin` can be enabled:

- The installer package must be public or the chart needs an image pull secret.
- The operator needs `pods/portforward` on the admin pod. The operator Role has none.
- `vws reconfigure` and `vws-admin` write the same field. The handoff between the field managers
  is not settled (see "Still open after PR 4a").
- The vws-storage reader (popododo0720/vws-storage#222), so a save reaches vws-storage without a
  `vws apply`.
- Not run: kubectl needs a discovery cache directory under a read-only root filesystem with no
  `HOME`. Unverified until a live run.
- PR 5 (docs: `docs/vwscluster.md`, `docs/vws-cli.md`).

## PR 4c as built (#391): the vws and vws-admin handoff

`vws init`, `reconfigure`, `upgrade`, `rollback` and `restore` saved the CR with server-side apply
as field manager `vws` and no `--force-conflicts`. The page writes with a JSON patch as
`vws-admin`, an Update. Two things broke after a page save:

- A CLI change to a type the page wrote was a field conflict, so the save failed.
- A CLI spec that drops a type the page added left it in the CR. Server-side apply removes only
  fields no other manager owns, so the CR and the spec the operator wrote differ.

Chosen: the CLI saves an existing CR the way the page does. `vws_save_spec`
(`scripts/vws.d/lib/applyrun.sh`) sends a JSON patch as field manager `vws`: a `test` of the
`metadata.resourceVersion` it read, then one `replace` of `/spec`. The whole spec is what the
operator wrote, whoever owned a field before, and a save over a CR that changed since the read
(a page save in the meantime) fails, writes nothing and exits 3 with a message to run the command
again. Each side owns what it wrote last, and neither overwrites a change it has not seen.
Creating the CR (no CR yet) stays server-side apply. The revert after a failed apply reads the CR
again first, because the save and the apply status moved the resourceVersion. `restore` tests the
live CR's resourceVersion, not the stale one in the backup.

Rejected:

- `--force-conflicts` on the server-side apply: fixes the conflict but not the dropped type, and
  it overwrites a page save the CLI never read.
- Server-side apply with `metadata.resourceVersion` set (a precondition) and `--force-conflicts`:
  stops the lost update, but a dropped type the page owns still stays.
- `kubectl replace` with the full object: needs the `update` verb and sends metadata the CLI does
  not own; the patch needs only `patch`, which the operator Role has.

Not changed: the preflight of `vws apply` writes `spec.network.vmMTU` and `underlayMTU` with a
merge patch when the tunnel NIC needs a lower MTU (`lib/checks.sh`, #236), and `vws apply` writes
the status subresource. Neither replaces the spec or touches `spec.storage.volumeTypes`.

Still open: the installer package on GHCR, `pods/portforward` for the operator, the vws-storage
reader (popododo0720/vws-storage#222), the kubectl discovery cache under a read-only root, and
PR 5 (docs).

## PR 5 as built (#391): docs

`docs/vwscluster.md` "Admin page for the volume type mapping" says how to turn the page on, reach
it, what a save checks and writes, and when vws-storage sees the change. `docs/vws-cli.md`
`vws apply` says it is the step that reaches vws-storage after a page save. The page is
`vws-installer admin`, not a `scripts/vws` command, so it has no `### vws` entry (R17).

The kubectl discovery cache is no longer open. kubectl v1.35.0, the binary the installer image
installs (`KUBERNETES_VERSION`, `KUBECTL_SHA256`), ran the page's three calls (`get configmaps -o
json`, `patch --type=json --field-manager=vws-admin`, `get storageclasses -o jsonpath`) against a
throwaway kube-apiserver with `HOME` on a read-only directory and with `HOME` unset in a read-only
working directory. All exited 0 and wrote nothing, with no warning on stderr: kubectl skips the
cache when it cannot write it. So the chart needs no cache volume and no `HOME`. The run used a
token and `--server`, not the in-cluster config; the cache path is the same code either way.
The walk-through on a real cluster, a page save inside the pod included, is
https://github.com/popododo0720/vws-deploy/issues/739 (hold).

Still open before box 3 can be ticked: the installer package on GHCR, `pods/portforward` for the
operator, and the vws-storage reader (popododo0720/vws-storage#222). With the reader, a save
applies with no `vws apply`; without it, a save applies at the next `vws apply` as a vws-storage
rollout, which already meets "without restarting the cluster".
