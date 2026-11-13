# Observability

The Helm telemetry renderer has been retired. Follow [the operator guide](../operator/regions.md) for deployment. Collector configuration examples remain independently testable with `scripts/guard.sh observability`.

## Which services call obs.Setup

`obs.Setup` reads this env, serves `/healthz` and `/readyz` on `VWS_HEALTH_ADDR` and marks the
pod NotReady on SIGTERM. Every Go control service calls it on its main branch as read on
2026-11-05.

| Service | Call | Commit |
| --- | --- | --- |
| compute | `cmd/vws-compute/main.go` (`setupObs = obs.Setup`) | vws-compute `4fbbb72` |
| network | `cmd/vws-network/main.go:89` | vws-network `26656bf` |
| storage | `cmd/vws-storage/main.go:98` | vws-storage `57b3459` |
| iam | `cmd/vws-iam/main.go:292` | vws-iam `c00c925` |
| console-api | `cmd/vws-console-api/main.go:48` | vws-console-api `dca6269` |

## Metrics are pushed

The services push metrics over OTLP. That is the design, not a gap: vws-lib names every
instrument with the prefix `vws.` (`obs.MetricPrefix`), and `NewCounter` and `NewHistogram`
refuse any other name. A backend that turns OTLP into Prometheus names writes `vws_`, since
Prometheus names cannot hold a dot. A backend must translate these names when it exposes Prometheus metrics.

## Collector examples

`collector-examples/` holds collector config only: no manifest, no RBAC, no host mount. Each
file states what a later deployment would need and the component's stability.
`scripts/guard.sh observability` runs `otelcol-contrib validate` on each.

| Example | Runs as | Needs | Stability |
| --- | --- | --- | --- |
| `k8s_cluster.yaml` | one per cluster | ClusterRole get, list, watch on workload and node objects, no Secrets | metrics beta, logs development |
| `k8sobjects.yaml` | one per cluster | ClusterRole get, list, watch on `events.k8s.io` events | logs beta |
| `kubeletstats.yaml` | one per node | ClusterRole get on `nodes/stats`, kubelet port 10250 | metrics beta |
| `hostmetrics.yaml` | one per node | host root read-only at `/hostfs`, likely the host network for NIC counters | metrics beta, logs development |
| `filelog.yaml` | one per node | host `/var/log/pods` read-only, maybe root | logs beta |

Stability is the collector-contrib README as read on 2026-10-12 (vws-deploy#42 section 15). The
per-node and per-cluster collector deployment designs are #46 and #47.
