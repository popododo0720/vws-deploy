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

`vws preflight --profile single-node|small|ha [--links FILE]`: 설치 전 검사. 클러스터에 쓰지 않는다(읽기 동사만). 검사 함수는 `scripts/vws.d/lib/checks.sh`에 하나씩 있고 status, doctor가 같은 함수를 쓴다(#43 4절).

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
| `node-network`, `node-ip/<노드>`, `node-links/<노드>` | 클러스터의 VWSCluster `spec.network.nodes`(#128, `lib/nodenet.py`). 노드 InternalIP가 management CIDR 안인지. `--links FILE`(`talosctl -n <모든 노드> get links -o json`)이 있으면 노드마다 호스트 망 인터페이스(`<nic>` 또는 `<nic>.<vlan>`)와 underlay NIC이 있고 MTU가 CR 값 이상인지 | IP 밖, 인터페이스 없음, MTU 부족, 노드가 links에 없음은 FAIL. `--links`가 없으면 `node-links` WARN(미확인). CR이 없거나 `nodes`가 없으면 OK |
| `vm-mtu`, `vm-mtu-set` | 터널 NIC MTU(`--links`에서 노드 중 최솟값, 없으면 `spec.network.underlayMTU`)가 VM MTU(`vmMTU`, 없으면 9001) + 터널 오버헤드 이상인지(#236). 9216 미만이면 OK 메시지에 권장값을 적는다 | 부족하면 WARN이고 낮춘 `spec.network.vmMTU`와 읽은 `underlayMTU`를 VWSCluster `vws`에 `kubectl patch`로 쓴다(`vm-mtu-set` OK, 거절되면 WARN과 `vws reconfigure` 안내). preflight가 쓰는 것은 이 두 필드뿐이고 NIC MTU는 건드리지 않는다. 근거가 없으면 `vm-mtu` WARN(미확인) |

FAIL이 하나라도 있으면 1, `--strict`이면 WARN도 1, 평가 불가면 3. 하네스 `tests/vws/preflight.test.sh`는 `tests/fixtures/preflight/ok`를 시나리오마다 한 파일씩 바꿔 검사별 통과/실패를 본다.

### vws init

`vws init [--from FILE | --print-url]`: 설치 파드 안(`VWS_INSTALLER_POD=1`)에서 인자 없이 부르면 installer API의 TUI(`vws-installer tui`, #142)를 띄우고, `--print-url`은 port-forward 주소, 1회용 접속 코드, 인증서 지문을 출력한다(`docs/installer-api.md`). 그 밖에는 `VWSCluster` CR을 저장한다. `--from`이 없으면 stdin으로 프로필, 스토리지 드라이버, StorageClass(`existing`일 때만), 이미지 태그, 엘라스틱 IP 풀 CIDR(필수, 기본값 없음, #148)을 묻고 같은 CR을 만든다. 검증은 `vws render`와 같은 코드(`lib/render.py`)이고 잘못되면 종료 코드 2로 아무것도 저장하지 않는다. 클러스터의 CR과 `spec`이 같으면 `unchanged`(변경 호출 0건). 워크로드는 건드리지 않는다. 저장은 `kubectl apply --server-side --field-manager=vws`. 저장 전에 `charts/vws/crds/vwsclusters.yaml`을 server-side apply(`--force-conflicts`)한다. Helm은 `crds/`를 첫 설치 때만 만들고 올리지 않아, 옛 CRD가 새 필드를 거절하기 때문이다(#112). `reconfigure`, `upgrade`, `restore`도 CR을 쓰기 전에, `apply`는 CR을 읽기 **전에** 클러스터의 CRD가 차트와 다를 때만 같은 CRD apply를 한다(옛 CRD가 읽은 CR에서 새 필드를 지워 RGW 등을 끄는 것을 막는다, #114. 같으면 쓰기 0건).

### vws apply

`vws apply [--unattended] [-f FILE]`: `-f FILE`이면 먼저 `vws init --from FILE`로 CR을 저장한다(잘못되면 2, 같은 spec이면 `unchanged`, #144). 그다음 CR → render → 상태 비교 → preflight → 비밀 → 번들 → helm → status. CR의 `status.appliedValuesHash`(렌더한 values의 sha256과 `charts/vws`의 모든 파일 경로·내용을 합친 해시, `lib/hash.sh`, #101. 차트만 바뀌어도 다시 적용한다)와 `observedGeneration`이 지금 값과 같으면 `unchanged`로 끝난다(변경 호출 0건. 예외: 아래 ConfigMap 삭제). 비교 전에 이전 apply가 만든 `kube-system/ovn-external-gw-config`(field manager `vws`)가 있으면 지우고 `OK extgw`를 낸다(#171). vws가 쓰지 않은 같은 이름의 ConfigMap은 두고 `WARN extgw`다. 번들은 `build/platform/*.yaml`을 파일 이름 순서(=`scripts/platform.sh`의 번호)대로 server-side apply하고 파일마다 CRD Established, Deployment Available을 기다린다. 이어 `helm upgrade --install vws --atomic --wait`, 이어 기본 이미지를 시드한다(#232): 이미지 네임스페이스(values `namespaces.images`, 기본 `vws-images`)에 DataVolume `ami-0123456789abcdef0`(`scripts/versions.env`의 digest 고정 `LAB_VM_IMAGE`를 CDI registry로 가져온다, 기본 1Gi)과 같은 이름의 CDI DataSource(그 PVC를 가리킨다)를 `kubectl create`로 만든다. console-api가 run-instances마다 이 id를 요청하고, vws-storage#88이 그 DataSource에서 루트 볼륨을 clone한다. 이미 있으면(AlreadyExists) 그대로 두고 `OK image ... kept`다. 덮어쓰거나 다시 가져오지 않는다. CDI는 번들이, 네임스페이스는 차트가 만들므로 helm 다음에 돈다. `--dry-run`은 만들 호출만 출력한다. 끝으로 status 하위 리소스를 쓴다. helm이 실패하면 status를 쓰지 않고 1. `--dry-run`은 변경 호출을 출력만 한다. 비밀: 지금 차트는 설치기가 만들 Secret이 없어 이 단계는 아무것도 만들지 않는다. preflight: `lib/checks.sh`(#55)의 api, nodes, storage, release 검사를 돌리고 FAIL이면 아무것도 바꾸지 않고 1. 동시 실행 잠금(Lease `vws-operator-lock`)은 아직 없다(확인 필요, 후속).

### vws installer

`vws installer finish`: 설치용 임시 cluster-admin 바인딩(`vws-installer-temp`)을 지우고 정말 사라졌는지 확인한다. 설계와 근거는 `docs/installer.md`. 확인은 `kubectl auth can-i`(SelfSubjectAccessReview, 파드 밖이면 `--as=system:serviceaccount:vws:vws-installer` 즉 SubjectAccessReview)로 `* *`, `create clusterrolebindings`, `create clusterroles`, `delete namespaces`, `create customresourcedefinitions`, `get secrets`가 모두 `no`이고, 운영용 `get vwsclusters`, `patch vwsclusters/status`는 `yes`인지 본다. 하나라도 어긋나면 1, 답을 못 읽으면 3. 이미 지워졌으면 `unchanged`이고 확인은 다시 한다(멱등).

### vws status

`vws status`: 설치된 상태를 읽기만 한다(변경 동사 0건). 검사: api, `cluster-cr`(프로필, `metadata.generation`과 `status.observedGeneration`), `applied-hash`(지금 CR이 렌더하는 values와 지금 차트의 해시와 `status.appliedValuesHash`; `vws apply`와 같은 계산이라 차트만 바뀌어도 드리프트), `release`와 `release-drift`(helm 리비전과 상태, 릴리스 values와 CR 렌더 결과 비교), `bundle-kubevirt`, `bundle-cdi`(운영자 CR이 보고하는 버전과 `scripts/versions.env`), `service-<이름>`(`charts/vws/values.yaml`의 서비스마다 Deployment/StatefulSet ready/desired), `pdb`. 드리프트는 WARN이라 `--strict`에서 1.

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

### vws diag

`vws diag [--out FILE] [--since 1h]`: 장애 신고용 진단 번들(tar.gz) 하나를 쓴다. 클러스터는 읽기만 하고 업로드하지 않는다(#43 7절).

| 파일 | 내용 |
| --- | --- |
| `vwscluster.json` | VWSCluster spec과 status(managedFields 제외) |
| `helm-status.json`, `helm-history.json`, `helm-values.json` | `helm status/history`, `helm get values -a`. 운영 역할은 Secret을 못 읽어 `unavailable: ...`가 된다(사용자 자격 증명으로 다시 돌리면 채워진다) |
| `cluster.txt` | `get nodes,pods,pvc,pdb,events -A -o wide` |
| `pods/<ns>_<pod>.describe.txt`, `.log` | 릴리스 네임스페이스와 번들 네임스페이스(kubevirt, cdi, kube-system, rook-ceph)의 비정상 파드만(Running/Succeeded가 아니거나 준비 안 된 컨테이너). 로그는 `--since`(기본 1h), 파드당 500줄 |
| `crds.txt`, `kubevirt-status.json`, `cdi-status.json`, `cephcluster-status.json` | CRD 이름, 각 CR의 status |
| `configmaps.json` | 릴리스 네임스페이스 ConfigMap 이름과 키별 크기(data 없음) |
| `status.json`, `doctor.json` | 이 빌드에 `vws status`/`doctor`(#56)가 있을 때 그 출력 |
| `versions.env`, `kubectl-version.txt`, `helm-version.txt`, `context.txt` | 버전과 컨텍스트 이름 |
| `README`, `REDACTION_REPORT` | 안내, 패턴별 마스킹 개수(값 없음) |

수집하지 않는 것: Secret 전부(어떤 호출도 secrets를 읽지 않는다), ConfigMap data, 환경 변수, 노드 호스트 파일, VM 콘솔/디스크.

마스킹: 키 이름 규칙이 먼저다. 이름이 비밀 모양인 키(대소문자 무시: `passw`, `passphrase`, `token`, `secret`, `credential`, `private`, `apikey`/`api-key`/`api_key`, `dsn`, `auth`, `key`로 끝나는 `-`/`_`/`.` 구분 이름, camelCase `...Key`)의 값은 모양과 상관없이 가린다. 중첩 객체/리스트면 그 아래 전체. JSON 파일(예: `helm get values -a -o json`)은 구조로 읽어 값만 `"[REDACTED]"`로 바꾸고 유효한 JSON으로 다시 쓰고, YAML/텍스트는 줄 단위(들여쓰기 블록 포함). `secretName`, `secretRef`, `secretKeyRef`는 Secret 이름 참조라 남긴다. `keys`, `monkey`, `privileged` 같은 비슷한 이름과 이미지 digest는 가리지 않는다. 이어서 `lib/redact.py`가 모든 파일을 훑어 PEM 개인키 블록, `Bearer` 토큰, `token` 값, `data:`/`binaryData:` 아래 40자 이상 base64 값, 12자리 이상 연속 숫자, 자격 증명 URL(`://user:pass@`)이 있는 줄을 `[REDACTED]`로 바꾼다. 스캐너는 먼저 패턴마다 명백히 가짜인 카나리를 잡는지 스스로 확인한다. 스캐너가 없거나(python3가 PATH에 없음) 카나리를 놓치면 번들을 쓰지 않고 3(실패 시 닫힘). 이미지 digest(16진수)는 `data:` 밖이라 가리지 않는다. 마스킹은 완전하지 않다: 공유 전에 사람이 번들을 훑는다(README에도 적힘).

파일: umask 077로 만들고 0600, 같은 이름이 있으면 덮어쓰지 않고 2. API가 응답하지 않으면 3.

### vws upgrade

`vws upgrade --to TAG`: VWSCluster의 `spec.version`을 TAG로 바꾸고 `vws apply`를 돌린다(#43 5절). 같은 태그면 `unchanged`, 쓰기 0건.

- apply의 helm 호출은 `--atomic --wait --timeout 15m --history-max 10`이고, 리비전 설명(`--description`)에 그 리비전을 만든 CR spec을 `vws-spec:<json>`(키 정렬한 한 줄 JSON)으로 남긴다(rollback이 CR을 되돌릴 근거). 설명이 `VWS_SPEC_DESCRIPTION_MAX`(기본 1024자)를 넘으면 `vws-spec-sha256:<spec의 sha256>`만 남기고 spec 전체는 CR의 `status.specHistory`(`{specHash, spec}`, 최신 10개, 오래된 것부터 버림)에 둔다.
- apply가 실패하면 helm이 `--atomic`으로 릴리스를 되돌렸으므로, CR spec도 이전 값으로 다시 저장하고 1로 끝난다(CR과 릴리스가 어긋나지 않게). `vws reconfigure`와 같은 `lib/applyrun.sh`의 `vws_apply_spec`.
- 출력에 항상 두 WARN: 백업 권고(`vws backup`(#61)이 아직 없어 최신성은 검사하지 못한다, etcd 스냅샷은 `docs/etcd-runbook.md`), 무중단 미주장(**vws-deploy#39가 닫히기 전에는 무중단을 주장하지 않는다**; 상태 서비스는 짧은 중단이 있다).
- 무상태 서비스의 롤링 업데이트는 현재 차트 설정(RollingUpdate, PDB)을 그대로 쓴다. 플랫폼 번들 업그레이드는 범위 밖.
- 업그레이드 뒤 판정(#43 5절 6단계): apply가 성공하면 `vws doctor`(status 포함)를 돌려 그 줄을 `after-<검사>`로 합친다. FAIL이 있으면 1이고 다음 행동으로 `vws rollback --yes`를 알린다(자동 롤백은 하지 않는다). doctor가 판정하지 못하면(종료 코드 2/3) WARN.
- `--dry-run`은 바뀔 값만 출력한다.

### vws rollback

`vws rollback [--to REVISION] --yes`: helm 릴리스를 리비전으로 되돌리고 VWSCluster spec도 그 리비전에 기록된 spec으로 되돌린다. `--to`가 없으면 직전 리비전.

- `--yes` 없이는 계획(리비전, 되돌아갈 spec)과 한계를 출력하고 2.
- `--yes`: `helm rollback vws N --wait --timeout 15m` → CR spec 저장 → CR status(appliedValuesHash, observedGeneration) 기록. helm이 실패하면 CR은 건드리지 않고 1.
- 리비전 설명이 `vws-spec-sha256:<hash>`이면 CR `status.specHistory`에서 그 hash의 spec을 찾는다. 없으면(10개를 넘어 밀려남) 아무것도 하지 않고 1.
- 리비전 설명에 둘 다 없으면(이 변경 전 리비전, 손으로 만든 리비전) 아무것도 하지 않고 1.
- 이미 그 리비전이고 CR spec도 같으면 `unchanged`.
- helm 이력은 Secret에 있어 운영 역할로는 읽을 수 없다. 사용자 자격 증명으로 돌린다(못 읽으면 3).

롤백 한계(출력에도 WARN으로 나온다):
- `helm rollback`은 차트 리소스와 값만 되돌린다. 새 버전이 저장소 형식을 바꿨다면 옛 바이너리가 못 읽을 수 있다. 직전 리비전이고 저장 형식이 안 바뀐 경우에만 안전하다고 본다.
- CRD와 플랫폼 operator는 되돌리지 않는다. 유일한 되돌림은 etcd 스냅샷(`docs/etcd-runbook.md`).
- vws-deploy#39가 닫히기 전에는 무중단을 주장하지 않는다.
- helm의 rollback은 새 리비전(설명 "Rollback to N")을 만든다. 그 리비전에는 `vws-spec:`이 없어 그리로 다시 rollback할 수는 없다(그 전 리비전 번호를 쓴다).

### vws verify-reboot

`vws verify-reboot --snapshot FILE` (재부팅 전), `vws verify-reboot --drained NODE` (drain 뒤, 재부팅 전), `vws verify-reboot --check FILE [--timeout 20m] [--allow-health-check CODE]... [--ceph-health-file FILE | --skip-ceph-gate]` (재부팅 뒤). 이 명령은 재부팅하지 않는다(사람이나 `talosctl`이 한다). 읽기 전용(#43 8절).

`--snapshot`은 `rbd.csi.ceph.com` VolumeAttachment가 붙은 노드마다 `WARN krbd`를 낸다. 그 노드는 재부팅 전에 drain한다(krbd shutdown 멈춤, #116, 절차는 [platform-bundle.md](platform-bundle.md)).

- `--drained NODE`: 노드가 cordon되어 있고 `rbd.csi.ceph.com` VolumeAttachment가 붙어 있지 않으면 0, drain이 안 됐거나 덜 끝났으면 1, 읽지 못하면 3. 0이 아니면 재부팅하지 않는다(#150).
- `--snapshot`은 KubeVirt infra 파드(virt-api, virt-controller, virt-exportproxy)가 갈 수 있는 노드가 하나 이하이면 `WARN infra`를 낸다. nodeSelector의 레이블을 모두 가진 스케줄 가능 노드를 세고, nodePlacement가 없으면 control-plane 노드를 센다. 그 노드를 drain하면 PDB 때문에 멈춘다(#168). 읽기만 하고 실패로 치지 않는다.
- 스냅샷의 목록 하나라도 읽지 못하면 3으로 끝나고 파일을 남기지 않는다(`snapshot failed: ...; do not reboot`). krbd 확인 없이 재부팅하지 않게 하려는 것이다(#150).
- 스냅샷(0600, 덮어쓰지 않음): 노드 이름과 boot ID, 서비스별 ready/desired, 네임스페이스별 파드 **개수**, 릴리스 네임스페이스 PVC 이름과 phase, VWS ConfigMap의 키 **개수**, `runStrategy: Always` VM 이름과 상태. 파드 UID, ConfigMap 데이터, Secret은 없다.
- 확인 게이트(기동 순서, 각 소요 시간 출력): `api` → `nodes`(스냅샷의 노드가 모두 Ready) → `cni`(kube-ovn 파드 Ready) → `csi`(Ceph `HEALTH_OK`, 또는 활성 check 코드가 모두 `--allow-health-check`에 있는 `HEALTH_WARN`. single-node를 포함해 모든 프로필이 같다) → `bundle`(KubeVirt, CDI CR `Deployed`) → `services`(스냅샷 서비스가 ready == desired). 기다리면 되는 상태는 `VWS_VERIFY_INTERVAL`(기본 10초)마다 다시 본다.
- `--allow-health-check CODE`(반복 가능, 기본은 빈 목록): `csi` 게이트는 `HEALTH_WARN`일 때 CephCluster `status.ceph.details`의 활성 check 코드가 **모두** 목록에 있어야 통과한다. 목록 밖 코드가 하나라도 있으면 그 코드를 이름으로 보여 주며 기다리고, `--timeout`에서 3으로 끝난다. 환경 변수로는 넓힐 수 없다. Ceph에서 아무것도 mute하지 않는다(#150). single-node 프로필도 예외가 없다(#154). 풀 size 1이라 뜨는 경고 코드도 `ceph health detail`로 확인한 뒤 직접 넘겨야 한다. `POOL_NO_REDUNDANCY`는 **예시일 뿐**이다. 실제 single-node 클러스터에서 실측하기 전까지는 이 코드가 뜬다고 가정하지 말고, 확인 없이 허용 목록에 넣지 않는다(#158).
  - vws-test 예: krbd 때문에 AES 키를 허용해 둔 상태라 `ceph health detail`에 `AUTH_INSECURE_CLIENT_KEY_TYPE`, `AUTH_INSECURE_KEYS_ALLOWED`, `AUTH_INSECURE_KEYS_CREATABLE` 세 코드가 뜬다(2026-10-13 실측). `vws verify-reboot --check before.json --allow-health-check AUTH_INSECURE_CLIENT_KEY_TYPE --allow-health-check AUTH_INSECURE_KEYS_ALLOWED --allow-health-check AUTH_INSECURE_KEYS_CREATABLE`.
- 외부 Ceph(#166, storage driver `existing`, CephCluster CRD 없음, vws-test가 그렇다): `csi` 게이트는 조용히 통과하지 않는다.
  - `--ceph-health-file FILE`: 재부팅 **뒤**에 `ceph health detail -f json > FILE`로 만든 파일을 읽는다(`.status`, `.checks`). 판정은 CephCluster와 같고 `--allow-health-check`도 check 코드에 똑같이 적용된다. 파일이 스냅숏보다 새것이 아니거나, 읽을 수 없거나, 형식이 맞지 않으면 기다린다(`--timeout`에서 3). 게이트는 기다리는 동안 파일을 다시 읽으므로 그사이에 다시 써도 된다.
  - `--skip-ceph-gate`: CephCluster CRD가 아예 없을 때만 게이트를 건너뛰고 `WARN gate-csi SKIPPED ... Ceph health was NOT checked`를 낸다. OK로 세지 않는다. `--ceph-health-file`이 있으면 파일이 우선한다.
  - 둘 다 없으면 1로 끝나며 두 옵션을 알려 준다. CRD는 있는데 CephCluster가 아직 없으면(Rook 기동 중) `--skip-ceph-gate`가 있어도 기다린다.
  - CephCluster가 있으면 언제나 그것을 읽고 파일은 쓰지 않는다.
- 막힌 게이트가 출력의 **첫 줄**이다. 통과할 수 없는 상태(노드 사라짐, Ceph `HEALTH_ERR`, operator `Failed`, 워크로드 없는 서비스)는 1, `--timeout`까지 기다려도 안 되면 3.
- 게이트 뒤 비교(실패 시 1): 스냅샷의 Bound PVC가 같은 이름으로 Bound, ConfigMap 키 수가 같거나 많음, Always VM이 모두 Running, 재시작 횟수가 임계(`VWS_VERIFY_RESTARTS`, 기본 3)를 넘는 파드 없음. 어느 노드도 boot ID가 바뀌지 않았으면 `WARN reboot not observed`.
- 부팅 시각 대신 노드의 `status.nodeInfo.bootID`로 재부팅 여부를 판단한다(재부팅마다 바뀐다).
- 실제 재부팅/전원 차단 실측은 미검증: cluster-verification-list I9, etcd 런북 시나리오 7(A7).

### vws backup

`vws backup [--out DIR] [--etcd-snapshot FILE]` reads the cluster and writes `vws-backup-<UTC>/` (0700, files 0600; an existing directory is refused, exit 2): the VWSCluster spec, `helm get values -a`, history and manifest, the release-namespace ConfigMaps labelled `app.kubernetes.io/part-of=vws` (WARN when there are none), `manifest.json` (files with size and sha256, versions, profile, VolumeSnapshot names, CephCluster health, the etcd snapshot path and whether it is under 24 hours old) and `SHA256SUMS`. Secrets, Ceph/VM disks and etcd itself are not included. The CR and helm files are scanned with `scripts/vws.d/lib/redact.py` first; any hit is a FAIL and nothing is written. Helm keeps its release in Secrets, so the operating role cannot run it: use your own kubeconfig. Copying the directory off the node is up to you. Not yet checked on a real cluster (cluster-verification-list I8).

### vws restore

`vws restore DIR [--yes]` checks `SHA256SUMS` first (a mismatch exits 1 before any write), then prints the plan; without `--yes` it exits 2, `--dry-run` stops after the plan. With `--yes` it applies the VWSCluster CRD from `charts/vws/crds`, saves the CR spec when it differs, runs `vws apply`, compares the release values with the backup (WARN when they differ), server-side applies only the ConfigMaps that differ, and folds `vws doctor` in as `after-*` lines. Restoring the same backup twice reports unchanged. State volumes are restored by hand from the VolumeSnapshots listed in the plan. Not yet checked on a real cluster (cluster-verification-list I8, I2).
