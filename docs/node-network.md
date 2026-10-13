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
        cidr: 198.18.10.0/24
        gateway: 198.18.10.1
        excludeIPs: [198.18.10.2..198.18.10.9]
        eipPool: 198.18.10.64/26
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
        cidr: 198.18.10.0/24
        gateway: 198.18.10.1
        excludeIPs: [198.18.10.2..198.18.10.9]
        eipPool: 198.18.10.64/26
    direct:
      - {name: prod, nic: enp4s0, vlan: 200, cidr: 192.168.200.0/24, gateway: 192.168.200.1}
      - {name: lab, nic: enp4s0, cidr: 192.168.10.0/24, gateway: 192.168.10.1}
```

이 문서의 세 예는 `vws render` 검증을 통과한다(PR 작성 때 직접 돌렸다).

## 운영 중 변경

`nodes`를 바꾸면 노드 재구성이다. Kube-OVN 인터페이스와 MTU 변경은 kube-ovn-cni 재시작, Ceph 네트워크 변경은 mon 재배포가 따른다. 한 번에 한 망씩 바꾸고 `vws verify-reboot`처럼 노드를 하나씩 확인한다. 무중단 여부는 실제 클러스터에서 재지 않았다(확인 필요).

## 확장 지점 (구현 안 함)

`direct[].mode`는 지금 `ovn-underlay` 하나다. SR-IOV(`sriov`)와 macvtap(`macvtap`)은 #125에 설계만 있다. 노드마다 다른 NIC 이름, IPv6, bond도 같다.

## vws-network로 가는 플래그 (#130)

`nodes.external`이 있으면 `vws render`가 vws-network의 underlay 플래그를 `services.network.flags`에 쓴다(vws-network `docs/underlay.md`). 이 플래그를 `spec.services.network.flags`에 직접 함께 적으면 종료 코드 2다.

| 플래그 | 값 |
| --- | --- |
| `pool` | `external.eipPool` |
| `kube-external-subnet` | `vws-external` |
| `kube-provider-network` | `vws-pn<N>`. N은 external과 direct가 쓰는 NIC 이름을 정렬한 순서(0부터) |
| `kube-underlay-interface` | `external.nic` |
| `kube-underlay-vlan` | `external.vlan`(0은 flat) |
| `kube-underlay-cidr`, `kube-underlay-gateway` | `external.cidr`, `external.gateway` |
| `kube-underlay-exclude` | `external.excludeIPs`를 쉼표로 이은 값. 게이트웨이는 vws-network가 늘 넣는다 |
| `kube-gateway-nodes` | `external.gatewayNodes`(노드 이름, 하나 이상, 중복 없음)를 쉼표로 이은 값. vws-network가 VPC가 있는 동안 이 노드들의 `ovn.kubernetes.io/external-gw=true`를 `-kube-gateway-recheck`(기본 30s)마다 되살린다. kube-ovn-controller가 재시작하면 라벨을 false로 되돌리기 때문이다(#197). 없으면 차트 기본값 빈 값이라 라벨을 건드리지 않는다 |

`kube-gateway-nodes`는 Kube-OVN 데이터플레인일 때만 쓰이고, 그 조건인 `kube-external-subnet`은 `nodes.external`이 이미 넣는다. `kubeconfig`는 더 필요 없다(vws-network main c640ea9 `internal/network/dataplane_kubeovn_open.go`).

`kube-external-subnet`만 있고 `kubeconfig`가 없으면 vws-network는 클러스터 안 ServiceAccount로 Kube-OVN 데이터플레인을 쓴다.

남은 일:
- EIP 풀은 Subnet `excludeIps`에 넣지 않는다. Kube-OVN이 VPC 외부 포트 주소를 풀 안에서 고를 수 있다. 넣을지는 excludeIps 안의 주소를 OvnEip `v4Ip`로 받는지 실측한 뒤 정한다(vws-network#52, 확인 필요).
- `extraExternalSubnets`는 vws-network #53이 모든 Vpc에 넣는다. #53 이전에 만든 Vpc는 Conflict가 나며 vws-network `docs/kube-ovn-conflict-recovery.md`로 복구한다.

## direct 네트워크 플래그 (#131)

`direct[]`가 비어 있지 않을 때만 `vws render`가 아래 두 플래그와 Role 하나를 더한다. 비어 있으면 아무것도 바뀌지 않는다. 두 플래그를 `spec.services.network.flags`에 직접 적으면 종료 코드 2다. vws-network는 Kube-OVN을 외부 Subnet 없이 시작하지 않으므로, `nodes.external`도 `kube-external-subnet` 플래그도 없으면 역시 종료 코드 2다.

| 플래그 | 값 |
| --- | --- |
| `kube-direct-networks` | `direct[]`마다 `{"name","providerNetwork","interface","vlan","cidr","gateway","exclude"}`인 JSON 한 줄. `providerNetwork`는 위의 `vws-pn<N>`이라 같은 NIC의 external과 ProviderNetwork를 함께 쓴다. `exclude`는 `excludeIPs` 그대로이고 없으면 뺀다. `mtu`와 `mode`는 넘기지 않는다 |
| `kube-direct-nad-namespace` | `{namespace: vms}`, 기본 values로 `vws-vms` |

근거: vws-network `8ff82a7`(#67) `cmd/vws-network/main.go:40-41`, `cmd/vws-network/start.go:80`(모르는 키는 거절), `internal/network/dataplane_kubeovn_open.go:56`(direct가 있는데 네임스페이스가 비면 시작 거절).

**VM 네임스페이스는 values `namespaces.vms`다.** #125는 direct NAD를 VM 네임스페이스에 둔다고만 했다. VM은 자기 네임스페이스의 NAD만 붙이므로 vws-compute의 `-vm-namespace`와 같은 키를 쓴다(`docs/service-config.md`). 테넌트 subnet NAD의 `nad` 네임스페이스와는 다르다.

같은 때 network에 `vms` 네임스페이스 Role이 붙는다. `k8s.cni.cncf.io` network-attachment-definitions에 `get`, `create`다. vws-network가 시작 때 `vws-direct-<name>`을 만들고 지우지 않기 때문에 `delete`는 없다(vws-network `docs/rbac.md` "direct NAD Role"). 렌더는 `services.network.rbac`을 차트 기본 목록에 이 규칙을 더한 목록으로 쓴다.

남은 일: 실제 클러스터에서 direct Subnet에 붙은 VM이 물리 게이트웨이에 닿는지는 확인 필요다(vws-network `docs/underlay.md`).
