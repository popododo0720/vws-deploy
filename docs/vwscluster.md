# VWSCluster CR, profiles and `vws render` (vws-deploy#53)

The design is vws-deploy#43 (sections 0 and 2). The source of truth for configuration is one `VWSCluster` CR (named `vws`) in the release namespace; values files and rendered YAML are artifacts built from it.

## The exception to the "no CRDs" rule (written in one place)

The vws-deploy#2 decision (no VWS-specific CRDs; service metadata goes in annotations and ConfigMaps) is about the service metadata store. The user **approved one deployment-configuration CRD as an explicit exception** (#43 section 0). Scope:

- Allowed: the single `VWSCluster` CRD in `charts/vws/crds/vwsclusters.yaml`, `v1alpha1`, a namespaced singleton `vws`, deployment configuration only.
- Also allowed (#369): the vws-iam store CRDs in `charts/vws/crds/iam.yaml`, a copy of vws-iam `deploy/crds/iam.yaml`, Namespaced, group `iam.<VWSCluster group>`. They are the vws-iam data store (vws-iam#2), not deployment configuration, and only vws-iam reads or writes them.
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
| `spec.version` | image tag; empty means the chart appVersion. Services with `services.<name>.imageTag` (now console-api, compute and iam) do not move | `global.tag` |
| `spec.storage.driver` | `rook-ceph` (default), `existing`, `nfs` | with `existing`, `global.storageClass` and storage's `kube-class-standard` and `kube-class-ssd`. With `nfs`, both classes are `vws-nfs`, `kube-volume-mode=Filesystem` and `kube-snapshot-class=` (empty, no snapshots). State volumes stay on the default class |
| `spec.storage.storageClass` | required with `existing`, forbidden otherwise (every generic CSI driver is `existing`) | same as above |
| `spec.storage.volumeMode` | only with `existing`, `Block` (default) or `Filesystem` | `Filesystem` gives `kube-volume-mode=Filesystem` and storage's `datavolumes.cdi.kubevirt.io` rights (vws-storage#73) |
| `spec.storage.nfs.{server,share}` | both required with `nfs`, forbidden otherwise. `share` starts with `/` | parameters of StorageClass `vws-nfs`. `vws apply` installs csi-driver-nfs and that StorageClass as part of the platform bundle (#170, `docs/platform-bundle.md`). With any other driver neither is installed |
| `spec.storage.volumeTypes.<type>.{storageClass,volumeMode,volumeSnapshotClass}` | `<type>` is one of `standard`, `gp2`, `gp3`, `io1`, `io2`, `st1`, `sc1`; any may be left unmapped. `storageClass` is required and a DNS-1123 name, `volumeMode` is `Block` or `Filesystem`, `volumeSnapshotClass` a DNS-1123 name or empty; an omitted mode or snapshot class takes the storage default. Forbidden with `nfs`. `vws apply` fails before the chart when a named StorageClass or VolumeSnapshotClass does not exist (#391) | `services.storage.repeatedFlags.kube-volume-type`: one vws-storage `-kube-volume-type=TYPE=CLASS[/MODE[/SNAPSHOTCLASS]]` per mapped type, in EBS order, empty trailing fields left out (vws-storage#123). A `Filesystem` type adds storage's `datavolumes.cdi.kubevirt.io` rights. A changed mapping applies with `vws apply` as a vws-storage rollout; the cluster is not restarted |
| `spec.storage.s3.rgw.{enabled,endpoint,bucketPrefix,existingSecret}` | RGW object backend (#108). `bucketPrefix` matches `^([a-z0-9][a-z0-9-]{0,17})?$`; `existingSecret` is a Secret name in the release namespace (keys `access-key`, `secret-key`) | `services.storage.s3.rgw` as is. Without it the object backend stays off (default fake). `vws apply` does not remove RGW settings |
| `spec.network.driver` | only `kube-ovn` | changes nothing |
| `spec.network.nodes`, `spec.network.direct` | per-NIC node networks and direct provider networks. Rules and examples in `docs/node-network.md` (#125) | Kube-OVN and Ceph bundle settings and `services.network.flags` |
| `spec.network.vmMTU`, `underlayMTU`, `tunnelOverhead`, `ipv4Only` | VPC VM MTU (#236, vws-network#93). `vmMTU` 68–9001; when unset, 9001 (AWS) is lowered to what the underlay can carry. `underlayMTU` is the tunnel NIC MTU the OS already set (1280–9216). `tunnelOverhead` 0–200, 100 when unset, 60 with `ipv4Only: true`. 9001 needs an underlay of 9101 or more; 9216 is recommended. The installer does not change NIC MTUs. Details in `docs/vm-mtu.md` | `services.network.flags.vm-mtu` (`-vm-mtu`). If `vmMTU` exceeds underlay − overhead, exit code 2. The same overhead is used for kube-ovn-cni `--mtu` |
| `spec.observability.otlpEndpoint` | must be empty | the chart has no observability values yet, so a value is exit code 2 |
| `spec.services.<name>.{replicas,cpu,memory,flags}` | only services in `charts/vws/values.yaml`. `console-api.replicas` must be 1 (sessions are held in memory, popododo0720/vws-console-api#95). render refuses other values with exit code 2. `iam.replicas` is at least 2 like any stateless service (#369) | the `services.<name>` key of the same name; `flags` are merged |
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

## iam has two replicas on a shared store (#369)

iam keeps its state in the vws-iam kube store in the release namespace (`-namespace`, `docs/iam-install.md`), so the chart runs `services.iam.replicas: 2` like any stateless service.

- `values.schema.json` and `scripts/check_render.py` R4: at least 2 replicas.
- `scripts/guard.sh config`: the iam render has the IAM CRDs, the Role, the API token, `-namespace` and no state volume.
- The NetworkPolicy listener port is the integer `9095`, with no `endPort` and no protocol other than TCP.

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
- Image pin (#280): `services.console-api.imageTag: sha-c09cba3` in `charts/vws/values.yaml` pins console-api alone to `ghcr.io/popododo0720/vws/console-api:sha-c09cba3`. This build still reads the credentials from env (`VWS_CONSOLE_TOKEN`, `VWS_ACCESS_KEY`, `VWS_SECRET_KEY`), and it makes no Kubernetes API call, so it needs no Role and the chart passes no credential flags. Other services still follow `global.tag` → appVersion. The schema checks only the tag shape (`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`).
  - This tag was published by vws-console-api's `publish.yml` (ko on every push to main, `sha-<7-character commit>`, label `org.opencontainers.image.revision=<full SHA>`) when c09cba3 merged (digest sha256:036dce4c…, an anonymous manifest request returned HTTP 200 on 2026-10-28). vws-test ran this tag in the 2026-10-22 nightly (#386).
  - Why c09cba3: it keeps the env credentials of a003d96, includes the attach state of console-api#299 and fixes DetachVolume replay (console-api#302). The previous pin was a003d96. To move to a newer main commit, change only `imageTag` after that commit is published, and measure again on vws-test.
  - The workflow does not change the package visibility. If it is private, the nodes need pull credentials (needs checking).
- compute pin: `services.compute.imageTag: sha-1b62a87` (vws-compute main on 2026-10-22, which includes 89f2ca3 (#111: valid KubeVirt disk names) and a31223a (#93: image authorization labels)). vws-compute's `publish.yml` published it at merge (digest sha256:a8d7e7aa…, an anonymous manifest request returned HTTP 200 on 2026-10-28, label revision 1b62a87, user 65532). It is a ko image, so the entrypoint is `/ko-app/vws-compute`. The chart passes only `args`, never `command`.
  - Rollout order: a compute image that includes a31223a needs the images-namespace Role to grant `list` and `patch` on `datasources`. Ship that Role in the same release as the image or an earlier one. Helm applies the Role before the compute StatefulSet only inside one release, so a pin change in an earlier release leaves every image sweep logging `cannot list datasources`. The current pin needs no RBAC change: the grant shipped in vws-deploy#311, and the VM namespace grants datavolume get/delete (vws-deploy#370). The optional `-legacy-image-scope` and `-public-images` flags stay unset.
- iam pin: `services.iam.imageTag: sha-74a29dc` (vws-iam#113). vws-iam's `publish.yml` published it at merge. An anonymous manifest request for `sha-74a29dc` returned HTTP 200 and digest `sha256:6c301b75fd46584fd4d6c86f77be9d4389e0492a210d34bc31ab5a34fb9acc5d` on 2026-10-16. The image binary contains go1.26.9. The chart passes only `args`, never `command`. `enabled` stays false, so the default render still leaves iam out. With `enabled` true the chart names `ghcr.io/popododo0720/vws/iam:sha-74a29dc`, and `spec.version` does not move it.
- Measured on vws-test: in the default install (`-iam=static`), with `-console-origins` set to the console's public address, every console API call returns 200 without a login. Being required does not block the default install.
