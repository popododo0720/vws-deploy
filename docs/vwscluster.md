# VWSCluster CR, profiles and `vws render` (vws-deploy#53)

The design is vws-deploy#43 (sections 0 and 2). The source of truth for configuration is one `VWSCluster` CR (named `vws`) in the release namespace; values files and rendered YAML are artifacts built from it.

## The exception to the "no CRDs" rule (written in one place)

The vws-deploy#2 decision (no VWS-specific CRDs; service metadata goes in annotations and ConfigMaps) is about the service metadata store. The user **approved one deployment-configuration CRD as an explicit exception** (#43 section 0). Scope:

- Allowed: the single `VWSCluster` CRD in `charts/vws/crds/vwsclusters.yaml`, `v1alpha1`, a namespaced singleton `vws`, deployment configuration only.
- Forbidden: storing service metadata in the CR, any other VWS-specific CRD, service code reading or writing the CR (no `vwsclusters` in service RBAC), a controller that acts on the CR.
- `status` holds only `observedGeneration`, `appliedValuesHash` and `conditions`, written by the installer at the end of `vws apply` (#54).
- The CRD and CR survive an uninstall. Helm only installs files in `crds/` and never deletes them, and the CRD also carries `helm.sh/resource-policy: keep`.
- Check: `scripts/guard.sh crd` (R18).

## API group

The API group is written in **one place only**: `spec.group` in `charts/vws/crds/vwsclusters.yaml`. It is a temporary name until a domain is secured (#43 decision 9). `vws render` and the guard read the group from that file. The `apiVersion` of the example CRs (`examples/vwscluster/*.yaml`) has to repeat the group by syntax, so the guard checks that it matches the CRD and fails if the group appears in any other file. To change it, change the CRD and the three example files together.

## CR schema

| Field | Value | To values |
| --- | --- | --- |
| `spec.profile` | `single-node`, `small`, `ha` (required) | starts from `profiles/<value>.yaml` |
| `spec.version` | image tag; empty means the chart appVersion. Services with `services.<name>.imageTag` (now console-api and compute) do not move | `global.tag` |
| `spec.storage.driver` | `rook-ceph` (default), `existing`, `nfs` | with `existing`, `global.storageClass` and storage's `kube-class-standard` and `kube-class-ssd`. With `nfs`, both classes are `vws-nfs`, `kube-volume-mode=Filesystem` and `kube-snapshot-class=` (empty, no snapshots). State volumes stay on the default class |
| `spec.storage.storageClass` | required with `existing`, forbidden otherwise (every generic CSI driver is `existing`) | same as above |
| `spec.storage.volumeMode` | only with `existing`, `Block` (default) or `Filesystem` | `Filesystem` gives `kube-volume-mode=Filesystem` and storage's `datavolumes.cdi.kubevirt.io` rights (vws-storage#73) |
| `spec.storage.nfs.{server,share}` | both required with `nfs`, forbidden otherwise. `share` starts with `/` | parameters of StorageClass `vws-nfs`. `vws apply` installs csi-driver-nfs and that StorageClass as part of the platform bundle (#170, `docs/platform-bundle.md`). With any other driver neither is installed |
| `spec.storage.s3.rgw.{enabled,endpoint,bucketPrefix,existingSecret}` | RGW object backend (#108). `bucketPrefix` matches `^([a-z0-9][a-z0-9-]{0,17})?$`; `existingSecret` is a Secret name in the release namespace (keys `access-key`, `secret-key`) | `services.storage.s3.rgw` as is. Without it the object backend stays off (default fake). `vws apply` does not remove RGW settings |
| `spec.network.driver` | only `kube-ovn` | changes nothing |
| `spec.network.nodes`, `spec.network.direct` | per-NIC node networks and direct provider networks. Rules and examples in `docs/node-network.md` (#125) | Kube-OVN and Ceph bundle settings and `services.network.flags` |
| `spec.network.vmMTU`, `underlayMTU`, `tunnelOverhead`, `ipv4Only` | VPC VM MTU (#236, vws-network#93). `vmMTU` 68–9001; when unset, 9001 (AWS) is lowered to what the underlay can carry. `underlayMTU` is the tunnel NIC MTU the OS already set (1280–9216). `tunnelOverhead` 0–200, 100 when unset, 60 with `ipv4Only: true`. 9001 needs an underlay of 9101 or more; 9216 is recommended. The installer does not change NIC MTUs. Details in `docs/vm-mtu.md` | `services.network.flags.vm-mtu` (`-vm-mtu`). If `vmMTU` exceeds underlay − overhead, exit code 2. The same overhead is used for kube-ovn-cni `--mtu` |
| `spec.observability.otlpEndpoint` | must be empty | the chart has no observability values yet, so a value is exit code 2 |
| `spec.services.<name>.{replicas,cpu,memory,flags}` | only services in `charts/vws/values.yaml`. `console-api.replicas` must be 1 (sessions are held in memory, popododo0720/vws-console-api#95); render refuses other values with exit code 2 | the `services.<name>` key of the same name; `flags` are merged |
| `spec.secrets.<name>.secretName` | Secret name references only | no values take it yet, so it must be empty |

- There are no secret value fields. If a field or flag name looks secret (`password`, `token`, `secret`, `credential`, `key` and so on, except `secrets`/`secretName`/`existingSecret`), `vws render` refuses it and the guard refuses it in the CRD schema.
- `nfs` and `Filesystem` need an image with vws-storage#73's `-kube-volume-mode` flag. Older images do not start with the unknown flag. The default (`Block`) renders no flag.
- The NFS export must already exist and be mountable from every node.
- With `existing`, the VolumeSnapshotClass (`kube-snapshot-class`) is not changed (needs checking: whether the CR should carry a snapshot class matching the existing StorageClass).

## Profiles

`profiles/<name>.yaml` is a values file layered over `charts/vws/values.yaml` and only changes values. The three profiles have the same keys (R19).

| | single-node | small | ha |
| --- | --- | --- | --- |
| stateless replicas | 2 (schema minimum, two on one node) | 2 | 3 |
| console-api replicas | 1 | 1 | 1 |
| stateful services | 1, 20Gi | 1, 20Gi | 1, 20Gi |
| cpu/memory (estimates, before T2 measurement) | 100m/256Mi | 200m/512Mi | 500m/1Gi |
| `global.topologySpread` | soft | soft | hard (`DoNotSchedule`) |
| Ceph overlay | `platform/rook-ceph-cluster.single-node.values.yaml`: 1 mon, 1 mgr, block pool and object store size 1, failure domain osd. **No data protection** | none (default: 3 mons, size 3, host) | `platform/rook-ceph-cluster.ha.values.yaml`: 3 mons, size 3, host, stated explicitly |

There are three new values keys: `global.profile` (for the record), `global.topologySpread` ("one new values key in the chart" from #43 section 2) and `global.tag` (the target of `spec.version`; image references are still built in one place in the templates).

## Needs checking (real cluster, T1 or above)

- Whether the API server accepts the CRD, and the CEL rules in `x-kubernetes-validations` (name `vws`). The kubeconform catalog has no CRD schema, so the guard checks the structural rules (types, one storage version) itself.
- Whether the Ceph size 1 settings (`requireSafeReplicaSize: false`) and 1 mon actually come up in Rook.
- The profile resource numbers.

## console-api has one replica (popododo0720/vws-console-api#95)

console-api holds sessions in memory. With a second pod or an HPA, requests land on different pods and sessions split. So:

- The chart default and every profile (`profiles/*.yaml`) have `services.console-api.replicas: 1`.
- `values.schema.json`: `services.console-api.replicas` is `const: 1` (required), and there are no values such as `autoscaling`. Other stateless services keep the minimum of 2.
- `scripts/check_render.py` R4: if Deployment console-api is not 1, `must have exactly one replica: sessions are held in memory`. An HPA is not a control-plane kind, so R10 already refuses it.
- `lib/render.py`: if the CR's `spec.services.console-api.replicas` is not 1, exit code 2.
- Limit: with one replica, sessions drop during a console-api rollout or a node failure, and the PDB (maxUnavailable 1) does not stop that pod's eviction. Lift this pin once there is a shared session store.

## console-api -console-origins (required), -trusted-proxies (optional) (popododo0720/vws-console-api#137)

Since console-api#137, login and cookie-writing requests must have an Origin that is the console's own host or is in `-console-origins`; otherwise they get 403. The console Service is a LoadBalancer, so vws cannot know the public address at render time, and the operator writes it in the CR:

```yaml
spec:
  services:
    console-api:
      flags:
        console-origins: https://console.example        # comma-separated, scheme://host[:port], no path
        # trusted-proxies: 10.0.0.0/8,192.0.2.1          # optional. When empty the flag is not passed
```

- There is no default. `lib/render.py` exits 2 without it (`spec.services.console-api.flags.console-origins: required`), and `vws init` asks for it.
- `scripts/check_render.py` R15: refuses a missing `-console-origins` or an entry that is not `scheme://host[:port]`, and refuses a `-trusted-proxies` entry that is not an IP/CIDR.
- `-trusted-proxies` is passed only when it has a value. It is supported together with `-console-origins` since #137 (5b42eb7).
- This chart needs console-api image #137 (5b42eb7) or later. Older images do not start with the unknown flag `-console-origins` (and `-trusted-proxies`, if set).
- Image pin (#280): `services.console-api.imageTag: sha-a003d96` in `charts/vws/values.yaml` pins console-api alone to `ghcr.io/popododo0720/vws/console-api:sha-a003d96`. From this build (console-api#156) the credentials are read from env (`VWS_CONSOLE_TOKEN`, `VWS_ACCESS_KEY`, `VWS_SECRET_KEY`), and this chart passes no credential flags, so `-backend=grpc` does not start with an older image. Other services still follow `global.tag` → appVersion. The schema checks only the tag shape (`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`).
  - This tag was published by vws-console-api's `publish.yml` (ko on every push to main, `sha-<7-character commit>`, label `org.opencontainers.image.revision=<full SHA>`) when a003d96 merged (digest sha256:dbd6ab8a…, anonymous pull checked 2026-10-15). Pull and startup on vws-test need checking.
  - Why a003d96: it is the first build that reads credentials from env (S1). The previous pin was 5b42eb7 (measured on vws-test in #267). To move to a newer main commit, change only `imageTag` after that commit is published, and measure again on vws-test.
  - The workflow does not change the package visibility. If it is private, the nodes need pull credentials (needs checking).
- compute pin: `services.compute.imageTag: sha-07b591c` (vws-compute#91, 07b591c = #89 99a45e4 + publish.yml). vws-compute's `publish.yml` published it at merge (digest sha256:292268f3…, checked anonymously 2026-10-15, label revision 07b591c, user 65532). It is a ko image, so the entrypoint is `/ko-app/vws-compute`. The chart passes only `args`, never `command`.
- Measured on vws-test: in the default install (`-iam=static`), with `-console-origins` set to the console's public address, every console API call returns 200 without a login. Being required does not block the default install.
