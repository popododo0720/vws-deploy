# 내부자 위험 가드를 다른 레포에 복사하기

설계는 https://github.com/popododo0720/vws-deploy/issues/33 이다. 이 레포는 private이고 무료 플랜이라 브랜치 보호와 ruleset을 쓸 수 없다. 그래서 CI와 규칙이 대신한다. PR 검사는 잘못된 머지를 막는 데 쓰고, main 푸시 감시는 이미 일어난 일을 알리기만 한다(차단하지 않는다).

## 파일 세 개

| 파일 | 하는 일 |
| --- | --- |
| `.github/workflows/repo-guard.yml` | PR에서만 돈다: base가 기본 브랜치인지, 본문에 `Part of #N`(N은 1 이상, HTML 주석 `<!-- -->`과 코드펜스 밖에 있어야 함)이 있고 PR 본문과 커밋에 닫는 키워드(`Closes/Fixes/Resolves` 뒤에 `#N`, `owner/repo#N` 또는 이슈·PR 전체 URL)가 없는지, AGENTS.md에 가드 섹션이 있는지 |
| `.github/workflows/direct-push-watch.yml` | `push` to main에서만 돈다: 새로 들어온 첫 부모 커밋마다 연결된 PR이 있는지, main 히스토리가 다시 쓰이지 않았는지. PR 실행에 건너뛴(skipped) 체크가 남지 않도록 별도 파일이다 |
| `scripts/guard-agents.sh` | 위 검사의 실제 코드. 레포 경로를 쓰지 않는다. `selftest`가 나쁜 입력과 정상 입력를 직접 넣어 모두 실패하는지 본다 |

## 복사 절차

1. 워크플로 두 파일과 스크립트를 같은 경로에 그대로 복사하고 `scripts/guard-agents.sh`에 실행 권한을 둔다.
2. 대상 레포 AGENTS.md에 `## Insider-risk guardrails` 섹션(번호 1~7)을 넣는다. 본문은 이 레포 AGENTS.md의 같은 섹션을 복사한다. 헤더 문자열과 번호가 맞아야 `agents` 검사가 통과한다.
3. 기본 브랜치가 `main`이 아니면 `direct-push-watch.yml`의 `branches: [main]` 한 줄만 바꾼다. 나머지는 `github.event.repository.default_branch`를 쓴다. 스크립트 쪽은 `GUARD_MAIN_BRANCH`로 바꾼다.
4. 레포에 이미 CI가 있어도 이 워크플로는 별도 파일로 둔다. 같은 일을 하는 job을 두 곳에 두지 않는다.
5. 로컬 확인: `scripts/guard-agents.sh selftest`, `scripts/guard-agents.sh agents`. 레포 가드(`guard.sh` 같은 것)가 있으면 `agents`와 `selftest`를 거기서 부르게 한다. 이 레포는 `scripts/guard.sh agents`가 한다.

환경 변수: `GUARD_MAIN_BRANCH`(기본 `main`), `GUARD_HEADING`, `GUARD_ITEMS`(기본 7), `GUARD_RETRY_SECONDS`(push-watch의 PR 연결 재시도 간격, 기본 5).

## 한계

- 이 검사는 같은 PR에서 스크립트나 워크플로를 고쳐 우회할 수 있다. 서버측 필수 체크가 아니다. 머지하는 쪽(`mergepr.sh` 등)이 base와 head CI를 확인해야 한다.
- push 감시(`direct-push-watch.yml`)는 사후 탐지다. PR 없이 들어간 커밋을 막지 못하고 실패로 표시할 뿐이다. 커밋과 PR의 연결은 머지 직후 몇 초 늦을 수 있어 3번 재시도한다.
- 작성 봇, 검증 봇, 머지자가 같은 GitHub 계정이면 "작성자와 머지자가 다르다"는 검사는 의미가 없어 넣지 않았다. 설계 원칙 1과 5는 프로세스 규칙이다.
- GitHub 쪽 실행 결과는 PR의 CI로만 확인한다. `direct-push-watch.yml`은 main에 머지된 뒤에야 처음 실행된다.

## guard-agents.sh 정본과 동일성 검사

- 정본은 vws-deploy의 `scripts/guard-agents.sh`이고 sha256을 `scripts/guard-agents.sh.sha256`(sha256sum 형식)에 고정한다. 다른 레포는 두 파일을 바이트 그대로 복사하고 고치지 않는다.
- 각 레포의 repo-guard는 `scripts/guard-agents.sh identity`로 자기 사본의 sha256이 고정값과 같은지 본다. 다르면 실패한다.
- 정본을 바꿀 때: vws-deploy PR에서 스크립트를 고치고 `(cd scripts && sha256sum guard-agents.sh > guard-agents.sh.sha256)`로 해시를 같은 커밋에 갱신한다. vws-deploy의 `scripts/guard.sh agents`(R13)가 둘이 어긋나면 실패한다. 머지 뒤 각 레포에 두 파일을 그대로 복사하는 PR을 연다.
- 한계: 해시 파일도 레포 안에 있으므로 두 파일을 함께 고친 사본은 이 검사를 통과한다. 이것은 실수로 생긴 차이를 잡는다. 의도적인 변경까지 막으려면 CI가 vws-deploy main의 해시를 직접 읽어야 하고, 그러려면 private 레포를 읽는 토큰이 필요하다(확인 필요).
- `rules [FILE] [HEADING]`(별칭 `judgment`)은 규칙표의 모든 행에 강제 수단이 있는지 보고 Judgment 행 수를 출력한다. 제목은 인자, `GUARD_RULES_HEADING`, 또는 파일에 있는 `## Rules`/`## 규칙표` 순으로 정한다.
- `archtest [FILE] [HEADING]`은 아키텍처 테스트를 부르는 통합 명령이다. `rules`의 Judgment 행 수 줄(`rules: 50 rows, 2 Judgment`)을 먼저 출력하고, `go test -v -count=1 ${GUARD_ARCHTEST_PKG:-./internal/archtest/...}`를 돌린다. 테스트가 실패해도 CI 로그에 행 수가 남는다. 디렉터리가 없는 레포는 그 사실을 출력하고 통과한다. 각 레포 CI는 `go test ./internal/archtest/...` 대신 `scripts/guard-agents.sh archtest`를 부른다(vws-network#99).

- `verify-gate HEAD LABELS [FILE]`: 판정은 `@@comment-by:` 댓글에서만 읽는다(PR 본문 제외). `GUARD_VERIFY_EXCLUDE`(PR 작성자, head를 푸시한 계정)의 댓글과 라벨은 세지 않는다. 예외는 `GUARD_VERIFY_IDENTITY`(vws-verify 로그인)뿐이다. 알려진 공백: 모든 봇이 한 계정을 쓰면 작성자와 vws-verify를 구별할 수 없다. 워크플로는 이 스크립트가 main에 들어간 뒤 따로 들어온다(#261).
