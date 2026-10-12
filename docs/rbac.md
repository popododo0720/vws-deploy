# 서비스 권한(RBAC) 근거

`charts/vws/values.yaml`의 서비스별 `rbac`는 서비스 코드가 실제로 부르는 쿠버네티스 API 호출만 허용한다. 규칙에 `namespace`가 있으면 그 네임스페이스의 Role로, 없으면 ClusterRole로 렌더되고, ClusterRole에는 클러스터 범위 리소스만 둘 수 있다(`scripts/check_render.py`의 `CLUSTER_SCOPED`). 아래는 각 서비스 저장소 `origin/main`의 코드를 읽은 결과다. 실제 클러스터에서 `kubectl auth can-i`로 확인하지 않았다(확인 필요, T1 이상).

읽은 커밋: vws-compute `df28dbd`(#20 머지 후), vws-storage `77450d5`, vws-network `890dbaf`. 규칙의 `namespace`는 `values.yaml` `namespaces`의 키이고(`release`는 릴리스 네임스페이스), 이름은 그 한 곳에만 있다. 서비스 플래그 표는 `docs/service-config.md`다.

## compute (`internal/hypervisor/kubevirt/agent.go`, `image.go`)

| 호출 | 리소스 | 동사 | 네임스페이스 |
| --- | --- | --- | --- |
| `Get`, `Create`, `Delete`, `Patch`(merge), `List` | `kubevirt.io` virtualmachines | get, create, delete, patch, list | VM 네임스페이스 `Config.Namespace` |
| `Delete`(재부팅 시) | `kubevirt.io` virtualmachineinstances | delete | VM 네임스페이스 |
| `Get`, `Create`, `Delete` | `cdi.kubevirt.io` datavolumes, datasources | get, create, delete | 이미지 네임스페이스 `Config.ImageNamespace` |
| `Get`(용량 읽기) | persistentvolumeclaims | get | 이미지 네임스페이스 |
| `Get`, `List`, `Create`, `Update`, `Delete`(ConfigMap 저장소) | configmaps | get, list, create, update, delete | 저장소 네임스페이스 `-namespace`(값에서는 릴리스 네임스페이스, `release`) |

- 시작과 정지는 `spec.runStrategy` 패치다. `virtualmachines/start` 같은 하위 리소스를 부르는 코드는 없다.
- 상태(status)를 쓰는 호출이 없다. 읽기만 한다.
- 이전 규칙에 있던 `update`, `watch`, `datasources`의 `update/patch`, PVC `list/watch`는 코드에 쓰이는 곳이 없어 뺐다.

## storage (`internal/backend/kube/kube.go`, `snapshot.go`)

| 호출 | 리소스 | 동사 | 네임스페이스 |
| --- | --- | --- | --- |
| `Get`, `Create`, `Update`(확장), `Delete` | persistentvolumeclaims | get, create, update, delete | `-kube-namespace` 하나. 값에서는 VM 네임스페이스(`namespaces.vms`)와 같다 |
| `Get`, `Create`, `Delete` | `snapshot.storage.k8s.io` volumesnapshots | get, create, delete | 같은 네임스페이스(`namespaces.vms`) |

- 모든 호출이 `b.ns` 한 곳을 가리킨다. 그래서 다른 네임스페이스의 PVC는 지울 수 없다.
- **readiness(vws-storage#39, 미머지 `feat/grpc-health`)**: `-block-backend=kube`의 readiness가 클러스터 범위 객체를 `get`한다. Namespace `-kube-namespace`(`internal/backend/kube/ready.go:20`), StorageClass `-kube-class-standard`/`-kube-class-ssd`(`:29`), VolumeSnapshotClass `-kube-snapshot-class`(`:33`). 그래서 storage ClusterRole에 셋의 `get`만 둔다(list, watch 없음). `check_render.py`의 `READY_NEEDS_GET`이 `get`이 있고 그 밖의 동사가 없는지 본다(#69). 이 권한은 게이트 없이 렌더된다(읽기 `get`뿐이고, #39 전 이미지는 쓰지 않는다).
- 그 밖에 StorageClass를 쓰는 코드는 없다. `patch`, `list`, `watch`는 뺐다.
- 하위 리소스를 부르는 코드는 없다.

## network (`internal/network/dataplane_kubeovn*.go`)

| 호출 | 리소스 | 동사 | 범위 |
| --- | --- | --- | --- |
| `Create`, `Get`(이미 있을 때 `ensure`), `Delete` | `kubeovn.io` vpcs, subnets, ovn-eips, ovn-snat-rules | get, create, delete | 클러스터 |
| `Get`, `Update`, `Create`, `Delete` | `kubeovn.io` security-groups | get, create, update, delete | 클러스터 |
| `List`, `Create`, `Get`(`ensure`), `Delete` | `kubeovn.io` ovn-fips | get, list, create, delete | 클러스터 |
| `Get`, `Create`, `Delete` | `kubeovn.io` vpc-nat-gateways | get, create, delete | 클러스터 |
| `Create`, `Get`(`ensure`), `Delete` | `k8s.cni.cncf.io` network-attachment-definitions | get, create, delete | NAD 네임스페이스 `NADNamespace` |

- **`get`의 근거(vws-network `72bbde9`, #27 머지 후)**: `ensureObject`가 `Create`(`internal/network/dataplane_kubeovn.go:69`) 뒤 `AlreadyExists`면 `Get`(`:74`)으로 id 어노테이션과 spec을 확인한다. `ensure`를 부르는 곳은 vpc `dataplane_kubeovn.go:107`, subnet `:124`, ovn-eip `dataplane_kubeovn_eip.go:18`, ovn-fip `dataplane_kubeovn_eip.go:41`, security-group `dataplane_kubeovn_sg.go:18`, vpc-nat-gateway `dataplane_kubeovn_snat.go:39`, SNAT용 ovn-eip `:47`, ovn-snat-rule `:53`이고, NAD는 `dataplane_kubeovn_nad.go:43`의 `ensureObject`다. security-groups와 vpc-nat-gateways의 `get`은 위 표에 이미 있다(`dataplane_kubeovn_sg.go:67`, `dataplane_kubeovn_snat.go:70`).
- **vws-network #33**(머지됨, main `005bfca`): `remove`/`removeObject`가 `Get`(`internal/network/dataplane_kubeovn.go:111`) 뒤 id 어노테이션을 확인하고 UID 전제 조건으로 `Delete`(`:118`)한다. 호출하는 곳은 vpc `dataplane_kubeovn.go:129`, subnet `:162`, ovn-eip `dataplane_kubeovn_eip.go:37`, ovn-fip `:49`, security-group `dataplane_kubeovn_sg.go:26`, ovn-snat-rule `dataplane_kubeovn_snat.go:62`, SNAT용 ovn-eip `:65`, vpc-nat-gateway `:77`, NAD `dataplane_kubeovn_nad.go:46`이다. 여덟 리소스 모두 위 표에 `get`과 `delete`가 있다. main 코드의 나머지 호출은 `List` ovn-fips(`dataplane_kubeovn_eip.go:29`), `Update` security-groups(`dataplane_kubeovn_sg.go:84`)뿐이고 `Patch`, `Watch`, `/status` 쓰기는 없다(`rg` 확인). 즉 이 PR 뒤 network Role과 코드의 동사가 일치한다. `check_render.py`가 ensure 대상마다 `get`과 `delete`를 요구한다.
- 렌더한 Kube-OVN CRD에서 위 kubeovn.io 리소스의 plural 이름이 일치하고 범위가 모두 `Cluster`인 것을 확인했다. NAD CRD는 `Namespaced`다.
- 모두 `/status` 하위 리소스 없이 본 객체를 쓴다. `subnets/status` 같은 규칙은 코드가 부르지 않아 넣지 않았다(상태는 Kube-OVN 컨트롤러가 쓴다).

## 결정 변경: `vws-volumes` 네임스페이스 없음

볼륨 PVC는 VM 네임스페이스(`vws-vms`)에 둔다. KubeVirt 핫플러그는 VM과 같은 네임스페이스의 PVC만 붙일 수 있어서다(dr eggbot 결정). 그래서 값의 `vws-volumes`, 그 Namespace, 그 Role을 지웠고 storage의 PVC와 VolumeSnapshot Role은 `namespaces.vms`에 있다. 서비스 코드 쪽은 `-kube-namespace`를 `-vm-namespace`와 같게 맞춰야 한다(확인 필요: 서비스 쪽 변경 이슈 번호는 이 문서에서 확인하지 못했다). 이 차트는 둘 다 `namespaces.vms`에서 가져와 같게 만든다.

## 남은 확인 필요

- 실제 API 서버에서의 권한(`kubectl auth can-i`)은 확인하지 못했다. UNVERIFIED.
- **compute 에이전트와 새 플래그는 main에 없다.** `internal/hypervisor/kubevirt`의 코드는 있지만 `cmd/vws-compute/main.go:59`가 `fakeagent.New()`만 쓴다. vm, image 네임스페이스 규칙은 이 코드가 부르는 호출이고, `-vm-namespace`, `-image-namespace`, `-hypervisor` 플래그는 vws-compute#23에서 들어온다. 그래서 이 세 플래그는 `pendingFlags`에 있고 `global.pendingFlags: true`일 때만 렌더된다(`docs/service-config.md`).
- vws-compute#23 표에는 PVC `get`이 VM 네임스페이스에도 있다(`checkAttachable`). 이 호출을 하는 코드는 main에서 확인하지 못해 규칙에 넣지 않았다. #23 머지 후 확인해서 추가한다.
- 클라이언트가 캐시를 쓰게 되면 `get`/`list`에 `watch`가 추가로 필요하다. 지금 main 코드는 캐시 없는 `client.New`(compute `internal/store/kubestore/cluster.go:20`, network `internal/network/dataplane_kubeovn_open.go:41`)나 `kubernetes.NewForConfig`(storage `internal/backend/kube/config.go:47`)를 쓴다.
- **교차 네임스페이스 복제(`datavolumes/source`)는 T2 랩 항목이다.** VM의 `dataVolumeTemplates`가 `vws-images`의 DataSource를 `vws-vms`로 복제할 때 CDI가 호출자 권한(`datavolumes/source` create 등)을 요구하는지 코드나 문서로 확인하지 못했다. 실제 클러스터에서 VM을 만들어 보고, 거절되면 compute 규칙에 추가한다.
- 네임스페이스 값 `vws-vms`, `vws-images`, `vws-nad`는 서비스가 같은 값을 받을 때만 의미가 있다. 값 한 곳에서 플래그로 넘기지만 서비스 이미지가 그 플래그를 받는지는 서비스 저장소 main의 `cmd`를 읽어 확인했다. compute의 세 플래그는 제외다(`docs/service-config.md`).

## 설치 뒤 운영 역할 (bootstrap, vws-deploy#54, #77)

설치용 임시 cluster-admin을 회수한 뒤 남는 권한은 `bootstrap/bootstrap.yaml`의 Role `vws-operator`(릴리스 네임스페이스)와 ClusterRole `vws-operator-read`(클러스터 범위, 읽기만)다. `vws-operator-read`의 정확한 목록과 근거는 `docs/installer.md`의 표. #77로 추가된 것: `kubevirt.io/kubevirts`, `cdi.kubevirt.io/cdis`, `ceph.rook.io/cephclusters`, 클러스터 범위 `persistentvolumeclaims`, 모두 get, list만. Secret, watch, 쓰기 동사, 와일드카드는 없다(R20).
