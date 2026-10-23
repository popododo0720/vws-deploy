# VPC VM MTU (vws-deploy#236, vws-network#93)

As on AWS, VMs inside a VPC get MTU 9001, and paths out through the IGW/NAT/EIP are 1500.
The installer never creates or changes any NIC or host MTU. It reads the physical (underlay) NIC MTU that
the user set in the OS beforehand and, if it is too small, lowers only the VM MTU in the VWSCluster. vws-network applies it through the Subnet and DHCP.

## Fields

| Field | Range, default | Meaning |
| --- | --- | --- |
| `spec.network.vmMTU` | 68–9001, 9001 when unset | VM interface MTU. The chart passes it to vws-network as `-vm-mtu` |
| `spec.network.underlayMTU` | 1280–9216 | MTU of the node NIC the tunnel crosses. Uses the value preflight read |
| `spec.network.tunnelOverhead` | 0–200, 100 when unset | Bytes Geneve adds |
| `spec.network.ipv4Only` | bool | When true and `tunnelOverhead` is unset, the overhead is 60 (about 58 for an IPv4 outer header, plus margin) |

The schema has no `default`. `vmMTU` must be able to be empty so that "lower 9001 to fit the underlay" can be told apart from "a value the user chose".

## Calculation

- underlay MTU = the smaller of `underlayMTU` and the `mtu` of the tunnel link in `spec.network.nodes` (`tunnel`, else `management`). If neither exists, it is unknown.
- Upper bound = underlay MTU − overhead. 9001 needs 9101; 9216 is recommended.
- `vws render`: without `vmMTU`, min(9001, upper bound); with it, that value. Above the upper bound, exit code 2.
- kube-ovn-cni `--mtu` uses the same overhead: tunnel MTU − overhead (the default of 100 keeps the previous value).

## preflight

`vws preflight --links FILE` reads the tunnel interface MTU on each node and takes the minimum as the underlay. If it is too small,
it emits a `vm-mtu` WARN and writes only `vmMTU` (the lowered value) and `underlayMTU` (the read value) with `kubectl patch vwsclusters vws`.
Without `--links` it decides from `underlayMTU`, and without that it is a WARN (unconfirmed).

Example: with a 1500 tunnel NIC the VM MTU is 1400 (overhead 100), or 1440 with `ipv4Only`.

## vws-test values

The product default is 9001. vws-test runs on OpenStack geneve, where the node NIC MTU is 8942, so it uses a lowered value.
When preflight reads 8942 from `--links` it writes the values below itself; to set them by hand use `vws reconfigure --set`.

```yaml
spec:
  network:
    underlayMTU: 8942
    vmMTU: 8842        # 8942 - 100. Up to 8882 with ipv4Only: true
```

```sh
vws reconfigure --set network.underlayMTU=8942 --set network.vmMTU=8842
```

## The 1500 path out

The bundle's Kube-OVN (`scripts/versions.env` KUBEOVN_VERSION) does not set OVN `gateway_mtu` and has no field for it.
`platform.py` adds the `vws-ovn-gateway-mtu` Deployment (#237), also when the external subnet comes only from
`spec.services.network.flags.kube-external-subnet` (#246); it sets `options:gateway_mtu` on each VPC port to that subnet.
OVN then answers an oversized IPv4 packet with ICMP fragmentation-needed (type 3 code 4) and, when the port has an IPv6
address, an oversized IPv6 packet with ICMPv6 Packet Too Big (type 2), so the router can signal MTU exhaustion for
both families. The live check on vws-test is still open in #246.

## Needs checking

- VM-to-VM traffic at the lowered MTU on vws-test (large-packet ping -M do) has not been checked yet.
- The vws-network image on vws-test must be rebuilt from main after #94, which has `-vm-mtu`. Older images do not know the flag and do not start.
