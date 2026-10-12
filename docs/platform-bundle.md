# 플랫폼 번들 설치

VWS 제어부가 올라가기 전에 클러스터에 있어야 하는 구성 요소다. 설계는 https://github.com/popododo0720/vws-deploy/issues/2 다. 이 저장소는 클러스터에 아무것도 적용하지 않는다. 아래 절차는 사람이 클러스터에서 실행하는 순서이고, 이 박스에서는 실행하지 않았다(미검증). 렌더와 스키마 검사만 `make check`가 한다.

## 버전

버전과 내려받는 파일의 sha256은 `scripts/versions.env` 한 곳에만 있다. 이 문서와 다른 파일에는 버전 숫자를 쓰지 않는다. `scripts/guard.sh bundle`이 이를 검사한다.

| 구성 요소 | 변수 | 설치 방식 |
| --- | --- | --- |
| Multus | `MULTUS_VERSION` | 업스트림 thick 데몬셋 매니페스트 |
| Kube-OVN | `KUBEOVN_VERSION` | Helm 차트 `kube-ovn`, 값 `platform/kube-ovn.values.yaml` |
| Rook operator | `ROOK_VERSION` | Helm 차트 `rook-ceph`, 값 `platform/rook-ceph.values.yaml` |
| Rook Ceph 클러스터 | `ROOK_VERSION` | Helm 차트 `rook-ceph-cluster`, 값 `platform/rook-ceph-cluster.values.yaml` |
| KubeVirt | `KUBEVIRT_VERSION` | 업스트림 operator 매니페스트, CR `platform/kubevirt-cr.yaml` |
| CDI | `CDI_VERSION` | 업스트림 operator 매니페스트, CR `platform/cdi-cr.yaml` |

`scripts/platform.sh`는 위를 `build/platform/`에 렌더하고 kubeconform으로 검사한다. 내려받는 매니페스트는 sha256을 확인한다. 이 스크립트가 만드는 파일 이름의 번호가 아래 설치 순서의 부분 순서다.

## 설치 순서

기동 순서는 etcd, API, CNI, CSI, KubeVirt, VWS다. 앞 단계가 Ready가 되기 전에 다음 단계를 올리지 않는다.

1. **etcd와 API 서버.** 쿠버네티스 배포판이 제공한다. 운영은 Talos(#8), 로컬 랩은 k3s(#7)다. 백업과 복원은 #6의 런북을 따른다.
2. **CNI.** 배포판의 기본 CNI 없이 시작하거나 Kube-OVN을 기본 CNI로 쓴다. Kube-OVN을 먼저 올리고(`20-kube-ovn.yaml`), 이어서 Multus(`10-multus.yaml`)를 올린다. VM 연결은 Multus의 NetworkAttachmentDefinition(provider kube-ovn)이다.
3. **CSI.** Rook operator(`30-rook-ceph-operator.yaml`), 그다음 Ceph 클러스터(`31-rook-ceph-cluster.yaml`). 기본 StorageClass는 Ceph RBD다.
4. **KubeVirt와 CDI.** operator(`40-`, `42-`)가 Ready가 된 뒤 CR(`41-`, `43-`)을 올린다.
5. **VWS.** `charts/vws`를 올린다. 컨트롤러 권한은 차트의 서비스별 `rbac`가 만든다.

## 필요한 호스트 커널 모듈

노드 OS가 제공해야 한다. 이 저장소의 코드는 호스트에 아무것도 설치하지 않는다.

| 모듈 | 쓰는 곳 |
| --- | --- |
| `rbd` | Ceph RBD CSI가 볼륨을 노드에 붙인다 |
| `openvswitch` | Kube-OVN의 데이터 경로 |
| `geneve` | Kube-OVN의 기본 터널(`platform/kube-ovn.values.yaml`의 `TUNNEL_TYPE`) |
| `kvm`(`kvm_intel` 또는 `kvm_amd`) | KubeVirt의 VM 가속. 없으면 `platform/kubevirt-cr.yaml`의 `useEmulation`을 켜야 하고 매우 느리다 |

## 알려진 한계

- 렌더와 kubeconform까지만 확인했다. 실제 클러스터 설치, 기동 순서, 업그레이드는 미검증이다. 실측은 T2 랩에서 한다.
- kubeconform은 CRD 기반 리소스(CephCluster, KubeVirt, CDI 등)의 스키마가 기본 카탈로그에 없어 건너뛴다. 이 리소스들은 문법만 확인한다.
- `KUBERNETES_VERSION`(검사용 스키마 버전)이 번들 구성 요소가 요구하는 쿠버네티스 버전과 맞는지는 확인하지 않았다.
