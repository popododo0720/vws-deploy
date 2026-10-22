# The `vws` command

A CLI that does deployment and operations in one command. The design is vws-deploy#43; this document covers only the skeleton (vws-deploy#52). The command bodies (`init`, `apply`, `preflight` and so on) arrive one at a time in sub-issues.

```
scripts/vws [flags] <command> [args]
make vws-<command> ARGS="<flags>"     # alias of the same command
make vws-test                          # harness (fake kubectl, helm)
```

## Structure

- `scripts/vws`: the dispatcher. It reads flags, loads `scripts/vws.d/<command>.sh` and runs `vws_cmd_main`. Names must match `[a-z][a-z0-9-]*`.
- `scripts/vws.d/<command>.sh`: one command. Near the first line, `# summary: <one line>`; required tools in `VWS_NEED=(kubectl helm)`; the body in `vws_cmd_main`. `help` builds its list from `# summary:`.
- `scripts/vws.d/lib/common.sh`: output, exit codes, tool checks, confirmation (`--yes`), and the kubectl and helm wrappers.
- Commands never call `kubectl` or `helm` directly; they use `vws_kubectl` and `vws_helm`. The wrappers add `--context` and `--namespace`, and under `--dry-run` they do not run mutating calls but print `DRYRUN ...` to stderr. Any verb not in the read verb list counts as mutating.
- Secret values never go into `vws_emit` output. Only names and counts are reported.
- The CRD API group constant is defined in one place in vws-deploy#53, where the CRD arrives. This skeleton does not read the CR, so it does not have one yet.

## Common flags

| Flag | Meaning |
| --- | --- |
| `--context NAME` | kubeconfig context (kubectl `--context`, helm `--kube-context`) |
| `-n`, `--namespace N` | namespace |
| `-o text\|json` | output format. json prints `{"command","status","checks":[{"level","name","message"}]}` once at the end |
| `--dry-run` | do not run mutating kubectl or helm calls |
| `-y`, `--yes` | confirmation for destructive commands |
| `--strict` | WARN also exits 1 |
| `-h`, `--help` | same as `help` |

Flags may go before or after the command. An unknown flag before the command name is exit code 2; one after the name goes to the command. After `--`, everything is a command argument.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | all OK (WARN is 0 unless `--strict`) |
| 1 | there is a FAIL (or a WARN under `--strict`) |
| 2 | usage error: unknown command or flag, a flag without a value, a destructive command without `--yes` |
| 3 | environment problem: a required tool is missing, the cluster is unreachable |

## Harness (`tests/vws/run.sh`)

It never reaches a cluster or the network. It puts the fake `kubectl` and `helm` from `tests/fixtures/bin/` first in PATH and tests the dispatcher and common library with the test commands in `tests/fixtures/commands/`.

- On every call the fake tools write `tool<TAB>verb<TAB>args` to `$VWS_FAKE_LOG` and return `tests/fixtures/<tool>/<verb>_<resource>.json` as is. A missing file fails loudly (an unplanned call never passes).
- `assert_no_changes` checks the log for mutating verbs. Its verb list is deliberately kept apart from `common.sh` (it does not trust the code under test). `assert_changes` is the control.
- What it tests: exit codes 0, 1, 2 and 3, parsing json output with `jq`, zero mutating calls in read scenarios, passing on `--dry-run`, `--context` and `-n`, destructive commands without `--yes`, `--strict`, and whether the log catches mutating calls.
- It does not test the output format of the real `kubectl` and `helm` or a real cluster. Fixtures for each command are added when its body arrives.

- Totals: each test file prints its own `N cases, M failed` line, and `run.sh` adds up every file and prints `total: C cases, F failed in S suites` as the last line. A file's failure count is the larger of its summary line and its number of `FAIL ` lines, and a file that exits non-zero without a summary counts as 1. It names the failed files and exits 1 when F is greater than 0. Before this, only the last file's summary stayed at the bottom of the screen, and an earlier file's FAIL was hidden behind `0 failed`. `VWS_TEST_DIR` runs another directory (`tests/vws/run.test.sh` uses it).

