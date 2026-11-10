# Compute multi-replica chart (vws-deploy#723)

## Problem

vws-compute now runs Lease leader election (vws-compute#138, PRs #193 and #195): every replica
serves gRPC, and only the Lease holder runs Repair and the controllers. Instance desired state
lives in the Kubernetes API (one ConfigMap per resource), not in a local file. The chart still
set `services.compute.state` (a 20Gi volume), so compute rendered as a one-replica StatefulSet.
Nothing asserted replicas, a PodDisruptionBudget, or topology spread for compute.

## Chosen

- Drop `services.compute.state` in `values.yaml` and every profile. Compute has no database flag
  and does not write under `/var/lib/vws`; the volume was empty. Without `state`, the existing
  template renders a Deployment.
- Set `services.compute.replicas: 2` in `values.yaml` and in all three profiles, including `ha`
  (the AC default). Replicas stay configurable; the schema already refuses a single replica on a
  stateless service (`deployment-replicas`).
- Keep the existing PodDisruptionBudget (`maxUnavailable: 1`) and the pod `topologySpreadConstraints`
  from `global.topologySpread` (soft / `ScheduleAnyway` by default, hard / `DoNotSchedule` in
  `ha`). No separate anti-affinity rule.
- A `scripts/check_render.py` self-test asserts those three facts whenever compute is rendered
  without `state`. A `tests/vws/render.test.sh` case shows `--set services.compute.replicas=3`
  and that `replicas=1` is refused.

## Rejected

- Keep a StatefulSet and set `replicas: 2` on it. R4 requires a stateful workload to have one
  replica and a ReadWriteOnce volume, which two pods cannot share. Compute has no local store
  to put on that volume (unlike storage and network SQLite files).
- Add pod anti-affinity next to topology spread. The chart already spreads replicas by
  `kubernetes.io/hostname` with a configurable `whenUnsatisfiable`. A second rule would duplicate
  that policy.
- Change the templates or the schema's replica rules. The Deployment / PDB / spread path already
  exists for other stateless services (iam, console). Only values and tests need to change.

## Limits

- An existing release has a StatefulSet `compute` and a `compute-0` PVC. Helm will not replace a
  StatefulSet with a Deployment of the same name in one upgrade. The operator must remove the
  StatefulSet (retain the PVC if they want to inspect it) and then apply this chart, which
  creates the Deployment. Instance state is in ConfigMaps, so the PVC is not required for
  recovery. A live walk-through is the follow-up issue for this work.
- Dropping `state` also drops the headless `compute-peers` Service. The LoadBalancer Service
  still does not publish `healthPort` (#623); probes and scrapers use the pod IP, as they do
  for any other external service.
