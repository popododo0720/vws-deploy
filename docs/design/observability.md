# Observability values and the minimal collector (vws-deploy#50)

Design: vws-deploy#42 sections 4 and 11.1. Section 15 (ClickHouse, node agents, gateways,
hardware collectors) is out of scope and stays in #46 to #49.

## Problem

The services read the standard OTel env (`OTEL_*`), `SENTRY_DSN` and `VWS_LOG_LEVEL` (vws-lib
`obs.Setup`), but the chart passed none of them, so no service could export telemetry. The chart
needed one switch, one place for defaults, a per-service override with the same keys, and a
receiver when the site has no OTLP backend yet. It must not build a stack, and with the switch off
the render must not change.

## Usage

```yaml
observability:
  enabled: true
  otlpEndpoint: ""          # empty: the chart runs otel-collector and the services send to it
  defaults:
    samplerArg: "0.1"
    resourceAttributes:
      deployment.environment: lab
services:
  compute:
    observability:
      samplerArg: "1.0"     # any key of defaults, nothing else
```

`docs/observability/README.md` is the operator's view. This record is the why.

## Data shape

- `observability.enabled` is the only switch. Off, `vws.otelEnv` returns nothing and no collector
  object renders, so the default render is the same bytes as before.
- `observability.defaults` holds every telemetry key: `protocol`, `sampler`, `samplerArg`,
  `logLevel`, `resourceAttributes`, `sentry`. `services.<name>.observability` takes the same keys.
  Both use one schema definition (`definitions.telemetry`), so the allowed keys cannot drift, and
  `additionalProperties: false` rejects unknown keys in both.
- A secret is only ever a Secret name: `otlpHeadersSecret` (key `headers`),
  `sentry.dsnSecret` (key `dsn`) and `collector.exporters.otlp.authorizationSecret` (key
  `authorization`). The chart fixes each key. The schema has no field that could hold a DSN or a
  header value.
- `observability.otlpEndpoint` decides the topology: empty renders the collector and points every
  service at `http://otel-collector.<release namespace>.svc:4317` (grpc) or `:4318`
  (http/protobuf); set renders no collector and points every service there.

## Decisions

| Decision | Chosen | Rejected | Why |
| --- | --- | --- | --- |
| Where the collector template lives | the end of `templates/control-plane.yaml` | `templates/observability-collector.yaml` (#42 section 11.1) | `scripts/guard.sh layout` (R10) allows only `_helpers.tpl` and `control-plane.yaml`. A second file would mean loosening that rule for a few dozen lines. This departs from #42 section 11.1. |
| How an override applies | key by key in `vws.otelEnv` (`hasKey`), maps merged key by key | Helm `mergeOverwrite` of defaults and override | `mergeOverwrite` skips `false` and `""` in the source, so `sentry.enabled: false` on one service could not turn off a default `true`. |
| `VWS_HEALTH_ADDR` | stays where `healthPort` renders it (R15) | move it into `vws.otelEnv` as #50 lists | It already exists, R15 owns it, and moving it would change the default render. |
| Collector probes | tcpSocket on `otlp-grpc` | the `health_check` extension with httpGet | #50 keeps R15 as it is: tcpSocket or grpc. A tcp check shows the receiver listens. |
| Collector replicas | one, no PodDisruptionBudget | two with a PDB | R4 covers the services map. The collector's queue lives in memory, so a second replica does not keep data either. A persistent queue needs a volume, which belongs to the gateway (#46). |
| Collector config | literal YAML in `vws.collectorConfig` | a dict built in the template and passed to `toYaml` | The literal reads like the upstream docs. `otelcol-contrib validate` checks the rendered text, and the pod carries its checksum so a change rolls it. |
| Who can send to the collector | a NetworkPolicy that admits only `app.kubernetes.io/part-of: vws` pods of the release on 4317 and 4318 | open to the cluster | Anyone could inject telemetry otherwise. |
| Where the checks run | `scripts/check_render.py` (env equals values, VWS_* allowlist, secrets by secretKeyRef, collector without API rights) and `scripts/guard.sh observability` (renders, schema and render negatives, `otelcol-contrib validate`) under the new rule R25 | extending R15 or R16 | #50 forbids reusing R15 and R16. R25 is the next free number. |
| Validator | `otelcol-contrib` `OTELCOL_VERSION` from `scripts/versions.env`, sha256 checked by `scripts/install-tools.sh` | running the collector image in CI | The box has no container runtime. The binary is about 110 MB per CI run. |

## What the collector does not do

It reads no Kubernetes API (`automountServiceAccountToken: false`, no Role or binding), mounts
only its ConfigMap, uses no host path or host namespace, and runs as a non-root user with a
read-only root filesystem and no capabilities. `scripts/check_render.py` fails a render that breaks
any of that. Cluster and host collection stay as config examples in
`docs/observability/collector-examples/`.

## Verification tiers

T0 on this box: render, schema, guard and `otelcol-contrib validate`. That a real collector
receives service OTLP and forwards it, and that the services start with this env, is T1 or above
and unverified (#51).
