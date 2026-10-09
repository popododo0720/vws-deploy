# Control plane deployment

## Problem

VWS needs a deployment repository whose Kubernetes output is only the control plane, while hypervisor agents stay on the host under systemd. After a process restart or a reboot, both sides have to come back with their state. The repo is greenfield: validation is `helm lint` and kubeconform, and nothing is applied to a cluster. Agents that edit one file at a time will copy the nearest example, so recovery and the plane split have to be properties of the only renderer, not a checklist each service repeats.

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

Add a hypervisor agent by creating `host/agents/<name>/deps.conf`. `host/install.sh` discovers that directory. It does not take the name as an argument. A second run converges.

Publish work from a branch. `git push origin main` is refused by `.githooks/pre-push`.

## Shape

The public control-plane type is `charts/vws/values.schema.json`. Callers set `global.registry`, `global.domain`, `global.storageClass`, and a map of services. Each service may set `port`, `replicas`, `cpu`, `memory`, `state.size`, `external`, and `startupSeconds`. Workload kind, probes, disruption budgets, restart policy, image tags, and volume retention are derived.

`charts/vws/templates/control-plane.yaml` is the only document emitter. It ranges the services map. `templates/_helpers.tpl` owns the pod body, including probes and `restartPolicy: Always`. A service with `state` becomes a StatefulSet (`replicas` maximum 1, `podManagementPolicy: Parallel`, `persistentVolumeClaimRetentionPolicy` Retain, ReadWriteOnce claim mounted at `/var/lib/vws`). A service without `state` becomes a Deployment with at least two replicas and `maxUnavailable: 0`. Every workload gets a PodDisruptionBudget with `maxUnavailable: 1`. The schema rejects unknown fields, an agent-shaped service name, a second replica on a volume, and a single replica on a stateless service.

Host recovery lives in one template unit, `host/systemd/vws-agent@.service`: `Type=simple`, `Restart=always`, `StartLimitIntervalSec=0`, `StateDirectory=vws/%i`, `WantedBy=vws-agents.target`. `vws-agents.target` is wanted by `multi-user.target`. Per-agent files may set `[Unit]` ordering only. The chart never reads `host/`, and the installer never reads the chart.

Interface depth: the caller adds a key or a directory. The chart hides the object bundle, and the unit hides restart and boot policy. There is no second manifest path and no per-service template to copy.

## Synthesis decision

Two designs were produced independently and scored by a third reviewer.

Candidate A was the base. It uses one services map, one emitting template, derived probes and disruption budgets, StatefulSets only when a service has state, at least two replicas when it does not, and one systemd template unit so restart policy is not copied per agent. Its branch rule is a hook that refuses `main`, and its commit rule is one scope per commit.

Grafted from candidate B: stateful services cannot set `replicas` above 1, because one ReadWriteOnce volume has one writer and this repo does not own a consensus protocol. The hook and the installer's refusal of agent-name arguments are executed in CI, not only described.

Rejected from candidate B: a PersistentVolumeClaim and `replicas: 1` on every service, including stateless ones (the disruption budget then allows every eviction); one full unit file per agent; failing the CI job on every push to `main`; and a commit rule that separates a renderer edit from the check that locks its output so the first commit cannot pass.

Deviations from candidate A, recorded because the sketch did not survive contact with the tools:

- No second kubeconform schema bundle. Kind and recovery checks read the rendered documents. A parallel OpenAPI bundle would restate the template.
- No `environments/` overlay. One values file is the catalog. An overlay that can clear `state` would turn a StatefulSet into a Deployment and orphan the volume.
- No Ingress and no endpoints ConfigMap. `external: true` is a LoadBalancer Service. In-cluster callers use the service DNS name. This repo does not ship an ingress controller.
- The host unit is `Type=simple` with no watchdog. This repo does not own the agent binaries, so it cannot require an sd_notify implementation.
- No unenforced branch-ruleset file. The pre-push hook is what CI actually runs.

## Tradeoffs accepted

- We accept downtime for a stateful service during rollout and during the loss of its node, in exchange for a single writer on a ReadWriteOnce volume that reattaches when the node returns.
- We accept a LoadBalancer where `external` is true, in exchange for an address hypervisor agents can use without an ingress controller.
- We accept lockstep image tags from `Chart.appVersion`, in exchange for one version source.
- We accept that `helm lint` prints an icon recommendation, in exchange for shipping no logo file.
- We accept a bash installer and a Python render check, in exchange for no second manifest compiler.

## Alternatives considered

- One Helm chart per service, plus a library chart for probes. Adding a service would mean a new chart, a dependency entry, and a values block. Recovery would hold only if every chart remembered to call the library. The public surface gets larger and hides less.
- Kustomize bases beside the chart. A patch can delete a probe, and `helm lint` would no longer see one render path.
- Generating committed manifests and validating those. The committed YAML becomes a second catalog that drifts from the templates.

## Open questions and risks

- Which control-plane processes actually persist data, and which only look stateless in this chart? Adding or removing `state` is a one-line catalog edit and changes the workload kind.
- Will the platform provide a load balancer for `external` services, or does a later change need a different Service type derived the same way for every external service?
- Do host agents need a control-plane CA or enrollment secret beyond `VWS_CONTROL_PLANE_URL`? The installer does not write secrets.

## Next implementation step

The chart, the host unit, and `make check` are the implementation of this sketch. A change to the values schema, the renderer, or the unit goes through architect again before the files move.
