# Installing vws-iam (#243)

The design is vws-iam#1 and the contract is vws-proto#24 (`vws/iam/v1`, branch `feat/iam-v1`).

## What is in now

- **Chart service `iam`**: `enabled: false`. A pullable `ghcr.io/popododo0720/vws/iam` tag is not proven, so it is left out of the default render (render-only). Turn it on with CR `spec.services.iam.enabled: true` or values `services.iam.enabled=true`. Port 9095, `-listen=0.0.0.0:9095`. The vws-iam binary's `-listen` default is `127.0.0.1:9095`; `-health-listen` stays `127.0.0.1:9096` and is not passed. console-api connects with CR flags `iam=grpc` plus the three `0.0.0.0` addr flags (`ec2-addr`, `s3-addr`, `iam-addr`), and the chart sets `-iam-endpoint=iam:9095`.
- **`enabled`**: available on every service. With `false` the chart creates none of that service's objects, and `check_render.py`, `vws status`, the profile resource calculation and the Secret step of `vws apply` skip it too.
- **KEK**: `kekSecret: vws-iam-kek`. `vws apply` creates the Secret in the release namespace only when it does not exist (`kek`: base64 of 32 random bytes, `key-id`: `kek-1`). The value goes only through kubectl stdin and never appears in arguments, output or logs. An existing Secret is left alone (no silent rotation). The pod gets `VWS_IAM_KEK` and `VWS_IAM_KEK_ID` only through secretKeyRef. `check_render.py` fails if the KEK is in args.
- **NetworkPolicy**: `allowFrom: [console-api]`. Only console-api pods of the same release reach the iam port. `check_render.py` fails if the policy is missing or lets another service in.
- **mTLS**: not added, because no other service uses it yet.

## What is missing and why

- **IAM CRDs (IamUser and others) and RBAC**: R18 allows a single VWS CRD, `VWSCluster`, and the API group in one place only. The IAM CRD schema and group are decided by vws-iam#2 (skeleton, CRDs, kube store). At that point the R18 exception (or a way for vws-iam to own the CRDs) and a minimal Role in namespace `vws-iam` go in together.
- **Bootstrap (root/admin user, importing the existing `vws-console-api-credentials` keys, the `vws-initial-admin` password)**: it needs the vws-iam API (vws-iam#3). Plan: after helm, once vws-iam is Ready, `vws apply` runs a one-shot Job inside the cluster, passing secrets only as Secret references. If everything already exists it does nothing.
- **Connecting console-api**: the chart sets `-iam-endpoint=iam:9095` when `flags.iam` is `grpc`. The three `0.0.0.0` addr flags publish Service ports 8081, 8082 and 8084. Live AWS CLI calls are outside this repo (it does not apply to a cluster).

## apply and check_render

Before touching helm or Secrets, `vws apply` runs `helm template` and `check_render.py` on the rendered values (#246). With `spec.services.iam.enabled: true`, the chart must have Deployment `iam` to pass the gate. The test harness's fake helm uses `helm/template.json` pre-rendered from the scenario CR, so a test that turns iam on through the CR must call `installer_template` again (`tests/vws/installer_lib.sh`).

## Needs checking

- No evidence of an install on vws-test (a pullable image tag is not proven).
- Whether NetworkPolicy on Kube-OVN blocks kubelet probes.
- When `iam` is turned on through the CR, the service list in `vws status` follows the chart default (`enabled: false`). The difference goes away once an image exists and the default becomes `true`.
