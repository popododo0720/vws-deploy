# 설치 파드와 임시 권한 회수 (vws-deploy#54, #43 결정 8)

## 권한의 일생

| 단계 | 바인딩 | 권한 |
| --- | --- | --- |
| `kubectl apply -f build/bootstrap.yaml` | `vws-installer-temp` (ClusterRoleBinding → `cluster-admin`), `vws-operator`(Role), `vws-operator-read`(ClusterRole) | cluster-admin + 운영 권한 |
| `vws init`, `vws apply` | 같음 | 번들(CRD, ClusterRole, 네임스페이스)과 차트를 설치 |
| `vws installer finish` | `vws-installer-temp` 삭제 | 운영 권한만: 릴리스 네임스페이스의 VWSCluster와 그 status, `vws-operator-lock` Lease, 워크로드 읽기, 클러스터 범위 읽기(아래 표). Secret, RBAC, 클러스터 쓰기, watch 없음 |

cluster-admin이 필요한 이유: 번들이 CRD, ClusterRole(Binding), 네임스페이스, 웹훅을 만든다. 그 목록을 동사 단위로 줄인 최소 권한 초안은 번들 매니페스트에 따라 바뀌어 유지 비용이 커서, 결정 8대로 임시 cluster-admin + 회수로 했다.

## 운영 ClusterRole `vws-operator-read` (정확한 목록, R20이 강제)

