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

### vws preflight

`vws preflight --profile single-node|small|ha`: 설치 전 검사. 클러스터에 쓰지 않는다(읽기 동사만). 검사 함수는 `scripts/vws.d/lib/checks.sh`에 하나씩 있고 status, doctor가 같은 함수를 쓴다(#43 4절).

| 검사 이름 | 방법 | 실패 |
| --- | --- | --- |
| `tools` | `helm version --short`가 `HELM_VERSION`으로 시작 | WARN |
| `api` | `kubectl version`, 현재 컨텍스트 이름 출력 | 응답 없음은 종료 코드 3 |
| `k8s-minor` | 서버 마이너 = `KUBERNETES_VERSION` 마이너 | WARN |
| `install-rights` | `kubectl auth can-i create`로 네임스페이스, CRD, 클러스터/네임스페이스 RBAC, 차트가 만드는 종류 | FAIL과 부족한 동사/리소스 |
| `nodes` | Ready 노드 ≥ 프로필 최소(1/3/6) | FAIL |
| `node-resources` | Ready 노드 allocatable 합 ≥ 프로필 requests×replicas 합(`profiles/<p>.yaml`, 없으면 차트 기본) | FAIL |
| `default-storageclass` | 기본 StorageClass 있음 | FAIL |
| `ceph-block`, `ceph-block-snapshot` | StorageClass와 VolumeSnapshotClass `ceph-block` | 번들 전이면 WARN |
| `crd-<group>` | 번들 CRD 그룹(kubevirt.io, cdi.kubevirt.io, kubeovn.io, k8s.cni.cncf.io, snapshot.storage.k8s.io)이 Established | 없으면 WARN, Established가 아니면 FAIL |
| `kubevirt-phase`, `cdi-phase` | KubeVirt/CDI CR의 phase Deployed | 없으면 WARN, 다른 phase면 FAIL |
| `kernel-indirect` | `kubevirt.io/schedulable` 노드 존재로 간접 확인(`--node-probe` 없음, 결정 3) | WARN |
| `release` | helm 릴리스 `vws`가 `pending-*`/`failed`면 | FAIL |
| `source` | 체크아웃이면 `guard.sh bundle`, `recovery`, `config` | FAIL. `VWS_SOURCE_CHECKS=0`이면 건너뜀(하네스) |

FAIL이 하나라도 있으면 1, `--strict`이면 WARN도 1, 평가 불가면 3. 하네스 `tests/vws/preflight.test.sh`는 `tests/fixtures/preflight/ok`를 시나리오마다 한 파일씩 바꿔 검사별 통과/실패를 본다.

### vws init

`vws init [--from FILE]`: `VWSCluster` CR을 저장한다. `--from`이 없으면 stdin으로 프로필, 스토리지 드라이버, StorageClass(`existing`일 때만), 이미지 태그를 묻고 같은 CR을 만든다. 검증은 `vws render`와 같은 코드(`lib/render.py`)이고 잘못되면 종료 코드 2로 아무것도 저장하지 않는다. 클러스터의 CR과 `spec`이 같으면 `unchanged`(변경 호출 0건). 워크로드는 건드리지 않는다. 저장은 `kubectl apply --server-side --field-manager=vws`.

### vws apply

`vws apply [--unattended]`: CR → render → 상태 비교 → preflight → 비밀 → 번들 → helm → status. CR의 `status.appliedValuesHash`(렌더한 values의 sha256)와 `observedGeneration`이 지금 값과 같으면 `unchanged`로 끝난다(변경 호출 0건). 번들은 `build/platform/*.yaml`을 파일 이름 순서(=`scripts/platform.sh`의 번호)대로 server-side apply하고 파일마다 CRD Established, Deployment Available을 기다린다. 이어 `helm upgrade --install vws --atomic --wait`, 끝으로 status 하위 리소스를 쓴다. helm이 실패하면 status를 쓰지 않고 1. `--dry-run`은 변경 호출을 출력만 한다. 비밀: 지금 차트는 설치기가 만들 Secret이 없어 이 단계는 아무것도 만들지 않는다. preflight: `lib/checks.sh`(#55)의 api, nodes, storage, release 검사를 돌리고 FAIL이면 아무것도 바꾸지 않고 1. 동시 실행 잠금(Lease `vws-operator-lock`)은 아직 없다(확인 필요, 후속).

### vws installer

`vws installer finish`: 설치용 임시 cluster-admin 바인딩(`vws-installer-temp`)을 지우고 정말 사라졌는지 확인한다. 설계와 근거는 `docs/installer.md`. 확인은 `kubectl auth can-i`(SelfSubjectAccessReview, 파드 밖이면 `--as=system:serviceaccount:vws:vws-installer` 즉 SubjectAccessReview)로 `* *`, `create clusterrolebindings`, `create clusterroles`, `delete namespaces`, `create customresourcedefinitions`, `get secrets`가 모두 `no`이고, 운영용 `get vwsclusters`, `patch vwsclusters/status`는 `yes`인지 본다. 하나라도 어긋나면 1, 답을 못 읽으면 3. 이미 지워졌으면 `unchanged`이고 확인은 다시 한다(멱등).

### vws status

`vws status`: 설치된 상태를 읽기만 한다(변경 동사 0건). 검사: api, `cluster-cr`(프로필, `metadata.generation`과 `status.observedGeneration`), `applied-hash`(지금 CR이 렌더하는 values의 sha256과 `status.appliedValuesHash`; `vws apply`와 같은 계산), `release`와 `release-drift`(helm 리비전과 상태, 릴리스 values와 CR 렌더 결과 비교), `bundle-kubevirt`, `bundle-cdi`(운영자 CR이 보고하는 버전과 `scripts/versions.env`), `service-<이름>`(`charts/vws/values.yaml`의 서비스마다 Deployment/StatefulSet ready/desired), `pdb`. 드리프트는 WARN이라 `--strict`에서 1.

**자격 증명.** helm은 릴리스를 Secret에 둔다. `vws installer finish` 뒤 운영 역할(`vws-operator`)은 Secret을 읽지 못하므로 `helm status/get`이 forbidden으로 실패한다. 그래서 status는 VWSCluster status와 워크로드를 1차 자료로 쓰고, 릴리스 리비전과 릴리스 드리프트는 읽을 수 있을 때만 덧붙인다. 읽을 수 없으면 `WARN release ... cannot read Secrets ...; For them run vws status with your own kubeconfig`를 내고 나머지 검사는 그대로 돈다(FAIL 아님). 운영 역할로 `--strict`를 쓰면 이 WARN 때문에 1이다.

### vws doctor

`vws doctor`: status 전부 + `restarts`(CrashLoopBackOff 또는 재시작 5회 이상), `pending-pvc`(모든 네임스페이스), `vws-crd`(VWSCluster CRD Established), `crd-<그룹>`과 `kubevirt-phase`, `cdi-phase`(preflight와 같은 `checks.sh` 함수, doctor 모드라 없으면 FAIL), `read-rights`(status에 필요한 읽기 권한; `get secrets`만 없으면 운영 역할로 보고 OK), `ceph`(CR의 storage driver가 rook-ceph일 때 CephCluster health), `pending-flags`(CR이 렌더하는 `global.pendingFlags`와 실제 컨테이너 인자에 pendingFlags가 있는지, #38). 모든 FAIL 메시지는 `next: <다음 행동 한 줄>`로 끝난다. 읽기 전용.

### vws reconfigure

`vws reconfigure (--from FILE | --set path=value [--set ...])`: VWSCluster를 고치고 `vws apply`를 돌린다(#43 3절). CR이 유일한 출처라 values 파일을 직접 고치는 경로는 없다.

- `--set`: `spec` 아래 경로(`spec.`은 생략 가능), 값은 YAML로 읽는다(`3`은 숫자, `true`는 불리언). 여러 번 쓸 수 있다. `--from`: CR 전체. 둘은 함께 쓰지 않는다.
- 검증(무엇이든 쓰기 전): `lib/render.py`(CRD 스키마, 비밀 모양 필드 거절) → `helm template`(values.schema.json) → `scripts/check_render.py`(복구 불변식). 하나라도 실패하면 종료 코드 2이고 클러스터에 아무것도 쓰지 않는다.
- 변경 요약: 바뀐 spec 잎마다 `OK change spec.x: 이전 -> 새 값`. 비밀 모양 필드는 `(hidden)`, `secretName`은 이름이라 그대로(지금 render.py는 `spec.secrets`를 받지 않아 거절된다).
- spec이 클러스터의 CR과 같으면 `unchanged`, 쓰는 호출 0건.
- `--dry-run`: 요약만 출력하고 CR 저장도 apply도 하지 않는다.
- 그 밖에는 CR을 server-side apply(`--field-manager=vws`)로 저장하고 `vws apply --unattended`를 돌려 그 결과 줄을 같은 출력(`-o json` 포함)에 합친다. apply가 실패하면 1. CR은 이미 저장된 상태로 남는다(다음 `vws apply`나 되돌리는 `reconfigure`로 맞춘다, 확인 필요).
