# storage health listener (vws-deploy#620)

## Problem

#620 limits `/metrics` on storage to the monitoring scraper with `scrapeMetrics`, which needs
`healthPort` (the vws-lib health listener that serves `/metrics`). The chart's R15 gives a service
with `healthPort` httpGet probes on that listener, but storage had gRPC probes with readiness on
`vws.storage.v1.Ready` (#110), and `scripts/guard.sh` required that readiness.

## What the image does

vws-storage main (070b210, vws-lib v0.3.0) registers one `/readyz` check, `monitorCheck.Ready`
(`cmd/vws-storage/main.go`), which calls `internal/health` `Monitor.Ready`: the aggregate
readiness from the last evaluation, the same value the gRPC `vws.storage.v1.Ready` answers.
vws-lib `/healthz` checks no dependency. So the httpGet probes keep what the gRPC probes gave:
liveness looks only at the process, readiness drops on an engine, store or kube failure.

## Options

| Option | Decision | Why |
| --- | --- | --- |
| `healthPort: 7402`, drop `grpcReadinessService`; guard requires httpGet `/readyz` readiness | chosen | Values only. R15 already renders it; `/readyz` is the same aggregate as the gRPC name |
| Keep gRPC probes and add `healthPort` only for `/metrics` | rejected | R15 and the chart tie `healthPort` to httpGet probes; it needs a template and schema change for no gain |
| gRPC readiness plus httpGet startup and liveness | rejected | Two probe kinds on one pod, a new template branch, same readiness result |

`healthPort` 7401 is rejected: `-fake-object-addr` defaults to `127.0.0.1:7401`, and the chart
leaves the object backend fake unless `s3.rgw.enabled`.

## Checks

- `scripts/guard.sh config`: storage readiness is httpGet `/readyz` on `metrics` in the default,
  `global.probe=grpc` and `global.pendingFlags=true` renders.
- `scripts/check_render.py`: the readiness ClusterRole `get` rules (`READY_NEEDS_GET`) are checked
  whatever the probe kind, since `/readyz` runs the same kube checks.

`grpcReadinessService` stays in the chart with no service using it.

## Not verified

Kubelet httpGet probes on a live cluster under Kube-OVN with the NetworkPolicy, and a scrape from
the scraper with denial from another namespace (#620 AC2, no test cluster yet).
