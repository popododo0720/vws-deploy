# 서비스 권한(RBAC) 근거

`charts/vws/values.yaml`의 서비스별 `rbac`는 서비스 코드가 실제로 부르는 쿠버네티스 API 호출만 허용한다. 규칙에 `namespace`가 있으면 그 네임스페이스의 Role로, 없으면 ClusterRole로 렌더되고, ClusterRole에는 클러스터 범위 리소스만 둘 수 있다(`scripts/check_render.py`의 `CLUSTER_SCOPED`). 아래는 각 서비스 저장소 `origin/main`의 코드를 읽은 결과다. 실제 클러스터에서 `kubectl auth can-i`로 확인하지 않았다(확인 필요, T1 이상).

읽은 커밋: vws-compute `e99cd59`, vws-storage `8e15f81`, vws-network `890dbaf`.

## compute (`internal/hypervisor/kubevirt/agent.go`, `image.go`)

| 호출 | 리소스 | 동사 | 네임스페이스 |
| --- | --- | --- | --- |
| `Get`, `Create`, `Delete`, `Patch`(merge), `List` | `kubevirt.io` virtualmachines | get, create, delete, patch, list | VM 네임스페이스 `Config.Namespace` |
| `Delete`(재부팅 시) | `kubevirt.io` virtualmachineinstances | delete | VM 네임스페이스 |
| `Get`, `Create`, `Delete` | `cdi.kubevirt.io` datavolumes, datasources | get, create, delete | 이미지 네임스페이스 `Config.ImageNamespace` |
| `Get`(용량 읽기) | persistentvolumeclaims | get | 이미지 네임스페이스 |

- 시작과 정지는 `spec.runStrategy` 패치다. `virtualmachines/start` 같은 하위 리소스를 부르는 코드는 없다.
- 상태(status)를 쓰는 호출이 없다. 읽기만 한다.
- 이전 규칙에 있던 `update`, `watch`, `datasources`의 `update/patch`, PVC `list/watch`는 코드에 쓰이는 곳이 없어 뺐다.

## storage (`internal/backend/kube/kube.go`, `snapshot.go`)

| 호출 | 리소스 | 동사 | 네임스페이스 |
| --- | --- | --- | --- |
| `Get`, `Create`, `Update`(확장), `Delete` | persistentvolumeclaims | get, create, update, delete | 설정된 `Namespace` 하나 |
| `Get`, `Create`, `Delete` | `snapshot.storage.k8s.io` volumesnapshots | get, create, delete | 같은 네임스페이스 |

- 모든 호출이 `b.ns` 한 곳을 가리킨다. 그래서 다른 네임스페이스의 PVC는 지울 수 없다.
- StorageClass를 읽는 코드가 없어 `storageclasses` 규칙을 뺐다. `patch`, `list`, `watch`도 뺐다.
- 하위 리소스를 부르는 코드는 없다.

## network (`internal/network/dataplane_kubeovn*.go`)

| 호출 | 리소스 | 동사 | 범위 |
| --- | --- | --- | --- |
| `Create`, `Delete` | `kubeovn.io` vpcs, subnets, ovn-eips, ovn-snat-rules | create, delete | 클러스터 |
| `Get`, `Update`, `Create`, `Delete` | `kubeovn.io` security-groups | get, create, update, delete | 클러스터 |
| `List`, `Create`, `Delete` | `kubeovn.io` ovn-fips | list, create, delete | 클러스터 |
| `Get`, `Create`, `Delete` | `kubeovn.io` vpc-nat-gateways | get, create, delete | 클러스터 |
| `Create`, `Delete` | `k8s.cni.cncf.io` network-attachment-definitions | create, delete | NAD 네임스페이스 `NADNamespace` |

- 렌더한 Kube-OVN CRD에서 위 kubeovn.io 리소스의 plural 이름이 일치하고 범위가 모두 `Cluster`인 것을 확인했다. NAD CRD는 `Namespaced`다.
- 모두 `/status` 하위 리소스 없이 본 객체를 쓴다. `subnets/status` 같은 규칙은 코드가 부르지 않아 넣지 않았다(상태는 Kube-OVN 컨트롤러가 쓴다).

## 네임스페이스 이름과 남은 확인 필요

- 값 파일의 `vws-vms`, `vws-images`, `vws-volumes`, `vws-nad`는 이 저장소가 정한 이름이다. 서비스가 실제로 쓰는 네임스페이스(compute `Config.Namespace`와 `ImageNamespace`, storage `Namespace`, network `NADNamespace`)가 이와 같아야 한다. 서비스 코드의 기본값은 storage가 `default`이고 나머지는 플래그로 받는다. 차트가 서비스에 이 값을 넘기지 않으므로 맞추는 일은 아직 사람이 한다(확인 필요).
- 차트는 규칙에 적힌 네임스페이스를 만들고, 삭제할 때 남긴다(`helm.sh/resource-policy: keep`).
- compute의 `kubevirt` 에이전트는 `origin/main`의 `cmd`에서 만들어지지 않는다. 클라이언트가 캐시를 쓰는 형태(informer)로 연결되면 `get`에 더해 `list`와 `watch`가 필요하다. 지금은 코드가 직접 호출하는 동사만 허용했다(확인 필요).
- KubeVirt가 VM의 `dataVolumeTemplates`와 CDI가 다른 네임스페이스의 DataSource 복제를 허가할 때 호출자 권한(`datavolumes/source` 등)을 확인하는지는 코드나 문서로 확인하지 못했다. 필요하면 compute 규칙에 추가해야 한다(확인 필요).
