# vws-iam 설치 (#243)

설계는 vws-iam#1, 계약은 vws-proto#24(`vws/iam/v1`, 브랜치 `feat/iam-v1`)다.

## 지금 들어간 것

- **차트 서비스 `iam`**: `enabled: false`. vws-iam 레포에 코드와 이미지가 아직 없어서 기본 렌더에는 빠진다(render-only). 켜려면 CR `spec.services.iam.enabled: true` 또는 values `services.iam.enabled=true`. 포트 7500, `-listen=0.0.0.0:7500`.
- **`enabled`**: 모든 서비스에 쓸 수 있다. `false`면 차트가 그 서비스의 오브젝트를 하나도 만들지 않고, `check_render.py`, `vws status`, 프로필 자원 계산, `vws apply`의 Secret 단계도 그 서비스를 건너뛴다.
- **KEK**: `kekSecret: vws-iam-kek`. `vws apply`가 릴리스 네임스페이스에 Secret이 없을 때만 만든다(`kek`: 32바이트 난수의 base64, `key-id`: `kek-1`). 값은 kubectl stdin으로만 가고 인자, 출력, 로그에 남지 않는다. 있으면 그대로 둔다(조용한 교체 없음). 파드는 `VWS_IAM_KEK`, `VWS_IAM_KEK_ID`를 secretKeyRef로만 받는다. args에 KEK가 있으면 `check_render.py`가 실패한다.
- **NetworkPolicy**: `allowFrom: [console-api]`. 같은 릴리스의 console-api 파드만 iam 포트로 들어온다. `check_render.py`가 정책이 없거나 다른 서비스를 들이면 실패한다.
- **mTLS**: 다른 서비스가 아직 쓰지 않아 넣지 않았다.

## 아직 없는 것과 이유

- **IAM CRD(IamUser 등)와 RBAC**: R18은 VWS CRD를 `VWSCluster` 하나로 정하고 API 그룹을 한 곳에만 쓰게 한다. IAM CRD 스키마와 그룹은 vws-iam#2(스켈레톤, CRD, kube store)가 정한다. 그때 R18 예외(또는 vws-iam이 CRD를 소유하는 방식)와 네임스페이스 `vws-iam`의 최소 Role을 함께 넣는다.
- **부트스트랩(루트/관리자 사용자, 기존 `vws-console-api-credentials` 키 가져오기, `vws-initial-admin` 비밀번호)**: vws-iam API(vws-iam#3)가 있어야 부를 수 있다. 계획: `vws apply`가 helm 뒤에 vws-iam이 Ready면 클러스터 안에서 한 번 실행하는 Job으로 처리하고, 비밀은 Secret 참조로만 넘긴다. 이미 있으면 아무것도 하지 않는다.
- **console-api 연결**: #243 범위가 아니다(vws-console-api#121).

## 확인 필요

- vws-test 설치 증거 없음(이미지 없음).
- Kube-OVN에서 NetworkPolicy가 kubelet 프로브를 막지 않는지.
- CR로 `iam`을 켰을 때 `vws status`의 서비스 목록은 차트 기본값(`enabled: false`)을 본다. 이미지가 나와 기본값을 `true`로 바꾸면 사라지는 차이다.
