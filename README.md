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

차트는 아이콘을 두지 않는다. `helm lint`가 아이콘을 권장한다고 알려도 로고 파일은 추가하지 않는다.

## 검사

`make check`가 로컬과 GitHub Actions에서 같은 검사를 돌린다. 렌더된 매니페스트는 `build/`에만 있고 커밋하지 않는다. 작업 규칙은 `AGENTS.md`에 있다.
