# VPC VM MTU (vws-deploy#236, vws-network#93)

AWS와 같게 VPC 안의 VM은 MTU 9001을 받고, IGW/NAT/EIP로 나가는 경로는 1500이다.
설치기는 어떤 NIC이나 호스트 MTU도 만들거나 바꾸지 않는다. 사용자가 OS에서 미리 설정한 물리(underlay)
NIC MTU를 읽고, 모자라면 VWSCluster의 VM MTU만 낮춘다. 실제 적용은 vws-network가 Subnet과 DHCP로 한다.

## 필드

| 필드 | 범위, 기본 | 뜻 |
| --- | --- | --- |
| `spec.network.vmMTU` | 68–9001, 없으면 9001 | VM 인터페이스 MTU. 차트가 vws-network `-vm-mtu`로 넘긴다 |
| `spec.network.underlayMTU` | 1280–9216 | 터널이 지나가는 노드 NIC의 MTU. preflight가 읽은 값을 쓴다 |
| `spec.network.tunnelOverhead` | 0–200, 없으면 100 | Geneve가 더하는 바이트 |
| `spec.network.ipv4Only` | bool | 참이고 `tunnelOverhead`가 없으면 오버헤드 60(IPv4 바깥 헤더 기준 약 58 + 여유) |

스키마에 `default`를 두지 않는다. `vmMTU`가 비어 있어야 "9001을 underlay에 맞게 낮춘다"와 "사용자가 정한 값"을 구분할 수 있기 때문이다.

## 계산

- underlay MTU = `underlayMTU`와 `spec.network.nodes`의 터널 링크(`tunnel`, 없으면 `management`) `mtu` 중 작은 값. 둘 다 없으면 모른다.
- 상한 = underlay MTU − 오버헤드. 9001에는 9101이 필요하고 9216을 권장한다.
- `vws render`: `vmMTU`가 없으면 min(9001, 상한), 있으면 그 값. 상한보다 크면 종료 코드 2.
- kube-ovn-cni `--mtu`도 같은 오버헤드로 터널 MTU − 오버헤드(기본 100이라 기존과 같다).

## preflight

`vws preflight --links FILE`은 노드마다 터널 인터페이스 MTU를 읽어 최솟값을 underlay로 본다. 모자라면
`vm-mtu` WARN을 내고 `kubectl patch vwsclusters vws`로 `vmMTU`(낮춘 값)와 `underlayMTU`(읽은 값)만 쓴다.
`--links`가 없으면 `underlayMTU`로 판단하고, 그것도 없으면 WARN(미확인)이다.

예: 터널 NIC이 1500이면 VM MTU 1400(오버헤드 100), `ipv4Only`면 1440.

## vws-test 값

제품 기본은 9001이다. vws-test는 OpenStack geneve 위라 노드 NIC MTU가 8942이므로 낮춘 값을 쓴다.
preflight가 `--links`에서 8942를 읽으면 아래 값을 직접 쓰고, 손으로 넣을 때는 `vws reconfigure --set`을 쓴다.

```yaml
spec:
  network:
    underlayMTU: 8942
    vmMTU: 8842        # 8942 - 100. ipv4Only: true면 8882까지 가능
```

```sh
vws reconfigure --set network.underlayMTU=8942 --set network.vmMTU=8842
```

## 나가는 1500 경로

번들의 Kube-OVN(`scripts/versions.env` KUBEOVN_VERSION)은 OVN `gateway_mtu`를 설정하지 않아 VPC 라우터가 1500 외부망으로 가는 큰 DF 패킷에
ICMP "fragmentation needed"를 보내지 않는다(vws-network `docs/mtu.md`). #237에서 다룬다.

## 확인 필요

- vws-test에서 낮아진 MTU로 VM 간 통신(큰 패킷 ping -M do)을 아직 확인하지 않았다.
- vws-test의 vws-network 이미지는 `-vm-mtu`가 있는 main(#94) 이후로 다시 빌드해야 한다. 그 전 이미지는 플래그를 몰라 시작하지 않는다.
