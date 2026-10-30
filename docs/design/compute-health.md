# compute health listener and metrics scrape (vws-deploy#212)

## Problem

vws-compute serves `/healthz`, `/readyz` and `/metrics` on a second HTTP listener when
`VWS_HEALTH_ADDR` is set (vws-compute `internal/obs`, 1cb732f). The chart passed no health
address, so the listener stayed off, every compute probe was a `tcpSocket` check on the gRPC
port, and no Prometheus target existed. The chart needed one value that turns the listener on,
drives the probes and the scrape target, and cannot drift from the port.

## Usage (caller's view)

```yaml
services:
  compute:
    port: 50051
    healthPort: 8081
```

The render then has env `VWS_HEALTH_ADDR=0.0.0.0:8081`, container and `metrics` Service ports on
8081, httpGet probes (`/healthz` for startup and liveness, `/readyz` for readiness) and pod
annotations `prometheus.io/scrape: "true"`, `prometheus.io/port: "8081"` and
`prometheus.io/path: /metrics`. A service without `healthPort` keeps the old tcpSocket or gRPC
probes.

## Data shape

`healthPort` is one integer on a service. It is listener wiring next to `port` and `portEnv`, the
same kind of key the chart already uses when an image reads a listener from env. Everything else
derives from it:

- the env value `0.0.0.0:<healthPort>`;
- the `metrics` container port and Service port, including the headless peers Service;
- the three probe checks and their paths;
- the three scrape annotations.

The chart fails when `healthPort` equals `port`, and the schema bounds both to 1024..65535. The
value is a port, not an address, so a caller cannot point the listener at loopback or a different
host and silently break the kubelet probe.

## Chosen and rejected options

| Option | Decision | Reason |
| --- | --- | --- |
| `healthPort` integer plus derived env, probes and annotations | chosen | One source for the listener, the probes and the scrape target; the schema bounds it and the equal-port failure rejects the invalid values |
| `flags.http: 0.0.0.0:8081` only | rejected | It renders an argument, not `VWS_HEALTH_ADDR`; the issue names the env, and a flag would duplicate the port without the derived Service port and annotations |
| HTTP probes for every service | rejected | Only compute serves the paths; console, console-api and network have no `/healthz`, so their probes would fail |
| `tcpSocket` on `healthPort` | rejected | It proves only that the socket is open, not that `/readyz` reports the dependency verdict |
| `/healthz` for readiness | rejected | `/healthz` ignores dependencies, so a storage or KubeVirt outage would keep the pod Ready |
| A ServiceMonitor | rejected | The chart installs no Prometheus operator, and `scripts/check_render.py` allows only control-plane kinds; the annotations are the portable target |
| One port for gRPC and HTTP | rejected | One listener cannot serve both protocols; a second port is required |

## Rule change

R15 said every probe port is the service `port` and no probe is HTTP. The rule now carries the
exception in the same row: a service with `healthPort` gets the HTTP listener, the `metrics` port
and the httpGet probes; every other service keeps tcpSocket or gRPC and never exec. The check in
`scripts/check_render.py` and `scripts/guard.sh config` enforces both sides, including a negative
case for a service without `healthPort` that uses an HTTP probe.

## Evidence

- vws-compute `internal/obs/obs.go` (`/healthz`, `/readyz`, `/metrics`) and
  `cmd/vws-compute/config.go` (`-http` defaults to `VWS_HEALTH_ADDR`). 1cb732f is an ancestor of
  the pinned build 1b62a87 (48 commits behind it).
- `charts/vws/values.yaml` `services.compute.healthPort`.
- `scripts/check_render.py` `check_health`, `check_listen` and the self-test mutations.
- `scripts/guard.sh config` default-render assertions and the four rejected inputs.

Unverified: a real kubelet passing these probes and a Prometheus scraping the annotations. This
repo does not apply to a cluster, and no Prometheus runs in it.
