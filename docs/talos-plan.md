# Talos 부트스트랩과 업그레이드 계획

운영 쿠버네티스 배포판은 Talos다. 로컬 랩은 k3s이고 RKE2는 쓰지 않는다. 설계는 https://github.com/popododo0720/vws-deploy/issues/2 다.

이 문서는 계획이다. 클러스터에 적용하지 않았고, 이 박스에는 Talos 도구와 일반 커널 VM이 없어 어떤 절차도 실행하지 않았다. 따라서 모든 절차와 예시 설정은 **미검증**이다. 실측은 T2 랩(일반 커널 VM 3대 이상)에서 한다. 아래에서 "확인 필요"는 문서에서 기억한 내용이고 랩에서 확인해야 한다는 뜻이다.

## 호스트에 우리 유닛 없음

Talos는 SSH와 셸이 없고 systemd 유닛을 추가하는 방법도 없다. 노드는 머신 설정과 `talosctl`의 머신 API로만 다룬다. 이 저장소는 `.service`, `.target` 파일을 두지 않으며 `scripts/guard.sh layout`(R10)이 검사한다. 그래서 Talos는 이 원칙과 충돌하지 않는다. 지켜야 할 조건은 둘이다. 시스템 확장은 커널 모듈 제공에만 쓰고 확장 서비스(호스트에서 도는 데몬)는 쓰지 않는다. 노드 상태를 바꾸는 일은 머신 설정으로만 한다.

## 구성

- 컨트롤 플레인 3대와 워커 N대. etcd는 컨트롤 플레인에 붙어 있다(stacked). 3대 중 1대를 잃어도 쿼럼이 유지된다.
- 컨트롤 플레인 노드에는 워크로드를 올리지 않는다(`allowSchedulingOnControlPlanes: false`).
- API 서버 엔드포인트는 컨트롤 플레인 공유 VIP 하나다. 예시는 `docs/talos/controlplane-patch.yaml`.
- 기본 CNI를 끈다(`cluster.network.cni.name: none`). Kube-OVN이 CNI다. CNI가 올라오기 전에는 노드가 NotReady이므로 부트스트랩 직후 Kube-OVN을 올려야 한다.
- 설정 생성. `talosctl gen config vws https://<vip>:6443 --config-patch @docs/talos/controlplane-patch.yaml --output-types controlplane`, 워커는 `--config-patch @docs/talos/worker-patch.yaml --output-types worker`. 각 노드에 `talosctl apply-config --insecure -n <ip> --file <file>`.
- 부트스트랩. 컨트롤 플레인 노드 하나에서 `talosctl bootstrap -n <cp-ip>` 한 번, 이어서 `talosctl kubeconfig`.
- 보안. `talosconfig`와 생성된 시크릿(`secrets.yaml`)은 저장소에 넣지 않는다.

## 기동 순서

etcd, API, CNI(Kube-OVN), CSI(Rook), KubeVirt, VWS다. Talos가 etcd와 API를 올리고, 이후 단계는 `docs/platform-bundle.md`의 순서와 확인 명령을 따른다. Rook과 KubeVirt, Kube-OVN의 네임스페이스는 특권 파드가 필요하다. Talos의 기본 파드 보안 수준에서 막힐 수 있어 네임스페이스에 privileged 레이블이 필요한지 랩에서 확인해야 한다(확인 필요).

## 커널 모듈과 확장

필요 모듈은 `rbd`, `openvswitch`, `geneve`, `kvm`이다. 근거는 `docs/platform-bundle.md`다.

| 모듈 | 제공 방법 | 상태 |
| --- | --- | --- |
| `kvm` | Talos 커널에 들어 있다고 기억한다. VM 가속은 `/dev/kvm`이 있어야 한다 | 확인 필요 |
| `geneve` | 머신 설정 `machine.kernel.modules` | 확인 필요 |
| `openvswitch` | 머신 설정 `machine.kernel.modules`. 기본 커널에 없으면 시스템 확장을 넣은 설치 이미지(Image Factory 스키매틱)가 필요하다 | 확인 필요 |
| `rbd` | 머신 설정 `machine.kernel.modules`. 없으면 시스템 확장 | 확인 필요 |

랩에서 처음 할 일은 모듈이 실제로 올라오는지 보는 것이다. `talosctl -n <ip> read /proc/modules`로 확인하고, 없으면 확장이 들어간 이미지로 `talosctl upgrade --image`를 한다. 설치 이미지 참조는 업그레이드 때 그대로 쓰므로 스키매틱 ID를 문서와 설정에 같이 남긴다.

## 업그레이드

