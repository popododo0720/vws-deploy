# etcd 백업, 복원, 장애 복구 테스트

설계는 https://github.com/popododo0720/vws-deploy/issues/2 다. 원하는 상태는 etcd 한 곳에 있다. 이 문서의 절차와 시나리오는 모두 **미검증**이다. 이 박스에서 클러스터를 띄우지 않았다. 실제로 돌리고 나면 해당 줄의 상태를 바꾼다.

운영은 Talos, 로컬 랩은 k3s다(#8, #7). 명령은 둘 다 적는다. `<cp-ip>`는 컨트롤 플레인 노드 주소다.

## 스냅샷

| 항목 | Talos(운영) | k3s(랩) |
| --- | --- | --- |
| 만들기 | `talosctl -n <cp-ip> etcd snapshot ./etcd-$(date +%Y%m%d-%H%M).db` | `k3s etcd-snapshot save --name vws` |
| 정기 실행 | 노드 밖 작업 호스트의 cron이 위 명령을 6시간마다 실행한다 | `--etcd-snapshot-schedule-cron "0 */6 * * *"`, `--etcd-snapshot-retention 28` |
| 노드 밖 복사 | 작업 호스트에서 오브젝트 스토리지나 다른 서버로 `rclone copy` | `--etcd-s3` 옵션 또는 `rclone copy` |
| 목록 확인 | `ls -l` 로 파일 크기와 시각 | `k3s etcd-snapshot ls` |

- 노드 안에만 있는 스냅샷은 백업이 아니다. 복사본이 노드 밖에 있는지 확인한 뒤에만 성공으로 센다.
- 스냅샷에는 Secret이 들어 있다. 복사 대상은 암호화하고 접근을 제한한다.
- 확인 명령. 스냅샷 파일이 24시간 안에 갱신되었는지 `find <dir> -name '*.db' -mmin -1440 | grep -q .`로 본다.

## 복원

복원은 etcd 쿼럼을 잃었거나 데이터가 손상됐을 때만 한다. 복원하면 스냅샷 이후의 변경은 사라진다. VM이 스냅샷 이후에 만들어졌다면 쿠버네티스 객체만 사라지고 Ceph 볼륨은 남는다. 복원 뒤에 고아 볼륨을 정리한다.

### Talos

1. 모든 컨트롤 플레인 노드의 etcd를 초기화한다. `talosctl -n <cp-ip> reset --graceful=false --reboot --system-labels-to-wipe=EPHEMERAL`
2. 컨트롤 플레인 노드 하나에서 스냅샷으로 부트스트랩한다. `talosctl -n <cp-ip> bootstrap --recover-from=./etcd.db`
3. 나머지 컨트롤 플레인 노드가 합류하는지 확인한다. `talosctl -n <cp-ip> etcd members`

### k3s

1. 모든 서버에서 k3s를 멈춘다. `systemctl stop k3s`
2. 한 서버에서 복원한다. `k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>`
3. 그 서버에서 k3s를 시작하고 `kubectl get nodes`를 확인한다.
4. 다른 서버는 `/var/lib/rancher/k3s/server/db`를 지운 뒤 시작해 합류시킨다.

## 기동 순서

전원 복구와 복원 뒤의 순서는 etcd, API, CNI(Kube-OVN), CSI(Rook), KubeVirt, VWS다. 각 단계는 앞 단계가 Ready인 것을 확인하고 넘어간다. 오퍼레이터는 스스로 되돌린다. 사람이 순서를 강제하는 곳은 etcd와 API뿐이다.

| 단계 | 확인 명령 | 기대 결과 |
| --- | --- | --- |
| etcd | `talosctl -n <cp-ip> etcd status` 또는 `k3s kubectl get --raw /readyz/etcd` | 멤버 전원 healthy, 리더 하나 |
| API | `kubectl get --raw /readyz` | `ok` |
| CNI | `kubectl -n kube-system get pods -l app=kube-ovn-cni` | 모두 Ready |
| CSI | `kubectl -n rook-ceph get cephcluster` | `HEALTH_OK` |
| KubeVirt | `kubectl -n kubevirt get kubevirt kubevirt -o jsonpath='{.status.phase}'` | `Deployed` |
| VWS | `kubectl -n vws get pods` | 모두 Ready |

## 복구 테스트 시나리오

계층은 설계 문서와 같다. T0은 박스(클러스터 없음), T1은 k3s 랩, T2는 일반 커널 VM 3대 이상의 랩이다. Kube-OVN과 Ceph가 필요한 시나리오는 박스에서 못 하므로 **T2 랩 전용**이다. 모든 시나리오의 현재 상태는 **미검증**이다.

| # | 시나리오 | 계층 | 동작 | 기대 결과 | 확인 명령 | 상태 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 컨트롤러 강제 종료 | T1 | `kubectl -n vws delete pod -l app.kubernetes.io/name=compute --force --grace-period=0` | 새 Pod가 뜨고 목록 조회와 watch로 다시 수렴한다. 인스턴스 상태는 그대로다 | `kubectl -n vws rollout status statefulset/compute`, 이어서 `kubectl get vm -A` 전후 비교 | 미검증 |
| 2 | 인스턴스 재시작 복구 | T1 | 노드를 재부팅한다 | `runStrategy: Always`인 VM의 VMI가 다시 생기고 `Halted`인 VM은 그대로다 | `kubectl get vmi -A -w` | 미검증 |
| 3 | 같은 ClientToken 멱등 | T0, T1 | 같은 `ClientToken`으로 RunInstances를 두 번 호출한다 | 같은 인스턴스 ID가 돌아오고 VM 객체는 하나다 | `kubectl get vm -A -o name \| wc -l`이 1 | 미검증 |
| 4 | etcd 1개 상실 | T1(k3s 임베디드 etcd 3대) | 컨트롤 플레인 노드 하나를 끈다 | 쿼럼이 유지되어 읽기와 쓰기가 계속된다 | `kubectl create configmap probe-$RANDOM -n vws --from-literal=a=b`가 성공 | 미검증 |
| 5 | 쿼럼 상실 뒤 복원 | T1, 네트워크와 스토리지 포함은 T2 | 컨트롤 플레인 노드 둘 이상을 끄고 스냅샷으로 복원한다 | 쓰기가 막혀 있다가 복원 뒤 API가 오르고 스냅샷 시점의 객체가 돌아온다. EC2 Query의 쓰기 호출은 중간에 `ServiceUnavailable`을 돌려준다 | 위 복원 절차, `kubectl get vm -A`, `aws ec2 run-instances`의 오류 코드 | 미검증 |
| 6 | 쿼럼 상실 중 실행 중 VM 유지 | T2 | etcd와 API를 전부 막고 VM에 ping한다 | 실행 중인 VM과 OVS 데이터 경로가 계속 동작한다(설계상 추정) | VM의 `ping`과 `ovs-vsctl show` | 미검증, T2 전용 |
| 7 | 전체 전원 차단 후 재기동 | T1(네트워크, 스토리지 제외), 전체는 T2 | 끄기 전에 `vws verify-reboot --snapshot before.json`, 모든 노드를 동시에 끄고 켠다 | 기동 순서대로 오르고 VM이 선언된 상태로 돌아온다 | `vws verify-reboot --check before.json --timeout 20m`(게이트 순서와 소요 시간, `docs/vws-cli.md`), 위 기동 순서 표의 확인 명령 | 미검증 |
| 8 | RWO 볼륨 노드 장애 페일오버 | T2 | VM이 쓰는 노드를 전원 차단한다 | out-of-service taint와 Ceph blocklist 뒤 VM이 다른 노드에서 오른다 | `kubectl get vmi -A -o wide`, `ceph osd blocklist ls` | 미검증, T2 전용 |

## 결과 기록

돌린 시나리오는 날짜, 계층, 실행한 명령, 실제 결과를 이 표 밑에 한 줄씩 적고, 위 표의 상태를 "검증(날짜)"로 바꾼다. 결과가 기대와 다르면 상태를 바꾸지 않고 이슈를 연다.
