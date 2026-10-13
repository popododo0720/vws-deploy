# 설치기 API 서버 (vws-deploy#141, 설계 #139 (b))

`installer/`는 Go 모듈 `github.com/popododo0720/vws-deploy/installer`다. 바이너리 `vws-installer`가 #139의 installer API를 TLS(자체 서명)로 연다. 로그·메트릭·트레이스는 vws-lib `obs`(`obs.Setup`, `httpobs.Middleware`), 오류는 vws-lib `errs`다. 루트에 `go.mod`가 없으므로 R10(차트가 유일한 매니페스트 경로)은 그대로다.

## 실행

```
vws-installer -api-version GROUP/VERSION [-listen 0.0.0.0:8443] [-state-dir /tmp/vws-installer] [-kubectl kubectl] [-vws vws]
```

- `-api-version`은 필수이고 기본값이 없다. 그룹은 `charts/vws/crds/vwsclusters.yaml`에만 적는다(R18). bootstrap Deployment는 `scripts/bootstrap.sh`가 CRD에서 읽은 group/version을 `-api-version=`으로 넘긴다(#191). R20이 명령줄 전체와 프로브를 검사한다.
- `-state-dir`(0700)에 1회용 접속 코드 `access-code`와 인증서 SHA-256 지문 `fingerprint`(둘 다 0600)를 쓴다. 로그에는 둘 다 쓰지 않는다. `vws init --print-url`(#142)이 이 파일을 읽는다.
- 설치는 `vws apply --unattended -f <state-dir>/vwscluster.json`이다. 이 명령이 CR을 저장(`vws init --from`)하고 적용한다(#144).

## 엔드포인트 (base `/api/v1`, JSON)

| 메서드 | 경로 | 이 PR |
| --- | --- | --- |
| POST | /session | 구현. 인증 없음 |
| GET | /state | 구현 |
| GET, PUT | /steps/{step} | 구현 |
| POST | /steps/{step}/preflight | 구현(저장된 값을 다시 검사) |
| GET | /summary | 구현. `editing`/`failed` → `confirming` |
| POST | /install | 구현. 202, 설치는 백그라운드 |
| GET | /result | 구현. `credentialAvailable`은 succeeded이고 자격 증명 저장소가 있을 때 true |
| GET | /install/events | 구현(#145). 설치 진행 SSE, `Last-Event-ID` 재개 |
| POST | /result/credential | 구현(#146). 초기 관리자 비밀번호를 한 번 준다. 그 뒤 410 `credential_consumed` |
| GET | /cr | 구현(#144). 모든 단계가 valid일 때 CR 파일(`vwscluster.json`) |
| POST | /cr | 구현(#144). CR 파일로 모든 단계를 채우고 검사 |

`/session` 밖의 모든 경로는 세션이 필요하다(`Authorization: Bearer <token>` 또는 쿠키 `vws_installer_session`, HttpOnly·Secure·SameSite=Strict). 본문은 1 MiB까지이고, 모르는 필드와 JSON 뒤의 데이터는 400 `bad_request`다.

## 인증 (1회용 접속 코드)

- 코드는 `crypto/rand`로 뽑은 16자다. 헷갈리는 글자(0 O 1 l I)를 빼고 거부 표집으로 고르게 뽑는다.
- `POST /session {"code"}`가 맞으면 세션 토큰(32바이트 hex)을 주고, 쓴 코드는 버리고 다음 코드를 파일에 쓴다. 새 로그인은 이전 세션을 끊는다. 그래서 세션은 언제나 하나다.
- 틀린 코드는 401 `unauthenticated`다. 연속 5번째는 403 `forbidden`이고 코드를 새로 낸다(옛 코드는 더 안 된다).
- 30분 동안 요청이 없으면 다음 요청이 401 `session_expired`이고 그 토큰은 버린다. 요청마다 유휴 시간이 다시 시작된다.

## 상태 기계

세션은 하나(싱글턴)다. `editing → confirming → installing → succeeded | failed`. `confirming`이나 `failed`에서 단계를 저장하면 `editing`으로 돌아간다. `succeeded`에서 `POST /result/credential`이 비밀번호를 주면 `finished`다(#146).

| 요청 | 거절 |
| --- | --- |
| PUT 단계, 앞 단계 중 valid가 아닌 것이 있음 | 409 `step_out_of_order` |
| GET /summary, valid가 아닌 단계가 있음 | 409 `step_out_of_order` |
| POST /install, `confirm`이 true가 아님 | 400 `bad_request` |
| POST /install, summary를 안 읽었거나 그 뒤 편집함 | 409 `not_confirmed` |
| POST /install, `specHash`가 지금 draft와 다름 | 409 `spec_changed` |
| 설치 중의 편집, summary, install | 409 `install_in_progress` |
| `succeeded` 뒤의 편집, install | 409 `already_installed` |

`specHash`는 CR JSON을 맵으로 다시 읽어 키를 정렬한 정규형의 SHA-256이다. 설치되는 CR은 확인한 CR과 바이트 단위로 같다. 단계를 저장하면 그 뒤에 저장된 단계도 다시 검사한다. 예를 들어 network의 external CIDR이 바뀌면 eip가 `invalid`가 된다.

## 단계와 CR

| 단계 | 값 | CR |
| --- | --- | --- |
| roles | `{"nodes": [...]}`, 모두 클러스터에 있는 노드 | 넣지 않는다. 역할은 클러스터 준비 때 붙인 노드 레이블에서 읽는다(설치기는 `vws-operator-read`의 nodes get/list로 읽기만). CRD에 `spec.nodes`는 없다(#192, 안 B) |
| network | `spec.network` 그대로(#125). driver는 kube-ovn만. CIDR, VLAN 0~4094, MTU 1280~9216, external·direct의 gateway가 CIDR 안 | `spec.network` |
| storage | `spec.storage`. rook-ceph, existing(storageClass 필수), nfs(server, share 필수). minio, local은 선택지에 비활성 "준비 중"(vws-storage#70, #71) | `spec.storage` |
| eip | CIDR 문자열. 기본값 없음, 필수. network의 external 안이어야 한다 | `spec.network.nodes.external.eipPool` |
| profile | single-node, small, ha. 기본은 노드 1대면 single-node, 아니면 small | `spec.profile` |
| observability | `{"otlpEndpoint": ""}`. vws render가 아직 엔드포인트를 받지 않아 비어 있어야 한다 | `spec.observability` |

검증에 실패하면 값은 `invalid`로 저장되고 400 `validation_failed`와 `fields`가 온다. `fail`+`error`는 단계를 `invalid`로 만든다. 나머지 `pass`가 아닌 검사(`warning`, `unknown`)는 막지 않고 summary의 `warnings`로 간다.

## preflight 엔진 (#143)

`internal/preflight`가 단계 값으로 검사를 만든다. Kubernetes API와 선택적인 links 파일만 읽는다(R10). #128의 노드망 검사(`scripts/vws.d/lib/nodenet.py`)를 같은 규칙으로 옮겼다. `vws preflight`의 nodenet.py는 파드 밖 CLI용으로 그대로 둔다.

| 단계 | id | 심각도 | 내용 |
| --- | --- | --- | --- |
| roles | `node.ready` | error | 고른 노드마다 Ready |
| network | `node.ip` | error | 노드마다 InternalIP 하나가 management CIDR 안 |
| network | `nic.exists`, `nic.mtu` | error | 노드마다 인터페이스가 있고 MTU가 CR 값(없으면 1500) 이상. 호스트 망은 `<nic>` 또는 `<nic>.<vlan>`, external과 direct(underlay)는 NIC 그대로. 한 인터페이스를 여러 망이 쓰면 큰 MTU |
| network | `nic.links` | warning, unknown | 그 노드의 links가 없음. 막지 않는다 |
| storage | `storage.class` | error(목록을 못 읽으면 warning, unknown) | existing의 StorageClass가 있음 |
| storage | `storage.nfs` | info, unknown | NFS 서버에는 접속하지 않는다 |
| profile | `profile.nodes` | error | Ready 노드 수 ≥ single-node 1, small 3, ha 6(`VWS_PROFILE_MIN_NODES`) |

NIC과 MTU는 `vws-installer -links FILE`(`talosctl -n <모든 노드> get links -o json`, nodenet.py와 같은 입력)에서 읽는다. links의 `node`는 노드 이름이나 InternalIP다. links가 그 노드를 모르면 `nic.links`가 unknown이고 network 선택지 `options.nics`도 그 노드는 비어 있다. bootstrap Deployment는 아직 `-links`를 넘기지 않는다(파일을 넣을 방법은 확인 필요).

## CR 저장과 무인 설치 (#144)

- `GET /cr`은 summary가 설치할 CR과 같은 파일을 준다(모든 단계 valid, 아니면 409 `step_out_of_order`). `vws apply --unattended -f vwscluster.json`으로 마법사 없이 같은 설치를 한다.
- `POST /cr`은 CR 본문을 단계로 나눈다. `spec.network.nodes.external.eipPool`은 eip 단계, 나머지 spec 필드는 같은 이름의 단계로 간다. roles는 CR에 없으므로 저장된 값이 valid면 그대로, 아니면 모든 노드다. observability가 없으면 비운다. 모든 단계를 다시 검사하고 오류가 하나라도 있으면 400 `validation_failed`에 필드를 모두 담는다(값은 `invalid`로 남는다). 편집할 수 있는 상태에서만 된다(설치 중 409 `install_in_progress`, 설치 뒤 409 `already_installed`).
- draft에 없는 `spec.version`, `spec.services`, `spec.secrets`는 `unsupported`로 거절한다. 그런 CR은 설치기를 거치지 않고 `vws apply --unattended -f`로 쓴다. `apiVersion`, `kind: VWSCluster`, `metadata.name: vws`도 확인한다.
- 같은 CR을 두 번 올리면 같은 단계와 같은 `specHash`다. `vws apply --unattended -f`를 두 번 돌리면 두 번째는 init도 apply도 `unchanged`다.

## 설치 이벤트 스트림 (#145)

- `GET /install/events`는 `text/event-stream`이다. 이벤트는 `state`(`installing`, `succeeded`, `failed`)와 `log`(설치 로그 한 줄)다. id는 1부터 늘고 재설치에도 다시 쓰지 않는다. 처음에 `retry: 5000`, 쉬는 동안 15초마다 `: keepalive`.
- `Last-Event-ID` 헤더(헤더를 못 쓰는 클라이언트는 `?lastEventId=`) 다음부터 보낸다. 서버는 최근 2000개(`EventBuffer`)만 둔다. 그 id 다음 이벤트가 이미 버려졌으면 `event: gap`(버린 개수) 뒤 남은 가장 오래된 것부터 보낸다. 잘못된 id는 400 `bad_request`.
- 설치가 돌지 않고 보낼 것이 없으면 스트림이 끝난다. EventSource는 retry 뒤 다시 붙어 이어 받는다.
- 로그 줄은 링에 넣기 전에 마스킹하고 파드 로그로는 보내지 않는다. 마스킹 대상: 현재 접속 코드와 세션 토큰(값 그대로), `password|secret|token|api_key|access_code|credential|private_key…` 이름의 `key=value`, `key: value`, `"key": "value"`의 값, `Bearer` 뒤 값, URL의 `user:password@`의 비밀번호, PEM private key 블록 전체. 모두 `***`이 된다. 이 모양에 맞지 않는 비밀은 걸러지지 않는다(예: 이름 없이 값만 찍는 로그). 그래서 `vws apply`의 로그에 비밀을 찍지 않는 것이 먼저다.

## 초기 관리자 비밀번호 (#146)

- 설치가 성공하면 설치기가 `crypto/rand`로 24자(영문 대소문자와 숫자, 편향 없음)를 만들어 `vws` 네임스페이스(`-namespace`)의 Secret `vws-initial-admin`(`username`, `password`)에만 만든다. kubectl에는 stdin으로 넘기고 인자, 오류, 로그, CR, status, state-dir 파일에는 넣지 않는다. 설치기 메모리에도 남기지 않는다.
- Secret이 이미 있으면 바꾸지 않는다(다시 설치해도 같은 비밀번호). Secret을 못 만들면 상태는 `failed`이고 다시 설치한다.
- `POST /result/credential`은 Secret을 읽고, Secret에 `vws-initial-admin-consumed=true` 라벨을 붙인 다음에만 `{"adminUser","password"}`를 `Cache-Control: no-store`로 준다. 라벨을 못 붙이면 500이고 비밀번호는 주지 않는다(다시 부르면 된다). 그 뒤와 라벨이 이미 있을 때(설치기 재시작 포함)는 410 `credential_consumed`, 상태는 `finished`. succeeded 전에는 409.
- 이 단계는 `vws installer finish` 전에 한다. finish가 임시 권한을 지우면 설치기가 Secret을 못 읽는다(확인 필요). 콘솔이 이 Secret으로 관리자 계정을 만드는지는 이 저장소 밖이다(확인 필요). TUI(#142)는 아직 이 엔드포인트를 부르지 않는다.

## 오류 모델

```json
{"error":{"code":"validation_failed","message":"...","fields":[{"field":"spec.network.nodes.external.eipPool","code":"required","message":"..."}],"retryable":false}}
```

각 code는 HTTP 상태와 vws-lib `errs.Code`를 하나씩 가진다. `retryable`은 `errs.Code.Retryable()`이다. #139 표에 더한 것: 404 `not_found`(모르는 경로나 단계), 409 `not_confirmed`, 501 `not_implemented`, 500 `internal`. 내부 오류의 원인은 로그에만 남고 응답에는 "internal error"만 간다. 설치 실패의 원인도 응답에 넣지 않는다.

## TUI `vws init` (#142)

설치 파드 안에서 `vws init`(인자 없음)은 `vws-installer tui`를 실행한다. state 디렉터리의 접속 코드로 로그인하고, 인증서를 `fingerprint`의 SHA-256으로 고정한다(CA 없음, 다른 인증서는 거절). 단계 순서대로 묻는다.

- roles: 노드 목록(기본은 전부). network: NIC 목록(노드별)을 보여 주고 management(필수), tunnel·storage public·external(선택, `skip`)의 NIC, CIDR, VLAN, MTU, external은 gateway도. storage: 비활성 드라이버를 "준비 중"으로 보여 주고 고를 수 있는 것만 묻는다. eip, profile을 묻고 observability는 비운다.
- `validation_failed`면 필드 오류를 보여 주고 그 단계를 다시 묻는다. preflight 중 `pass`가 아닌 검사를 보여 주고, 막히면 다시 묻는다.
- summary의 CR과 경고를 보여 주고 `yes`일 때만 install한다. 진행률 스트림(#145) 전까지는 `/result`를 2초마다 읽는다. 끝나면 콘솔 URL과 관리자 이름을 보여 준다. 초기 비밀번호는 #146 전까지 없다고 알린다.
- `no`나 입력 끝이면 아무것도 설치하지 않고 끝난다(draft는 설치기에 남는다).

`vws init --print-url`은 `vws-installer print-url`이다. port-forward 주소, 접속 코드, 인증서 지문을 exec 터미널에만 출력한다. 코드는 1회용이라 로그인할 때마다 새 코드를 다시 읽어야 한다.

## 시험 (가짜 클러스터, T0)

`cd installer && go vet ./... && go test -race ./...`. CI는 `installer` 잡이 돌린다. 시험 범위:

- 인증: 코드 모양, 반복 없음, 1회용, 새 로그인이 옛 세션을 끊음, 5회 실패 시 403과 새 코드, 30분 유휴 만료와 갱신, 쿠키 속성, 모든 경로가 세션 없이 401, 응답 본문에 코드 없음.
- 상태 기계: 처음부터 끝까지, 순서 위반, `spec_changed`, `not_confirmed`, 설치 중 409, 설치 후 409, 실패 뒤 재시도.
- 단계 검증 16가지, 비활성 드라이버, network 변경 뒤 eip 재검사, preflight 차단과 경고, 잘못된 본문, 404, 501, CR의 apiVersion이 차트 CRD와 같음.
- TUI(`internal/tui`): 실제 API를 TLS로 띄워 처음부터 설치까지, 필드 오류 뒤 재질문, preflight 표시, 거절과 입력 끝, 설치 실패, 다른 인증서 거절, 클라이언트 오류 모델. `print-url` 출력. `tests/vws/init.test.sh`: 파드 안 `vws init`이 TUI를, `--print-url`이 print-url을 실행하고 `--from`은 그대로 저장.
- CR(`cr_test.go`): 내보내기는 모든 단계 필요, 내보낸 CR을 새 설치기에 올리면 같은 summary 해시, 두 번 올려도 같음, apiVersion/kind/name/services/eipPool 없음/profile 없음/비활성 드라이버 거절, 설치 중·후 409. `tests/vws/apply.test.sh`: `apply -f`가 init 뒤 적용, 두 번째는 unchanged와 변경 0건, 잘못된 파일은 2이고 helm 호출 없음.
- 이벤트(`events_test.go`, `mask_test.go`): 설치 중 붙은 클라이언트와 끝난 뒤 다시 읽은 것이 같음, 가짜 시크릿·접속 코드·토큰이 원본 스트림 어디에도 없음, `Last-Event-ID` 재개, 2000개 링과 gap, 잘못된 id 400, 설치 전 빈 스트림. 마스킹 모양별 표.
- 자격 증명(`credential_test.go`): 1회 반환 뒤 410과 `finished`, 모양(24자, 알파벳), 반환 응답 말고 모든 응답·CR·코드 파일에 값이 없음, Secret 생성 실패는 failed, 라벨 실패는 500이고 값 없음, 이미 쓴 Secret은 410이고 바뀌지 않음. `cluster.KubeSecret`: 가짜 kubectl로 만들기, AlreadyExists, 읽기, 라벨, 값이 kubectl 인자에 없음. `credential_leak_test.go`: 라벨 실패(500, ERROR 로그)와 정상 반환을 거친 뒤 생성된 비밀번호가 `GET /install/events` 원본 스트림과 slog 로그(debug 레벨까지) 어디에도 없음.
- `cluster.VWS`: 가짜 `kubectl`, `vws` 스크립트로 노드 목록, `apply --unattended -f` 한 번, apply 실패 전달, CR 파일 0600.

## 남은 일 (이 PR 밖)

- 이미지: `bootstrap/Containerfile`의 빌드 단계(`INSTALLER_GO_IMAGE`, `installer/go.mod`의 Go 버전과 같아야 한다, R20)가 `CGO_ENABLED=0`으로 빌드해 `/opt/vws/bin/vws-installer`에 넣는다(#191). 발행과 digest 고정은 아직이다(`VWS_INSTALLER_IMAGE` digest 0).
- 실제 클러스터에서 TLS, port-forward, 설치 경로(미실행). 실제 links 파일로 NIC 검사(미실행).
