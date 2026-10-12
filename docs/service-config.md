# 서비스 설정(플래그)과 네임스페이스

차트는 서비스 설정을 `charts/vws/values.yaml` 한 구조로 넘긴다. 플래그만 쓰고 환경 변수는 쓰지 않는다. 설계는 vws-deploy#30, #32다. 값 스키마(`values.schema.json`)가 형식을 강제한다.

## 값 구조

```yaml
global:
  pendingFlags: false        # true면 pendingFlags도 렌더한다
namespaces:                  # 네임스페이스 이름이 있는 유일한 곳
  vms: vws-vms
  images: vws-images
  nad: vws-nad
services:
  storage:
    flags:                   # -이름=값 인자로 렌더된다. 키 순서대로 정렬
      block-backend: kube
      kube-namespace: {namespace: vms}   # namespaces.vms를 가리킨다
    rbac:
    - namespace: vms         # 같은 키를 가리킨다
      ...
```

- 플래그 값은 문자열, 불리언, 정수, `{namespace: <키>}` 중 하나다. `release`는 릴리스 네임스페이스를 뜻하는 예약 키다.
- `rbac` 규칙의 `namespace`도 같은 키다. 키가 `namespaces`에 없으면 렌더가 실패한다.
- `pendingFlags`는 서비스 이미지가 아직 받지 않는 플래그다. `global.pendingFlags: true`일 때만 `flags`에 더해진다.
- 규칙에 쓰인 네임스페이스(릴리스 것 제외)는 차트가 만들고 `helm.sh/resource-policy: keep`을 단다.

## 서비스별 키와 플래그

근거는 각 서비스 저장소 origin/main(compute `df28dbd`, storage `77450d5`, network `890dbaf`)의 `cmd/*/main.go`를 읽은 결과다. 코드 줄 번호는 그 커밋 기준이다.

| 서비스 | values 키(`services.<이름>.flags`) | 인자 | 기본값 | 코드 |
| --- | --- | --- | --- | --- |
| storage | `block-backend: kube` | `-block-backend=kube` | `fake`가 코드 기본 | `cmd/vws-storage/main.go:44` |
| storage | `kube-namespace: {namespace: vms}` | `-kube-namespace=<vms>` | PVC와 스냅샷이 같이 쓴다 | `main.go:47` |
| storage | `kube-class-standard`, `kube-class-ssd`, `kube-snapshot-class` | 같은 이름 | 모두 `ceph-block` | `main.go:48-50` |
| storage | (넘기지 않음) `kube-config` | `-kube-config` | 비면 인클러스터 자격 | `main.go:46` |
| compute | `namespace: {namespace: release}` | `-namespace=<릴리스 네임스페이스>` | ConfigMap 저장소. 비면 메모리 저장소 | `cmd/vws-compute/main.go:34` |
| compute | `pendingFlags.hypervisor: kubevirt` | `-hypervisor=kubevirt` | vws-compute#23에서 들어옴 | main에 없음 |
| compute | `pendingFlags.vm-namespace: {namespace: vms}` | `-vm-namespace=<vms>` | `vws-vms` | main에 없음 |
| compute | `pendingFlags.image-namespace: {namespace: images}` | `-image-namespace=<images>` | `vws-images` | main에 없음 |
| 세 서비스 | `listen: 0.0.0.0:<port>` | `-listen=0.0.0.0:<port>` | 코드 기본은 `127.0.0.1`. `port` 키와 같아야 한다 | compute `main.go:33`, storage `main.go:40`, network `main.go:22` |
| network | `db: /var/lib/vws/network.db`, `pool: <CIDR>` | `-db`, `-pool` | 둘 다 필수. `pool`의 기본값은 자리표시자 | `main.go:23-24,33-34` |
| network | (기본 없음) `kubeconfig`와 `kubeconfigSecret` | `-kubeconfig=<경로>`, Secret 볼륨 | 있으면 Kube-OVN 데이터플레인, 없으면 fake | `cmd/vws-network/main.go:25` |
| network | `kube-external-subnet` | `-kube-external-subnet=<이름>` | `-kubeconfig`와 함께 필수 | `main.go:26` |
| network | `kube-nad-namespace: {namespace: nad}` | `-kube-nad-namespace=<nad>` | `-kubeconfig`만 있으면 `vws-system`이라 차트는 항상 넘긴다 | `main.go:27,30-31` |
| network | `kube-snat` | `-kube-snat=true` | 꺼짐. 외부 서브넷 필요 | `main.go:28` |

