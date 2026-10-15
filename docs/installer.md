# The installer pod and reclaiming temporary rights (vws-deploy#54, #43 decision 8)

## Lifetime of the rights

| Stage | Bindings | Rights |
| --- | --- | --- |
| `kubectl apply -f build/bootstrap.yaml` | `vws-installer-temp` (ClusterRoleBinding → `cluster-admin`), `vws-operator` (Role), `vws-operator-read` (ClusterRole) | cluster-admin + operator rights |
| `vws init`, `vws apply` | same | installs the bundle (CRDs, ClusterRoles, namespaces) and the chart |
| `vws installer finish` | deletes `vws-installer-temp` | operator rights only: the VWSCluster and its status in the release namespace, the `vws-operator-lock` Lease, reading workloads, cluster-scoped reads (table below). No Secrets, RBAC, cluster writes or watch |

Why cluster-admin is needed: the bundle creates CRDs, ClusterRoles (and bindings), namespaces and webhooks. A least-privilege draft that narrows that list verb by verb changes with every bundle manifest and is expensive to maintain, so per decision 8 it is temporary cluster-admin plus reclaim.

## Operator ClusterRole `vws-operator-read` (exact list, enforced by R20)

| apiGroup | resource | verbs | Used by |
| --- | --- | --- | --- |
| `""` | `nodes` | get, list | preflight, doctor |
| `storage.k8s.io` | `storageclasses` | get, list | preflight |
| `snapshot.storage.k8s.io` | `volumesnapshotclasses` | get, list | preflight |
| `apiextensions.k8s.io` | `customresourcedefinitions` | get, list | doctor (CRD Established) |
| `kubevirt.io` | `kubevirts` | get, list | status (bundle version), doctor (phase), diag (#77) |
| `cdi.kubevirt.io` | `cdis` | get, list | status (bundle version), doctor (phase), diag (#77) |
| `ceph.rook.io` | `cephclusters` | get, list | doctor (Ceph health), diag (#77) |
| `""` | `persistentvolumeclaims` (all namespaces) | get, list | doctor (Pending PVC) (#77) |

`scripts/guard.sh bootstrap` refuses groups/resources outside the list, verbs other than get/list (including watch), `resourceNames` and `nonResourceURLs` (negative tests: watch, a resource outside the list, PVC write verbs, a group outside the list, cluster-wide pods, an allowed entry removed).

## Reclaim and confirmation (`vws installer finish`)

1. `kubectl delete clusterrolebinding vws-installer-temp --ignore-not-found`. With no output, `unchanged`.
2. `kubectl auth can-i` confirms that every right that must be gone (`* *`, `create clusterrolebindings`, `create clusterroles`, `delete namespaces`, `create customresourcedefinitions`, `get secrets`) answers `no`. Inside the installer pod (`VWS_INSTALLER_POD=1`) it is a SelfSubjectAccessReview of itself; outside, a SubjectAccessReview of that ServiceAccount through `--as`.
3. The rights that must remain (`get vwsclusters`, `patch vwsclusters/status`) answer `yes`.
4. Any mismatch exits 1; an unreadable answer exits 3. Re-running is safe (the delete uses `--ignore-not-found` and the checks run every time).

Note: step 2 also fails when rights remain through another binding (one the user granted separately). That is intended: the criterion is whether the rights actually shrank.

## Safety against crashes and restarts

- **Reaper Job** `vws-installer-reaper`: sleeps until the binding's `creationTimestamp` + `vws-installer-ttl-seconds` (= `INSTALLER_TTL_SECONDS`, 4 hours), then deletes the binding. If its pod dies and comes back, it recomputes the remaining time from the binding, so the deadline never grows. The Job deadline is TTL + 10 minutes, so it is not cut off before the delete. After finish it can no longer read the binding and ends soon.
- **State check at start**: `vws init` and `vws apply` first check whether the binding is past its TTL; if so they delete it and stop with exit code 1 (a second defence in case the reaper did not run).
- **Installer Deployment** `vws-installer` (#140): replicas 1, `Recreate` (one draft session). A Deployment pod cannot have `activeDeadlineSeconds`, so the deadline is on the rights, not the pod. When the reaper deletes the binding at the TTL, the pod stays with operator rights only. If the pod dies it comes back, and `kubectl exec` keeps working.
- **Service** `vws-installer`: ClusterIP only, port `api` 8443 (`kubectl port-forward svc/vws-installer 8443`). No NodePort, LoadBalancer, externalIPs or Ingress. The installer container runs `/opt/vws/bin/vws-installer` (#141, #191) with tcpSocket readiness/liveness probes on 8443. Read the access code with `kubectl -n vws exec deploy/vws-installer -- cat /tmp/vws-installer/access-code` (`vws init --print-url` is #142).
- Remaining risk (needs checking): re-applying the bootstrap after the reaper Job has completed does not run the Job again (a completed Job of the same name). With `ttlSecondsAfterFinished: 600` it is deleted after 10 minutes, so a later re-apply runs it afresh. A re-apply in between leaves only the state check at start.

## T0 acceptance criteria (this box, fake kubectl/helm)

- `scripts/guard.sh bootstrap` (R20): the rendered bootstrap passes kubeconform; no privileged/hostPath/hostNetwork/hostPID/hostIPC; every image by digest; no privilege escalation; the only cluster-admin binding is `vws-installer-temp` and its TTL annotation is `INSTALLER_TTL_SECONDS`; no bare Pod; the installer is a replicas 1, `Recreate` Deployment whose pod has no `activeDeadlineSeconds`/`restartPolicy`; one ClusterIP Service with no nodePort, externalIPs or loadBalancerIP; the selector and targetPort match the installer pod; the reaper exists with a deadline > TTL; the operator Role/ClusterRole have no wildcards, Secrets or RBAC; the ClusterRole only reads. Negative tests check that a copy breaking each rule (NodePort, replicas 2, RollingUpdate, a pod deadline, a selector mismatch, a missing targetPort, a bare Pod) fails.
- `tests/vws/installer.test.sh`: reclaim and confirmation pass (0), a second run is `unchanged` (0), no `--as` inside the pod, `* *` remains (1), `create clusterrolebindings` remains (1), operator rights lost (1), no answer from can-i (3), `--dry-run` makes 0 changes, a wrong subcommand (2).
- `tests/vws/init.test.sh`, `tests/vws/apply.test.sh`: a second run is `unchanged` with 0 changes, interactive and `--from` give the same CR, bundle order, no status after a helm failure, `--dry-run` only plans, a binding past its TTL is reclaimed and then exits 1.

## What must be measured on a real cluster (`/workspace/cluster-verification-list.md` I1, I2, I4)

- Real RBAC reclaim: after finish, `kubectl auth can-i --as=system:serviceaccount:vws:vws-installer '*' '*'` is `no`; the reaper actually deletes at the TTL (with a shortened TTL); the deadline holds after the reaper pod is force-deleted.
- Whether the bundle and chart install completely with cluster-admin, and whether `status` works with operator rights (helm releases live in Secrets, so `helm list` does not work with operator rights; status/doctor assume the user's credentials, to be checked in #56).
- Whether the Deployment and Service actually come up, and whether `kubectl -n vws exec deploy/vws-installer` and `port-forward svc/vws-installer 8443` work (#140, not run).
- Building and publishing the installer image (the digest is zeros now), and whether helm runs with `HOME=/tmp` inside the pod.

## Publishing the installer image and pinning the digest (#96)

`.github/workflows/installer-image.yml` (pull request builds) and `.github/workflows/installer-publish.yml` (publishing on pushes to main and `v*` tags) build `bootstrap/Containerfile`. They are split so a pull request check has no skipped job.

- Pull requests: build only. Permission is `contents: read`, and nothing is pushed.
- Pushes to main and `v*` tags: only the `publish` job of `installer-publish.yml` gets `packages: write` and pushes `ghcr.io/<owner>/vws/installer:sha-<commit>` and `:<branch or tag>`. It leaves one `VWS_INSTALLER_IMAGE=...@sha256:...` line in the job summary.
- CI never commits to the repository. A person pins the digest.
  1. Copy the `VWS_INSTALLER_IMAGE` line from the job summary of the publishing run (or `crane digest ghcr.io/<owner>/vws/installer:<tag>`).
  2. On a new branch, change `VWS_INSTALLER_IMAGE` and the "NOT PUBLISHED YET" comment in `scripts/versions.env`. The commit subject is `ci: pin the installer image digest`.
  3. Regenerate `bootstrap/bootstrap.yaml` with `scripts/bootstrap.sh`, add it as a separate `cli:` commit and open a PR (`Part of #N`).
- The ghcr package is created private. Whether to make it public, and the cluster's pull credentials, are separate decisions (needs checking).
