# Control plane deployment

## Problem

VWS needs a deployment repository whose Kubernetes output is the control plane. The platform design, including the removal of host agents, is https://github.com/popododo0720/vws-deploy/issues/2. After a process restart or a reboot, the control plane has to come back with its state. The repo is greenfield: validation is `helm lint` and kubeconform, and nothing is applied to a cluster. Agents that edit one file at a time will copy the nearest example, so recovery and the plane split have to be properties of the only renderer, not a checklist each service repeats.

## Usage (caller's view)

Add a control-plane service with one map entry in `charts/vws/values.yaml`:

```yaml
services:
  billing:
    external: true
    state:
      size: 10Gi
```

`make check` then renders a ServiceAccount, a LoadBalancer Service, a one-replica StatefulSet with a retained volume, and a PodDisruptionBudget. Probes and `restartPolicy: Always` are not fields the caller can set.

Publish work from a branch. `git push origin main` is refused by `.githooks/pre-push`.

## Shape

The public control-plane type is `charts/vws/values.schema.json`. Callers set `global.registry`, `global.storageClass`, and a map of services. Each service may set `port`, `replicas`, `cpu`, `memory`, `state.size`, `external`, and `startupSeconds`. Workload kind, probes, disruption budgets, restart policy, image tags, and volume retention are derived. Volume-claim labels are only the stable selector, because claim templates cannot change on upgrade. The startup probe's failure threshold covers the full `startupSeconds` window. A service name cannot end in `-agent` or `-peers`.

`charts/vws/templates/control-plane.yaml` is the only document emitter. It ranges the services map. `templates/_helpers.tpl` owns the pod body, including probes and `restartPolicy: Always`. A service with `state` becomes a StatefulSet (`replicas` maximum 1, `podManagementPolicy: Parallel`, `persistentVolumeClaimRetentionPolicy` Retain, ReadWriteOnce claim mounted at `/var/lib/vws`). A service without `state` becomes a Deployment with at least two replicas and `maxUnavailable: 0`. Every workload gets a PodDisruptionBudget with `maxUnavailable: 1`. The schema rejects unknown fields, an agent-shaped service name, a second replica on a volume, and a single replica on a stateless service.

Interface depth: the caller adds a key. The chart hides the object bundle and the recovery policy. There is no second manifest path and no per-service template to copy.

## Synthesis decision

Two designs were produced independently and scored by a third reviewer.

Candidate A was the base. It uses one services map, one emitting template, derived probes and disruption budgets, StatefulSets only when a service has state, at least two replicas when it does not, and one emitting template so recovery policy is not copied per service. Its branch rule is a hook that refuses `main`, and its commit rule is one scope per commit.

Grafted from candidate B: stateful services cannot set `replicas` above 1, because one ReadWriteOnce volume has one writer and this repo does not own a consensus protocol. The hook is executed in CI, not only described.

Rejected from candidate B: a PersistentVolumeClaim and `replicas: 1` on every service, including stateless ones (the disruption budget then allows every eviction); failing the CI job on every push to `main`; and a commit rule that separates a renderer edit from the check that locks its output so the first commit cannot pass.

Deviations from candidate A, recorded because the sketch did not survive contact with the tools:

- No second kubeconform schema bundle. Kind and recovery checks read the rendered documents. A parallel OpenAPI bundle would restate the template.
- No `environments/` overlay. One values file is the catalog. An overlay that can clear `state` would turn a StatefulSet into a Deployment and orphan the volume.
- No Ingress and no endpoints ConfigMap. `external: true` is a LoadBalancer Service. In-cluster callers use the service DNS name. This repo does not ship an ingress controller.
- No unenforced branch-ruleset file. The pre-push hook is what CI actually runs.

## Tradeoffs accepted

- We accept downtime for a stateful service during rollout and during the loss of its node, in exchange for a single writer on a ReadWriteOnce volume that reattaches when the node returns.
- We accept a LoadBalancer where `external` is true, in exchange for an address callers can use without an ingress controller.
- We accept lockstep image tags from `Chart.appVersion`, in exchange for one version source.
- We accept that `helm lint` prints an icon recommendation, in exchange for shipping no logo file.
- We accept a Python render check, in exchange for no second manifest compiler.

## Alternatives considered

- One Helm chart per service, plus a library chart for probes. Adding a service would mean a new chart, a dependency entry, and a values block. Recovery would hold only if every chart remembered to call the library. The public surface gets larger and hides less.
- Kustomize bases beside the chart. A patch can delete a probe, and `helm lint` would no longer see one render path.
- Generating committed manifests and validating those. The committed YAML becomes a second catalog that drifts from the templates.

## Open questions and risks

- Which control-plane processes actually persist data, and which only look stateless in this chart? Adding or removing `state` is a one-line catalog edit and changes the workload kind.
- Will the platform provide a load balancer for `external` services, or does a later change need a different Service type derived the same way for every external service?

## Next implementation step

The chart and `make check` are the implementation of this sketch. A change to the values schema, the renderer, goes through architect again before the files move.
