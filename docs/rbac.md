# Why each service has its rights (RBAC)

Each service's `rbac` in `charts/vws/values.yaml` allows only the Kubernetes API calls the service code actually makes. A rule with a `namespace` renders as a Role in that namespace, a rule without one as a ClusterRole, and a ClusterRole may hold only cluster-scoped resources (`CLUSTER_SCOPED` in `scripts/check_render.py`). What follows comes from reading the code on `origin/main` of each service repository. It was not checked with `kubectl auth can-i` on a real cluster (needs checking, T1 or above).

Commits read: vws-compute `df28dbd` (after the #20 merge), vws-storage `77450d5`, vws-network `890dbaf`. A rule's `namespace` is a key of `namespaces` in `values.yaml` (`release` is the release namespace), and the name lives only there. The service flag table is in `docs/service-config.md`.

## compute (`internal/hypervisor/kubevirt/agent.go`, `image.go`)

| Call | Resource | Verbs | Namespace |
| --- | --- | --- | --- |
| `Get`, `Create`, `Delete`, `Patch` (merge), `List` | `kubevirt.io` virtualmachines | get, create, delete, patch, list | VM namespace `Config.Namespace` |
| `Get` (READY decision, vws-compute#50), `Delete` (on reboot) | `kubevirt.io` virtualmachineinstances | get, delete | VM namespace |
| `Create` (before creating the VM), `Get`/`Update` (create retry), `Delete` (destroy, after the VM): the cloud-init Secret `<instance>-cloudinit` (vws-compute#73, `userDataSecretRef`) | `""` secrets | get, create, update, delete | VM namespace. No list/watch (compute uses the uncached `client.New`). Not narrowed with `resourceNames`: the name differs per instance, and RBAC cannot limit create by name. So this Role can read, change and delete every Secret in the VM namespace. compute touches only Secrets with its own managed label (guaranteed by code, not by RBAC). Keep no other Secrets in the VM namespace |
| `Get`, `Create`, `Delete` | `cdi.kubevirt.io` datavolumes | get, create, delete | image namespace `Config.ImageNamespace` |
| `Get`, `List`, `Create`, `Patch` (merge), `Delete`. `image_auth.go` `AuthorizeImages` lists managed DataSources on every sweep and merge-patches their authorization labels (vws-compute#93, a31223a). The client is uncached, so no watch. The pin `sha-1b62a87` includes a31223a, so the call is live | `cdi.kubevirt.io` datasources | get, list, create, patch, delete | image namespace `Config.ImageNamespace`. datavolumes do not get `list` or `patch` |
| `Get` (`checkAttachable`, health probe), `Delete` (instance destroy removes the root claim, vws-compute#105) | persistentvolumeclaims | get, delete | VM namespace (#121) |
| `Get` (reading capacity, health probe) | persistentvolumeclaims | get | image namespace |
| `Get`, `Delete` (instance destroy cleanup, vws-compute#105) | `cdi.kubevirt.io` datavolumes | get, delete | VM namespace. No `create` or `list` |
| `Get`, `List`, `Create`, `Update`, `Delete` (ConfigMap store) | configmaps | get, list, create, update, delete | store namespace `-namespace` (the release namespace in values, `release`) |

- Start and stop are patches of `spec.runStrategy`. No code calls subresources such as `virtualmachines/start`.
- No call writes status; it is only read.
- `update` and `watch` on `datasources`, and PVC `list` and `watch`, stay off because no code uses them. `list` and `patch` on `datasources` are the image sweep in vws-compute#93. In the image namespace datavolumes stay `get`, `create`, and `delete`; in the VM namespace they are `get` and `delete` (vws-compute#105).

## storage (`internal/backend/kube/kube.go`, `snapshot.go`)

| Call | Resource | Verbs | Namespace |
| --- | --- | --- | --- |
| `Get`, `Create`, `Update` (expand), `Delete` | persistentvolumeclaims | get, create, update, delete | the single `-kube-namespace`. In values it equals the VM namespace (`namespaces.vms`) |
| `Get`, `Create`, `Delete` | `snapshot.storage.k8s.io` volumesnapshots | get, create, delete | the same namespace (`namespaces.vms`) |
| `Create`, `Get`, `Delete` (image volume, vws-storage#86); `List` only with `-kube-volume-mode=Filesystem` (vws-storage#73 `datavolume.go`) | `cdi.kubevirt.io` datavolumes | get, create, delete; list added with a Filesystem volume | VM namespace (`namespaces.vms`). Creating a volume from an image creates a CDI clone DataVolume with the DataSource as its source. Always added, regardless of render overrides (nfs, existing Filesystem, direct networks) (#201). `vws render` adds list, not a second rule, only with `spec.storage.driver: nfs`, `existing` + `volumeMode: Filesystem`, or a Filesystem `volumeTypes` entry (#615) |
| `Get` (checking the image volume source) | `cdi.kubevirt.io` datasources | get | image namespace (`namespaces.images`) |
| (CDI clone authorization) | `cdi.kubevirt.io` datavolumes/source | create | image namespace. Before allowing a cross-namespace clone, CDI checks with a SubjectAccessReview that the requesting SA has `create` on this subresource |
| `List` (managed VMs, `vws.io/managed=true`) | `kubevirt.io` virtualmachines | list | the same namespace (`namespaces.vms`) |

- Every call targets the single `b.ns`, so PVCs in other namespaces cannot be deleted.
- **readiness (vws-storage#39, unmerged `feat/grpc-health`)**: readiness with `-block-backend=kube` does `get` on cluster-scoped objects: Namespace `-kube-namespace` (`internal/backend/kube/ready.go:20`), StorageClass `-kube-class-standard`/`-kube-class-ssd` (`:29`) and VolumeSnapshotClass `-kube-snapshot-class` (`:33`). So the storage ClusterRole has only `get` on those three (no list or watch). `READY_NEEDS_GET` in `check_render.py` checks that `get` is there and no other verb (#69). These rights render without a gate (they are a read-only `get`, and images before #39 do not use them).
- No other code uses StorageClasses. `patch`, `list` and `watch` were removed.
- No code calls subresources.
- **VM `list` (#123, vws-storage#68).** `internal/backend/kube/attach.go` lists the managed VMs once per GetVolume/ListVolumes, without a cache, and fills `status.instance` with the VM that uses the claim. No code uses `Get`, `Watch` or informers, so only `list` is granted. Without the right the field stays empty and the error is only logged. Images before #68 do not make this call.

## network (`internal/network/dataplane_kubeovn*.go`)

| Call | Resource | Verbs | Scope |
| --- | --- | --- | --- |
| `Create`, `Get` (`ensure` when it already exists), `Delete` | `kubeovn.io` vpcs, subnets, ovn-eips, ovn-snat-rules | get, create, delete | cluster |
| `Get`, `Update`, `Create`, `Delete` | `kubeovn.io` security-groups | get, create, update, delete | cluster |
| `List`, `Create`, `Get` (`ensure`), `Delete` | `kubeovn.io` ovn-fips | get, list, create, delete | cluster |
| `List` (`rehomeGateway` → `firstOtherSubnet`, `dataplane_kubeovn_snat.go`) | `kubeovn.io` subnets | list | cluster. When an SNAT is deleted it lists everything once to move the NAT gateway to another Subnet of the same VPC. This failed on vws-test without the right (#164). There is no cache, so no watch |
| `Get`, `Create`, `Delete` | `kubeovn.io` vpc-nat-gateways | get, create, delete | cluster |
| `Get`, `Create` | `kubeovn.io` provider-networks, vlans | get, create | cluster |
| `Get`, `Patch` (reconciling the gateway node label) | nodes | get, patch | cluster. vws-network re-applies `ovn.kubernetes.io/external-gw=true` to external-network nodes. When kube-ovn-controller restarted it reset a hand-applied label to `false` and SNAT broke (vws-test). This label belongs to vws-network (#164) |
| `Create`, `Get` (`ensure`), `Delete` | `k8s.cni.cncf.io` network-attachment-definitions | get, create, delete | NAD namespace `NADNamespace` |
| `Create`, `Get` (`ensureDirect`, at start) | `k8s.cni.cncf.io` network-attachment-definitions | get, create | VM namespace `vms`. `vws render` adds it only when `spec.network.direct` is set (#131, `docs/node-network.md`) |
| `Get` (`interfaceAddress`, when an address attaches to an instance) | `kubevirt.io` virtualmachineinstances | get, list, watch | VM namespace `vms` (`-kube-vm-namespace`). Only get is used now; list/watch are for a later watch (vws-network#87 `docs/rbac.md`) |

- **Why `get` (vws-network `72bbde9`, after the #27 merge)**: after `Create` (`internal/network/dataplane_kubeovn.go:69`), `ensureObject` handles `AlreadyExists` with a `Get` (`:74`) to check the id annotation and the spec. `ensure` is called for vpc `dataplane_kubeovn.go:107`, subnet `:124`, ovn-eip `dataplane_kubeovn_eip.go:18`, ovn-fip `dataplane_kubeovn_eip.go:41`, security-group `dataplane_kubeovn_sg.go:18`, vpc-nat-gateway `dataplane_kubeovn_snat.go:39`, the SNAT ovn-eip `:47` and ovn-snat-rule `:53`, and for the NAD through `ensureObject` in `dataplane_kubeovn_nad.go:43`. `get` on security-groups and vpc-nat-gateways is already in the table above (`dataplane_kubeovn_sg.go:67`, `dataplane_kubeovn_snat.go:70`).
- **provider-networks, vlans (#149)**: vws-network #53 (head fe93c1b, unmerged) creates the external-network underlay's ProviderNetwork and Vlan at start with `ensure` (Create, then Get on AlreadyExists) (`internal/network/dataplane_kubeovn_underlay.go`, `docs/underlay.md`). The service never deletes these objects, so no delete is granted. Create on the external Subnet is already in the subnets rule above.
- **vws-network #33** (merged, main `005bfca`): `remove`/`removeObject` does a `Get` (`internal/network/dataplane_kubeovn.go:111`), checks the id annotation and does a `Delete` (`:118`) with a UID precondition. It is called for vpc `dataplane_kubeovn.go:129`, subnet `:162`, ovn-eip `dataplane_kubeovn_eip.go:37`, ovn-fip `:49`, security-group `dataplane_kubeovn_sg.go:26`, ovn-snat-rule `dataplane_kubeovn_snat.go:62`, the SNAT ovn-eip `:65`, vpc-nat-gateway `:77` and NAD `dataplane_kubeovn_nad.go:46`. All eight resources have `get` and `delete` in the table above. The only other calls in the main code are `List` on ovn-fips (`dataplane_kubeovn_eip.go:29`) and `Update` on security-groups (`dataplane_kubeovn_sg.go:84`); there are no `Patch`, `Watch` or `/status` writes (checked with `rg`). So after this PR the verbs of the network Role match the code. `check_render.py` requires `get` and `delete` for every ensure target.
- In the rendered Kube-OVN CRDs, the plural names of the kubeovn.io resources above match and their scope is `Cluster`. The NAD CRD is `Namespaced`.
- All of them write the main object, not a `/status` subresource. Rules such as `subnets/status` were left out because the code does not call them (the Kube-OVN controller writes status).

## iam (vws-iam `internal/store/kubestore`, #369)

| Call | Resource | Verbs | Namespace |
| --- | --- | --- | --- |
| `Get`, `List` (two call sites), `Create`, `Update` (CAS on resourceVersion), `Delete` | `iam.vws.popododo0720.io` iamusers, iamgroups, iamroles, iampolicies, iamaccesskeys, iammfadevices, iamaccesskeyindexes, iamsessions, iamoperations, iamreplayentries, iamattemptcounters, iamgroupmembershiplocks, iamfidodevices | get, list, create, update, delete | the store namespace `-namespace` (the release namespace in values, `release`) |

- `charts/vws/crds/iam.yaml` is a byte copy of vws-iam main `deploy/crds/iam.yaml` at `caf768e` (13 CRDs), and the resource list equals vws-iam main `deploy/rbac/role.yaml`. `tests/vws/render.test.sh` fails if the rendered `vws-iam` Role and the chart CRDs name different kinds (#243).
- The pinned image `74a29dc` predates `iamgroupmembershiplocks` (vws-iam#136) and `iamfidodevices` (vws-iam#168), so it does not touch them yet. The CRDs and grants are installed ahead so moving the pin needs no RBAC or CRD change.
- The client is uncached (no informer), so no watch. No patch, no status subresource, no Secrets: the KEK comes by secretKeyRef env.
- Like every service with `rbac`, the chart also renders the `iam-leader` Lease Role. vws-iam does not use leader election.

## Decision change: no `vws-volumes` namespace

Volume PVCs live in the VM namespace (`vws-vms`), because KubeVirt hotplug can attach only PVCs in the VM's namespace (dr eggbot's decision). So `vws-volumes` in values, its Namespace and its Role were removed, and storage's PVC and VolumeSnapshot Role is in `namespaces.vms`. On the service side, `-kube-namespace` must equal `-vm-namespace` (needs checking: this document could not confirm the issue number of the service-side change). This chart takes both from `namespaces.vms` so they are equal.

## Still needs checking

- The rights on a real API server (`kubectl auth can-i`) were not checked. UNVERIFIED.
- **The compute KubeVirt agent is on by default (#121).** vws-compute `62d90e7`'s `-hypervisor=kubevirt` and the namespace flags are in the default render (`docs/service-config.md`).
- **PVC `get` in the VM namespace (#121).** `internal/hypervisor/kubevirt/volumes.go:145` `checkAttachable` (reads the claim's access mode before attaching and before booting from a volume) and `ping.go:23` (health: gets the probe claim in the VM and image namespaces; NotFound is normal). Without this rule compute health was not serving on vws-test because of forbidden (#51 T1).
- **VMI `get` (#121, vws-compute#50).** The READY decision reads the VMI phase. Without it, running instances stay in RECONCILING. Images from before the vws-compute#50 merge do not make this call (only a read right is added).
- If the clients start using a cache, `get`/`list` will also need `watch`. The main code now uses the uncached `client.New` (compute `internal/store/kubestore/cluster.go:20`, network `internal/network/dataplane_kubeovn_open.go:41`) or `kubernetes.NewForConfig` (storage `internal/backend/kube/config.go:47`).
- **Cross-namespace cloning (`datavolumes/source`) is a T2 lab item.** Neither code nor docs confirmed whether CDI requires caller rights (such as create on `datavolumes/source`) when a VM's `dataVolumeTemplates` clone a DataSource in `vws-images` into `vws-vms`. Create a VM on a real cluster, and if it is refused, add it to the compute rules.
- The namespace values `vws-vms`, `vws-images` and `vws-nad` mean something only if the services receive the same values. They are passed as flags from the one place in values, and whether each service image accepts the flag was checked by reading `cmd` on the service repository's main. compute's three flags are the exception (`docs/service-config.md`).

## Operator roles after installation (bootstrap, vws-deploy#54, #77)

After the temporary installation cluster-admin is reclaimed, the remaining rights are Role `vws-operator` (release namespace) and ClusterRole `vws-operator-read` (cluster scope, read only) in `bootstrap/bootstrap.yaml`. The exact list for `vws-operator-read` and its reasons are in the table in `docs/installer.md`. Added by #77: `kubevirt.io/kubevirts`, `cdi.kubevirt.io/cdis`, `ceph.rook.io/cephclusters` and cluster-scoped `persistentvolumeclaims`, all get and list only. No Secrets, watch, write verbs or wildcards (R20).
