# compute health listener (vws-deploy#212)

## Problem

vws-compute main serves `/healthz` and `/readyz` on a second HTTP listener when `VWS_HEALTH_ADDR`
is set (vws-lib `obs.Setup`, since vws-compute#156). The chart passed no health address, so the
listener stayed off and every compute probe was a `tcpSocket` check on the gRPC port. The chart
needed one value that turns the listener on, drives the probes, and cannot drift from the port.

The first record (#212) also rendered `prometheus.io` scrape annotations on `/metrics`.
vws-compute#156 (ff1252d) replaced the local `internal/obs` with vws-lib `obs.Setup`, which
serves only `/healthz` and `/readyz` and sends metrics over OTLP. vws-deploy#635 therefore
removed the annotations and made the check reject them.

## Usage (caller's view)

```yaml
services:
  compute:
    port: 50051
    healthPort: 8081
```

The render then has env `VWS_HEALTH_ADDR=0.0.0.0:8081`, container and `metrics` Service ports on
8081, and httpGet probes (`/healthz` for startup and liveness, `/readyz` for readiness). It has
no `prometheus.io` annotations: vws-compute main no longer serves `/metrics`. A service without
`healthPort` keeps the old tcpSocket or gRPC probes. Wiring an OTLP endpoint is separate work
(#217).

## Data shape

`healthPort` is one integer on a service. It is listener wiring next to `port` and `portEnv`, the
same kind of key the chart already uses when an image reads a listener from env. Everything else
derives from it:

- the env value `0.0.0.0:<healthPort>`;
- the `metrics` container port and Service port, including the headless peers Service;
- the three probe checks and their paths.

The chart fails when `healthPort` equals `port`, and the schema bounds both to 1024..65535. The
value is a port, not an address, so a caller cannot point the listener at loopback or a different
host and silently break the kubelet probe. No annotation derives from the value: a scrape target
needs an exposition endpoint, and no service has one.

## Chosen and rejected options

| Option | Decision | Reason |
| --- | --- | --- |
| `healthPort` integer plus derived env, ports and probes | chosen | One source for the listener, the probes and the Service port; the schema bounds it and the equal-port failure rejects the invalid values |
| Remove the scrape annotations from the #212 render | chosen | vws-compute#156 removed `/metrics`, so a target on that path would fail; metrics go over OTLP |
| A per-service `metricsPath` or scrape toggle | rejected | No service serves an exposition endpoint, so the switch could only keep a broken target. Add one when a service does |
| Keep `prometheus.io/path: /healthz` | rejected | Prometheus scrapes an exposition format, not a health check |
| A ServiceMonitor | rejected | The chart installs no Prometheus operator, `scripts/check_render.py` allows only control-plane kinds, and there is still no `/metrics` to scrape |
| `flags.http: 0.0.0.0:8081` only | rejected | It renders an argument, not `VWS_HEALTH_ADDR`; the issue names the env, and a flag would duplicate the port without the derived Service port and probes |
| HTTP probes for every service | rejected | Only compute serves the paths; console, console-api and network have no `/healthz`, so their probes would fail |
| `tcpSocket` on `healthPort` | rejected | It proves only that the socket is open, not that `/readyz` reports the dependency verdict |
| `/healthz` for readiness | rejected | `/healthz` ignores dependencies, so a storage or KubeVirt outage would keep the pod Ready |
| One port for gRPC and HTTP | rejected | One listener cannot serve both protocols; a second port is required |

## Rule change

R15 still says a service with `healthPort` gets the env, the `metrics` port and the httpGet probes.
The #212 check also required three scrape annotations; #635 reverses that half.
`scripts/check_render.py` `check_health` now fails on any `prometheus.io` annotation, and its
self-test adds them to a compute render to prove the failure. `scripts/guard.sh config` fails when
the default render carries one.

## Evidence

- vws-compute#156 (ff1252d) replaced `internal/obs` with vws-lib `obs.Setup`, which serves
  `/healthz` and `/readyz` and sends metrics over OTLP.
- vws-compute `internal/obs/obs.go` at 1cb732f served `/metrics`; that commit is in the pinned
  image `sha-1b62a87`, which still serves it.
- `charts/vws/values.yaml` `services.compute.healthPort`, and the pod template in
  `charts/vws/templates/_helpers.tpl` with no annotations.
- `scripts/check_render.py` `check_health`, `check_listen` and the self-test mutations.
- `scripts/guard.sh config` default-render assertions.

Unverified: a real kubelet passing these probes. This repo does not apply to a cluster. No OTLP
endpoint is rendered, so compute metrics reach no backend until #217 is done. The `metrics` port
rides the compute Service, which is `external: true`, so whether the LoadBalancer publishes 8081
is also unverified; the chart adds no NetworkPolicy for compute.
