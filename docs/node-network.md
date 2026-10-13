# 노드망 (`spec.network.nodes`, `spec.network.direct`)

설계와 결정은 vws-deploy#125다. 이 문서는 운영자가 CR에 적는 법과 NIC 1장, 2장, 4장 예를 담는다.

VWS는 호스트 네트워크를 설정하지 않는다(R10). NIC, VLAN 서브인터페이스(`<nic>.<vlan>`), 노드 IP, MTU는 OS가 먼저 만든다. CR은 그 상태를 적고, `vws preflight`가 대조하고, `vws apply`가 Kube-OVN, Rook, vws-network 설정을 거기서 만든다.

## 필드

| 필드 | 뜻 |
| --- | --- |
| `nodes.management` | 노드 IP가 있는 망. `nodes`를 쓰면 필수 |
| `nodes.tunnel` | Kube-OVN geneve 터널. 없으면 management를 쓴다. Pod/VM 오버레이 MTU는 이 `mtu` − 100 |
| `nodes.storage.public`, `.cluster` | Ceph public/cluster 망. 없으면 Ceph 네트워크 설정을 바꾸지 않는다. cluster가 없으면 public 하나 |
| `nodes.external` | EIP/NAT용 underlay. `gateway`, `eipPool` 필수. vws-network가 ProviderNetwork, Vlan, Subnet `vws-external`을 만든다 |
| `direct[]` | VM 포트가 물리망에 바로 붙는 provider network. `name`, `gateway` 필수, `vlan: 0`이면 flat |

모든 망은 `nic`(15자 이하, 모든 노드에서 같은 이름), `vlan`(0~4094, 0은 untagged), `cidr`(정규형 IPv4), `mtu`(1280~9216, 기본 1500)를 갖는다.

## 규칙

- NIC와 VLAN이 같은 두 망은 같은 L2라서 `cidr`과 `mtu`가 같아야 한다. 다른 망끼리 CIDR이 겹치면 안 된다.
- external이나 direct가 쓰는 NIC는 OVS 브리지로 넘어간다. 그 NIC에 호스트 망을 함께 두려면 untagged 하나만 된다. 호스트 VLAN 서브인터페이스는 브리지 편입 뒤 트래픽을 받지 못한다.
- underlay끼리(external, direct)는 VLAN이 다르면 한 NIC를 같이 쓴다.
- `eipPool`은 external `cidr` 안에 있고 `gateway`, `excludeIPs`와 겹치지 않는다. 풀에는 기본값이 없다(#148).
- 규칙을 어기면 `vws render`, `vws init`, `vws apply`가 종료 코드 2로 멈춘다.

## NIC 1장 (기본)

`nodes`를 쓰지 않는다. 모든 망이 기본 경로 NIC 하나에 있고 지금과 동작이 같다. EIP 풀만 준다.

```yaml
spec:
  network:
    driver: kube-ovn
  services:
    network:
      flags:
        pool: 198.18.0.0/24
```

NIC 1장에 외부망 underlay까지 두려면 management를 untagged로 두고 external을 VLAN으로 싣는다. 노드 IP가 OVS 브리지로 옮겨지므로 실측 전에는 권하지 않는다(확인 필요).

## NIC 2장

`enp1s0`에 호스트 망을 VLAN으로 나누어 싣고, `enp2s0`은 underlay 전용이다.

```yaml
spec:
  network:
    driver: kube-ovn
    nodes:
      management: {nic: enp1s0, cidr: 10.77.0.0/24}
      tunnel: {nic: enp1s0, vlan: 20, cidr: 10.20.0.0/24, mtu: 9000}
      storage:
        public: {nic: enp1s0, vlan: 30, cidr: 10.30.0.0/24, mtu: 9000}
      external:
        nic: enp2s0
        vlan: 100
        cidr: 203.0.113.0/24
        gateway: 203.0.113.1
        excludeIPs: [203.0.113.2..203.0.113.9]
        eipPool: 203.0.113.64/26
    direct:
      - {name: prod, nic: enp2s0, vlan: 200, cidr: 192.168.200.0/24, gateway: 192.168.200.1}
```

## NIC 4장

망마다 NIC 하나. Ceph는 public과 cluster를 VLAN으로 나눈다. direct 하나는 flat이다.

```yaml
spec:
  network:
    driver: kube-ovn
    nodes:
      management: {nic: enp1s0, cidr: 10.77.0.0/24}
      tunnel: {nic: enp2s0, cidr: 10.20.0.0/24, mtu: 9000}
      storage:
        public: {nic: enp3s0, cidr: 10.30.0.0/24, mtu: 9000}
        cluster: {nic: enp3s0, vlan: 31, cidr: 10.31.0.0/24, mtu: 9000}
      external:
        nic: enp4s0
        vlan: 100
        cidr: 203.0.113.0/24
        gateway: 203.0.113.1
        excludeIPs: [203.0.113.2..203.0.113.9]
        eipPool: 203.0.113.64/26
    direct:
      - {name: prod, nic: enp4s0, vlan: 200, cidr: 192.168.200.0/24, gateway: 192.168.200.1}
      - {name: lab, nic: enp4s0, cidr: 192.168.10.0/24, gateway: 192.168.10.1}
```

이 예는 `tests/fixtures/render/node-network.yaml`과 같은 모양이고 render 테스트가 통과시킨다.

## 운영 중 변경

`nodes`를 바꾸면 노드 재구성이다. Kube-OVN 인터페이스와 MTU 변경은 kube-ovn-cni 재시작, Ceph 네트워크 변경은 mon 재배포가 따른다. 한 번에 한 망씩 바꾸고 `vws verify-reboot`처럼 노드를 하나씩 확인한다. 무중단 여부는 실제 클러스터에서 재지 않았다(확인 필요).

## 확장 지점 (구현 안 함)

`direct[].mode`는 지금 `ovn-underlay` 하나다. SR-IOV(`sriov`)와 macvtap(`macvtap`)은 #125에 설계만 있다. 노드마다 다른 NIC 이름, IPv6, bond도 같다.
