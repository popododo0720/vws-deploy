# Zone model: one failure domain per cluster (#624)

## Problem

AWS documents each Availability Zone as an isolated failure domain: separate power, networking and connectivity, so an outage in one zone does not take down resources in another. In VWS a zone (for example `vws-1a`) is a name only. Every zone shares the same nodes, the same Ceph pool and the same StorageClass:

- `platform/rook-ceph-cluster.ha.values.yaml` has one `ceph-blockpool` with `failureDomain: host` and three replicas. `platform/rook-ceph-cluster.single-node.values.yaml` has the same pool with `failureDomain: osd` and one replica. `profiles/small.yaml` uses the chart default pool.
- Each profile has one `ceph-block` StorageClass with `allowedTopologies: []` and `volumeBindingMode: Immediate`.
- Nothing in the chart, the platform bundle or the installer sets `topology.kubernetes.io/zone` (or any other zone label) on nodes, and the chart passes no zone list to any service. vws-storage admits volumes only in its `-zones` list (default `vws-1a`) and records the zone as a PVC annotation (vws-storage `docs/decisions/0001-zone-and-pvc-topology.md`).
- vws-compute does not place a VM by zone. It refuses to attach a volume from another zone (`checkAttachments`), but with a generic invalid-argument error, not the EBS `InvalidVolume.ZoneMismatch` code. That error-code gap is tracked in popododo0720/vws-compute#167 and popododo0720/vws-console-api#531.

## Decision

Single-failure-domain zones are the supported model. A VWS cluster is one failure domain. Zones are logical: they are validated, stored and enforced at the API (create only in a known zone, address by zone, no cross-zone attach), but they give no physical isolation. Inside the cluster, the `ha` profile still tolerates the loss of one host, because the pool keeps one replica per host. That is host redundancy, not zone isolation.

The chart and the platform bundle do not render per-zone node labels, per-zone Ceph pools or CRUSH rules, or per-zone StorageClasses. The second acceptance box of #624 ("if zones map to failure domains") does not apply under this decision.

## Options considered

1. **Logical zones, one failure domain (chosen).** No change to the bundle. The gap is documented for users (below).
2. **Zone = a set of nodes, one stretched Ceph cluster.** Label nodes with `topology.kubernetes.io/zone`, add a Ceph CRUSH bucket per zone and one pool per zone (`failureDomain: host` inside a zone-rooted CRUSH rule), and one StorageClass per zone with `allowedTopologies` on the zone key. vws-storage would pick the class by zone and vws-compute would pin each VM to its zone. Ceph monitors would need a quorum that survives the loss of a zone (at least three zones, or Rook stretch mode with an arbiter), and node network latency between zones must stay low enough for synchronous replication.
3. **Zone = a separate cluster.** One Kubernetes and Ceph cluster per zone, with a region-level control plane above them. This is the closest to AWS, and it is a different product shape (multi-cluster control plane, cross-cluster IAM and networking).

Option 1 is chosen because:

- No supported profile has enough independent hardware for option 2. `single-node` has one host, `small` uses the chart defaults, and `ha` assumes three hosts in one failure domain. A zone needs its own hosts, its own Ceph replicas and a share of the monitor quorum; with three hosts, three zones would leave one host per zone and no host redundancy inside a zone.
- Option 2 changes vws-storage (class by zone, vws-storage#100 decision revisit) and vws-compute (VM placement by zone) as well as the bundle. Shipping only the bundle half would create labels and pools that nothing uses.
- The API behavior that callers observe today already matches AWS for the single-zone case: a volume is created only in a known zone and cannot be attached to an instance in another zone. The one difference is the cross-zone attach error code (see the parity table).

## When to revisit

Write a new design record and open implementation issues in vws-deploy, vws-storage and vws-compute when either of these is true:

- An install profile with at least three groups of hosts, each with its own power or network failure domain, is required.
- More than one zone is offered to users in the same region (vws-storage `-zones` lists more than one zone in a supported profile).

At that point, vws-storage `docs/decisions/0001-zone-and-pvc-topology.md` (revisit trigger) and this record are revised together: existing claims keep the zone annotation, so they can be found per zone, but a bound claim's topology cannot change, so moving existing volumes into a zone pool is a migration.

## AWS parity status

| AWS behavior | VWS status |
| --- | --- |
| A zone is an isolated failure domain (power, network, facility) | **Gap.** All zones in a VWS cluster share one failure domain. A cluster-wide outage affects every zone. |
| Spreading instances or volumes across zones protects against a zone outage | **Gap.** Placing resources in different VWS zones gives no extra availability. Use the `ha` profile for host redundancy. |
| `CreateVolume` requires a known `AvailabilityZone` | Supported (vws-storage admission, unknown zone is refused). This record does not check which EC2 error code the refusal maps to. |
| `AttachVolume` refuses a volume in another zone with `InvalidVolume.ZoneMismatch` | **Partial.** vws-compute `checkAttachments` refuses the attach, but returns a generic invalid-argument error, and vws-console-api has no `InvalidVolume.ZoneMismatch` mapping. Tracked in popododo0720/vws-compute#167 and popododo0720/vws-console-api#531. |

Users who need protection from a full outage must run a second VWS cluster and replicate data themselves; VWS has no cross-cluster replication.

## Links

- Issue: popododo0720/vws-deploy#624
- vws-storage decision: popododo0720/vws-storage#100, decision record popododo0720/vws-storage#164 (`docs/decisions/0001-zone-and-pvc-topology.md`)
- AWS EC2 User Guide, "Regions and Availability Zones" (AWS documentation)
