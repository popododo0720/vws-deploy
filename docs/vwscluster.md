# VWSCluster CR, 프로필, `vws render` (vws-deploy#53)

설계는 vws-deploy#43(0절, 2절)이다. 설정의 진실은 릴리스 네임스페이스의 `VWSCluster` CR 하나(이름 `vws`)이고, values 파일과 렌더된 YAML은 거기서 만든 산출물이다.

## 'CRD 없음' 규칙의 예외 (한 곳에 적는다)

vws-deploy#2의 결정(VWS 전용 CRD를 만들지 않는다, 서비스 메타데이터는 annotation과 ConfigMap)은 서비스 메타데이터 저장소에 관한 것이다. 배포 설정 CRD 하나를 사용자가 **명시적 예외로 승인**했다(#43 0절). 범위:

- 허용: `charts/vws/crds/vwsclusters.yaml`의 `VWSCluster` CRD 하나, `v1alpha1`, 네임스페이스 범위 싱글턴 `vws`, 배포 설정만.
- 금지: 서비스 메타데이터를 CR로 저장, 다른 VWS 전용 CRD, 서비스 코드가 CR을 읽거나 쓰기(서비스 RBAC에 `vwsclusters` 없음), CR을 보고 움직이는 컨트롤러.
- `status`는 설치기가 `vws apply` 끝에 쓰는 `observedGeneration`, `appliedValuesHash`, `conditions`뿐이다(#54).
- 언인스톨해도 CRD와 CR은 남는다. Helm은 `crds/`의 파일을 설치만 하고 지우지 않으며, CRD에 `helm.sh/resource-policy: keep`도 적었다.
- 검사: `scripts/guard.sh crd`(R18).

## API 그룹

API 그룹은 `charts/vws/crds/vwsclusters.yaml`의 `spec.group` **한 곳**에만 쓴다. 도메인을 확보할 때까지의 임시 이름이다(#43 결정 9). `vws render`와 가드는 그 파일에서 그룹을 읽는다. 예제 CR(`examples/vwscluster/*.yaml`)의 `apiVersion`은 문법상 그룹을 다시 적을 수밖에 없어서, 가드가 CRD와 같은지 검사하고 그 밖의 파일에 그룹이 나오면 실패한다. 바꿀 때는 CRD와 예제 세 파일을 함께 바꾼다.

## CR 스키마

| 필드 | 값 | values로 |
| --- | --- | --- |
| `spec.profile` | `single-node`, `small`, `ha` (필수) | `profiles/<값>.yaml`을 바탕으로 쓴다 |
| `spec.version` | 이미지 태그, 비면 차트 appVersion | `global.tag` |
| `spec.storage.driver` | `rook-ceph`(기본), `existing` | `existing`이면 `global.storageClass`와 storage의 `kube-class-standard`, `kube-class-ssd` |
| `spec.storage.storageClass` | `existing`일 때 필수, 아니면 금지 | 위와 같음 |
| `spec.storage.s3.rgw.{enabled,endpoint,bucketPrefix,existingSecret}` | RGW 객체 백엔드(#108). `bucketPrefix`는 `^([a-z0-9][a-z0-9-]{0,17})?$`, `existingSecret`은 릴리스 네임스페이스의 Secret 이름(키 `access-key`, `secret-key`) | `services.storage.s3.rgw` 그대로. 없으면 객체 백엔드는 꺼진 채(기본 fake). `vws apply`가 RGW 설정을 지우지 않는다 |
| `spec.network.driver` | `kube-ovn` 하나 | 바꾸는 값 없음 |
| `spec.network.nodes`, `spec.network.direct` | 노드 NIC별 망과 direct provider network. 규칙과 예는 `docs/node-network.md`(#125) | Kube-OVN, Ceph 번들 설정과 `services.network.flags` |
| `spec.observability.otlpEndpoint` | 비어 있어야 한다 | 차트에 관측성 값이 아직 없어 값이 있으면 종료 코드 2 |
| `spec.services.<이름>.{replicas,cpu,memory,flags}` | `charts/vws/values.yaml`에 있는 서비스만 | 같은 이름의 `services.<이름>` 키, `flags`는 합친다 |
| `spec.secrets.<이름>.secretName` | Secret 이름 참조만 | 지금은 받는 values가 없어 비어 있어야 한다 |

- 비밀 값 필드는 없다. 필드나 플래그 이름이 비밀처럼 보이면(`password`, `token`, `secret`, `credential`, `key` 등, `secrets`/`secretName`/`existingSecret` 제외) `vws render`가 거절하고 가드가 CRD 스키마에서 거절한다.
- `existing`의 VolumeSnapshotClass(`kube-snapshot-class`)는 바꾸지 않는다(확인 필요: 기존 StorageClass에 맞는 스냅숏 클래스를 CR에 둘지).

## 프로필

`profiles/<이름>.yaml`은 `charts/vws/values.yaml` 위에 겹치는 값 파일이고 값만 바꾼다. 세 프로필은 같은 키를 갖는다(R19).

| | single-node | small | ha |
| --- | --- | --- | --- |
| 무상태 replicas | 2(스키마 최소, 한 노드에 둘) | 2 | 3 |
| 상태 서비스 | 1, 20Gi | 1, 20Gi | 1, 20Gi |
| cpu/memory (추정, T2 실측 전) | 100m/256Mi | 200m/512Mi | 500m/1Gi |
| `global.topologySpread` | soft | soft | hard (`DoNotSchedule`) |
| Ceph 오버레이 | `platform/rook-ceph-cluster.single-node.values.yaml`: mon 1, mgr 1, 블록 풀과 오브젝트 스토어 size 1, 장애 도메인 osd. **데이터 보호 없음** | 없음(기본: mon 3, size 3, host) | `platform/rook-ceph-cluster.ha.values.yaml`: mon 3, size 3, host를 명시 |

새 values 키는 셋이다: `global.profile`(기록용), `global.topologySpread`(#43 2절의 "차트에 새 값 키 하나"), `global.tag`(`spec.version`의 대상, 이미지 참조는 여전히 템플릿 한 곳에서 만든다).

## 확인 필요 (실제 클러스터, T1 이상)

- API 서버가 CRD를 받아들이는지와 `x-kubernetes-validations`의 CEL 규칙(이름 `vws`). kubeconform 카탈로그에 CRD 스키마가 없어 가드가 구조 규칙(타입, 저장 버전 하나)을 직접 본다.
- Ceph size 1 설정(`requireSafeReplicaSize: false`)과 mon 1이 Rook에서 실제로 뜨는지.
- 프로필 리소스 숫자.