순서는 Talos OS, 쿠버네티스, 플랫폼 번들이다. 한 번에 하나만 바꾸고 각 단계 뒤에 기동 순서의 확인 명령을 돌린다.

1. **Talos OS.** 노드 한 대씩 `talosctl upgrade -n <ip> --image <installer-image>:<version>`. 워커를 먼저 하고 컨트롤 플레인은 한 대씩 한다. 컨트롤 플레인 한 대를 올릴 때마다 `talosctl etcd status`로 쿼럼이 돌아왔는지 본다. 시작 전에 etcd 스냅샷을 만든다(`docs/etcd-runbook.md`).
2. **쿠버네티스.** `talosctl -n <cp-ip> upgrade-k8s --to <version>`. 먼저 `--dry-run`을 쓴다. 번들 구성 요소가 요구하는 쿠버네티스 버전 범위는 `scripts/versions.env`를 바꾸기 전에 각 프로젝트 문서에서 확인한다.
3. **플랫폼 번들.** `scripts/versions.env`의 버전을 바꾸고 `make check`(렌더와 kubeconform)를 통과시킨 뒤, 구성 요소를 기동 순서대로 하나씩 올린다. Rook은 operator, Ceph 클러스터 순서이고 Ceph가 `HEALTH_OK`가 된 뒤 다음으로 간다.

롤백.

- Talos OS는 이중 부트 영역을 쓰는 것으로 기억한다. 부팅이 실패하면 이전 이미지로 돌아가고, 수동으로는 `talosctl rollback -n <ip>`다(확인 필요).
- 쿠버네티스 롤백은 지원되지 않는 것으로 기억한다. 그래서 2단계 전에 etcd 스냅샷이 필요하고, 실패하면 `docs/etcd-runbook.md`의 복원 절차를 쓴다(확인 필요).
- 플랫폼 번들은 `scripts/versions.env`를 이전 값으로 되돌려 다시 올린다. CRD가 바뀐 operator는 되돌리기 어려울 수 있어 업그레이드 전 스냅샷을 만든다.

## 재부팅, 전원 차단, etcd 런북과의 연결

- 워커 재부팅. Talos가 kubelet을 올리고 KubeVirt가 `runStrategy: Always` VM을 다시 만든다.
- 전체 전원 차단. 컨트롤 플레인 노드가 etcd를 복구하고 API가 오른 뒤 기동 순서대로 올라온다. 시나리오 7과 같다.
- etcd 데이터 손실. 스냅샷 복원은 `docs/etcd-runbook.md`의 Talos 절을 따른다. `talosctl etcd snapshot`을 노드 밖 작업 호스트에서 정기 실행한다.
- 복구 테스트 시나리오는 `docs/etcd-runbook.md`에 있고 Talos에서의 실측은 T2 랩에서 한다.

## 로컬 랩(k3s)과 달라지는 점

| 항목 | k3s 로컬 랩(T1) | Talos 운영 |
| --- | --- | --- |
| 노드 접근 | SSH와 셸, `systemctl` | 머신 API(`talosctl`)만. SSH 없음 |
| OS 설정 | 호스트 패키지, `modprobe` | 머신 설정과 시스템 확장 |
| 커널 모듈 | 호스트 커널의 모듈을 로드 | 머신 설정과 이미지에 들어간 것만 |
| etcd | 임베디드 etcd 또는 SQLite | 컨트롤 플레인 3대의 etcd |
| 스냅샷 | `k3s etcd-snapshot` | `talosctl etcd snapshot` |
| 복원 | `--cluster-reset-restore-path` | `talosctl bootstrap --recover-from` |
| CNI | flannel(기본) | Kube-OVN. 기본 CNI 끔 |
| 스토리지 | local-path | Rook Ceph RBD |
| 업그레이드 | 설치 스크립트 재실행 | `talosctl upgrade`, `upgrade-k8s` |
| 파드 보안 | 기본 허용에 가깝다 | 기본 파드 보안 수준이 있어 네임스페이스 설정이 필요할 수 있다(확인 필요) |
| 검증 계층 | 박스(T1 중 일부)와 랩 | T2 랩 |

## 랩에서 처음 확인할 목록

1. 모듈 4개가 올라오는지, 확장이 필요한지.
2. Kube-OVN과 Rook의 네임스페이스에 필요한 파드 보안 설정.
3. 컨트롤 플레인 VIP가 CNI 없이 부트스트랩 중에 동작하는지.
4. `talosctl bootstrap --recover-from`으로 스냅샷 복원이 되는지(시나리오 5).
5. OS 업그레이드 중 컨트롤 플레인이 한 대씩 쿼럼을 유지하는지.
