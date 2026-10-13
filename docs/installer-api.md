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
| GET | /result | 구현. `credentialAvailable`은 #146 전까지 항상 false |
| GET | /install/events | 501 `not_implemented` (#145) |
| POST | /result/credential | 501 `not_implemented` (#146) |
| GET | /cr | 구현(#144). 모든 단계가 valid일 때 CR 파일(`vwscluster.json`) |
| POST | /cr | 구현(#144). CR 파일로 모든 단계를 채우고 검사 |

`/session` 밖의 모든 경로는 세션이 필요하다(`Authorization: Bearer <token>` 또는 쿠키 `vws_installer_session`, HttpOnly·Secure·SameSite=Strict). 본문은 1 MiB까지이고, 모르는 필드와 JSON 뒤의 데이터는 400 `bad_request`다.

## 인증 (1회용 접속 코드)

- 코드는 `crypto/rand`로 뽑은 16자다. 헷갈리는 글자(0 O 1 l I)를 빼고 거부 표집으로 고르게 뽑는다.
- `POST /session {"code"}`가 맞으면 세션 토큰(32바이트 hex)을 주고, 쓴 코드는 버리고 다음 코드를 파일에 쓴다. 새 로그인은 이전 세션을 끊는다. 그래서 세션은 언제나 하나다.
- 틀린 코드는 401 `unauthenticated`다. 연속 5번째는 403 `forbidden`이고 코드를 새로 낸다(옛 코드는 더 안 된다).
- 30분 동안 요청이 없으면 다음 요청이 401 `session_expired`이고 그 토큰은 버린다. 요청마다 유휴 시간이 다시 시작된다.

## 상태 기계

세션은 하나(싱글턴)다. `editing → confirming → installing → succeeded | failed`. `confirming`이나 `failed`에서 단계를 저장하면 `editing`으로 돌아간다. `finished`(자격 증명 전달 후)는 #146이다.

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

검증에 실패하면 값은 `invalid`로 저장되고 400 `validation_failed`와 `fields`가 온다. preflight는 `Preflight` 함수로 꽂는다. 엔진(#143) 전에는 검사가 없다. `fail`+`error`는 단계를 `invalid`로 만든다. 나머지 `pass`가 아닌 검사는 summary의 `warnings`로 간다.

## CR 저장과 무인 설치 (#144)

- `GET /cr`은 summary가 설치할 CR과 같은 파일을 준다(모든 단계 valid, 아니면 409 `step_out_of_order`). `vws apply --unattended -f vwscluster.json`으로 마법사 없이 같은 설치를 한다.
- `POST /cr`은 CR 본문을 단계로 나눈다. `spec.network.nodes.external.eipPool`은 eip 단계, 나머지 spec 필드는 같은 이름의 단계로 간다. roles는 CR에 없으므로 저장된 값이 valid면 그대로, 아니면 모든 노드다. observability가 없으면 비운다. 모든 단계를 다시 검사하고 오류가 하나라도 있으면 400 `validation_failed`에 필드를 모두 담는다(값은 `invalid`로 남는다). 편집할 수 있는 상태에서만 된다(설치 중 409 `install_in_progress`, 설치 뒤 409 `already_installed`).
- draft에 없는 `spec.version`, `spec.services`, `spec.secrets`는 `unsupported`로 거절한다. 그런 CR은 설치기를 거치지 않고 `vws apply --unattended -f`로 쓴다. `apiVersion`, `kind: VWSCluster`, `metadata.name: vws`도 확인한다.
- 같은 CR을 두 번 올리면 같은 단계와 같은 `specHash`다. `vws apply --unattended -f`를 두 번 돌리면 두 번째는 init도 apply도 `unchanged`다.

## 오류 모델

```json
{"error":{"code":"validation_failed","message":"...","fields":[{"field":"spec.network.nodes.external.eipPool","code":"required","message":"..."}],"retryable":false}}
```

각 code는 HTTP 상태와 vws-lib `errs.Code`를 하나씩 가진다. `retryable`은 `errs.Code.Retryable()`이다. #139 표에 더한 것: 404 `not_found`(모르는 경로나 단계), 409 `not_confirmed`, 501 `not_implemented`, 500 `internal`. 내부 오류의 원인은 로그에만 남고 응답에는 "internal error"만 간다. 설치 실패의 원인도 응답에 넣지 않는다.

## 시험 (가짜 클러스터, T0)

`cd installer && go vet ./... && go test -race ./...`. CI는 `installer` 잡이 돌린다. 시험 범위:

- 인증: 코드 모양, 반복 없음, 1회용, 새 로그인이 옛 세션을 끊음, 5회 실패 시 403과 새 코드, 30분 유휴 만료와 갱신, 쿠키 속성, 모든 경로가 세션 없이 401, 응답 본문에 코드 없음.
- 상태 기계: 처음부터 끝까지, 순서 위반, `spec_changed`, `not_confirmed`, 설치 중 409, 설치 후 409, 실패 뒤 재시도.
- 단계 검증 16가지, 비활성 드라이버, network 변경 뒤 eip 재검사, preflight 차단과 경고, 잘못된 본문, 404, 501, CR의 apiVersion이 차트 CRD와 같음.
- CR(`cr_test.go`): 내보내기는 모든 단계 필요, 내보낸 CR을 새 설치기에 올리면 같은 summary 해시, 두 번 올려도 같음, apiVersion/kind/name/services/eipPool 없음/profile 없음/비활성 드라이버 거절, 설치 중·후 409. `tests/vws/apply.test.sh`: `apply -f`가 init 뒤 적용, 두 번째는 unchanged와 변경 0건, 잘못된 파일은 2이고 helm 호출 없음.
- `cluster.VWS`: 가짜 `kubectl`, `vws` 스크립트로 노드 목록, init 다음 apply 순서, init 실패 시 apply 안 함, CR 파일 0600.

## 남은 일 (이 PR 밖)

- 이미지: `bootstrap/Containerfile`의 빌드 단계(`INSTALLER_GO_IMAGE`, `installer/go.mod`의 Go 버전과 같아야 한다, R20)가 `CGO_ENABLED=0`으로 빌드해 `/opt/vws/bin/vws-installer`에 넣는다(#191). 발행과 digest 고정은 아직이다(`VWS_INSTALLER_IMAGE` digest 0).
- 실제 클러스터에서 TLS, port-forward, 설치 경로(미실행). 노드 NIC 목록은 Node 객체에 없어서(R10) #143이 채운다. 그 전에는 `options.nics`가 비어 있다.
