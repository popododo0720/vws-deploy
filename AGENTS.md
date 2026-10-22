# Agent rules

This repository holds only the deployment artifacts of the VWS private cloud. The control plane is one Helm chart, and we install no processes on the hosts. The design is https://github.com/popododo0720/vws-deploy/issues/2. No command here applies anything to a cluster.

## Rules

| ID | Rule | Structure | Check |
| --- | --- | --- | --- |
| R1 | Work on a branch; never push to main or master | `.githooks/pre-push` | `scripts/guard.sh hooks` |
| R2 | A commit touches one scope from scripts/scopes, and its subject is `scope: summary` | `scripts/scopes` | `scripts/guard.sh commits` |
| R4 | Control-plane workloads have startup, liveness and readiness probes, a PodDisruptionBudget and restartPolicy Always, and a workload with state has a volume that survives a restart | `charts/vws/templates/control-plane.yaml` `charts/vws/values.schema.json` | `scripts/guard.sh recovery` |
| R5 | The services map in values.yaml is the one place to add or change a control service | `charts/vws/values.yaml` `charts/vws/values.schema.json` | `scripts/guard.sh names` |
| R7 | No logo files and no trademark notices | - | `scripts/guard.sh assets` |
| R8 | Manifests are checked with helm lint and kubeconform and never applied to a cluster. The only exceptions are the throwaway local lab `scripts/lab/`, `scripts/crd-check.sh`, which sends CRDs with `--dry-run=server` only to a throwaway envtest kube-apiserver (#210), and operator CLI calls through wrappers named exactly `vws_kubectl` and `vws_helm` (which support `--dry-run`). Any other prefix or suffix (`foo_kubectl`, `xhelm`, `my-helm`, `foo_vws_kubectl`, `Vws_helm`) fails like a direct call | `.github/workflows/ci.yml` `tests/fixtures/r8/` | `scripts/guard.sh ci` (negative `tests/fixtures/r8/bad/`, positive `tests/fixtures/r8/good/`) |
| R9 | Do not use Cursor cloud agents | `AGENTS.md` | `scripts/guard.sh cursor` |
| R10 | No units of ours on the hosts. The repo has no `.service` or `.target` files and no `host/`, and the chart uses no host namespaces and no hostPath | `charts/vws/templates/control-plane.yaml` | `scripts/guard.sh layout` |
| R11 | A control service's Kubernetes API rights come only from its `rbac` in values.yaml. No wildcards and no verbs such as escalate or bind. A rule on a namespaced resource names its `namespace` and is granted only through a Role in that namespace; a ClusterRole holds only cluster-scoped resources. A leader-election Lease is used only under the service's own name | `charts/vws/values.yaml` `charts/vws/values.schema.json` `charts/vws/templates/control-plane.yaml` | `scripts/guard.sh recovery` |
| R12 | Platform bundle versions (KubeVirt, CDI, Multus, Kube-OVN, Rook-Ceph, and k3s for the local lab) live only in `scripts/versions.env`, and the required kernel modules are documented | `scripts/versions.env` `docs/platform-bundle.md` | `scripts/guard.sh bundle` |
| R13 | A PR targets main and its body has `Part of #N`; neither the PR body nor any commit has `Closes/Fixes/Resolves #N`; CI reports commits that reached main without a PR. AGENTS.md has Insider-risk guardrails 1-7 | `.github/workflows/repo-guard.yml` `.github/workflows/direct-push-watch.yml` `scripts/guard-agents.sh` | `scripts/guard.sh agents` |
| R14 | Service configuration lives only in `services.*.flags` in values.yaml, and namespace names live only in `namespaces`. Rendered args equal the values, and a service that takes a namespace flag has a Role in that namespace | `charts/vws/values.yaml` `charts/vws/values.schema.json` `charts/vws/templates/_helpers.tpl` | `scripts/guard.sh config` |
| R15 | Every service's `-listen` port, container port and probe port is the service's single `port`. Probes are tcpSocket or grpc, never HTTP. network gets `-db` and `-pool` | `charts/vws/values.yaml` `charts/vws/templates/_helpers.tpl` `scripts/check_render.py` | `scripts/guard.sh config` |
| R16 | The network kubeconfig comes only from a read-only mount of an operator-created Secret, as a stopgap. The chart renders no Secret and puts no credential values in values or env | `charts/vws/values.schema.json` `charts/vws/templates/control-plane.yaml` `charts/vws/templates/_helpers.tpl` | `scripts/guard.sh config` |
| R17 | Every `scripts/vws` command is one file `scripts/vws.d/<name>.sh` with a `# summary:` line, `vws_cmd_main`, `tests/vws/<name>.test.sh` and a `### vws <name>` entry in `docs/vws-cli.md`. Commands never call kubectl or helm directly, only `vws_kubectl` and `vws_helm` (`--dry-run` blocks mutating calls). The harness runs on fake kubectl and helm and never reaches a cluster | `scripts/vws` `scripts/vws.d/lib/common.sh` `tests/vws/run.sh` | `scripts/guard.sh vws` |
| R18 | `VWSCluster` is the only VWS-specific deployment-configuration CRD (an exception to the #2 decision, `docs/vwscluster.md`). The only other VWS CRDs are the vws-iam store in `charts/vws/crds/iam.yaml` (#369): a copy of vws-iam `deploy/crds/iam.yaml`, Namespaced, group `iam.<VWSCluster group>`. The VWSCluster API group is written only in that CRD's `spec.group` (the guard compares the example CR's `apiVersion`). The CR schema has no secret-looking field names, and the CR holds only Secret names | `charts/vws/crds/vwsclusters.yaml` `charts/vws/crds/iam.yaml` `scripts/vws.d/lib/render.py` | `scripts/guard.sh crd` |
| R19 | A profile is a values file `profiles/<name>.yaml`, and the three profiles have the same keys. Each profile passes lint, template, kubeconform and check_render, and `vws render` of the same CR gives the same bytes | `profiles/` `platform/rook-ceph-cluster.single-node.values.yaml` `platform/rook-ceph-cluster.ha.values.yaml` | `scripts/guard.sh profiles` |
| R20 | The only cluster-admin for installation is the `vws-installer-temp` binding, with a TTL (`INSTALLER_TTL_SECONDS`) annotation, an installer pod whose deadline is the TTL, and a reaper Job that deletes the binding at the TTL. The bootstrap image is pinned by digest and has no privilege escalation, privileged mode, host namespaces or hostPath. The rights left after reclaim have no wildcards, Secrets, RBAC or cluster writes, and ClusterRole `vws-operator-read` has only get and list on the groups/resources in the `docs/installer.md` table (anything outside it fails). `vws installer finish` confirms the reclaim with auth can-i and exits 1 on failure | `bootstrap/bootstrap.yaml` `scripts/bootstrap.sh` `scripts/vws.d/installer.sh` `scripts/vws.d/lib/installer.sh` | `scripts/guard.sh bootstrap` `tests/vws/installer.test.sh` |
| R21 | A corrected mistake becomes an incident issue, and a repeated one is promoted to a check (the /correct loop). AGENTS.md has that section (with a body), the incident form exists, and the `incident` label is defined in `.github/labels.yml`. Whether the label actually exists on GitHub is not checked | `AGENTS.md` `.github/ISSUE_TEMPLATE/incident.yml` `.github/labels.yml` | `scripts/guard.sh agents` |
| R22 | The repo is English only: README, code comments, docs, AGENTS.md, templates, commit messages, PR and issue text and Verified-by lines. A tracked file may contain Hangul only when `.github/english-allowlist` lists it with a reason (test data, user-facing UI text); commit messages and PR bodies have no exception | `.github/english-allowlist` `scripts/guard-agents.sh` | `scripts/guard.sh english` |

The structure and check cells point at real paths and real commands. A rule that is only a sentence is not a rule.

## English only

Write everything in this repo in English: README, code comments, docs, AGENTS.md, QUALITY.md, PR and issue templates, commit messages, PR and issue bodies, review comments and `Verified-by` sections. `scripts/guard.sh english` (R22) fails on a tracked file with Hangul unless `.github/english-allowlist` lists it as `<glob> <reason>`; only test data and user-facing UI strings belong there. `scripts/guard-agents.sh commit-msg` and `pr-body` (run by `repo-guard.yml`) reject Hangul in commit messages and PR bodies outright.

## Branches and atomic commits

Do not commit on main. Fix things on a branch made with `git switch -c <branch>`, and open a pull request when done. Do not push to main directly; the pre-push hook refuses `refs/heads/main` and `refs/heads/master`.

One commit is one change. Only `scripts/scopes` says which path belongs to which scope; do not repeat the scope list in this file. The subject is `scope: summary`, as in `chart: add one service key`. A commit that touches two scopes fails the check, so split it.

`make check` sets `core.hooksPath` to `.githooks`. Do not skip hooks with `--no-verify`.

## Using pstack

Design and review go in the order below. Model choice follows the user's pstack rules; do not write model names in this file.

### architect

Design first, before changing a surface that takes values or the way things render. The files in question are `charts/vws/values.schema.json`, `charts/vws/templates/control-plane.yaml` and `charts/vws/templates/_helpers.tpl`. Record the chosen and rejected options in `docs/design/`. If the implementation departs from that design, write down why in the same record.

### interrogate

After changing the files above, review that diff adversarially before asking for review. Do not apply every finding automatically; fix only the ones that break behaviour or drop a recovery invariant.

### correct

When the same mistake repeats, do not write more sentences. First change the structure so the bad state cannot be expressed, then add a failing check, and only then update the table. The check must actually fail on an input that contains the mistake. A fix that only edits the table is not a rule change.

## /correct loop

1. When a bot is corrected (by the user Su Kim, tmp or eggbot, or by a failed `vws verify`), it opens an issue the same day with the `incident` label using the `.github/ISSUE_TEMPLATE/incident.yml` form. It must fill in the correction and "the check that would have caught this".
2. Every few days vws-verify runs a gardener pass. It groups open incidents by cause and promotes repeated ones to a check (test, lint, guard rule, archtest), not a sentence. The check must actually fail on an input that contains the mistake (the `correct` section above).
3. When the promoting PR is merged, close the incident with its link. An incident that will not be promoted is closed with the reason.
4. The milestone report (to tmp) lists the number of incidents in the period and the checks promoted.
5. The `incident` label is defined in `.github/labels.yml` (the only definition). That file is only a definition and does not create the label on GitHub. `scripts/guard-agents.sh correct` checks this section and its body, the required fields of the form and the label definition.

## Way of working (poteto-mode)

- Write concisely and solve with the smallest code change. Do not add abstraction layers; delete them when you can.
- Decide the shape of the data before writing code. When the same mistake shows up twice, block it with structure or a check instead of a sentence.
- Say something is verified only if you actually ran it. Mark what you could not run as "unverified". `make check` is this repo's T0 check, not evidence that something compiles.
- Write commit messages and PR descriptions as short plain statements. No unsupported claims and no made-up links.
- Do not use Cursor cloud agents (CloudAgent). Usage is exhausted; work with `gh` and `git`. `scripts/guard.sh cursor` keeps `.cursor/` and Cursor agent calls out of the repo.

## Insider-risk guardrails

Bots are insider risks too. The rules below have no exceptions.

1. **Independent verification**: an important result (merge, security, data) is never verified by the bot/model that wrote it. Another bot (or a person) verifies it. (Process rule; CI cannot enforce it.)
2. **Records**: every change leaves a trail of issue → PR (`Part of #N`) → commits. People close issues, so closing keywords such as `Closes #N` never appear in PR bodies or commit messages. Work logs go in the PR body or issue comments in a form people can read. No force-push or history rewriting (except with the user's approval).
3. **Failure tests**: a new feature is not tested on the happy path only. Include at least one case each for power loss/restart, idempotent re-run and malicious or boundary input, or state in the PR why that is not possible.
4. **Least privilege**: no direct pushes to main. Every change is a single PR from its own branch cut from main. No stacked PRs. No local merges. Tokens are scoped to the repo. RBAC covers only the resources the service code actually calls, as a Role (namespace scope) where possible.
5. **Separated audit**: author bot ≠ verifier bot ≠ merge decision. An author bot never merges its own PR. Merge only when base==main and every CI check on the head commit is success.
6. **Isolation and emergency stop**: a bot/agent is stopped immediately at the user's request or by the orchestrator stopping that bot's session and revoking its token (`gh auth`/PAT revoke). Risky work happens only in a sandbox (the box or a throwaway cluster). No access to production clusters.
7. **Incident disclosure**: rule violations and incidents are never hidden; record them in an issue as `Cause / Impact / Action / Prevention` (label `incident`).

This repo has no server-side protection (private, free plan). 1, 5 and 6 are process rules. For parts of 2 and 4, `.github/workflows/repo-guard.yml` checks the PR's base, the `Part of #N` in the body, and that neither the PR body nor the commits have a closing keyword, and `.github/workflows/direct-push-watch.yml` reports after the fact any commit that reached main without a PR. This is detection, not blocking.

## Commands

There are two supported entry points.

- `make check`: helm lint, kubeconform, the CRD check against an envtest kube-apiserver (`make crd`), the platform bundle render check, and the checks on this file
- `helm template vws charts/vws --namespace vws`: view the manifests without applying them

## Verify gate

`repo-guard / verify-gate` (#249) requires the `verify-approved` label and a PR comment (not the
body) with a `Verified-by: vws-verify` section that names the PR head SHA, with evidence for each
docs/QUALITY.md item. A push voids the approval. The PR author and the head pusher cannot approve
unless they are the vws-verify identity. The gate runs under `pull_request` (never
`pull_request_target`) and uses the guard script from the base commit, but the PR can still edit
the workflow YAML, and all bots share one account; see "Known gaps" in docs/QUALITY.md. It is not a
required check yet. vws-verify judges and labels; eggbot merges only after that. Test it locally
with `scripts/guard-agents.sh verify-gate <sha> <labels> <file>`.

Codex review result attached, no blocking findings: the same section must have a line
`Codex review: <full path or URL> blocking=<N>`, and the gate passes only with `blocking=0`.
Missing line: "no Codex review"; N > 0: "Codex review has N blocking findings"; no absolute path
or http(s) URL: "Codex review needs a full path or URL". Path rule:
`/workspace/handoff/codex/<repo>-<pr>-<sha7>.md`, for example
`Codex review: /workspace/handoff/codex/vws-deploy-301-7d994a0.md blocking=0`.
Standard commands: per PR `codex review --base <PR base branch>` (usually `main`); per commit
`codex review --commit <SHA>`. Save the output under the path rule, for example
`codex review --base main > /workspace/handoff/codex/vws-deploy-123-abc1234.md`. `blocking=<N>` is
the number of blocking findings in that saved review for the head being verified; the Verified-by
line points at that file, e.g. `Codex review: /workspace/handoff/codex/vws-deploy-123-abc1234.md blocking=0`.
Fix the findings and re-run on the new head until N is 0.
