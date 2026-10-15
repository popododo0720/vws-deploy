# Installing vws-iam (#243)

The design is vws-iam#1 and the contract is vws-proto#24 (`vws/iam/v1`, branch `feat/iam-v1`).

## What is in now

- **Chart service `iam`**: `enabled: false`. The vws-iam repo has no code or image yet, so it is left out of the default render (render-only). Turn it on with CR `spec.services.iam.enabled: true` or values `services.iam.enabled=true`. Port 7500, `-listen=0.0.0.0:7500`.
- **`enabled`**: available on every service. With `false` the chart creates none of that service's objects, and `check_render.py`, `vws status`, the profile resource calculation and the Secret step of `vws apply` skip it too.
- **KEK**: `kekSecret: vws-iam-kek`. `vws apply` creates the Secret in the release namespace only when it does not exist (`kek`: base64 of 32 random bytes, `key-id`: `kek-1`). The value goes only through kubectl stdin and never appears in arguments, output or logs. An existing Secret is left alone (no silent rotation). The pod gets `VWS_IAM_KEK` and `VWS_IAM_KEK_ID` only through secretKeyRef. `check_render.py` fails if the KEK is in args.
- **NetworkPolicy**: `allowFrom: [console-api]`. Only console-api pods of the same release reach the iam port. `check_render.py` fails if the policy is missing or lets another service in.
- **mTLS**: not added, because no other service uses it yet.

## What is missing and why

- **IAM CRDs (IamUser and others) and RBAC**: R18 allows a single VWS CRD, `VWSCluster`, and the API group in one place only. The IAM CRD schema and group are decided by vws-iam#2 (skeleton, CRDs, kube store). At that point the R18 exception (or a way for vws-iam to own the CRDs) and a minimal Role in namespace `vws-iam` go in together.
- **Bootstrap (root/admin user, importing the existing `vws-console-api-credentials` keys, the `vws-initial-admin` password)**: it needs the vws-iam API (vws-iam#3). Plan: after helm, once vws-iam is Ready, `vws apply` runs a one-shot Job inside the cluster, passing secrets only as Secret references. If everything already exists it does nothing.
- **Connecting console-api**: not in #243's scope (vws-console-api#121).

## apply and check_render

Before touching helm or Secrets, `vws apply` runs `helm template` and `check_render.py` on the rendered values (#246). With `spec.services.iam.enabled: true`, the chart must have Deployment `iam` to pass the gate. The test harness's fake helm uses `helm/template.json` pre-rendered from the scenario CR, so a test that turns iam on through the CR must call `installer_template` again (`tests/vws/installer_lib.sh`).

## Needs checking

- No evidence of an install on vws-test (no image).
- Whether NetworkPolicy on Kube-OVN blocks kubelet probes.
- When `iam` is turned on through the CR, the service list in `vws status` follows the chart default (`enabled: false`). The difference goes away once an image exists and the default becomes `true`.
