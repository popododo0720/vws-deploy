# 플랫폼 번들 설치

VWS 제어부가 올라가기 전에 클러스터에 있어야 하는 구성 요소다. 설계는 https://github.com/popododo0720/vws-deploy/issues/2 다. 이 저장소는 클러스터에 아무것도 적용하지 않는다. 아래 절차는 사람이 클러스터에서 실행하는 순서이고, 이 박스에서는 실행하지 않았다(미검증). 렌더와 스키마 검사만 `make check`가 한다.

## 버전

버전과 내려받는 파일의 sha256은 `scripts/versions.env` 한 곳에만 있다. 이 문서와 다른 파일에는 버전 숫자를 쓰지 않는다. `scripts/guard.sh bundle`이 이를 검사한다.

| 구성 요소 | 변수 | 설치 방식 |
| --- | --- | --- |
| Multus | `MULTUS_VERSION` | 업스트림 thick 데몬셋 매니페스트. `kube-multus` 메모리만 request 100Mi, limit 300Mi로 바꾼다(업스트림 50Mi는 노드 재부팅 때 OOMKilled, #88) |
| Kube-OVN | `KUBEOVN_VERSION` | Helm 차트 `kube-ovn`, 값 `platform/kube-ovn.values.yaml` |
| Rook operator | `ROOK_VERSION` | Helm 차트 `rook-ceph`, 값 `platform/rook-ceph.values.yaml` |
| Rook Ceph 클러스터 | `ROOK_VERSION` | Helm 차트 `rook-ceph-cluster`, 값 `platform/rook-ceph-cluster.values.yaml` |
| KubeVirt | `KUBEVIRT_VERSION` | 업스트림 operator 매니페스트, CR `platform/kubevirt-cr.yaml`. `spec.infra.nodePlacement.nodeSelector: kubernetes.io/os: linux`로 virt-api, virt-controller, virt-exportproxy를 control-plane에 묶지 않는다. 기본 배치에서는 control-plane이 하나인 클러스터에서 그 노드를 drain하면 PDB 때문에 멈춘다(#168) |
| CDI | `CDI_VERSION` | 업스트림 operator 매니페스트, CR `platform/cdi-cr.yaml` |
| csi-driver-nfs | `CSI_DRIVER_NFS_VERSION` | Helm 차트 `csi-driver-nfs`(kubernetes-csi), 값 `platform/csi-driver-nfs.values.yaml`. StorageClass, VolumeSnapshotClass, snapshot controller와 CRD는 만들지 않는다. `35-csi-driver-nfs.yaml`은 번들에 항상 있지만 `vws apply`는 `spec.storage.driver: nfs`일 때만 올린다(#170) |

`scripts/platform.sh`는 위를 `build/platform/`에 렌더하고 kubeconform으로 검사한다. 내려받는 매니페스트는 sha256을 확인한다. 이 스크립트가 만드는 파일 이름의 번호가 아래 설치 순서의 부분 순서다.

## 설치 순서

기동 순서는 etcd, API, CNI, CSI, KubeVirt, VWS다. 앞 단계가 Ready가 되기 전에 다음 단계를 올리지 않는다.

1. **etcd와 API 서버.** 쿠버네티스 배포판이 제공한다. 운영은 Talos(#8), 로컬 랩은 k3s(#7)다. 백업과 복원은 #6의 런북을 따른다.
2. **CNI.** 배포판의 기본 CNI 없이 시작하거나 Kube-OVN을 기본 CNI로 쓴다. Kube-OVN을 먼저 올리고(`20-kube-ovn.yaml`), 이어서 Multus(`10-multus.yaml`)를 올린다. VM 연결은 Multus의 NetworkAttachmentDefinition(provider kube-ovn)이다.
3. **CSI.** Rook operator(`30-rook-ceph-operator.yaml`), 그다음 Ceph 클러스터(`31-rook-ceph-cluster.yaml`). 기본 StorageClass는 Ceph RBD다.
   `spec.storage.driver: nfs`이면 csi-driver-nfs(`35-csi-driver-nfs.yaml`)와 StorageClass `vws-nfs`(`36-vws-nfs-storageclass.yaml`, `vws apply`가 CR에서 만든다)를 올린다. NFS export는 미리 있어야 하고 모든 노드에서 마운트할 수 있어야 한다.
4. **KubeVirt와 CDI.** operator(`40-`, `42-`)가 Ready가 된 뒤 CR(`41-`, `43-`)을 올린다.
5. **VWS.** `charts/vws`를 올린다. 컨트롤러 권한은 차트의 서비스별 `rbac`가 만든다.

## 스토리지 클래스 이름

Rook Ceph 클러스터 차트가 만드는 이름을 vws-storage의 설정값과 맞춘다. 번들 값 파일은 이름을 덮어쓰지 않으므로 이름은 차트 기본값이다. 번들 이름은 바꾸지 않고, vws-storage 기본값을 번들에 맞춘다.

| 대상 | 번들이 만드는 이름 | vws-storage 쪽 |
| --- | --- | --- |
| StorageClass | `ceph-block` | 볼륨 타입 표(`kube.Classes`)의 값 |
| VolumeSnapshotClass | `ceph-block` | `kube.NewBlock`의 `snapshotClass` 인자 |

`spec.storage.driver: nfs`이면 StorageClass `vws-nfs`(provisioner `nfs.csi.k8s.io`, parameters `server`, `share`는 CR의 `spec.storage.nfs`, `reclaimPolicy: Delete`, `allowVolumeExpansion: true`)를 `scripts/vws.d/lib/platform.py`가 만든다. VolumeSnapshotClass는 없다(스냅숏 없음). vws-storage는 `-kube-volume-mode=Filesystem`으로 돌고 CDI blank DataVolume으로 `disk.img`를 만든다(vws-storage#73). 다른 driver면 csi-driver-nfs도 `vws-nfs`도 올리지 않는다. 실제 NAS에서 프로비저닝은 확인하지 않았다(확인 필요, vws-test).

두 이름은 번들이 만드는 이름과 일치해야 한다. `scripts/guard.sh bundle`이 `build/platform/31-rook-ceph-cluster.yaml`(`scripts/platform.sh`가 만든다)에서 두 이름이 각각 한 번씩 있고 provisioner와 driver가 같은지 보고, 이 문서에 같은 이름이 적혀 있는지 본다. 실제 클러스터에서 PVC 바인딩과 VolumeSnapshot 생성은 확인하지 않았다(확인 필요, T1 이상).

## CDI 기능 게이트

`platform/cdi-cr.yaml`은 `spec.config.featureGates`에 `HonorWaitForFirstConsumer`를 켠다. 이 게이트는 켜는 항목이고 CR에 없으면 꺼진다. 켜면 `WaitForFirstConsumer` StorageClass에서 CDI가 워커 파드를 VM이 스케줄될 때까지 만들지 않는다. 기본 Ceph 클래스는 `Immediate`라 이 게이트가 아무 일도 하지 않는다. 로컬 랩의 k3s 기본 클래스(`local-path`)는 `WaitForFirstConsumer`라 이때 의미가 있다. 게이트 이름과 필드 경로는 고정한 CDI의 소스, 문서, CRD로 확인했다. `scripts/guard.sh bundle`은 번들 CRD 스키마에 이 CR의 필드가 있는지 본다. 실제 클러스터에서 DataVolume이 동작하는지는 확인하지 않았다(확인 필요, T1 이상).

## 필요한 호스트 커널 모듈

노드 OS가 제공해야 한다. 이 저장소의 코드는 호스트에 아무것도 설치하지 않는다.

| 모듈 | 쓰는 곳 |
| --- | --- |
| `rbd` | Ceph RBD CSI가 볼륨을 노드에 붙인다 |
| `openvswitch` | Kube-OVN의 데이터 경로 |
| `geneve` | Kube-OVN의 기본 터널(`platform/kube-ovn.values.yaml`의 `TUNNEL_TYPE`) |
| `nfs` | `spec.storage.driver: nfs`일 때 csi-driver-nfs가 export를 노드에 마운트한다(NFS 클라이언트) |
| `kvm`(`kvm_intel` 또는 `kvm_amd`) | KubeVirt의 VM 가속. 없으면 `platform/kubevirt-cr.yaml`의 `useEmulation`을 켜야 하고 매우 느리다 |

## 실제 클러스터에서 필요했던 것 (vws-test, #51)

k3s v1.35.9 + 외부 Ceph(ceph-csi-rbd) 3노드에서 확인한 것이다. 다른 배포판은 미검증.

- **LoadBalancer 구현(#86).** 차트의 `external: true` 서비스는 `type: LoadBalancer`다. servicelb를 끈 k3s에서는 EXTERNAL-IP가 pending이고 `vws apply`의 `helm --wait --atomic`이 타임아웃까지 기다린다. MetalLB(L2, IPAddressPool과 L2Advertisement) 같은 구현을 먼저 올리거나 서비스의 `external`을 끈다.
- **OpenStack 위 랩의 LB 주소(#86).** OVN이 모르는 주소는 ARP에 답하지 않으므로 LB 주소를 광고 노드 포트의 추가 fixed IP로 넣었다. 추가 fixed IP가 있으면 cloud-init DHCP가 다른 주소를 잡아 노드 주소가 바뀌었으므로 노드 주소를 정적 netplan으로 고정하고 cloud-init 네트워크 설정을 끈다.
- **블록 장치 소유권(#87).** CDI importer가 Block 모드 PVC에 쓰려면 containerd의 `device_ownership_from_security_context = true`가 필요하다. k3s(containerd v2, config v3)에서는 `config-v3.toml.tmpl`이 아니라 드롭인 `/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/10-device-ownership.toml`로 넣고 k3s를 재시작한다.

  ```toml
  version = 3
  [plugins."io.containerd.cri.v1.runtime"]
    device_ownership_from_security_context = true
  ```
- **krbd 노드 재부팅(#87).** RBD를 매핑한 노드를 그냥 재부팅하면 네트워크가 먼저 내려가 `libceph ... error -101`로 shutdown이 멈췄다. 재부팅 전에 `kubectl drain`으로 RBD 파드를 옮긴다. 멈추면 하이퍼바이저에서 하드 리부트가 필요했다.
  재부팅 절차(#116, #150). 어느 단계든 종료 코드가 0이 아니면 거기서 멈추고 재부팅하지 않는다. ① `vws verify-reboot --snapshot FILE` — RBD 볼륨이 붙은 노드마다 `WARN krbd`를 낸다(읽기만 한다). 목록을 하나라도 읽지 못하면 3으로 끝나고 파일을 남기지 않는다. ② 경고된 노드는 `kubectl drain NODE --ignore-daemonsets --delete-emptydir-data`로 비운다. drain이 실패하면 멈춘다. ③ `vws verify-reboot --drained NODE` — 노드가 cordon되어 있고 `rbd.csi.ceph.com` VolumeAttachment가 남지 않았을 때만 0, 아니면 1(읽지 못하면 3). 0일 때만 다음으로 간다. ④ 재부팅. ⑤ `kubectl uncordon NODE`. ⑥ `vws verify-reboot --check FILE`.

## 알려진 한계

- 렌더와 kubeconform까지만 확인했다. 실제 클러스터 설치, 기동 순서, 업그레이드는 미검증이다. 실측은 T2 랩에서 한다.
- kubeconform은 CRD 기반 리소스(CephCluster, KubeVirt, CDI 등)의 스키마가 기본 카탈로그에 없어 건너뛴다. 이 리소스들은 문법만 확인한다.
- `KUBERNETES_VERSION`(kubeconform 스키마)과 `K3S_VERSION`은 같은 쿠버네티스 마이너여야 하고 `scripts/guard.sh bundle`이 이를 검사한다. 그 마이너는 문서로 확인한 지원 범위(KubeVirt, Rook, Kube-OVN)의 교집합 안에서 골랐다. CDI와 Multus는 지원 범위를 문서에서 찾지 못했다(확인 필요). 실제 클러스터에서의 동작도 확인하지 않았다.

## 노드망에 맞춘 번들 (#127)

`vws apply`는 번들을 그대로 쓰지 않고 `scripts/vws.d/lib/platform.py`가 CR에 맞춘 사본을 쓴다. `spec.network.nodes`가 없으면 바이트 단위로 같은 사본이다. 있으면 다음만 바꾼다(설계 #125).

| 대상 | 값 |
| --- | --- |
| kube-ovn-cni `--iface` | tunnel 망의 호스트 인터페이스(`<nic>` 또는 `<nic>.<vlan>`). tunnel이 없으면 management |
| kube-ovn-cni `--mtu` | tunnel `mtu` − 100(geneve 오버헤드) |
| CephCluster `spec.network` | `provider: host`, `addressRanges.public`/`cluster`(storage가 있을 때만) |

번들의 kube-ovn-cni에 빈 `--iface=` 인자가 정확히 하나가 아니면(차트가 바뀜) 종료 코드 2다. `ovn-external-gw-config`는 만들지 않는다(#164). vws-test 증거 53: 이 ConfigMap을 켜면 kube-ovn-controller가 underlay 브리지 `br-vwsul`을 지우고, 이름이 `external`인 Subnet을 요구한다. 사용자 VPC와 `extraExternalSubnets` 조합의 EIP/SNAT에는 필요 없었다. 게이트웨이 노드 레이블 `ovn.kubernetes.io/external-gw`는 vws-network가 reconcile로 다시 붙인다(vws-network 담당, RBAC은 [rbac.md](rbac.md)). 이전 apply가 이미 만든 ConfigMap은 `vws apply`가 지우지 않는다(확인 필요: 지울 때 영향). 확인 필요: 운영 중 `--iface`/`--mtu`/Ceph network 변경의 영향(kube-ovn-cni 재시작, mon 재배포). 실제 클러스터에서 재지 않았다.
