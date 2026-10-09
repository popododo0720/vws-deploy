# vws-deploy

VWS 프라이빗 클라우드의 배포 저장소다. 제어부만 Kubernetes에 올리고, 하이퍼바이저 노드 에이전트는 호스트의 systemd로 띄운다. 이 저장소는 클러스터에 적용하지 않는다.

## 제어부

서비스 목록은 `charts/vws/values.yaml`의 `services` 맵이 유일하다. 서비스를 더하려면 키 하나를 추가한다.

```yaml
services:
  billing:
    external: true
    state:
      size: 10Gi
```

`state`가 있으면 복제본 하나의 StatefulSet과 지워지지 않는 볼륨이 생긴다. `state`가 없으면 복제본 둘 이상의 Deployment가 생긴다. startup, liveness, readiness, PodDisruptionBudget, `restartPolicy: Always`는 값으로 끌 수 없다.

```bash
scripts/install-tools.sh
make check
helm template vws charts/vws --namespace vws
```

이미지는 `<global.registry>/<서비스>:<Chart.appVersion>`이다. 외부로 열 `external: true`인 서비스는 LoadBalancer가 된다. 상태 있는 워크로드는 기동 순서를 기다리지 않고 같이 올라와서, 전원 복구 때 순번 대기로 멈추지 않는다.

차트는 아이콘을 두지 않는다. `helm lint`가 아이콘을 권장한다고 알려도 로고 파일은 추가하지 않는다.

## 하이퍼바이저 에이전트

에이전트 목록은 `host/agents/` 아래 디렉터리 이름이다. 유닛 파일은 `host/systemd/vws-agent@.service` 하나다. 디렉터리 안의 `deps.conf`는 `[Unit]` 순서만 적을 수 있다.

```bash
host/install.sh --dry-run
sudo host/install.sh --control-plane https://compute.example.test --bundle /srv/vws-agents
```

바이너리 이름은 `vws-agent-<디렉터리>`다. 프로세스가 죽으면 systemd가 다시 띄우고, 부팅 때는 `vws-agents.target`이 붙는다. 에이전트 상태는 `/var/lib/vws/<디렉터리>`에 남는다. 인자로 에이전트 이름을 넘기는 설치 방법은 없다.

## 검사

`make check`가 로컬과 GitHub Actions에서 같은 검사를 돌린다. 렌더된 매니페스트는 `build/`에만 있고 커밋하지 않는다. 작업 규칙은 `AGENTS.md`에 있다.
