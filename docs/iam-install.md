# Installing vws-iam (#243)

The design is vws-iam#1 and the contract is vws-proto#24 (`vws/iam/v1`, branch `feat/iam-v1`).

## What is in now

- **Chart service `iam`**: `enabled: false`, so the default render still leaves it out. The image is pinned with `services.iam.imageTag: sha-74a29dc` to `ghcr.io/popododo0720/vws/iam:sha-74a29dc` (digest `sha256:6c301b75fd46584fd4d6c86f77be9d4389e0492a210d34bc31ab5a34fb9acc5d`). vws-iam#113 published that tag. The image binary contains go1.26.9. An anonymous manifest request returned HTTP 200 and that digest on 2026-10-16. `spec.version` does not move it. Port 9095, `-listen=0.0.0.0:9095`. The vws-iam binary's `-listen` default is `127.0.0.1:9095`; `-health-listen` stays `127.0.0.1:9096` and is not passed. console-api connects with CR flags `iam=grpc` plus the three `0.0.0.0` addr flags (`ec2-addr`, `s3-addr`, `iam-addr`), and the chart sets `-iam-endpoint=iam:9095`.
- **`enabled`**: available on every service. With `false` the chart creates none of that service's objects, and `check_render.py`, `vws status`, the profile resource calculation and the Secret step of `vws apply` skip it too.
- **KEK**: `kekSecret: vws-iam-kek`. `vws apply` creates the Secret in the release namespace only when it does not exist (`kek`: base64 of 32 random bytes, `key-id`: `kek-1`). The value goes only through kubectl stdin and never appears in arguments, output or logs. An existing Secret is left alone (no silent rotation). The pod gets `VWS_IAM_KEK` and `VWS_IAM_KEK_ID` only through secretKeyRef. `check_render.py` fails if the KEK is in args.
- **State and replicas (#369)**: `replicas: 2`. The chart passes `-namespace=<release namespace>`, so every replica reads and writes the vws-iam kube store: the custom resources of `charts/vws/crds/iam.yaml` in the release namespace, through an uncached client with resourceVersion compare-and-swap. A write on one replica is read on the other, and a restart rereads it. No volume holds state (the only volume is the `/tmp` scratch dir). The pinned image `sha-74a29dc` already has this store behind `-namespace`; vws-iam#110 only stops a pod from falling back to memory when the namespace is missing. Like any stateless service, the schema and `check_render.py` require at least 2 replicas.
- **CRDs and RBAC (#369)**: `charts/vws/crds/iam.yaml` is a byte copy of vws-iam `deploy/crds/iam.yaml` (11 Namespaced CRDs, group `iam.vws.popododo0720.io`). `services.iam.rbac` is one rule in the release namespace on exactly those 11 resources with get, list, create, update and delete, the same as vws-iam `deploy/rbac/role.yaml`; it also mounts the API token. `scripts/guard.sh config` checks the render. Helm installs `crds/` only on the first install, so an existing release needs the IAM CRDs applied before iam is enabled; without them vws-iam cannot open its store.
- **NetworkPolicy**: `allowFrom: [console-api]`. Only console-api pods of the same release reach the iam port. The policy port is the integer service port (`9095`), with no `endPort` and no protocol other than TCP. A named port such as `http` is rejected, because kube-ovn does not resolve it. `check_render.py` fails if the policy is missing, lets another service in, uses any port other than that integer, sets `endPort`, or sets a non-TCP protocol.
- **mTLS**: not added, because no other service uses it yet.

## What is missing and why

- **IAM CRDs on an existing release**: `vws apply` server-side applies only `vwsclusters.yaml` (`scripts/vws.d/lib/applyrun.sh`), so it does not yet add `iam.yaml` to a cluster installed before #369.
- **Bootstrap (root/admin user, importing the existing `vws-console-api-credentials` keys, the `vws-initial-admin` password)**: it needs the vws-iam API (vws-iam#3). Plan: after helm, once vws-iam is Ready, `vws apply` runs a one-shot Job inside the cluster, passing secrets only as Secret references. If everything already exists it does nothing.
- **Connecting console-api**: the chart sets `-iam-endpoint=iam:9095` when `flags.iam` is `grpc`. The three `0.0.0.0` addr flags publish Service ports 8081, 8082 and 8084. Live AWS CLI calls are outside this repo (it does not apply to a cluster).

## apply and check_render

Before touching helm or Secrets, `vws apply` runs `helm template` and `check_render.py` on the rendered values (#246). With `spec.services.iam.enabled: true`, the chart must have Deployment `iam` to pass the gate. The test harness's fake helm uses `helm/template.json` pre-rendered from the scenario CR, so a test that turns iam on through the CR must call `installer_template` again (`tests/vws/installer_lib.sh`).

## Needs checking

- Startup of `sha-74a29dc` on vws-test is not checked here. The anonymous manifest pull of that tag returned HTTP 200 on 2026-10-16. This repo does not apply the chart to a cluster.
- Whether NetworkPolicy on Kube-OVN blocks kubelet probes.
- When `iam` is turned on through the CR, the service list in `vws status` follows the chart default (`enabled: false`). The image pin does not change that. The lists match only after the chart default becomes `true`.
