# Installing vws-iam (#243)

The design is vws-iam#1 and the contract is vws-proto#24 (`vws/iam/v1`, branch `feat/iam-v1`).

## What is in now

- **Chart service `iam`**: `enabled: false`, so the default render still leaves it out. The image is pinned with `services.iam.imageTag: sha-74a29dc` to `ghcr.io/popododo0720/vws/iam:sha-74a29dc` (digest `sha256:6c301b75fd46584fd4d6c86f77be9d4389e0492a210d34bc31ab5a34fb9acc5d`). vws-iam#113 published that tag. The image binary contains go1.26.9. An anonymous manifest request returned HTTP 200 and that digest on 2026-10-16. `spec.version` does not move it. Port 9095, `-listen=0.0.0.0:9095`. The vws-iam binary's `-listen` default is `127.0.0.1:9095`; `-health-listen` stays `127.0.0.1:9096` and is not passed. console-api connects with CR flags `iam=grpc` plus the three `0.0.0.0` addr flags (`ec2-addr`, `s3-addr`, `iam-addr`), and the chart sets `-iam-endpoint=iam:9095`.
- **`enabled`**: available on every service. With `false` the chart creates none of that service's objects, and `check_render.py`, `vws status`, the profile resource calculation and the Secret step of `vws apply` skip it too.
- **KEK**: `kekSecret: vws-iam-kek`. `vws apply` creates the Secret in the release namespace only when it does not exist (`kek`: base64 of 32 random bytes, `key-id`: `kek-1`). The value goes only through kubectl stdin and never appears in arguments, output or logs. An existing Secret is left alone (no silent rotation). The pod gets `VWS_IAM_KEK` and `VWS_IAM_KEK_ID` only through secretKeyRef. `check_render.py` fails if the KEK is in args.
- **State and replicas (#369)**: `replicas: 2`. The chart passes `-namespace=<release namespace>`, so every replica reads and writes the vws-iam kube store: the custom resources of `charts/vws/crds/iam.yaml` in the release namespace, through an uncached client with resourceVersion compare-and-swap. A write on one replica is read on the other, and a restart rereads it. No volume holds state (the only volume is the `/tmp` scratch dir). The pinned image `sha-74a29dc` already has this store behind `-namespace`; vws-iam#110 only stops a pod from falling back to memory when the namespace is missing. Like any stateless service, the schema and `check_render.py` require at least 2 replicas.
- **CRDs and RBAC (#369, #633)**: `charts/vws/crds/iam.yaml` is a byte copy of vws-iam `deploy/crds/iam.yaml` (13 Namespaced CRDs since #633, group `iam.vws.popododo0720.io`). `services.iam.rbac` is one rule in the release namespace on exactly those 13 resources with get, list, create, update and delete, the same as vws-iam `deploy/rbac/role.yaml`; it also mounts the API token. `scripts/guard.sh config` checks the render. Helm installs `crds/` only on the first install, so an existing release needs the IAM CRDs applied before iam is enabled; without them vws-iam cannot open its store.
- **NetworkPolicy**: `allowFrom: [console-api]`. Only console-api pods of the same release reach the iam port. The policy port is the integer service port (`9095`), with no `endPort` and no protocol other than TCP. A named port such as `http` is rejected, because kube-ovn does not resolve it. `check_render.py` fails if the policy is missing, lets another service in, uses any port other than that integer, sets `endPort`, or sets a non-TCP protocol.
- **mTLS**: not added, because no other service uses it yet.

## Rollout order for vws-iam#168 (FIDO security keys, #603)

vws-iam#168 (merge `caf768e`) adds the kind `IamFidoDevice`. `DeleteUser` lists it on every call, so an image with #168 fails `DeleteUser` on the kube store when the CRD or the grant is missing.

1. **CRD and Role first.** `charts/vws/crds/iam.yaml` has the `iamfidodevices.iam.vws.popododo0720.io` CRD, and `services.iam.rbac` grants get, list, create, update and delete on `iamfidodevices` (vws-deploy#633). Both equal vws-iam main `deploy/crds/iam.yaml` and `deploy/rbac/role.yaml` at `caf768e`. `tests/vws/render.test.sh` ("iam sync") fails if the CRD is missing, the Role names other kinds than the CRDs, or the verbs change. On an existing cluster, run `vws apply` or `vws upgrade` with this chart and check `kubectl get crd iamfidodevices.iam.vws.popododo0720.io` reports Established before the next step. The pinned `sha-74a29dc` does not use the kind, so this step changes nothing for it.
2. **Then the iam image with #168.** Move `services.iam.imageTag` to a published vws-iam build at or after `caf768e` in a later release, or in the same release as step 1 (`vws apply` and `vws upgrade` apply the CRDs before the helm upgrade, and Helm applies the Role before the Deployment). Never move the pin on a cluster that has not had step 1.
3. **Client note: FIDO registration changes the user's etag.** `RegisterFido` commits by updating the user (`FidoRegistrations`, vws-iam `internal/mfa/fido.go` `claimUser`), so the user's store version and etag change. A client that sends the old etag on a later user write gets `ABORTED` / `ETAG_MISMATCH` (AWS `ConcurrentModification`) and must read the user again and retry. A `DeleteUser` that races a registration fails the same way, and its retry is `DeleteConflict` while the user has a key. No RPC reaches `RegisterFido` yet (vws-iam#133), so clients see this only once the FIDO RPCs ship.

## What is missing and why

- **IAM CRDs on an existing release**: `vws apply` and `vws upgrade` apply every `charts/vws/crds/*.yaml` with server-side apply (`scripts/vws.d/lib/applyrun.sh`) and wait for the CRDs to be Established before the workload upgrade, so a cluster installed before #369 gains `iam.yaml` on its next apply or upgrade (#382).
- **Bootstrap (root/admin user, importing the existing `vws-console-api-credentials` keys, the `vws-initial-admin` password)**: it needs the vws-iam API (vws-iam#3). Plan: after helm, once vws-iam is Ready, `vws apply` runs a one-shot Job inside the cluster, passing secrets only as Secret references. If everything already exists it does nothing.
- **Connecting console-api**: the chart sets `-iam-endpoint=iam:9095` when `flags.iam` is `grpc`. The three `0.0.0.0` addr flags publish Service ports 8081, 8082 and 8084. Live AWS CLI calls are outside this repo (it does not apply to a cluster).

## apply and check_render

Before touching helm or Secrets, `vws apply` runs `helm template` and `check_render.py` on the rendered values (#246). With `spec.services.iam.enabled: true`, the chart must have Deployment `iam` to pass the gate. The test harness's fake helm uses `helm/template.json` pre-rendered from the scenario CR, so a test that turns iam on through the CR must call `installer_template` again (`tests/vws/installer_lib.sh`).

## Needs checking

- Startup of `sha-74a29dc` on vws-test is not checked here. The anonymous manifest pull of that tag returned HTTP 200 on 2026-10-16. This repo does not apply the chart to a cluster.
- Whether NetworkPolicy on Kube-OVN blocks kubelet probes.
- When `iam` is turned on through the CR, the service list in `vws status` follows the chart default (`enabled: false`). The image pin does not change that. The lists match only after the chart default becomes `true`.