- 스키마가 `kubeconfig`가 있으면 `kube-external-subnet`과 `kube-nad-namespace`를, `kube-snat`이 있으면 `kube-external-subnet`을 요구한다(vws-deploy#30). 빠진 값으로 렌더하면 거절된다.
- network는 `-db`와 `-pool`이 없으면 시작하지 않는다(`main.go:33-34`). 기본 values는 `db: /var/lib/vws/network.db`(상태 볼륨 마운트 경로 안, 루트 파일시스템은 읽기 전용이라 거기만 쓸 수 있다)와 `pool: 192.0.2.0/24`를 넘긴다. **`pool`은 문서용 대역(RFC 5737)의 자리표시자이므로 사이트마다 바꿔야 한다(확인 필요: 실제 엘라스틱 IP 대역).** Kube-OVN 데이터플레인을 켜려면 `kubeconfig`, `kube-external-subnet`, `kube-nad-namespace`와 함께 kubeconfig를 담은 Secret의 이름 `kubeconfigSecret`을 넣는다. 차트는 그 Secret을 `flags.kubeconfig` 경로의 디렉터리에 읽기 전용으로 마운트한다(아래 "network kubeconfig"). `scripts/guard.sh config`가 이 형태를 시험한다.
- compute의 `-nad-namespace`(`VWS_NAD_NAMESPACE`, 기본 `vws-nad`)는 예약뿐이고 NAD를 쓰는 코드가 없어 넘기지 않는다.

## 포트와 프로브 (vws-deploy#39)

- 서비스의 `-listen` 기본값은 `127.0.0.1`이라 파드 밖에서 닿지 않는다(compute `main.go:33`, storage `main.go:40`, network `main.go:22`). 세 서비스 모두 values에서 `flags.listen: 0.0.0.0:<port>`를 넘긴다. 기본 포트는 서비스 코드의 기본 포트와 같다(compute 50051, storage 7400, network 8080).
- 서비스의 `port` 키 하나가 컨테이너 포트, 프로브 포트, `-listen` 포트의 기준이다. 셋이 다르면 `scripts/guard.sh config`(R15)가 실패한다.
- 프로브는 HTTP가 아니다. 서비스의 비-vendor Go 코드에 `healthz`, `readyz`, `grpc_health`가 없다(위 세 커밋 기준, 부재 증거일 뿐이다). 기본은 `tcpSocket`이다. 이 검사는 포트가 열려 있음만 보장하고 DB나 클러스터 접근 같은 의존성은 보장하지 않는다.
- **gRPC 프로브로 바꾸는 방법**: 서비스 이미지가 `grpc.health.v1`을 구현한 뒤 `global.probe: grpc` 한 줄만 바꾼다. 렌더가 `grpc: {port: <port>}` 프로브로 바뀌고 같은 포트 검사를 받는다. 구현 전에 켜면 프로브가 실패한다. 서비스의 구현은 별도로 요청되어 있다.
- 확인 필요: 실제 파드에서 프로브 통과 여부(T1 이상), `0.0.0.0`으로 연 포트의 NetworkPolicy 필요 여부, console과 console-api가 같은 `8080` tcp 검사로 충분한지(이 PR은 console 레포를 읽지 않았다).

## network kubeconfig (vws-deploy#40, 임시)

vws-network는 `-kubeconfig`가 있어야 Kube-OVN 데이터플레인을 쓴다(`main.go:25`). 차트는 **선택지 1(Secret 볼륨)만 임시로** 지원한다.

- values 키는 `services.network.kubeconfigSecret`(이미 만든 Secret의 이름) 하나가 새로 늘었다. 마운트 경로는 `flags.kubeconfig` 파일 경로의 디렉터리이고 Secret의 키 이름은 그 파일 이름이다. 둘은 서로를 요구한다(스키마 `if/then`). 기존 규칙(`kubeconfig`가 있으면 `kube-external-subnet`과 `kube-nad-namespace`)은 그대로다.
- 차트는 Secret을 만들지 않고 값 파일에 자격 증명이 없다. 운영자가 `kubectl create secret generic <이름> --from-file=<키>=<kubeconfig 파일>`로 릴리스 네임스페이스에 만든다.
- 렌더 결과: Secret 볼륨을 읽기 전용으로 `flags.kubeconfig`의 디렉터리에 마운트하고, 키 하나만 투영하며 파일 모드는 `0440`(파드는 `runAsNonRoot`와 `fsGroup 65532`)이다. 환경 변수로 자격 증명을 넘기지 않는다. `scripts/guard.sh config`(R16)가 이를 검사한다. 경로는 `/tmp`와 `/var/lib/vws` 안이면 거절한다.
- **목표는 선택지 2(인클러스터 ServiceAccount)다.** 이를 위해 vws-network가 `-kubeconfig` 없이도 Kube-OVN을 쓰게 해야 하고, 이는 별도 요청이다. 그때 이 키와 볼륨은 지운다. 선택지 1은 장기 자격 증명이 Secret에 남고 회전이 수동이며 Secret 읽기와 파드 exec 권한 보유자가 자격을 얻는다는 한계를 가진 임시 수단이다.
- 확인 필요: 실제 클러스터에서 이 kubeconfig로 Kube-OVN CRD에 쓰는지, 그 kubeconfig가 `docs/rbac.md`의 network 권한 이상을 갖지 않게 만드는 방법(차트가 통제하지 못한다), `0440`과 `fsGroup`으로 파드가 파일을 읽는지(T1 이상).

## 확인 필요

- **compute 플래그 3개(`-hypervisor`, `-vm-namespace`, `-image-namespace`)는 main에 없다.** vws-compute#23이 머지되기 전에 `global.pendingFlags: true`로 켜면 현재 이미지가 알 수 없는 플래그로 시작을 거절한다. 기본은 꺼짐이다.
- compute의 환경 변수 폴백(`VWS_VM_NAMESPACE` 등)은 이 차트가 쓰지 않는다. 플래그만 넘긴다.
- 실제 클러스터에서 렌더한 인자로 서비스를 띄워 보지 않았다(정적 렌더 검사만).

## 네임스페이스를 남기는 이유

차트가 만든 네임스페이스에는 `helm.sh/resource-policy: keep`을 단다. `helm uninstall`이 VM, 디스크(PVC), 스냅샷이 든 네임스페이스를 같이 지우면 데이터를 잃기 때문이다(데이터 보호). 대가는 정리를 사람이 해야 한다는 것이다. 완전히 지울 때는 데이터를 확인한 뒤 `kubectl delete namespace <이름>`을 직접 실행한다. 릴리스 네임스페이스(`release`)는 차트가 만들지 않는다.

## 네임스페이스 이름을 바꾸려면

`values.yaml`의 `namespaces`만 바꾼다. 템플릿, Role, 플래그가 모두 그 값을 따라간다. `scripts/guard.sh config`가 이름을 바꿔 렌더하고 인자와 Role이 같이 바뀌는지, 옛 이름이 남지 않는지 확인한다.
