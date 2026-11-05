# Observability

The chart wires every service to OpenTelemetry when `observability.enabled` is true. It builds
no backend: it either runs one minimal collector or points the services at an OTLP endpoint you
already have. The design record is `docs/design/observability.md` (vws-deploy#50).

## Turning it on

```yaml
observability:
  enabled: true
```

With `otlpEndpoint` empty the chart renders `otel-collector` (ServiceAccount, ConfigMap, Service,
Deployment, NetworkPolicy). Its pipeline for traces, metrics and logs is OTLP in, `memory_limiter`
and `batch`, then `debug` (the collector log) and, when
`observability.collector.exporters.otlp.endpoint` is set, `otlp` to that next hop with bounded
retry (5s to 30s, giving up after 300s) and a 1000-batch queue. A non-empty `otlpEndpoint` renders
no collector and sends every service there.

## Service env

`vws.otelEnv` in `charts/vws/templates/_helpers.tpl` is the only place these names are written.

| Env | From |
| --- | --- |
| `OTEL_SERVICE_NAME` | the services map key |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `otlpEndpoint`, else the collector Service |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `protocol` (`grpc` or `http/protobuf`) |
| `OTEL_EXPORTER_OTLP_HEADERS` | secretKeyRef `otlpHeadersSecret`, key `headers`, only with `otlpEndpoint` |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `sampler`, `samplerArg` |
| `OTEL_RESOURCE_ATTRIBUTES` | `resourceAttributes`, sorted `key=value` pairs, omitted when empty |
| `VWS_LOG_LEVEL` | `logLevel` |
| `SENTRY_DSN` | secretKeyRef `sentry.dsnSecret`, key `dsn`, only when `sentry.enabled` |

`VWS_HEALTH_ADDR` comes from `healthPort` (R15), not from this block. A service overrides any key
of `observability.defaults` under `services.<name>.observability`. Scalars replace the default;
`resourceAttributes` and `sentry` merge key by key. The schema rejects any other key, and
`scripts/check_render.py` fails a render whose env differs from values or has a `VWS_*` name
outside its allowlist.

You create the Secrets. The chart never renders one and values hold only their names.

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
per-node and per-cluster collectors, and the R10 and R11 exceptions they need, are #46 and #47.
