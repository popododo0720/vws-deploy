# Service configuration (flags) and namespaces

The chart passes service configuration through one structure in `charts/vws/values.yaml`. It uses flags, plus the listener env keys (`portEnv`, `healthPort`) and Secret references. The design is vws-deploy#30 and #32. The values schema (`values.schema.json`) enforces the format.

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
- `repeatedFlags` maps a flag name to a list of strings: the flag is given once per item, in list order, after `flags`, such as vws-storage `-kube-volume-type` (#391). An item has no whitespace, a list is not empty and has no duplicates, and a name must not also be in `flags`.
- The chart creates the namespaces used by rules (except the release one) and annotates them with `helm.sh/resource-policy: keep`.

## Keys and flags per service

The evidence comes from reading `cmd/*/main.go` on origin/main of each service repository (compute `df28dbd`, storage `77450d5`, network `890dbaf`). Line numbers refer to those commits.

| Service | values key | Argument or env | Default | Code |
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
| iam | `enabled: false`, `replicas: 2`, `imageTag: sha-74a29dc`, `listen: 0.0.0.0:9095`, `namespace: {namespace: release}`, `kekSecret`, `allowFrom`, `rbac` | `-listen=…`, `-namespace=<release namespace>` (the kube store, #369), env `VWS_IAM_KEK`/`VWS_IAM_KEK_ID` (secretKeyRef), NetworkPolicy on numeric TCP port 9095 | left out of the default render. `imageTag` is `sha-74a29dc` (digest `sha256:6c301b75fd46584fd4d6c86f77be9d4389e0492a210d34bc31ab5a34fb9acc5d`, vws-iam#113, go1.26.9). An anonymous manifest request returned HTTP 200 on 2026-10-16 (`docs/iam-install.md`). Two replicas share the kube store in the release namespace (#369, `docs/iam-install.md`). The binary's `-listen` default is `127.0.0.1:9095`. `-health-listen` stays `127.0.0.1:9096` and is not passed | vws-iam `-listen` / `-health-listen` / `-namespace` |
| compute | `healthPort: 8081` | env `VWS_HEALTH_ADDR=0.0.0.0:8081` (the default of `-http`), plus `-listen=0.0.0.0:50051` | unset serves no health listener. `-http` itself is not passed | vws-compute `cmd/vws-compute/config.go`; vws-lib `obs.Setup` (vws-compute#156) |
| network | `vm-mtu` | `-vm-mtu=<MTU>` | code default 9001, 68–9001. `vws render` always writes it from `spec.network.vmMTU` (#236, `docs/vm-mtu.md`). It becomes the VPC Subnet's `spec.mtu` and the DHCP `mtu` option | vws-network main (#93, #94) `main.go:28` |

- The schema requires `kube-external-subnet` and `kube-nad-namespace` when `kubeconfig` is set, and `kube-external-subnet` when `kube-snat` is set (vws-deploy#30). Rendering with a missing value is refused.
- network does not start without `-db` and `-pool` (`main.go:33-34`). The default values pass only `db: /var/lib/vws/network.db` (inside the state volume mount path; the root filesystem is read-only, so only there is writable). `pool` (the elastic IP range) has no default and is given per site in the CR's `spec.services.network.flags.pool`. When it is missing or an RFC 5737 documentation range, `vws render` refuses it with exit code 2 (#148). The repo checks use the test range in `charts/vws/ci/site.yaml`. To turn on the Kube-OVN data plane, set `kubeconfig`, `kube-external-subnet` and `kube-nad-namespace` along with `kubeconfigSecret`, the name of the Secret holding the kubeconfig. The chart mounts that Secret read-only in the directory of the `flags.kubeconfig` path ("network kubeconfig" below). `scripts/guard.sh config` tests this shape.
- compute's `-nad-namespace` (`VWS_NAD_NAMESPACE`, default `vws-nad`) is only reserved; no code uses NADs, so it is not passed.

## Ports and probes (vws-deploy#39)

- A service's `-listen` default is `127.0.0.1`, which is unreachable from outside the pod (compute `main.go:33`, storage `main.go:40`, network `main.go:22`). All three services pass `flags.listen: 0.0.0.0:<port>` in values. The default ports equal the service code's default ports (compute 50051, storage 7400, network 8080).
- A service's single `port` key is the reference for the `http` container and Service port and the `-listen` port. A service with `healthPort` gets a second, HTTP listener on that port; its probe port and `metrics` Service port are `healthPort`, which must differ from `port`. `scripts/guard.sh config` (R15) fails when a rendered port or probe differs from the values.
- Probes are not HTTP, except a service with `healthPort` or `httpProbes`. The services' non-vendor Go code has no `healthz`, `readyz` or `grpc_health` (as of the three commits above; this is only evidence of absence), except compute, whose health listener serves `/healthz` and `/readyz` (#212; vws-compute#156 removed `/metrics`). Every other service defaults to `tcpSocket`. This check only guarantees the port is open, not dependencies such as DB or cluster access. For compute the HTTP check goes further: `/readyz` follows the same readiness verdict as `grpc.health.v1`.
- **Switching to gRPC probes**: once the service image implements `grpc.health.v1`, change only the one line `global.probe: grpc`. The render switches to a `grpc: {port: <port>}` probe under the same port check. Turning it on before the implementation makes the probe fail. The implementation has been requested from the services separately.
- **storage gRPC health (vws-storage#39, #69)**: `services.storage.grpcReadinessService: vws.storage.v1.Ready` (`ReadyService` in `internal/health/health.go:22`). A service with this key always gets gRPC probes (#110; the gate was removed after vws-storage#39 merged): startup and liveness use the empty service name (they look only at the process and do not restart on cluster failures), readiness uses `vws.storage.v1.Ready` (engine, store and kube checks). storage `grpc.health.v1` was confirmed SERVING on vws-test (#51). compute now uses its health listener and network stays on tcpSocket. `-listen` is already `0.0.0.0:7400` (the #39 default `127.0.0.1:7400` is unreachable for probes, `cmd/vws-storage/main.go:42`). `-health-period` (default 10s) and `-health-timeout` (default 3s, `:57-58`) use their defaults and are not passed. A real kubelet gRPC probe passing needs checking on T1.
- **console probes (vws-deploy#612).** `services.console.httpProbes: true` renders httpGet probes on the console's own `http` port (the `PORT` the Next.js server listens on): `/healthz` for startup and liveness, `/readyz` for readiness, with the same periods, timeouts and thresholds as every other service (`vws.probes`). vws-console#117 (c668c6f) added these routes and `GET /metrics` (Prometheus, `vws_console_` prefix) on that same port, so the chart pins `services.console.imageTag: sha-c668c6f`; an older image answers 404 on `/healthz`. `httpProbes` and `healthPort` are exclusive (R15). Restricting who may scrape `/metrics` is a separate step of #612.
- **compute health listener (vws-deploy#212).** `services.compute.healthPort: 8081` renders env `VWS_HEALTH_ADDR=0.0.0.0:8081`, container and `metrics` Service ports on 8081, and httpGet probes (`/healthz` for startup and liveness, `/readyz` for readiness). vws-compute#156 (ff1252d) replaced the local `internal/obs` (which still served `/metrics` in the pinned image `sha-1b62a87`) with vws-lib `obs.Setup`; it serves only `/healthz` and `/readyz` and sends metrics over OTLP, so the chart renders no `prometheus.io` scrape annotations. `/readyz` follows the readiness verdict, so a dependency outage marks the pod unready instead of restarting it. No OTLP endpoint is rendered yet (#217). The chart renders no ServiceMonitor: it installs no Prometheus operator, and a ServiceMonitor is not one of the control-plane kinds `scripts/check_render.py` allows. The design record is `docs/design/compute-health.md`.
- **Measured (vws-test, #89).** Checked with images built from main. console and console-api :8080 return 404 for `/healthz`, `/readyz` and `/metrics`. compute :50051 and network :8080 return Unimplemented for `grpc.health.v1`; storage :7400 returns `SERVING`. At that time no service had `/metrics`. So `global.probe: grpc` could not be turned on then (the compute and network probes would fail), and 7 pods were Ready with tcpSocket probes.
- Needs checking: whether probes pass on real pods (T1 or above), whether ports opened on `0.0.0.0` need a NetworkPolicy, whether `/readyz` reports ready on a real cluster with the KubeVirt and storage dependencies, and whether the same `8080` tcp check is enough for console and console-api (this PR did not read the console repo).
- Needs checking: whether the compute LoadBalancer publishes port 8081 outside the cluster. It selects every port of the Service, compute has no `allowFrom` NetworkPolicy, and this chart adds none; if the load balancer is reachable from outside, restrict 8081 there or add a NetworkPolicy.

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

## Placeholder values (R24)

A development or lab render keeps placeholder values so the chart examples stay readable. A production render refuses them, because a placeholder that reaches a real site is a configuration mistake that would otherwise deploy quietly. `scripts/check_render.py` holds the list in `placeholder_reason`, scans the merged values and the rendered container args, and turns the check on with `--profile production`. `scripts/guard.sh placeholders` runs a negative case for every entry and `scripts/guard.sh all` includes it. The failure names the key and the value, for example `R24: services.network.flags.pool: 192.0.2.0/24 is a placeholder`.

| Required site value | Values key | Placeholder refused |
| --- | --- | --- |
| Elastic IP pool CIDR | `services.network.flags.pool`, from the CR's `spec.services.network.flags.pool` | `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` and addresses in them (RFC 5737); `2001:db8::/32` and addresses in it (RFC 3849) |
| Public console origin | `services.console-api.flags.console-origins`, from the CR's `spec.services.console-api.flags.console-origins` | `example.com`, `example.org`, `example.net`, `.example`, `.invalid`, `.test`, `.localhost` (RFC 2606) |

- Display strings such as `CHANGE_ME`, `changeme`, `TODO` and `<...>` are refused in any value.
- A private range such as `10.20.0.0/24` passes. An address in a documentation range fails even with a host or a port, such as `https://192.0.2.10:8443`.
- The development and lab profiles keep the same values. `scripts/guard.sh placeholders --profile dev FILE...` checks a values file without the production rule.
- Validate a production values file with `scripts/guard.sh placeholders FILE...`. Its default profile is `production`.
- This is a guard, not a schema rule: the same value is legal in a development values file. `vws render` also refuses an RFC 5737 pool before this guard (#148).
- A private range that overlaps a documentation range is not refused; only a range or address inside one is. The list covers the values this chart renders, not values injected outside values such as the installer or the CR. The VWSCluster CRD gets the same guard once the design in #62 decides the profile key.

## Needs checking

- **compute's KubeVirt flags are in the default render (#121).** vws-compute main (`62d90e7`) accepts `-hypervisor`, `-vm-namespace`, `-image-namespace` and `-storage`. They used to be `pendingFlags`, but no service uses `pendingFlags` now. Volume boot was confirmed with these four flags on vws-test T1 (#51).
- Except for the health listener's `VWS_HEALTH_ADDR` (#212), this chart does not use compute's environment variable fallbacks (`VWS_VM_NAMESPACE` and others). It passes flags.
- The services were not started on a real cluster with the rendered arguments (static render checks only).

## Why namespaces are kept

Namespaces the chart creates are annotated with `helm.sh/resource-policy: keep`. If `helm uninstall` deleted namespaces holding VMs, disks (PVCs) and snapshots along with the release, data would be lost (data protection). The cost is that a person must clean up. To remove everything, check the data and then run `kubectl delete namespace <name>` yourself. The chart does not create the release namespace (`release`).

## Renaming namespaces

Change only `namespaces` in `values.yaml`. Templates, Roles and flags all follow that value. `scripts/guard.sh config` renders with renamed namespaces and checks that the arguments and Roles change together and that no old name remains.
