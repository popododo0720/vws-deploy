# vws-deploy

VWS 프라이빗 클라우드의 배포 저장소다. Kubernetes 안에서 도는 제어부와 플랫폼 번들만 다룬다. 호스트에 우리가 설치하는 프로세스는 없다. 설계는 https://github.com/popododo0720/vws-deploy/issues/2 다. 이 저장소는 클러스터에 적용하지 않는다.

## 제어부

서비스 목록은 `charts/vws/values.yaml`의 `services` 맵이 유일하다. 서비스를 더하려면 키 하나를 추가한다.

```yaml
services:
  billing:
    external: true
    state:
      size: 10Gi
```

`state`가 있으면 복제본 하나의 StatefulSet과 지워지지 않는 볼륨이 생긴다. `state`가 없으면 복제본 둘 이상의 Deployment가 생긴다. startup, liveness, readiness, PodDisruptionBudget, `restartPolicy: Always`는 값으로 끌 수 없다. 이미지는 UID 65532로 동작하고, 루트 파일시스템은 읽기 전용이며, 쓸 수 있는 경로는 `/tmp`와 상태 볼륨뿐이다. `external: true`는 암호 없는 HTTP LoadBalancer다. 누가 그 주소에 닿는지는 클러스터 네트워크가 제한한다.

```bash
scripts/install-tools.sh
make check
helm template vws charts/vws --namespace vws
```

이미지는 `<global.registry>/<서비스>:<Chart.appVersion>`이다. 외부로 열 `external: true`인 서비스는 LoadBalancer가 된다. 상태 있는 워크로드는 기동 순서를 기다리지 않고 같이 올라와서, 전원 복구 때 순번 대기로 멈추지 않는다.

컨트롤러가 쿠버네티스 API를 쓰는 서비스는 `rbac` 규칙 목록을 가진다. 차트는 `namespace`가 있는 규칙을 그 네임스페이스의 Role과 바인딩으로, 없는 규칙을 ClusterRole과 바인딩으로 만들고(ClusterRole에는 클러스터 범위 리소스만 둔다), 자기 이름의 Lease만 쓰는 Role을 더해 리더 선출을 허용한다. 규칙의 근거는 `docs/rbac.md`다. `rbac`가 없는 서비스는 API 토큰을 마운트하지 않는다. 와일드카드와 `escalate`, `bind` 같은 동사는 스키마가 거절한다.

```yaml
services:
  compute:
    rbac:
      - namespace: vws-vms
        apiGroups: [kubevirt.io]
        resources: [virtualmachines]
        verbs: [get, list, create, patch, delete]
```

차트는 아이콘을 두지 않는다. `helm lint`가 아이콘을 권장한다고 알려도 로고 파일은 추가하지 않는다.

## 로컬 랩

`scripts/lab/`은 k3s, KubeVirt, CDI를 올리는 일회용 T1 랩이다. 이 저장소에서 클러스터에 적용하는 유일한 경로다. 순서는 `k3s-up.sh`, `virt-up.sh`, `vm-test.sh`, `down.sh`다. `/dev/kvm`이 없으면 `kvm-check.sh`가 `emulation`을 돌려주고 KubeVirt는 `useEmulation`으로 뜬다. `LAB_DRY_RUN=1`이면 상태를 바꾸는 명령을 실행하지 않고 출력만 한다. **미검증**. 박스 RAM이 모자라 실제 클러스터에서 돌려 보지 않았다.

## 검사

`make check`가 로컬과 GitHub Actions에서 같은 검사를 돌린다. 렌더된 매니페스트는 `build/`에만 있고 커밋하지 않는다. 작업 규칙은 `AGENTS.md`에 있다.