`scripts/guard.sh vws` (R17) runs the harness above and shellcheck, and checks that each command file has a test file and an entry in this document, and that no command calls kubectl or helm directly. It also tests with broken copies that this check actually fails.

## Commands

### vws help

Shows the command list, common flags and exit codes. With `-o json`, one entry per command. Needs no cluster tools.

### vws render

`vws render <CR.yaml> <OUT.yaml>`: turns one `VWSCluster` CR into a values overlay for `charts/vws`. It changes nothing in the cluster. The same CR gives the same bytes (`sort_keys`, no times or random values). Order: validate the CR (CRD schema, name `vws`, unknown fields, secret-looking field or flag names) → apply the CR values on top of `profiles/<spec.profile>.yaml` → re-validate against `values.schema.json` with `helm template -f` → write `OUT`. A bad CR is exit code 2 and creates no `OUT`. When `OUT` is `-`, it writes only YAML to stdout (not combined with `-o json`). The mapping is in `docs/vwscluster.md`.

### vws preflight

`vws preflight --profile single-node|small|ha [--links FILE]`: checks before installing. It never writes to the cluster (read verbs only). Each check function lives in `scripts/vws.d/lib/checks.sh`, and status and doctor use the same functions (#43 section 4).

| Check name | Method | Failure |
| --- | --- | --- |
| `tools` | `helm version --short` starts with `HELM_VERSION` | WARN |
| `api` | `kubectl version`, prints the current context name | no answer is exit code 3 |
| `k8s-minor` | server minor = `KUBERNETES_VERSION` minor | WARN |
| `install-rights` | `kubectl auth can-i create` for namespaces, CRDs, cluster/namespace RBAC and the kinds the chart creates | FAIL with the missing verbs/resources |
| `nodes` | Ready nodes ≥ the profile minimum (1/3/6) | FAIL |
| `node-resources` | sum of Ready node allocatable ≥ sum of profile requests×replicas (`profiles/<p>.yaml`, else the chart default) | FAIL |
| `default-storageclass` | a default StorageClass exists | FAIL |
| `ceph-block`, `ceph-block-snapshot` | StorageClass and VolumeSnapshotClass `ceph-block` | WARN before the bundle |
| `crd-<group>` | the bundle CRD groups (kubevirt.io, cdi.kubevirt.io, kubeovn.io, k8s.cni.cncf.io, snapshot.storage.k8s.io) are Established | WARN when missing, FAIL when not Established |
| `kubevirt-phase`, `cdi-phase` | the KubeVirt/CDI CR phase is Deployed | WARN when missing, FAIL on another phase |
| `kernel-indirect` | indirect check through the existence of `kubevirt.io/schedulable` nodes (no `--node-probe`, decision 3) | WARN |
| `release` | helm release `vws` is `pending-*`/`failed` | FAIL |
| `source` | in a checkout, `guard.sh bundle`, `recovery`, `config` | FAIL. Skipped with `VWS_SOURCE_CHECKS=0` (harness) |
| `node-network`, `node-ip/<node>`, `node-links/<node>` | the cluster's VWSCluster `spec.network.nodes` (#128, `lib/nodenet.py`). Whether each node's InternalIP is inside the management CIDR. With `--links FILE` (`talosctl -n <all nodes> get links -o json`), whether each node has the host network interfaces (`<nic>` or `<nic>.<vlan>`) and underlay NICs with an MTU of at least the CR value | an IP outside, a missing interface, a too-small MTU or a node missing from links is FAIL. Without `--links`, `node-links` WARN (unconfirmed). OK when there is no CR or no `nodes` |
| `vm-mtu`, `vm-mtu-set` | whether the tunnel NIC MTU (the minimum over nodes from `--links`, else `spec.network.underlayMTU`) is at least the VM MTU (`vmMTU`, else 9001) + tunnel overhead (#236). Below 9216 the OK message states the recommended value | when too small, WARN, and it writes the lowered `spec.network.vmMTU` and the read `underlayMTU` to VWSCluster `vws` with `kubectl patch` (`vm-mtu-set` OK; if refused, WARN with a hint to use `vws reconfigure`). These two fields are all preflight writes; it never touches NIC MTUs. With no basis, `vm-mtu` WARN (unconfirmed) |

Any FAIL exits 1, WARN also exits 1 under `--strict`, and 3 when evaluation is impossible. The harness `tests/vws/preflight.test.sh` changes one file of `tests/fixtures/preflight/ok` per scenario and checks each check's pass/fail.

### vws init

`vws init [--from FILE | --print-url]`: inside the installer pod (`VWS_INSTALLER_POD=1`), called without arguments, it starts the installer API TUI (`vws-installer tui`, #142), and `--print-url` prints the port-forward address, the one-time access code and the certificate fingerprint (`docs/installer-api.md`). Otherwise it stores the `VWSCluster` CR. Without `--from` it asks on stdin for the profile, storage driver, StorageClass (only with `existing`), image tag and elastic IP pool CIDR (required, no default, #148) and builds the same CR. Validation is the same code as `vws render` (`lib/render.py`), and on error it exits 2 and stores nothing. If `spec` equals the cluster's CR, `unchanged` (0 mutating calls). It never touches workloads. Storing is `kubectl apply --server-side --field-manager=vws`. Before storing it server-side applies every `charts/vws/crds/*.yaml` (`--force-conflicts`) and waits for the CRDs to be Established, because Helm creates `crds/` only on the first install and never upgrades them, so an old CRD refuses new fields (#112) and a new CRD set is missing (#382). `reconfigure`, `upgrade` and `restore` do the same CRD apply before writing the CR, and `apply` does it **before** reading the CR, but only when the cluster's CRDs differ from the chart's (this stops an old CRD from stripping new fields from the CR it reads and turning off RGW and the like, #114; when equal, 0 writes).

### vws apply

`vws apply [--unattended] [-f FILE]`: with `-f FILE` it first stores the CR with `vws init --from FILE` (2 on error, `unchanged` for the same spec, #144). Before reading the CR, when the cluster's CRDs differ from the chart's, it server-side applies every `charts/vws/crds/*.yaml` and waits for the CRDs to be Established (#382). Then CR → render → validation (`vws_validate_rendered` in `lib/applyrun.sh`, shared with `vws reconfigure`: `helm template` (values.schema.json) and `scripts/check_render.py` (R15 and others). On failure it prints every rule line, changes nothing and exits 2, #246. Before, only apply skipped this check, so apply applied CRs that reconfigure refused) → state comparison → preflight → secrets → bundle → helm → status. If the CR's `status.appliedValuesHash` (a hash over the sha256 of the rendered values and every file path and content in `charts/vws`, `lib/hash.sh`, #101; a chart-only change also re-applies) and `observedGeneration` equal the current values, it ends with `unchanged` (0 mutating calls, except the ConfigMap deletion below). Before comparing, if `kube-system/ovn-external-gw-config` created by an earlier apply (field manager `vws`) exists, it deletes it and emits `OK extgw` (#171). A ConfigMap of the same name that vws did not write is left alone with `WARN extgw`. The bundle server-side applies `build/platform/*.yaml` in file name order (= the numbers of `scripts/platform.sh`) and waits per file for CRDs to be Established and Deployments Available. Then `helm upgrade --install vws --atomic --wait`, then it seeds the default image (#232): in the image namespace (values `namespaces.images`, default `vws-images`) it creates with `kubectl create` DataVolume `ami-0123456789abcdef0` (imports the digest-pinned `LAB_VM_IMAGE` from `scripts/versions.env` through the CDI registry source, 1Gi by default. The StorageClass matches the class vws-storage clones volumes into, chosen in the order: rendered values `services.storage.flags.kube-class-standard` → `global.storageClass` → the chart default (`ceph-block` for rook-ceph, `spec.storage.storageClass` for existing, `vws-nfs` for nfs); when all are empty, the cluster default StorageClass, and without one, no `storageClassName`. #238: before, it always went to the cluster default (such as local-path) and the clone failed) and a CDI DataSource of the same name (pointing at that PVC). console-api requests this id on every run-instances, and vws-storage#88 clones the root volume from that DataSource. If it already exists (AlreadyExists) it is left alone with `OK image ... kept`; it is never overwritten or re-imported. CDI comes from the bundle and the namespace from the chart, so this runs after helm. `--dry-run` only prints the calls it would make. Finally it writes the status subresource. If helm fails, no status is written and it exits 1. `--dry-run` only prints mutating calls. Secrets: the current chart has no Secret for the installer to create, so this step creates nothing. Preflight: runs the api, nodes, storage and release checks from `lib/checks.sh` (#55) and on FAIL changes nothing and exits 1. There is no concurrency lock (Lease `vws-operator-lock`) yet (needs checking, follow-up).

### vws installer

`vws installer finish`: deletes the temporary installation cluster-admin binding (`vws-installer-temp`) and confirms it is really gone. Design and reasons are in `docs/installer.md`. The confirmation uses `kubectl auth can-i` (a SelfSubjectAccessReview; outside the pod, `--as=system:serviceaccount:vws:vws-installer`, i.e. a SubjectAccessReview) and checks that `* *`, `create clusterrolebindings`, `create clusterroles`, `delete namespaces`, `create customresourcedefinitions` and `get secrets` are all `no`, and that the operator rights `get vwsclusters` and `patch vwsclusters/status` are `yes`. Any mismatch exits 1; an unreadable answer exits 3. If the binding is already gone, `unchanged`, and the confirmation runs again (idempotent).

### vws status

`vws status`: only reads the installed state (0 mutating verbs). Checks: api, `cluster-cr` (profile, `metadata.generation` and `status.observedGeneration`), `applied-hash` (the hash of the values the current CR renders and the current chart versus `status.appliedValuesHash`; the same calculation as `vws apply`, so a chart-only change is drift), `release` and `release-drift` (helm revision and state, release values versus the CR render), `bundle-kubevirt`, `bundle-cdi` (the version the operator CR reports versus `scripts/versions.env`), `service-<name>` (Deployment/StatefulSet ready/desired for each service in `charts/vws/values.yaml`) and `pdb`. Drift is WARN, so it is 1 under `--strict`.

**Credentials.** helm keeps releases in Secrets. After `vws installer finish`, the operator role (`vws-operator`) cannot read Secrets, so `helm status/get` fails with forbidden. So status uses the VWSCluster status and workloads as its primary sources and adds the release revision and release drift only when readable. When they are not, it emits `WARN release ... cannot read Secrets ...; For them run vws status with your own kubeconfig` and the other checks run as usual (not a FAIL). With the operator role, `--strict` is 1 because of this WARN.

### vws doctor

`vws doctor`: all of status + `restarts` (CrashLoopBackOff or 5 or more restarts), `pending-pvc` (all namespaces), `vws-crd` (the VWSCluster CRD is Established), `crd-<group>` and `kubevirt-phase`, `cdi-phase` (the same `checks.sh` functions as preflight; in doctor mode a missing one is FAIL), `read-rights` (the read rights status needs; when only `get secrets` is missing it is taken as the operator role and OK), `ceph` (CephCluster health when the CR's storage driver is rook-ceph) and `pending-flags` (whether the `global.pendingFlags` the CR renders match pendingFlags in the actual container arguments, #38). Every FAIL message ends with `next: <one line with the next action>`. Read only.

### vws reconfigure

`vws reconfigure (--from FILE | --set path=value [--set ...])`: changes the VWSCluster and runs `vws apply` (#43 section 3). The CR is the only source, so there is no path that edits values files directly.

- `--set`: a path under `spec` (`spec.` may be omitted); the value is read as YAML (`3` is a number, `true` a boolean). May be repeated. `--from`: the whole CR. The two are not combined.
- Validation (before writing anything): `lib/render.py` (CRD schema, refusing secret-shaped fields) → `helm template` (values.schema.json) → `scripts/check_render.py` (recovery invariants). Any failure exits 2 and writes nothing to the cluster.
- Change summary: `OK change spec.x: old -> new` for each changed spec leaf. Secret-shaped fields show `(hidden)`; `secretName` is a name and shows as is (render.py does not accept `spec.secrets` now, so it is refused).
- If the spec equals the cluster's CR, `unchanged`, 0 writes.
- `--dry-run`: prints only the summary; neither stores the CR nor applies.
- Otherwise it stores the CR with server-side apply (`--field-manager=vws`), runs `vws apply --unattended` and merges its result lines into the same output (including `-o json`). If apply fails, 1. The CR stays stored (the next `vws apply` or a reverting `reconfigure` reconciles it, needs checking).

### vws diag

`vws diag [--out FILE] [--since 1h]`: writes one diagnostic bundle (tar.gz) for incident reports. It only reads the cluster and uploads nothing (#43 section 7).

| File | Content |
| --- | --- |
| `vwscluster.json` | VWSCluster spec and status (without managedFields) |
| `helm-status.json`, `helm-history.json`, `helm-values.json` | `helm status/history`, `helm get values -a`. The operator role cannot read Secrets, so these become `unavailable: ...` (running again with user credentials fills them) |
| `cluster.txt` | `get nodes,pods,pvc,pdb,events -A -o wide` |
| `pods/<ns>_<pod>.describe.txt`, `.log` | only unhealthy pods (not Running/Succeeded, or with unready containers) in the release namespace and the bundle namespaces (kubevirt, cdi, kube-system, rook-ceph). Logs cover `--since` (default 1h), 500 lines per pod |
| `crds.txt`, `kubevirt-status.json`, `cdi-status.json`, `cephcluster-status.json` | CRD names, the status of each CR |
| `configmaps.json` | release namespace ConfigMap names and per-key sizes (no data) |
| `status.json`, `doctor.json` | the output of `vws status`/`doctor` (#56) when this build has them |
| `versions.env`, `kubectl-version.txt`, `helm-version.txt`, `context.txt` | versions and the context name |
| `README`, `REDACTION_REPORT` | instructions, masked counts per pattern (no values) |

Not collected: any Secret (no call reads secrets), ConfigMap data, environment variables, node host files, VM consoles or disks.

Masking: the key name rule comes first. Values of keys with secret-shaped names (case-insensitive: `passw`, `passphrase`, `token`, `secret`, `credential`, `private`, `apikey`/`api-key`/`api_key`, `dsn`, `auth`, names ending in `key` after a `-`/`_`/`.` separator, camelCase `...Key`) are hidden whatever their shape; for nested objects/lists, everything below. JSON files (such as `helm get values -a -o json`) are parsed structurally, only the values become `"[REDACTED]"`, and they are written back as valid JSON; YAML/text is handled line by line (including indented blocks). `secretName`, `secretRef` and `secretKeyRef` are Secret name references and stay. Similar names such as `keys`, `monkey` and `privileged`, and image digests, are not hidden. Then `lib/redact.py` scans every file and replaces with `[REDACTED]` lines that contain PEM private key blocks, `Bearer` tokens, `token` values, base64 values of 40 or more characters under `data:`/`binaryData:`, runs of 12 or more digits, or credential URLs (`://user:pass@`). The scanner first checks that it catches an obviously fake canary for each pattern. If the scanner is missing (no python3 in PATH) or misses a canary, it writes no bundle and exits 3 (fail closed). Image digests (hex) are outside `data:` and are not hidden. Masking is not complete: a person reviews the bundle before sharing it (the README says so too).

Files: created with umask 077 and mode 0600; an existing file of the same name is not overwritten (exit 2). If the API does not answer, 3.

### vws upgrade

`vws upgrade --to TAG`: changes the VWSCluster's `spec.version` to TAG and runs `vws apply` (#43 section 5). The same tag is `unchanged`, 0 writes.

- apply's helm call is `--atomic --wait --timeout 15m --history-max 10`, and the revision description (`--description`) records the CR spec that produced the revision as `vws-spec:<json>` (one-line JSON with sorted keys), which is what rollback uses to restore the CR. If the description exceeds `VWS_SPEC_DESCRIPTION_MAX` (default 1024 characters), only `vws-spec-sha256:<sha256 of the spec>` is recorded and the full spec goes into the CR's `status.specHistory` (`{specHash, spec}`, the latest 10, dropping the oldest).
- If apply fails, helm has rolled the release back with `--atomic`, so the CR spec is stored back to the previous value too and it exits 1 (so the CR and release do not disagree). This is `vws_apply_spec` in `lib/applyrun.sh`, shared with `vws reconfigure`.
- The output always has two WARNs: a backup recommendation (`vws backup` (#61) does not exist yet, so freshness is not checked; for etcd snapshots see `docs/etcd-runbook.md`) and no zero-downtime claim (**no zero-downtime claim until vws-deploy#39 is closed**; stateful services have a short outage).
- Rolling updates of stateless services use the current chart settings (RollingUpdate, PDB) as they are. Upgrading the platform bundle is out of scope.
- Post-upgrade verdict (#43 section 5 step 6): when apply succeeds it runs `vws doctor` (including status) and merges its lines as `after-<check>`. Any FAIL exits 1 and suggests `vws rollback --yes` as the next action (no automatic rollback). If doctor cannot decide (exit code 2/3), WARN.
- `--dry-run` prints only the values that would change.

### vws rollback

`vws rollback [--to REVISION] --yes`: rolls the helm release back to a revision and restores the VWSCluster spec recorded for that revision. Without `--to`, the previous revision.

- Without `--yes` it prints the plan (revision, the spec to return to) and the limits, and exits 2.
- `--yes`: `helm rollback vws N --wait --timeout 15m` → store the CR spec → record the CR status (appliedValuesHash, observedGeneration). If helm fails, the CR is left alone and it exits 1.
- If the revision description is `vws-spec-sha256:<hash>`, it looks up the spec with that hash in the CR's `status.specHistory`. If it is missing (pushed out past 10), it does nothing and exits 1.
- If the revision description has neither (revisions from before this change, revisions made by hand), it does nothing and exits 1.
- If already at that revision with the same CR spec, `unchanged`.
- helm history lives in Secrets, so the operator role cannot read it. Run with user credentials (3 when unreadable).

Rollback limits (also printed as WARNs):
- `helm rollback` restores only chart resources and values. If the new version changed a storage format, the old binary may not read it. It is considered safe only for the previous revision with no storage format change.
- CRDs and platform operators are not rolled back. The only way back for them is an etcd snapshot (`docs/etcd-runbook.md`).
- No zero-downtime claim until vws-deploy#39 is closed.
- helm's rollback creates a new revision (description "Rollback to N"). That revision has no `vws-spec:`, so it cannot be rolled back to again (use the earlier revision number).

### vws verify-reboot

`vws verify-reboot --snapshot FILE` (before the reboot), `vws verify-reboot --drained NODE` (after the drain, before the reboot), `vws verify-reboot --check FILE [--timeout 20m] [--allow-health-check CODE]... [--ceph-health-file FILE | --skip-ceph-gate]` (after the reboot). This command does not reboot (a person or `talosctl` does). Read only (#43 section 8).

`--snapshot` emits `WARN krbd` for each node with a `rbd.csi.ceph.com` VolumeAttachment. Drain those nodes before rebooting (krbd shutdown hang, #116; the procedure is in [platform-bundle.md](platform-bundle.md)).

- `--drained NODE`: 0 when the node is cordoned and no `rbd.csi.ceph.com` VolumeAttachment is attached, 1 when the drain did not happen or is incomplete, 3 when unreadable. Do not reboot on non-zero (#150).
- `--snapshot` emits `WARN infra` when one or fewer nodes can take the KubeVirt infra pods (virt-api, virt-controller, virt-exportproxy). It counts schedulable nodes that have every nodeSelector label, or control-plane nodes when there is no nodePlacement. Draining that node stalls on the PDBs (#168). It only reads and does not count as a failure.
- If any list in the snapshot cannot be read, it exits 3 and leaves no file (`snapshot failed: ...; do not reboot`). This keeps anyone from rebooting without the krbd check (#150).
- Snapshot (0600, never overwritten): node names and boot IDs, ready/desired per service, pod **counts** per namespace, release namespace PVC names and phases, the **number** of keys in VWS ConfigMaps, `runStrategy: Always` VM names and states. No pod UIDs, ConfigMap data or Secrets.
- Confirmation gates (startup order, each duration printed): `api` → `nodes` (every node in the snapshot is Ready) → `cni` (kube-ovn pods Ready) → `csi` (Ceph `HEALTH_OK`, or `HEALTH_WARN` whose active check codes are all in `--allow-health-check`; the same for every profile including single-node) → `bundle` (KubeVirt and CDI CRs `Deployed`) → `services` (snapshot services have ready == desired). States worth waiting for are re-checked every `VWS_VERIFY_INTERVAL` (default 10 seconds).
- `--allow-health-check CODE` (repeatable, empty list by default): on `HEALTH_WARN`, the `csi` gate passes only when **every** active check code in CephCluster `status.ceph.details` is in the list. If any code is outside the list, it waits while showing that code by name and exits 3 at `--timeout`. It cannot be widened through environment variables. Nothing is muted in Ceph (#150). The single-node profile has no exception (#154). Warning codes raised because the pool size is 1 must also be confirmed with `ceph health detail` and passed explicitly. `POOL_NO_REDUNDANCY` is **only an example**. Until measured on a real single-node cluster, do not assume this code appears, and do not add it to the allowlist unconfirmed (#158).
  - vws-test example: because AES keys are allowed for krbd, `ceph health detail` shows three codes, `AUTH_INSECURE_CLIENT_KEY_TYPE`, `AUTH_INSECURE_KEYS_ALLOWED` and `AUTH_INSECURE_KEYS_CREATABLE` (measured 2026-10-13). `vws verify-reboot --check before.json --allow-health-check AUTH_INSECURE_CLIENT_KEY_TYPE --allow-health-check AUTH_INSECURE_KEYS_ALLOWED --allow-health-check AUTH_INSECURE_KEYS_CREATABLE`.
- External Ceph (#166, storage driver `existing`, no CephCluster CRD, as on vws-test): the `csi` gate never passes silently.
  - `--ceph-health-file FILE`: reads a file made **after** the reboot with `ceph health detail -f json > FILE` (`.status`, `.checks`). The verdict is the same as for a CephCluster, and `--allow-health-check` applies to the check codes the same way. If the file is not newer than the snapshot, unreadable or malformed, it waits (3 at `--timeout`). The gate re-reads the file while waiting, so it may be rewritten in between.
  - `--skip-ceph-gate`: only when there is no CephCluster CRD at all, it skips the gate and emits `WARN gate-csi SKIPPED ... Ceph health was NOT checked`. It does not count as OK. With `--ceph-health-file` present, the file wins.
  - With neither, it exits 1 and names both options. When the CRD exists but no CephCluster yet (Rook starting), it waits even with `--skip-ceph-gate`.
  - When a CephCluster exists it is always read, and the file is not used.
- The blocked gate is the **first line** of the output. A state that cannot pass (a node gone, Ceph `HEALTH_ERR`, an operator `Failed`, a service without workloads) is 1; still not passing at `--timeout` is 3.
- Comparison after the gates (1 on failure): the snapshot's Bound PVCs are Bound under the same names, ConfigMap key counts are equal or higher, every Always VM is Running, and no pod has restarts above the threshold (`VWS_VERIFY_RESTARTS`, default 3). If no node's boot ID changed, `WARN reboot not observed`.
- Whether a node rebooted is judged by its `status.nodeInfo.bootID` (it changes on every reboot), not by boot time.
- Real reboot/power-cut measurement is unverified: cluster-verification-list I9, etcd runbook scenario 7 (A7).

### vws backup

`vws backup [--out DIR] [--etcd-snapshot FILE]` reads the cluster and writes `vws-backup-<UTC>/` (0700, files 0600; an existing directory is refused, exit 2): the VWSCluster spec, `helm get values -a`, history and manifest, the release-namespace ConfigMaps labelled `app.kubernetes.io/part-of=vws` (WARN when there are none), `manifest.json` (files with size and sha256, versions, profile, VolumeSnapshot names, CephCluster health, the etcd snapshot path and whether it is under 24 hours old) and `SHA256SUMS`. Secrets, Ceph/VM disks and etcd itself are not included. The CR and helm files are scanned with `scripts/vws.d/lib/redact.py` first; any hit is a FAIL and nothing is written. Helm keeps its release in Secrets, so the operating role cannot run it: use your own kubeconfig. Copying the directory off the node is up to you. Not yet checked on a real cluster (cluster-verification-list I8).

### vws restore

`vws restore DIR [--yes]` checks `SHA256SUMS` first (a mismatch exits 1 before any write), then prints the plan; without `--yes` it exits 2, `--dry-run` stops after the plan. With `--yes` it applies every CRD file from `charts/vws/crds` and waits for them to be Established, saves the CR spec when it differs, runs `vws apply`, compares the release values with the backup (WARN when they differ), server-side applies only the ConfigMaps that differ, and folds `vws doctor` in as `after-*` lines. Restoring the same backup twice reports unchanged. State volumes are restored by hand from the VolumeSnapshots listed in the plan. Not yet checked on a real cluster (cluster-verification-list I8, I2).

### vws verify

`vws verify --update-feature-map [--map FILE] RESULT.json...` records vws-test scenario results in `docs/feature-map.json` and regenerates `docs/feature-map.md` (feature, verify command, last pass in KST, evidence). A result is `{"scenario", "result": "pass"|"fail", "cluster": "vws-test", "finishedAt": RFC3339, "evidence", "clusterUID"}`. `clusterUID` is the kube-system namespace UID of the cluster the scenario ran on and must equal `vwsTest.kubeSystemUID` in the map; a missing or different UID, or an unset fingerprint, is refused with FAIL (exit 1). The fingerprint is set to the vws-test kube-system namespace UID (`18a8b0c5-891b-4a39-8cdd-3a18723047b9`, vws-deploy#252). A matching `clusterUID` is provenance, not a signature: it catches results from another cluster or a mislabelled file, but a hand-written file that copies the committed UID is still accepted. Exit codes: 0 recorded or ignored (WARN lines); 1 a result refused (UID mismatch, unset fingerprint) or not readable as JSON; 2 usage error or invalid input, i.e. `scenario`, `cluster`, `finishedAt` or `result` missing or not a non-empty string, `clusterUID`/`evidence` not a string, `finishedAt` not RFC3339 with a zone or more than 5 minutes in the future, or `vwsTest.kubeSystemUID` neither a string nor null; exit 2 prints one `FAIL: <reason>` line, no traceback, and leaves the map and md unchanged; 3 the map itself is unreadable. Only a `pass` on `vws-test` moves a last pass forward; a fail, another cluster or an older time changes nothing. Features without a scenario print `WARN unverified`. `vws verify --check-feature-map` exits 1 when the md was edited by hand or is stale. Local files only; no cluster call (vws-deploy#252). `vws verify scenario NAME` itself is not implemented yet.