| apiGroup | resource | verbs | 쓰는 곳 |
| --- | --- | --- | --- |
| `""` | `nodes` | get, list | preflight, doctor |
| `storage.k8s.io` | `storageclasses` | get, list | preflight |
| `snapshot.storage.k8s.io` | `volumesnapshotclasses` | get, list | preflight |
| `apiextensions.k8s.io` | `customresourcedefinitions` | get, list | doctor(CRD Established) |
| `kubevirt.io` | `kubevirts` | get, list | status(번들 버전), doctor(phase), diag (#77) |
| `cdi.kubevirt.io` | `cdis` | get, list | status(번들 버전), doctor(phase), diag (#77) |
| `ceph.rook.io` | `cephclusters` | get, list | doctor(Ceph health), diag (#77) |
| `""` | `persistentvolumeclaims` (모든 네임스페이스) | get, list | doctor(Pending PVC) (#77) |

목록 밖 그룹/리소스, get/list 밖 동사(watch 포함), `resourceNames`, `nonResourceURLs`는 `scripts/guard.sh bootstrap`이 거절한다(음성 시험: watch, 목록 밖 리소스, PVC 쓰기 동사, 목록 밖 그룹, 클러스터 범위 pods, 허용 항목 제거).

## 회수와 확인 (`vws installer finish`)

1. `kubectl delete clusterrolebinding vws-installer-temp --ignore-not-found`. 출력이 없으면 `unchanged`.
2. `kubectl auth can-i`로 사라져야 할 권한(`* *`, `create clusterrolebindings`, `create clusterroles`, `delete namespaces`, `create customresourcedefinitions`, `get secrets`)이 모두 `no`인지. 설치 파드 안(`VWS_INSTALLER_POD=1`)에서는 자기 자신의 SelfSubjectAccessReview, 밖에서는 `--as`로 그 ServiceAccount의 SubjectAccessReview다.
3. 남아야 할 권한(`get vwsclusters`, `patch vwsclusters/status`)이 `yes`인지.
4. 하나라도 어긋나면 종료 코드 1, 답을 못 읽으면 3. 재실행해도 안전하다(삭제는 `--ignore-not-found`, 확인은 매번).

주의: 다른 바인딩(사용자가 따로 준 것)으로 권한이 남아 있어도 2에서 실패한다. 의도한 동작이다(권한이 실제로 줄었는지가 기준).

## 충돌, 재시작에 대한 안전

- **리퍼 Job** `vws-installer-reaper`: 바인딩의 `creationTimestamp` + `vws-installer-ttl-seconds`(= `INSTALLER_TTL_SECONDS`, 4시간)까지 자고 바인딩을 지운다. 파드가 죽어 다시 떠도 남은 시간을 바인딩에서 다시 계산하므로 시한이 늘지 않는다. Job 시한은 TTL + 10분이라 삭제 전에 끊기지 않는다. finish 뒤에는 바인딩을 읽을 수 없어 곧 끝난다.
- **시작 시 상태 확인**: `vws init`, `vws apply`는 먼저 바인딩이 TTL을 넘겼는지 보고, 넘겼으면 지우고 종료 코드 1로 멈춘다(리퍼가 못 돈 경우의 두 번째 방어).
- **설치 파드**: `activeDeadlineSeconds` = TTL, `restartPolicy: Never`, 자기 `create clusterrolebindings` 권한이 사라지면 스스로 끝난다.
- 남는 위험(확인 필요): 리퍼 Job이 완료된 뒤 bootstrap을 다시 apply하면 Job이 다시 돌지 않는다(같은 이름의 완료된 Job). `ttlSecondsAfterFinished: 600`이라 10분 뒤에는 지워져 다시 apply하면 새로 돈다. 그 사이 다시 apply하면 시작 시 상태 확인만 남는다.

## T0 수용 기준 (이 박스, 가짜 kubectl/helm)

- `scripts/guard.sh bootstrap`(R20): 렌더한 bootstrap이 kubeconform 통과, privileged/hostPath/hostNetwork/hostPID/hostIPC 없음, 모든 이미지가 digest, 권한 상승 금지, cluster-admin 바인딩은 `vws-installer-temp` 하나이고 TTL 주석이 `INSTALLER_TTL_SECONDS`, 설치 파드 시한 = TTL, 리퍼 존재와 시한 > TTL, 운영 Role/ClusterRole에 와일드카드·Secret·RBAC 없음, ClusterRole은 읽기만. 각 규칙을 깨뜨린 사본 10개가 실패하는지 음성 시험.
- `tests/vws/installer.test.sh`: 회수와 확인 통과(0), 두 번째 실행 `unchanged`(0), 파드 안에서는 `--as` 없음, `* *`이 남음(1), `create clusterrolebindings`가 남음(1), 운영 권한 상실(1), can-i 무응답(3), `--dry-run`은 변경 0건, 잘못된 하위 명령(2).
- `tests/vws/init.test.sh`, `tests/vws/apply.test.sh`: 두 번째 실행 `unchanged`와 변경 0건, 대화형과 `--from`이 같은 CR, 번들 순서, helm 실패 시 status 없음, `--dry-run` 계획만, TTL 지난 바인딩은 회수 후 1.

## 실제 클러스터에서 재야 하는 것 (`/workspace/cluster-verification-list.md` I1, I2, I4)

- 실제 RBAC 회수: finish 뒤 `kubectl auth can-i --as=system:serviceaccount:vws:vws-installer '*' '*'`가 `no`, 리퍼가 TTL에 실제로 지우는지(TTL을 짧게 바꿔서), 리퍼 파드 강제 삭제 뒤 시한 유지.
- cluster-admin으로 번들과 차트가 끝까지 설치되는지, 운영 권한으로 `status`가 되는지(helm 릴리스는 Secret에 있어 운영 권한으로는 `helm list`가 안 된다; status/doctor는 사용자 자격으로 돌리는 것을 전제, #56에서 확인 필요).
- 설치 이미지 빌드와 발행(지금 digest 0), 파드 안에서 helm이 `HOME=/tmp`로 도는지.

## 설치 이미지 발행과 digest 고정 (#96)

`.github/workflows/installer-image.yml`이 `bootstrap/Containerfile`을 빌드한다.

- 풀 리퀘스트: 빌드만 한다. 권한은 `contents: read`이고 푸시하지 않는다.
- main 푸시와 `v*` 태그 푸시: `publish` 잡만 `packages: write`를 받아 `ghcr.io/<owner>/vws/installer:sha-<커밋>`과 `:<브랜치 또는 태그>`로 푸시한다. 잡 요약에 `VWS_INSTALLER_IMAGE=...@sha256:...` 한 줄을 남긴다.
- CI는 저장소에 커밋하지 않는다. digest 고정은 사람이 한다.
  1. 발행된 실행의 잡 요약에서 `VWS_INSTALLER_IMAGE` 줄을 복사한다(또는 `crane digest ghcr.io/<owner>/vws/installer:<태그>`).
  2. 새 브랜치에서 `scripts/versions.env`의 `VWS_INSTALLER_IMAGE`와 "NOT PUBLISHED YET" 주석을 바꾼다. 커밋 제목은 `ci: 설치 이미지 digest를 고정한다`.
  3. `scripts/bootstrap.sh`로 `bootstrap/bootstrap.yaml`을 다시 만들어 별도 `cli:` 커밋으로 넣고 PR을 연다(`Closes #N`).
- ghcr 패키지는 비공개로 만들어진다. 공개 여부와 클러스터의 pull 자격은 별도 결정이다(확인 필요).
