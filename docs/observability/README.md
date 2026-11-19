# Observability

The Helm telemetry renderer and the Kubernetes collector examples have been retired. Follow [the operator guide](../operator/regions.md) for deployment. Collector configuration examples remain independently testable with `scripts/guard.sh observability`.

## Which services call obs.Setup

`obs.Setup` reads the standard OTel env, serves `/healthz` and `/readyz` on `VWS_HEALTH_ADDR` and
marks the service not ready on SIGTERM. Both services below call it on their main branch as read on 2026-11-05.

| Service | Call | Commit |
| --- | --- | --- |
| iam | `cmd/vws-iam/main.go:292` | vws-iam `c00c925` |
| console-api | `cmd/vws-console-api/main.go:48` | vws-console-api `dca6269` |

## Metrics are pushed

The services push metrics over OTLP. That is the design, not a gap: vws-lib names every
instrument with the prefix `vws.` (`obs.MetricPrefix`), and `NewCounter` and `NewHistogram`
refuse any other name. A backend that turns OTLP into Prometheus names writes `vws_`, since
Prometheus names cannot hold a dot. A backend must translate these names when it exposes Prometheus metrics.

## Collector examples

`collector-examples/` holds collector config only: no manifest, no RBAC. They run on the VM or
bare-metal host that runs the VWS systemd units. Each file states what it needs and the component's
stability. `scripts/guard.sh observability` runs `otelcol-contrib validate` on each.

| Example | Runs as | Needs | Stability |
| --- | --- | --- | --- |
| `hostmetrics.yaml` | one per VM or host | a collector on the host itself | metrics beta, logs development |
| `journald.yaml` | one per VM or host | journal read access (group `systemd-journal`), `journalctl` on PATH | logs alpha |

Stability is the collector-contrib README as read on 2026-10-12 (vws-deploy#42 section 15); the
journald entry was added on 2026-11-19 and needs re-checking before use.
