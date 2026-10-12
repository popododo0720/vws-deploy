# 에이전트 규칙

이 저장소는 VWS 프라이빗 클라우드의 배포 산출물만 다룬다. 제어부는 Helm 차트 하나이고, 호스트에는 우리가 설치하는 프로세스가 없다. 설계는 https://github.com/popododo0720/vws-deploy/issues/2 다. 클러스터에 적용하는 명령은 없다.

## 규칙

| ID | 규칙 | 구조 | 검사 |
| --- | --- | --- | --- |
| R1 | 브랜치에서 작업하고 main이나 master에 푸시하지 않는다 | `.githooks/pre-push` | `scripts/guard.sh hooks` |
| R2 | 커밋 하나는 scripts/scopes의 한 스코프만 건드리고, 제목은 `scope: 요약`이다 | `scripts/scopes` | `scripts/guard.sh commits` |
| R4 | 제어부 워크로드는 startup, liveness, readiness, PodDisruptionBudget, restartPolicy Always를 갖고, state가 있으면 재시작 뒤에도 남는 볼륨을 갖는다 | `charts/vws/templates/control-plane.yaml` `charts/vws/values.schema.json` | `scripts/guard.sh recovery` |
| R5 | 제어 서비스를 추가하거나 고치는 곳은 values.yaml의 services 맵 하나다 | `charts/vws/values.yaml` `charts/vws/values.schema.json` | `scripts/guard.sh names` |
| R7 | 로고 파일과 상표 고지를 넣지 않는다 | - | `scripts/guard.sh assets` |
| R8 | 매니페스트 검사는 helm lint와 kubeconform이고, 클러스터에는 적용하지 않는다. 예외는 일회용 로컬 랩 `scripts/lab/`뿐이다 | `.github/workflows/ci.yml` | `scripts/guard.sh ci` |
| R9 | Cursor 클라우드 에이전트를 쓰지 않는다 | `AGENTS.md` | `scripts/guard.sh cursor` |
| R10 | 호스트에 우리 유닛을 두지 않는다. 저장소에 `.service`, `.target` 파일과 `host/`가 없고, 차트는 호스트 네임스페이스나 hostPath를 쓰지 않는다 | `charts/vws/templates/control-plane.yaml` | `scripts/guard.sh layout` |
| R11 | 제어 서비스의 쿠버네티스 API 권한은 values.yaml의 서비스별 `rbac`뿐이다. 와일드카드와 escalate, bind 같은 동사를 쓰지 않고, 리더 선출용 Lease는 자기 이름의 것만 쓴다 | `charts/vws/values.yaml` `charts/vws/values.schema.json` `charts/vws/templates/control-plane.yaml` | `scripts/guard.sh recovery` |
| R12 | 플랫폼 번들(KubeVirt, CDI, Multus, Kube-OVN, Rook-Ceph)의 버전은 `scripts/versions.env`에만 쓰고(로컬 랩의 k3s 포함), 필요한 커널 모듈을 문서에 적는다 | `scripts/versions.env` `docs/platform-bundle.md` | `scripts/guard.sh bundle` |

표의 구조 칸과 검사 칸은 실제 경로와 실제 명령을 가리킨다. 문장만 있는 규칙은 규칙이 아니다.

## 브랜치와 원자적 커밋

main에서 커밋하지 않는다. `git switch -c <branch>`로 갈라진 브랜치에서 고치고, 끝나면 풀 리퀘스트를 연다. main에 직접 푸시하지 않는다. pre-push 훅이 `refs/heads/main`과 `refs/heads/master`를 거절한다.

커밋 하나는 한 가지 변경이다. 어느 경로가 어느 스코프인지는 `scripts/scopes`만 본다. 이 파일에 스코프 목록을 다시 적지 않는다. 제목은 `chart: 서비스 키를 추가한다`처럼 `scope: 요약`이다. 한 커밋이 두 스코프를 건드리면 검사를 통과하지 못하므로 커밋을 나눈다.

훅은 `make check`가 `core.hooksPath`를 `.githooks`로 맞춘다. `--no-verify`로 훅을 건너뛰지 않는다.

## pstack 사용

설계와 검토는 아래 순서로 한다. 모델 선택은 사용자 쪽 pstack 규칙을 따르고, 이 파일에 모델 이름을 적지 않는다.

### architect

값을 받는 표면이나 렌더 방식을 바꾸기 전에 먼저 설계한다. 대상 파일은 `charts/vws/values.schema.json`, `charts/vws/templates/control-plane.yaml`, `charts/vws/templates/_helpers.tpl`다. 고른 안과 버린 안은 `docs/design/`에 남긴다. 구현이 그 설계와 어긋나면 이유를 같은 기록에 적는다.

### interrogate

위 파일을 고친 뒤에는 리뷰를 요청하기 전에 그 diff를 적대적으로 검토한다. 검토 결과는 자동으로 전부 적용하지 않는다. 동작이 깨지거나 복구 불변식이 빠지는 항목만 고친다.

### correct

같은 실수가 반복되면 문장을 더 쓰지 않는다. 먼저 잘못된 상태를 표현할 수 없게 구조를 바꾸고, 그다음 실패하는 검사를 넣고, 마지막에 이 표를 고친다. 검사는 그 실수가 들어 있는 입력에서 실제로 실패해야 한다. 표만 고친 수정은 규칙 변경이 아니다.

## 작업 방식 (poteto-mode)

- 간결하게 쓰고 코드는 가장 작은 변경으로 푼다. 추상화 계층을 늘리지 않고 지울 수 있으면 지운다.
- 코드를 쓰기 전에 데이터 모양을 먼저 정한다. 같은 실수가 두 번 나오면 문장 대신 구조나 검사로 막는다.
- 실제로 돌린 것만 검증했다고 쓴다. 돌리지 못한 것은 "미검증"으로 표시한다. `make check`는 컴파일 증거가 아니라 이 저장소의 T0 검사다.
- 커밋 메시지와 PR 설명은 짧은 평서문으로 쓴다. 근거 없는 주장과 만들어낸 링크를 넣지 않는다.
- Cursor 클라우드 에이전트(CloudAgent)를 쓰지 않는다. 사용량이 소진된 상태이고, 작업은 `gh`와 `git`으로 한다. `scripts/guard.sh cursor`가 `.cursor/`와 Cursor 에이전트 호출이 저장소에 들어오는 것을 막는다.

## 명령

지원하는 진입점은 두 개다.

- `make check` — helm lint, kubeconform, 플랫폼 번들 렌더 검사, 이 파일의 검사
- `helm template vws charts/vws --namespace vws` — 적용하지 않고 매니페스트를 본다
