# Service configuration (flags) and namespaces

The chart passes service configuration through one structure in `charts/vws/values.yaml`. It uses flags only, no environment variables. The design is vws-deploy#30 and #32. The values schema (`values.schema.json`) enforces the format.

## Values structure

```yaml
global:
  pendingFlags: false        # when true, pendingFlags are rendered too
namespaces:                  # the only place namespace names live
  vms: vws-vms
  images: vws-images
  nad: vws-nad
services:
  storage:
    flags:                   # rendered as -name=value arguments, sorted by key
      block-backend: kube
      kube-namespace: {namespace: vms}   # points at namespaces.vms
    rbac:
    - namespace: vms         # points at the same key
      ...
```

- A flag value is a string, a boolean, an integer or `{namespace: <key>}`. `release` is a reserved key meaning the release namespace.
- The `namespace` of an `rbac` rule is the same kind of key. If the key is not in `namespaces`, the render fails.
- `pendingFlags` are flags the service image does not accept yet. They are added to `flags` only when `global.pendingFlags: true`.
- The chart creates the namespaces used by rules (except the release one) and annotates them with `helm.sh/resource-policy: keep`.

## Keys and flags per service

The evidence comes from reading `cmd/*/main.go` on origin/main of each service repository (compute `df28dbd`, storage `77450d5`, network `890dbaf`). Line numbers refer to those commits.

