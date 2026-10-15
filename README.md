# vws-deploy

The deployment repository of the VWS private cloud. It covers only the control plane and platform bundle that run inside Kubernetes; we install no processes on the hosts. The design is https://github.com/popododo0720/vws-deploy/issues/2. This repository applies nothing to a cluster.

## Who it is for

The same Terraform, aws-cli and SDKs run unchanged with only the endpoint swapped.

- Home labs
- Test environments before deploying to real AWS
- Teams running hybrid with AWS
- Teams moving from AWS to on-premises

The first priority is EC2/S3 API compatibility. The roadmap is https://github.com/popododo0720/vws-deploy/issues/132.

## Control plane

The `services` map in `charts/vws/values.yaml` is the only service list. To add a service, add one key.

```yaml
services:
  billing:
    external: true
    state:
      size: 10Gi
```

With `state`, the service becomes a single-replica StatefulSet with a volume that is never deleted. Without `state`, it becomes a Deployment with two or more replicas. startup, liveness, readiness, the PodDisruptionBudget and `restartPolicy: Always` cannot be turned off through values. Images run as UID 65532, the root filesystem is read-only, and the only writable paths are `/tmp` and the state volume. `external: true` is a plain-HTTP LoadBalancer without authentication; the cluster network limits who can reach that address.

```bash
scripts/install-tools.sh
make check
helm template vws charts/vws --namespace vws
```

The image is `<global.registry>/<service>:<Chart.appVersion>`. A service with `external: true` becomes a LoadBalancer. Stateful workloads start together without waiting for an ordinal order, so a power recovery does not stall on ordered startup.

A service whose controller uses the Kubernetes API has a list of `rbac` rules. The chart turns a rule with a `namespace` into a Role and binding in that namespace and a rule without one into a ClusterRole and binding (a ClusterRole holds only cluster-scoped resources), and adds a Role that allows leader election on a Lease with the service's own name only. The reasons for the rules are in `docs/rbac.md`; service flags and namespaces are in `docs/service-config.md`. A service without `rbac` mounts no API token. The schema refuses wildcards and verbs such as `escalate` and `bind`.

```yaml
services:
  compute:
    rbac:
      - namespace: vms        # a key of namespaces in values.yaml
        apiGroups: [kubevirt.io]
        resources: [virtualmachines]
        verbs: [get, list, create, patch, delete]
```

The chart has no icon. Even when `helm lint` recommends one, do not add a logo file.

## Local lab

`scripts/lab/` is a throwaway T1 lab that brings up k3s, KubeVirt and CDI. It is the only path in this repository that applies anything to a cluster. The order is `k3s-up.sh`, `virt-up.sh`, `vm-test.sh`, `down.sh`. Without `/dev/kvm`, `kvm-check.sh` returns `emulation` and KubeVirt starts with `useEmulation`. With `LAB_DRY_RUN=1`, commands that change state are printed instead of run. **Unverified**: the box lacks the RAM, so it has not been run on a real cluster.

## Checks

`make check` runs the same checks locally and in GitHub Actions. For CRDs, `make crd` (`scripts/crd-check.sh`) sends them with `--dry-run=server` to an envtest kube-apiserver so apiserver rules that kubeconform misses (such as `uniqueItems` in structural schemas) are checked too (#210). Rendered manifests live only in `build/` and are not committed. The working rules are in `AGENTS.md`.
