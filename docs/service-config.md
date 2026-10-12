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
| network | (기본 없음) `kubeconfig` | `-kubeconfig=<경로>` | 있으면 Kube-OVN 데이터플레인, 없으면 fake | `cmd/vws-network/main.go:25` |
| network | `kube-external-subnet` | `-kube-external-subnet=<이름>` | `-kubeconfig`와 함께 필수 | `main.go:26` |
| network | `kube-nad-namespace: {namespace: nad}` | `-kube-nad-namespace=<nad>` | `-kubeconfig`만 있으면 `vws-system`이라 차트는 항상 넘긴다 | `main.go:27,30-31` |
| network | `kube-snat` | `-kube-snat=true` | 꺼짐. 외부 서브넷 필요 | `main.go:28` |

- 스키마가 `kubeconfig`가 있으면 `kube-external-subnet`과 `kube-nad-namespace`를, `kube-snat`이 있으면 `kube-external-subnet`을 요구한다(vws-deploy#30). 빠진 값으로 렌더하면 거절된다.
- 기본값에서 network에는 플래그가 없다. Kube-OVN 데이터플레인을 켜려면 values에 `kubeconfig`, `kube-external-subnet`, `kube-nad-namespace`를 넣는다(`scripts/guard.sh config`가 이 형태를 시험한다). network는 `-kubeconfig`가 비면 인클러스터 자격을 쓰지 않고 fake로 가므로, 차트가 kubeconfig 파일을 볼륨으로 마운트하는 일은 아직 없다(확인 필요: 마운트 방법).
- compute의 `-nad-namespace`(`VWS_NAD_NAMESPACE`, 기본 `vws-nad`)는 예약뿐이고 NAD를 쓰는 코드가 없어 넘기지 않는다.

## 확인 필요

- **compute 플래그 3개(`-hypervisor`, `-vm-namespace`, `-image-namespace`)는 main에 없다.** vws-compute#23이 머지되기 전에 `global.pendingFlags: true`로 켜면 현재 이미지가 알 수 없는 플래그로 시작을 거절한다. 기본은 꺼짐이다.
- compute의 환경 변수 폴백(`VWS_VM_NAMESPACE` 등)은 이 차트가 쓰지 않는다. 플래그만 넘긴다.
- **차트의 컨테이너 포트와 프로브는 서비스 코드와 맞지 않는다.** 차트는 `http` 8080에서 `/healthz`, `/readyz`를 본다. 서비스의 `-listen` 기본값은 `127.0.0.1`의 gRPC 주소이고(compute `main.go:33`, storage `main.go:40`, network `main.go:22`) HTTP 프로브를 구현하는지는 확인하지 못했다. network는 `-db`와 `-pool`이 필요하다(`main.go:34`의 사용법 문구). 이 PR은 `-listen`, `-db`, `-pool`을 다루지 않는다. 별도 이슈로 다뤄야 한다.
- 실제 클러스터에서 렌더한 인자로 서비스를 띄워 보지 않았다(정적 렌더 검사만).

## 네임스페이스를 남기는 이유

차트가 만든 네임스페이스에는 `helm.sh/resource-policy: keep`을 단다. `helm uninstall`이 VM, 디스크(PVC), 스냅샷이 든 네임스페이스를 같이 지우면 데이터를 잃기 때문이다(데이터 보호). 대가는 정리를 사람이 해야 한다는 것이다. 완전히 지울 때는 데이터를 확인한 뒤 `kubectl delete namespace <이름>`을 직접 실행한다. 릴리스 네임스페이스(`release`)는 차트가 만들지 않는다.

## 네임스페이스 이름을 바꾸려면

`values.yaml`의 `namespaces`만 바꾼다. 템플릿, Role, 플래그가 모두 그 값을 따라간다. `scripts/guard.sh config`가 이름을 바꿔 렌더하고 인자와 Role이 같이 바뀌는지, 옛 이름이 남지 않는지 확인한다.