| Service | values key (`services.<name>.flags`) | Argument | Default | Code |
| --- | --- | --- | --- | --- |
| storage | `block-backend: kube` | `-block-backend=kube` | `fake` is the code default | `cmd/vws-storage/main.go:44` |
| storage | `kube-namespace: {namespace: vms}` | `-kube-namespace=<vms>` | shared by PVCs and snapshots | `main.go:47` |
| storage | `kube-class-standard`, `kube-class-ssd`, `kube-snapshot-class` | same names | all `ceph-block` | `main.go:48-50` |
| storage | `db: /var/lib/vws/storage.db` | `-db=<path>` | empty means in-memory SQLite, which loses state on restart (#85). `scripts/check_render.py` requires it | vws-storage README `-db` |
| storage | (not passed) `kube-config` | `-kube-config` | empty means in-cluster credentials | `main.go:46` |
| compute | `namespace: {namespace: release}` | `-namespace=<release namespace>` | ConfigMap store. Empty means an in-memory store | `cmd/vws-compute/main.go:34` |
| compute | `hypervisor: kubevirt` | `-hypervisor=kubevirt` | code default `fake` (creates no VMs, #121) | vws-compute `62d90e7` `cmd/vws-compute/config.go` |
| compute | `vm-namespace: {namespace: vms}` | `-vm-namespace=<vms>` | `vws-vms` | same |
| compute | `image-namespace: {namespace: images}` | `-image-namespace=<images>` | `vws-images` | same |
| compute | `storage: storage:7400` | `-storage=storage:7400` | empty makes CreateInstance return DEPENDENCY_MISSING (#121) | same |
| all three services | `listen: 0.0.0.0:<port>` | `-listen=0.0.0.0:<port>` | code default is `127.0.0.1`. Must equal the `port` key | compute `main.go:33`, storage `main.go:40`, network `main.go:22` |
| console-api | `backend: grpc`, `console-addr: 0.0.0.0:8080`, `compute-addr`/`storage-addr`/`network-addr: <service>:<port>` | same names | code defaults are `fake` and `127.0.0.1` (#84). A `0.0.0.0:<port>` value of an `-addr` flag other than the primary port is a Service port; the name is the flag with `-addr` removed. `compute:50051` and the primary `0.0.0.0:8080` stay flags. `127.0.0.1:<port>` fails the render. No credential flags are passed either, so development defaults apply (needs checking, #84). `scripts/check_render.py` (R15) enforces `backend: grpc`, `console-addr: 0.0.0.0:<port>` and the three backend addresses = `<service>:<that service's port>` | vws-console-api `cmd/vws-console-api/main.go:40-50` |
| network | `db: /var/lib/vws/network.db`, `pool: <CIDR>` | `-db`, `-pool` | both required. `pool` has no default (#148) | `main.go:23-24,33-34` |
| network | (no default) `kubeconfig` and `kubeconfigSecret` | `-kubeconfig=<path>`, Secret volume | when set, the Kube-OVN data plane; otherwise fake | `cmd/vws-network/main.go:25` |
| network | `kube-external-subnet` | `-kube-external-subnet=<name>` | required together with `-kubeconfig` | `main.go:26` |
| network | `kube-nad-namespace: {namespace: nad}` | `-kube-nad-namespace=<nad>` | with only `-kubeconfig` it is `vws-system`, so the chart always passes it | `main.go:27,30-31` |
| network | `kube-direct-networks`, `kube-direct-nad-namespace: {namespace: vms}` | `-kube-direct-networks=<JSON>`, `-kube-direct-nad-namespace=<vms>` | no default. `vws render` writes them only when `spec.network.direct` is set (#131, `docs/node-network.md`) | vws-network `8ff82a7` `main.go:40-41` |
| network | `kube-vm-namespace: {namespace: vms}` | `-kube-vm-namespace=<vms>` | code default is empty. When an address attaches to an instance, it creates the OvnFip with that VMI's interface IP. The chart always passes it and adds a VMI get/list/watch Role in the same namespace | vws-network `ac269d0` (#87) `main.go:42` |
| network | `kube-snat` | `-kube-snat=true` | off. Needs an external subnet | `main.go:28` |
| iam | `enabled: false`, `replicas: 1`, `imageTag: sha-b567471`, `listen: 0.0.0.0:9095`, `kekSecret`, `allowFrom` | `-listen=…`, env `VWS_IAM_KEK`/`VWS_IAM_KEK_ID` (secretKeyRef), NetworkPolicy on numeric TCP port 9095 | left out of the default render. `imageTag` is `sha-b567471` (digest `sha256:ea481d637e103b7426e7c9b12b8ea1e9291ab1de65ecfe6ce746f8c6660b231d`, vws-iam#112). An anonymous manifest request returned HTTP 200 on 2026-10-15 (`docs/iam-install.md`). One replica until a shared store (vws-iam#110). One replica does not survive a restart. The binary's `-listen` default is `127.0.0.1:9095`. `-health-listen` stays `127.0.0.1:9096` and is not passed | vws-iam `-listen` / `-health-listen` |
| network | `vm-mtu` | `-vm-mtu=<MTU>` | code default 9001, 68–9001. `vws render` always writes it from `spec.network.vmMTU` (#236, `docs/vm-mtu.md`). It becomes the VPC Subnet's `spec.mtu` and the DHCP `mtu` option | vws-network main (#93, #94) `main.go:28` |

- The schema requires `kube-external-subnet` and `kube-nad-namespace` when `kubeconfig` is set, and `kube-external-subnet` when `kube-snat` is set (vws-deploy#30). Rendering with a missing value is refused.
- network does not start without `-db` and `-pool` (`main.go:33-34`). The default values pass only `db: /var/lib/vws/network.db` (inside the state volume mount path; the root filesystem is read-only, so only there is writable). `pool` (the elastic IP range) has no default and is given per site in the CR's `spec.services.network.flags.pool`. When it is missing or an RFC 5737 documentation range, `vws render` refuses it with exit code 2 (#148). The repo checks use the test range in `charts/vws/ci/site.yaml`. To turn on the Kube-OVN data plane, set `kubeconfig`, `kube-external-subnet` and `kube-nad-namespace` along with `kubeconfigSecret`, the name of the Secret holding the kubeconfig. The chart mounts that Secret read-only in the directory of the `flags.kubeconfig` path ("network kubeconfig" below). `scripts/guard.sh config` tests this shape.
- compute's `-nad-namespace` (`VWS_NAD_NAMESPACE`, default `vws-nad`) is only reserved; no code uses NADs, so it is not passed.

## Ports and probes (vws-deploy#39)

- A service's `-listen` default is `127.0.0.1`, which is unreachable from outside the pod (compute `main.go:33`, storage `main.go:40`, network `main.go:22`). All three services pass `flags.listen: 0.0.0.0:<port>` in values. The default ports equal the service code's default ports (compute 50051, storage 7400, network 8080).
- A service's single `port` key is the reference for the container port, the probe port and the `-listen` port. If the three differ, `scripts/guard.sh config` (R15) fails.
- Probes are not HTTP. The services' non-vendor Go code has no `healthz`, `readyz` or `grpc_health` (as of the three commits above; this is only evidence of absence). The default is `tcpSocket`. This check only guarantees the port is open, not dependencies such as DB or cluster access.
- **Switching to gRPC probes**: once the service image implements `grpc.health.v1`, change only the one line `global.probe: grpc`. The render switches to a `grpc: {port: <port>}` probe under the same port check. Turning it on before the implementation makes the probe fail. The implementation has been requested from the services separately.
- **storage gRPC health (vws-storage#39, #69)**: `services.storage.grpcReadinessService: vws.storage.v1.Ready` (`ReadyService` in `internal/health/health.go:22`). A service with this key always gets gRPC probes (#110; the gate was removed after vws-storage#39 merged): startup and liveness use the empty service name (they look only at the process and do not restart on cluster failures), readiness uses `vws.storage.v1.Ready` (engine, store and kube checks). storage `grpc.health.v1` was confirmed SERVING on vws-test (#51). compute and network stay on tcpSocket. `-listen` is already `0.0.0.0:7400` (the #39 default `127.0.0.1:7400` is unreachable for probes, `cmd/vws-storage/main.go:42`). `-health-period` (default 10s) and `-health-timeout` (default 3s, `:57-58`) use their defaults and are not passed. A real kubelet gRPC probe passing needs checking on T1.
- **Measured (vws-test, #89).** Checked with images built from main. console and console-api :8080 return 404 for `/healthz`, `/readyz` and `/metrics`. compute :50051 and network :8080 return Unimplemented for `grpc.health.v1`; storage :7400 returns `SERVING`. No service has `/metrics`. So `global.probe: grpc` cannot be turned on yet (the compute and network probes would fail), and 7 pods were Ready with tcpSocket probes.
- Needs checking: whether probes pass on real pods (T1 or above), whether ports opened on `0.0.0.0` need a NetworkPolicy, and whether the same `8080` tcp check is enough for console and console-api (this PR did not read the console repo).

## network kubeconfig (vws-deploy#40, stopgap)

vws-network uses the Kube-OVN data plane only with `-kubeconfig` (`main.go:25`). The chart supports **only option 1 (a Secret volume), as a stopgap**.

- The one new values key is `services.network.kubeconfigSecret` (the name of an existing Secret). The mount path is the directory of the `flags.kubeconfig` file path, and the Secret's key name is that file name. Each requires the other (schema `if/then`). The existing rule (`kubeconfig` requires `kube-external-subnet` and `kube-nad-namespace`) stays.
- The chart creates no Secret and the values files hold no credentials. The operator creates it in the release namespace with `kubectl create secret generic <name> --from-file=<key>=<kubeconfig file>`.
- Render result: the Secret volume is mounted read-only in the directory of `flags.kubeconfig`, projecting a single key with file mode `0440` (the pod has `runAsNonRoot` and `fsGroup 65532`). No credentials are passed through environment variables. `scripts/guard.sh config` (R16) checks this. Paths inside `/tmp` or `/var/lib/vws` are refused.
- **The goal is option 2 (the in-cluster ServiceAccount).** That needs vws-network to use Kube-OVN without `-kubeconfig`, which is a separate request. Then this key and volume are removed. Option 1 is a stopgap with known limits: a long-lived credential stays in a Secret, rotation is manual, and anyone who can read Secrets or exec into the pod gets the credential.
- Needs checking: whether this kubeconfig writes Kube-OVN CRDs on a real cluster, how to keep that kubeconfig from having more than the network rights in `docs/rbac.md` (the chart cannot control it), and whether the pod can read the file with `0440` and `fsGroup` (T1 or above).

## storage RGW object backend (vws-deploy#90)

vws-storage implements the S3 object backend. It has the `-object-backend=s3`, `-s3-profile`, `-s3-endpoint` and `-s3-region` flags (`cmd/vws-storage/main.go:53-56`) and code that reads the keys from the `VWS_S3_ACCESS_KEY` and `VWS_S3_SECRET_KEY` environment variables (`main.go:189-205`, `internal/backend/s3/s3.go:75-81`). vws-console-api is only the S3 REST surface and hands objects to storage's gRPC `ObjectService` (`internal/backend/grpcclient/client.go:54-55`).

- The values keys are `services.storage.s3.rgw.{enabled, endpoint, bucketPrefix, existingSecret}`. Off by default, `existingSecret: ceph-rgw-s3`.
- When on, the chart passes `-object-backend=s3 -s3-profile=rgw -s3-endpoint=<endpoint>`, and the keys go in only as `secretKeyRef` env from `access-key` and `secret-key` of `existingSecret`. The keys are in neither arguments nor values. This is the only exception to the "flags only, no env" principle (the service accepts keys only through env, `main.go:189-190`).
- `secretKeyRef` points only at a Secret in the pod's namespace. The operator creates it in the release namespace (default `vws-system`) with `kubectl -n vws-system create secret generic ceph-rgw-s3 --from-literal=access-key=... --from-literal=secret-key=...`. The chart creates no Secret.
- `bucketPrefix` is lowercase letters, digits and `-`, starts with an alphanumeric, and is at most 18 characters (the bucket name `<prefix>-vws-<40 hex>` must fit in 63 characters, or storage does not start, vws-storage#46, #103). The schema refuses anything else.
- A non-empty `bucketPrefix` renders as `-s3-bucket-prefix=<prefix>` (vws-storage#46, #105). The RGW bucket name is `<prefix>-vws-<40 hex>`; the prefix is not added to API bucket names. Changing the prefix hides the earlier buckets from VWS.
- With `enabled: true` and an empty `endpoint` or `existingSecret`, the render fails, and `scripts/guard.sh config` (R16) tests that an env with a literal value or pointing at another Secret fails.

## console-api credentials and the console connection

With `-backend=grpc`, vws-console-api does not start unless its three credentials (`VWS_CONSOLE_TOKEN`, `VWS_ACCESS_KEY`, `VWS_SECRET_KEY`, or each `_FILE` path) are given (`cmd/vws-console-api/main.go` `requireExplicit`, console-api #80). The read order is flag, `_FILE`, env, and the `-console-token`, `-access-key` and `-secret` flags are deprecated (S1). The console surface authenticates with `Authorization: Bearer <console-token>`, and the EC2/S3 surfaces with SigV4 (`access-key`/`secret`).

- `services.console-api.apiCredentials.existingSecret` (default `vws-console-api-credentials`): the chart puts keys `console-token`, `access-key` and `secret-key` only into the `VWS_CONSOLE_TOKEN`, `VWS_ACCESS_KEY` and `VWS_SECRET_KEY` env through `secretKeyRef`. No credential flags are in the arguments (no `$(VAR)` references either). The values appear nowhere in values, arguments, the pod spec or process arguments. `scripts/check_render.py` R16 fails when a credential flag is in the arguments. A console-api build that reads env is required.
- `services.console.apiClient.service` (default `console-api`): sets `VWS_CONSOLE_API_URL=http://<service>.<release namespace>.svc:<that service's port>` and passes the same Secret's `console-token` as `VWS_CONSOLE_API_TOKEN`. The console gets no SigV4 keys.
- `services.console.portEnv` (default `PORT`): the console image (a Next.js server) reads its listen port from env `PORT`. The chart puts the service's `port` (default 8080) in that env, so the containerPort, probes and Service all come from one value. Without it the image listens on its default 3000 and the probes and Service cannot reach it. `scripts/check_render.py` (R15) checks that the value equals the containerPort, and that a render with a different value fails.
- The chart does not render the Secret (R16). Before helm, `vws apply` generates the values with `scripts/vws.d/lib/apisecret.py` (Python `secrets`, os.urandom) and passes them only through the stdin of kubectl create. If it already exists (AlreadyExists) it is left alone and not rotated, and no right to read Secrets is needed. Any other failure stops with 3 before helm. `--dry-run` generates no values. To rotate, delete the Secret, run `vws apply` again, then restart both Deployments.
- `scripts/guard.sh config` (R16) tests that an env that is a literal, points at another Secret or key, or a URL that differs from values fails.

## Needs checking

- **compute's KubeVirt flags are in the default render (#121).** vws-compute main (`62d90e7`) accepts `-hypervisor`, `-vm-namespace`, `-image-namespace` and `-storage`. They used to be `pendingFlags`, but no service uses `pendingFlags` now. Volume boot was confirmed with these four flags on vws-test T1 (#51).
- This chart does not use compute's environment variable fallbacks (`VWS_VM_NAMESPACE` and others). It passes flags only.
- The services were not started on a real cluster with the rendered arguments (static render checks only).

## Why namespaces are kept

Namespaces the chart creates are annotated with `helm.sh/resource-policy: keep`. If `helm uninstall` deleted namespaces holding VMs, disks (PVCs) and snapshots along with the release, data would be lost (data protection). The cost is that a person must clean up. To remove everything, check the data and then run `kubectl delete namespace <name>` yourself. The chart does not create the release namespace (`release`).

## Renaming namespaces

Change only `namespaces` in `values.yaml`. Templates, Roles and flags all follow that value. `scripts/guard.sh config` renders with renamed namespaces and checks that the arguments and Roles change together and that no old name remains.
