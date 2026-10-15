# Node networks (`spec.network.nodes`, `spec.network.direct`)

The design and decisions are in vws-deploy#125. This document shows how an operator writes them in the CR, with examples for 1, 2 and 4 NICs.

VWS does not configure host networking (R10). The OS creates NICs, VLAN subinterfaces (`<nic>.<vlan>`), node IPs and MTUs first. The CR describes that state, `vws preflight` compares against it, and `vws apply` derives the Kube-OVN, Rook and vws-network settings from it.

## Fields

| Field | Meaning |
| --- | --- |
| `nodes.management` | The network with the node IPs. Required when `nodes` is used |
| `nodes.tunnel` | The Kube-OVN geneve tunnel. Uses management when unset. The Pod/VM overlay MTU is this `mtu` − 100 |
| `nodes.storage.public`, `.cluster` | Ceph public/cluster networks. When unset, Ceph network settings are left alone. Without cluster, public alone |
| `nodes.external` | Underlay for EIP/NAT. `gateway` and `eipPool` are required. vws-network creates the ProviderNetwork, Vlan and Subnet `vws-external` |
| `direct[]` | Provider networks whose VM ports attach straight to the physical network. `name` and `gateway` are required; `vlan: 0` is flat |

Every network has `nic` (at most 15 characters, the same name on every node), `vlan` (0-4094, 0 is untagged), `cidr` (canonical IPv4) and `mtu` (1280-9216, default 1500).

## Rules

- Two networks with the same NIC and VLAN are the same L2, so their `cidr` and `mtu` must match. CIDRs of different networks must not overlap.
- A NIC used by external or direct moves into an OVS bridge. A host network can share that NIC only as a single untagged one; host VLAN subinterfaces stop receiving traffic once the NIC joins the bridge.
- Underlays (external, direct) may share a NIC when their VLANs differ.
- `eipPool` lies inside the external `cidr` and does not overlap `gateway` or `excludeIPs`. The pool has no default (#148).
- On a rule violation, `vws render`, `vws init` and `vws apply` stop with exit code 2.

## One NIC (default)

Do not use `nodes`. Every network is on the single default-route NIC, and behaviour is the same as now. Only give the EIP pool.

```yaml
spec:
  network:
    driver: kube-ovn
  services:
    network:
      flags:
        pool: 198.18.0.0/24
```

To carry the external underlay on a single NIC too, keep management untagged and put external on a VLAN. The node IP moves to the OVS bridge, so this is not recommended until measured (needs checking).

## Two NICs

`enp1s0` carries the host networks split by VLAN, and `enp2s0` is for underlays only.

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

## Four NICs

One NIC per network. Ceph splits public and cluster by VLAN. One direct network is flat.

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

The three examples in this document pass `vws render` validation (run by hand when the PR was written).

## Changes in operation

Changing `nodes` reconfigures the nodes. A Kube-OVN interface or MTU change restarts kube-ovn-cni, and a Ceph network change redeploys the mons. Change one network at a time and check node by node, as `vws verify-reboot` does. Whether this is disruption-free was not measured on a real cluster (needs checking).

## Extension points (not implemented)

`direct[].mode` is only `ovn-underlay` for now. SR-IOV (`sriov`) and macvtap (`macvtap`) exist only as designs in #125, as do per-node NIC names, IPv6 and bonds.

## Flags passed to vws-network (#130)

When `nodes.external` is set, `vws render` writes vws-network's underlay flags into `services.network.flags` (vws-network `docs/underlay.md`). Writing these flags in `spec.services.network.flags` as well is exit code 2.

| Flag | Value |
| --- | --- |
| `pool` | `external.eipPool` |
| `kube-external-subnet` | `vws-external` |
| `kube-provider-network` | `vws-pn<N>`. N is the position (from 0) in the sorted list of NIC names used by external and direct |
| `kube-underlay-interface` | `external.nic` |
| `kube-underlay-vlan` | `external.vlan` (0 is flat) |
| `kube-underlay-cidr`, `kube-underlay-gateway` | `external.cidr`, `external.gateway` |
| `kube-underlay-exclude` | `external.excludeIPs` joined with commas. vws-network always adds the gateway |
| `kube-gateway-nodes` | `external.gatewayNodes` (node names, one or more, no duplicates) joined with commas. While a VPC exists, vws-network restores `ovn.kubernetes.io/external-gw=true` on these nodes every `-kube-gateway-recheck` (default 30s), because kube-ovn-controller resets the label to false when it restarts (#197). When unset, the chart default is empty and the label is left alone |

`kube-gateway-nodes` is used only with the Kube-OVN data plane, whose condition, `kube-external-subnet`, `nodes.external` already sets. `kubeconfig` is no longer needed (vws-network main c640ea9 `internal/network/dataplane_kubeovn_open.go`).

With `kube-external-subnet` and no `kubeconfig`, vws-network uses the Kube-OVN data plane through its in-cluster ServiceAccount.

Remaining work:
- The EIP pool is not put into the Subnet's `excludeIps`, so Kube-OVN may pick a VPC external port address from inside the pool. Whether to add it is decided after measuring whether an address inside excludeIps is accepted as an OvnEip `v4Ip` (vws-network#52, needs checking).
- vws-network #53 adds `extraExternalSubnets` to every Vpc. A Vpc created before #53 hits a Conflict and is recovered with vws-network `docs/kube-ovn-conflict-recovery.md`.

## Direct network flags (#131)

Only when `direct[]` is not empty does `vws render` add the two flags below and one Role. When it is empty nothing changes. Writing the two flags in `spec.services.network.flags` directly is exit code 2. vws-network does not start Kube-OVN without an external Subnet, so having neither `nodes.external` nor a `kube-external-subnet` flag is also exit code 2.

| Flag | Value |
| --- | --- |
| `kube-direct-networks` | One JSON line with `{"name","providerNetwork","interface","vlan","cidr","gateway","exclude"}` per `direct[]` entry. `providerNetwork` is the `vws-pn<N>` above, so external on the same NIC shares the ProviderNetwork. `exclude` is `excludeIPs` as is, and is omitted when unset. `mtu` and `mode` are not passed |
| `kube-direct-nad-namespace` | `{namespace: vms}`, `vws-vms` with the default values |

Evidence: vws-network `8ff82a7` (#67) `cmd/vws-network/main.go:40-41`, `cmd/vws-network/start.go:80` (unknown keys are refused), `internal/network/dataplane_kubeovn_open.go:56` (refuses to start when direct is set but the namespace is empty).

**The VM namespace is values `namespaces.vms`.** #125 only said direct NADs live in the VM namespace. A VM attaches only NADs in its own namespace, so this uses the same key as vws-compute's `-vm-namespace` (`docs/service-config.md`). It differs from the `nad` namespace of tenant subnet NADs.

At the same time network gets a Role in the `vms` namespace: `get` and `create` on `k8s.cni.cncf.io` network-attachment-definitions. vws-network creates `vws-direct-<name>` at start and never deletes it, so there is no `delete` (vws-network `docs/rbac.md` "direct NAD Role"). The render writes `services.network.rbac` as the chart's default list plus this rule.

Remaining work: whether a VM on a direct Subnet reaches the physical gateway on a real cluster needs checking (vws-network `docs/underlay.md`).
