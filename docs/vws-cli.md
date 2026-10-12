# `vws` 명령

배포와 운영을 한 명령으로 하는 CLI다. 설계는 vws-deploy#43, 이 문서는 골격(vws-deploy#52)만 다룬다. 명령 본체(`init`, `apply`, `preflight` 등)는 하위 이슈에서 하나씩 들어온다.

```
scripts/vws [flags] <command> [args]
make vws-<command> ARGS="<flags>"     # 같은 명령의 별칭
make vws-test                          # 하네스(가짜 kubectl, helm)
```

## 구조

- `scripts/vws`: 디스패처. 플래그를 읽고 `scripts/vws.d/<command>.sh`를 불러 `vws_cmd_main`을 실행한다. 이름은 `[a-z][a-z0-9-]*`만 받는다.
- `scripts/vws.d/<command>.sh`: 명령 하나. 첫 줄 근처에 `# summary: <한 줄>`, 필요한 도구는 `VWS_NEED=(kubectl helm)`, 본체는 `vws_cmd_main`. `help`는 `# summary:`를 읽어 목록을 만든다.
- `scripts/vws.d/lib/common.sh`: 출력, 종료 코드, 도구 확인, 확인(`--yes`), kubectl과 helm 래퍼.
- 명령은 `kubectl`과 `helm`을 직접 부르지 않고 `vws_kubectl`, `vws_helm`을 쓴다. 래퍼가 `--context`, `--namespace`를 붙이고 `--dry-run`에서는 변경 호출을 실행하지 않고 `DRYRUN ...`을 stderr에 찍는다. 읽기 동사 목록에 없는 동사는 변경으로 본다.
- 비밀 값은 `vws_emit` 출력에 넣지 않는다. 이름과 개수만 보고한다.
- CRD API 그룹 상수는 CRD가 들어오는 vws-deploy#53에서 한 곳에 정의한다. 이 골격은 CR을 읽지 않으므로 아직 없다.

## 공통 플래그

| 플래그 | 뜻 |
| --- | --- |
| `--context NAME` | kubeconfig 컨텍스트(kubectl `--context`, helm `--kube-context`) |
| `-n`, `--namespace N` | 네임스페이스 |
| `-o text\|json` | 출력 형식. json은 끝에 한 번 `{"command","status","checks":[{"level","name","message"}]}` |
| `--dry-run` | 변경 kubectl, helm 호출을 실행하지 않는다 |
| `-y`, `--yes` | 파괴적 명령의 확인 |
| `--strict` | WARN도 종료 코드 1 |
| `-h`, `--help` | `help`와 같다 |

플래그는 명령 앞뒤 어디든 둘 수 있다. 명령 이름 앞의 알 수 없는 플래그는 종료 코드 2이고, 이름 뒤의 것은 명령이 받는다. `--`가 오면 그 뒤는 전부 명령 인자다.

## 종료 코드

| 코드 | 뜻 |
| --- | --- |
| 0 | 모두 OK(WARN은 `--strict`가 아니면 0) |
| 1 | FAIL이 있다(또는 `--strict`에서 WARN) |
| 2 | 사용법 오류: 알 수 없는 명령이나 플래그, 값 없는 플래그, `--yes` 없는 파괴적 명령 |
| 3 | 환경 문제: 필요한 도구가 없다, 클러스터에 닿지 않는다 |

## 하네스 (`tests/vws/run.sh`)

클러스터와 네트워크에 닿지 않는다. `tests/fixtures/bin/`의 가짜 `kubectl`, `helm`을 PATH 앞에 두고 `tests/fixtures/commands/`의 시험용 명령으로 디스패처와 공통 라이브러리를 시험한다.

- 가짜 도구는 호출마다 `$VWS_FAKE_LOG`에 `도구<TAB>동사<TAB>인자`를 남기고, `tests/fixtures/<도구>/<동사>_<리소스>.json`을 그대로 돌려준다. 파일이 없으면 큰 소리로 실패한다(계획하지 않은 호출로 통과하지 않는다).
- `assert_no_changes`는 로그에 변경 동사가 있는지 본다. 그 동사 목록은 `common.sh`와 일부러 따로 둔다(검사 대상 코드를 믿지 않는다). `assert_changes`가 대조군이다.
- 시험하는 것: 종료 코드 0, 1, 2, 3, json 출력의 `jq` 파싱, 읽기 시나리오의 변경 호출 0건, `--dry-run`, `--context`, `-n`의 전달, `--yes` 없는 파괴적 명령, `--strict`, 로그가 변경 호출을 잡아내는지.
- 실제 `kubectl`, `helm`의 출력 형식과 실제 클러스터는 시험하지 않는다. 명령 본체가 들어올 때 각 명령의 fixture를 더한다.

`scripts/guard.sh vws`(R17)는 위 하네스와 shellcheck를 돌리고, 명령 파일마다 시험 파일과 이 문서의 항목이 있는지, 명령이 kubectl이나 helm을 직접 부르지 않는지 본다. 깨진 사본으로 이 검사가 실제로 실패하는지도 시험한다.

## 명령

### vws help

명령 목록과 공통 플래그, 종료 코드를 보인다. `-o json`이면 명령마다 한 항목. 클러스터 도구가 필요 없다.

### vws render

`vws render <CR.yaml> <OUT.yaml>`: `VWSCluster` CR 하나를 `charts/vws`의 values 오버레이로 바꾼다. 클러스터를 바꾸지 않는다. 같은 CR이면 같은 바이트가 나온다(`sort_keys`, 시각이나 난수 없음). 순서: CR 검증(CRD 스키마, 이름 `vws`, 알 수 없는 필드, 비밀처럼 보이는 필드나 플래그 이름) → `profiles/<spec.profile>.yaml` 위에 CR 값 반영 → `helm template -f`로 `values.schema.json` 재검증 → `OUT`에 쓴다. 잘못된 CR은 종료 코드 2이고 `OUT`을 만들지 않는다. `OUT`이 `-`이면 YAML만 stdout에 쓴다(`-o json`과 함께 쓰지 않는다). 매핑은 `docs/vwscluster.md`.
