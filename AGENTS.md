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
| R11 | 제어 서비스의 쿠버네티스 API 권한은 values.yaml의 서비스별 `rbac`뿐이다. 와일드카드와 escalate, bind 같은 동사를 쓰지 않는다. 네임스페이스 리소스의 규칙에는 `namespace`를 적어 그 네임스페이스의 Role로만 주고, ClusterRole에는 클러스터 범위 리소스만 둔다. 리더 선출용 Lease는 자기 이름의 것만 쓴다 | `charts/vws/values.yaml` `charts/vws/values.schema.json` `charts/vws/templates/control-plane.yaml` | `scripts/guard.sh recovery` |
| R12 | 플랫폼 번들(KubeVirt, CDI, Multus, Kube-OVN, Rook-Ceph)의 버전은 `scripts/versions.env`에만 쓰고(로컬 랩의 k3s 포함), 필요한 커널 모듈을 문서에 적는다 | `scripts/versions.env` `docs/platform-bundle.md` | `scripts/guard.sh bundle` |
| R13 | PR은 main을 대상으로 하고 본문에 `Closes #N`이 있으며, main에 PR 없이 들어온 커밋은 CI가 알린다. AGENTS.md는 Insider-risk guardrails 1~7을 갖는다 | `.github/workflows/repo-guard.yml` `.github/workflows/direct-push-watch.yml` `scripts/guard-agents.sh` | `scripts/guard.sh agents` |
| R14 | 서비스 설정은 values.yaml의 `services.*.flags`뿐이고 네임스페이스 이름은 `namespaces` 한 곳뿐이다. 렌더된 args는 값과 같고, 네임스페이스 플래그를 받는 서비스는 그 네임스페이스에 Role이 있다 | `charts/vws/values.yaml` `charts/vws/values.schema.json` `charts/vws/templates/_helpers.tpl` | `scripts/guard.sh config` |
| R15 | 모든 서비스의 `-listen` 포트, 컨테이너 포트, 프로브 포트는 서비스의 `port` 하나다. 프로브는 tcpSocket 또는 grpc이고 HTTP가 아니다. network는 `-db`와 `-pool`을 넘긴다 | `charts/vws/values.yaml` `charts/vws/templates/_helpers.tpl` `scripts/check_render.py` | `scripts/guard.sh config` |
| R16 | network kubeconfig는 operator가 만든 Secret을 읽기 전용으로 마운트하는 임시 방법뿐이다. 차트는 Secret을 렌더하지 않고 자격 증명 값을 values나 env에 두지 않는다 | `charts/vws/values.schema.json` `charts/vws/templates/control-plane.yaml` `charts/vws/templates/_helpers.tpl` | `scripts/guard.sh config` |
| R17 | `scripts/vws`의 모든 명령은 `scripts/vws.d/<이름>.sh` 한 파일이고 `# summary:` 줄, `vws_cmd_main`, `tests/vws/<이름>.test.sh`, `docs/vws-cli.md`의 `### vws <이름>` 항목을 가진다. 명령은 kubectl과 helm을 직접 부르지 않고 `vws_kubectl`, `vws_helm`만 쓴다(`--dry-run`이 변경 호출을 막는다). 하네스는 가짜 kubectl과 helm으로 돌고 클러스터에 닿지 않는다 | `scripts/vws` `scripts/vws.d/lib/common.sh` `tests/vws/run.sh` | `scripts/guard.sh vws` |
| R18 | VWS 전용 CRD는 `VWSCluster` 하나뿐이고(#2 결정의 예외, `docs/vwscluster.md`), API 그룹은 그 CRD의 `spec.group` 한 곳에만 쓴다(예제 CR의 `apiVersion`은 가드가 대조). CR 스키마에 비밀처럼 보이는 필드 이름이 없고 CR은 Secret 이름만 담는다 | `charts/vws/crds/vwsclusters.yaml` `scripts/vws.d/lib/render.py` | `scripts/guard.sh crd` |
| R19 | 프로필은 `profiles/<이름>.yaml`의 값 파일이고 세 프로필은 같은 키를 갖는다. 프로필마다 lint, template, kubeconform, check_render가 통과하고, 같은 CR의 `vws render`는 같은 바이트다 | `profiles/` `platform/rook-ceph-cluster.single-node.values.yaml` `platform/rook-ceph-cluster.ha.values.yaml` | `scripts/guard.sh profiles` |

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

## Insider-risk guardrails

봇도 내부자 위험이다. 아래는 예외 없는 규칙이다.

1. **검증 독립**: 중요 결과(머지, 보안, 데이터)는 작성한 봇/모델이 스스로 검증하지 않는다. 검증은 작성 봇이 아닌 다른 봇(또는 사람)이 한다. (프로세스 규칙, CI로 강제 불가)
2. **기록**: 모든 변경은 이슈 → PR(`Closes #N`) → 커밋으로 남긴다. 작업 로그는 PR 본문/이슈 코멘트에 사람이 읽을 수 있게 쓴다. force-push·히스토리 재작성 금지(사용자 승인 시 예외).
3. **실패 테스트**: 새 기능은 정상 경로만 테스트하지 않는다. 전원 차단/재시작, 멱등 재실행, 악성·경계 입력 케이스를 최소 1개씩 포함하거나 불가 사유를 PR에 적는다.
4. **최소 권한**: main 직접 푸시 금지. 모든 변경은 main에서 딴 독립 브랜치의 단일 PR. 스택 PR 금지. 로컬 머지 금지. 토큰은 해당 레포 범위만. RBAC은 서비스 코드가 실제 호출하는 리소스만, 가능하면 Role(네임스페이스 범위).
5. **감사 분리**: 작성 봇 ≠ 검증 봇 ≠ 머지 판단. 작성 봇은 자기 PR을 머지하지 않는다. 머지는 base==main 이고 head 커밋 CI가 전부 success일 때만.
6. **격리·비상정지**: 봇/에이전트 즉시 중단은 사용자 요청 또는 오케스트레이터가 해당 봇 세션 중단 + 토큰 폐기(`gh auth`/PAT revoke). 위험 작업은 샌드박스(박스/임시 클러스터)에서만. 운영 클러스터 접근 금지.
7. **사고 공개**: 규칙 위반·사고는 숨기지 않고 이슈에 `원인 / 영향 / 조치 / 재발방지`로 기록한다 (라벨 `incident`).

이 레포의 서버측 보호는 없다(private, 무료 플랜). 1, 5, 6은 프로세스 규칙이고, 2와 4의 일부는 `.github/workflows/repo-guard.yml`이 PR의 base와 `Closes #N`을 검사하고 `.github/workflows/direct-push-watch.yml`이 main에 PR 없이 들어온 커밋을 사후에 알린다. 차단이 아니라 탐지다.

## 명령

지원하는 진입점은 두 개다.

- `make check` — helm lint, kubeconform, 플랫폼 번들 렌더 검사, 이 파일의 검사
- `helm template vws charts/vws --namespace vws` — 적용하지 않고 매니페스트를 본다
