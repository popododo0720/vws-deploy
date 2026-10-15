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
| `spec.version` | 이미지 태그, 비면 차트 appVersion. `services.<이름>.imageTag`가 있는 서비스(지금은 console-api, compute)는 움직이지 않는다 | `global.tag` |
| `spec.storage.driver` | `rook-ceph`(기본), `existing`, `nfs` | `existing`이면 `global.storageClass`와 storage의 `kube-class-standard`, `kube-class-ssd`. `nfs`이면 두 클래스가 `vws-nfs`, `kube-volume-mode=Filesystem`, `kube-snapshot-class=`(빈 값, 스냅숏 없음). 상태 볼륨은 기본 클래스에 남는다 |
| `spec.storage.storageClass` | `existing`일 때 필수, 아니면 금지(범용 CSI 드라이버는 모두 `existing`이다) | 위와 같음 |
| `spec.storage.volumeMode` | `existing`일 때만, `Block`(기본) 또는 `Filesystem` | `Filesystem`이면 `kube-volume-mode=Filesystem`과 storage의 `datavolumes.cdi.kubevirt.io` 권한(vws-storage#73) |
| `spec.storage.nfs.{server,share}` | `nfs`일 때 둘 다 필수, 아니면 금지. `share`는 `/`로 시작 | StorageClass `vws-nfs`의 파라미터. `vws apply`가 csi-driver-nfs와 그 StorageClass를 플랫폼 번들로 올린다(#170, `docs/platform-bundle.md`). 다른 driver면 둘 다 올리지 않는다 |
| `spec.storage.s3.rgw.{enabled,endpoint,bucketPrefix,existingSecret}` | RGW 객체 백엔드(#108). `bucketPrefix`는 `^([a-z0-9][a-z0-9-]{0,17})?$`, `existingSecret`은 릴리스 네임스페이스의 Secret 이름(키 `access-key`, `secret-key`) | `services.storage.s3.rgw` 그대로. 없으면 객체 백엔드는 꺼진 채(기본 fake). `vws apply`가 RGW 설정을 지우지 않는다 |
| `spec.network.driver` | `kube-ovn` 하나 | 바꾸는 값 없음 |
| `spec.network.nodes`, `spec.network.direct` | 노드 NIC별 망과 direct provider network. 규칙과 예는 `docs/node-network.md`(#125) | Kube-OVN, Ceph 번들 설정과 `services.network.flags` |
| `spec.network.vmMTU`, `underlayMTU`, `tunnelOverhead`, `ipv4Only` | VPC VM MTU(#236, vws-network#93). `vmMTU` 68–9001, 없으면 9001(AWS)을 underlay가 실어 나르는 값으로 낮춘다. `underlayMTU`는 OS가 이미 설정한 터널 NIC MTU(1280–9216). `tunnelOverhead` 0–200, 없으면 100, `ipv4Only: true`면 60. 9001에는 underlay 9101 이상이 필요하고 9216을 권장한다. 설치기는 NIC MTU를 바꾸지 않는다. 자세한 것은 `docs/vm-mtu.md` | `services.network.flags.vm-mtu`(`-vm-mtu`). `vmMTU`가 underlay − 오버헤드보다 크면 종료 코드 2. 같은 오버헤드가 kube-ovn-cni `--mtu`에도 쓰인다 |
| `spec.observability.otlpEndpoint` | 비어 있어야 한다 | 차트에 관측성 값이 아직 없어 값이 있으면 종료 코드 2 |
| `spec.services.<이름>.{replicas,cpu,memory,flags}` | `charts/vws/values.yaml`에 있는 서비스만. `console-api.replicas`는 1만(세션을 메모리에 둔다, popododo0720/vws-console-api#95). 다른 값은 render가 종료 코드 2로 거절 | 같은 이름의 `services.<이름>` 키, `flags`는 합친다 |
| `spec.secrets.<이름>.secretName` | Secret 이름 참조만 | 지금은 받는 values가 없어 비어 있어야 한다 |

- 비밀 값 필드는 없다. 필드나 플래그 이름이 비밀처럼 보이면(`password`, `token`, `secret`, `credential`, `key` 등, `secrets`/`secretName`/`existingSecret` 제외) `vws render`가 거절하고 가드가 CRD 스키마에서 거절한다.
- `nfs`와 `Filesystem`은 vws-storage#73의 `-kube-volume-mode` 플래그가 있는 이미지가 필요하다. 그 전 이미지는 모르는 플래그로 시작하지 못한다. 기본(`Block`)은 플래그를 렌더하지 않는다.
- NFS 익스포트는 미리 있어야 하고 모든 노드에서 마운트할 수 있어야 한다.
- `existing`의 VolumeSnapshotClass(`kube-snapshot-class`)는 바꾸지 않는다(확인 필요: 기존 StorageClass에 맞는 스냅숏 클래스를 CR에 둘지).

## 프로필

`profiles/<이름>.yaml`은 `charts/vws/values.yaml` 위에 겹치는 값 파일이고 값만 바꾼다. 세 프로필은 같은 키를 갖는다(R19).

| | single-node | small | ha |
| --- | --- | --- | --- |
| 무상태 replicas | 2(스키마 최소, 한 노드에 둘) | 2 | 3 |
| console-api replicas | 1 | 1 | 1 |
| 상태 서비스 | 1, 20Gi | 1, 20Gi | 1, 20Gi |
| cpu/memory (추정, T2 실측 전) | 100m/256Mi | 200m/512Mi | 500m/1Gi |
| `global.topologySpread` | soft | soft | hard (`DoNotSchedule`) |
| Ceph 오버레이 | `platform/rook-ceph-cluster.single-node.values.yaml`: mon 1, mgr 1, 블록 풀과 오브젝트 스토어 size 1, 장애 도메인 osd. **데이터 보호 없음** | 없음(기본: mon 3, size 3, host) | `platform/rook-ceph-cluster.ha.values.yaml`: mon 3, size 3, host를 명시 |

새 values 키는 셋이다: `global.profile`(기록용), `global.topologySpread`(#43 2절의 "차트에 새 값 키 하나"), `global.tag`(`spec.version`의 대상, 이미지 참조는 여전히 템플릿 한 곳에서 만든다).

## 확인 필요 (실제 클러스터, T1 이상)

- API 서버가 CRD를 받아들이는지와 `x-kubernetes-validations`의 CEL 규칙(이름 `vws`). kubeconform 카탈로그에 CRD 스키마가 없어 가드가 구조 규칙(타입, 저장 버전 하나)을 직접 본다.
- Ceph size 1 설정(`requireSafeReplicaSize: false`)과 mon 1이 Rook에서 실제로 뜨는지.
- 프로필 리소스 숫자.

## console-api는 레플리카 1 (popododo0720/vws-console-api#95)

console-api는 세션을 메모리에 둔다. 두 번째 파드나 HPA가 있으면 요청마다 다른 파드로 가서 세션이 갈라진다. 그래서:

- 차트 기본값과 모든 프로필(`profiles/*.yaml`)이 `services.console-api.replicas: 1`이다.
- `values.schema.json`: `services.console-api.replicas`는 `const: 1`(필수), `autoscaling` 같은 값은 없다. 다른 무상태 서비스는 그대로 최소 2.
- `scripts/check_render.py` R4: Deployment console-api가 1이 아니면 `must have exactly one replica: sessions are held in memory`. HPA는 control-plane 종류가 아니라 R10이 이미 거절한다.
- `lib/render.py`: CR의 `spec.services.console-api.replicas`가 1이 아니면 종료 코드 2.
- 한계: 레플리카 1이라 console-api 롤아웃과 노드 장애 동안 세션이 끊기고, PDB(maxUnavailable 1)는 그 파드의 축출을 막지 않는다. 공유 세션 저장소가 생기면 이 고정을 푼다.

## console-api -console-origins (필수), -trusted-proxies (선택) (popododo0720/vws-console-api#137)

console-api#137부터 로그인과 쿠키 쓰기 요청은 Origin이 콘솔 자신의 호스트이거나 `-console-origins`에 있어야 하고, 아니면 403이다. 콘솔 Service는 LoadBalancer라 vws가 렌더링 때 공개 주소를 알 수 없으므로 운영자가 CR에 적는다:

```yaml
spec:
  services:
    console-api:
      flags:
        console-origins: https://console.example        # 쉼표로 여러 개, scheme://host[:port], 경로 없음
        # trusted-proxies: 10.0.0.0/8,192.0.2.1          # 선택. 비우면 플래그를 넘기지 않는다
```

- 기본값이 없다. `lib/render.py`는 없으면 종료 코드 2(`spec.services.console-api.flags.console-origins: required`), `vws init`도 묻는다.
- `scripts/check_render.py` R15: `-console-origins`가 없거나 항목이 `scheme://host[:port]`가 아니면 거절, `-trusted-proxies` 항목이 IP/CIDR가 아니면 거절.
- `-trusted-proxies`는 값이 있을 때만 넘긴다. #137(5b42eb7)부터 `-console-origins`와 함께 지원한다.
- 이 차트는 console-api 이미지 #137(5b42eb7) 이상이 필요하다. 그 전 이미지는 모르는 플래그 `-console-origins`(그리고 설정했다면 `-trusted-proxies`)로 시작하지 못한다.
- 이미지 고정(#280): `charts/vws/values.yaml`의 `services.console-api.imageTag: sha-a003d96`이 console-api만 `ghcr.io/popododo0720/vws/console-api:sha-a003d96`로 고정한다. 이 빌드(console-api#156)부터 자격 증명을 env(`VWS_CONSOLE_TOKEN`, `VWS_ACCESS_KEY`, `VWS_SECRET_KEY`)로 읽고, 이 차트는 자격 증명 플래그를 넘기지 않으므로 그 전 이미지로는 `-backend=grpc`가 시작하지 않는다. 다른 서비스는 그대로 `global.tag` → appVersion이다. 스키마는 태그 모양(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)만 본다.
  - 이 태그는 vws-console-api의 `publish.yml`(main 푸시마다 ko로 `sha-<커밋 7자>`, 라벨 `org.opencontainers.image.revision=<전체 SHA>`)이 a003d96 머지 때 발행했다(digest sha256:dbd6ab8a…, 2026-10-15 익명 pull 확인). vws-test에서의 pull과 기동은 확인 필요.
  - a003d96을 고른 이유: 자격 증명 env를 읽는 첫 빌드다(S1). 이전 고정은 5b42eb7(#267에서 vws-test 실측)이었다. 더 새 main 커밋으로 옮길 때는 그 커밋이 발행된 뒤 `imageTag`만 바꾸고 vws-test에서 다시 잰다.
  - 패키지 공개 여부는 워크플로가 바꾸지 않는다. 비공개면 노드에 pull 자격 증명이 필요하다(확인 필요).
- compute 고정: `services.compute.imageTag: sha-07b591c`(vws-compute#91, 07b591c = #89 99a45e4 + publish.yml). vws-compute `publish.yml`이 머지 때 발행했다(digest sha256:292268f3…, 2026-10-15 익명 확인, 라벨 revision 07b591c, user 65532). ko 이미지라 entrypoint가 `/ko-app/vws-compute`다. 차트는 `args`만 넘기고 `command`는 넘기지 않는다.
- vws-test 실측: 기본 설치(`-iam=static`)에서 `-console-origins`를 콘솔 공개 주소로 두면 로그인 없이 콘솔 API가 모두 200이다. 필수여도 기본 설치를 막지 않는다.
