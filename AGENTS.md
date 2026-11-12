# Agent rules

This repository holds only the deployment artifacts of the VWS private cloud. Systemd packages deploy the control plane; see docs/operator/regions.md. The design is https://github.com/popododo0720/vws-deploy/issues/2. No command here applies anything to a cluster.

## Rules

| ID | Rule | Structure | Enforcer |
| --- | --- | --- | --- |
| R1 | Work on a branch; never push to main or master. A push to an existing branch must fast-forward it, except a bot may rebuild its own PR branch on current main and push with `git push --force-with-lease=<branch>:<old-head>` when that is needed to meet the commit rules (each commit builds and passes tests, no merge commits, one concern per commit). The exception is only for that PR branch, never main or another branch; the PR must name the old and new heads | `.githooks/pre-push` | `scripts/guard.sh hooks` |
| R2 | A commit touches one scope from scripts/scopes, and its subject is `scope: summary` | `scripts/scopes` | `scripts/guard.sh commits` |
| R7 | No logo files and no trademark notices | - | `scripts/guard.sh assets` |
| R8 | CI runs the static guards and admin CLI tests. Manifests are never applied to a cluster except operator CLI calls through wrappers named exactly `vws_kubectl` and `vws_helm` (which support `--dry-run`). Any other prefix or suffix fails like a direct call | `.github/workflows/ci.yml` `tests/fixtures/r8/` | `scripts/guard.sh ci` (negative `tests/fixtures/r8/bad/`, positive `tests/fixtures/r8/good/`) |
| R9 | Do not use Cursor cloud agents | `AGENTS.md` | `scripts/guard.sh cursor` |
| R10 | Systemd units are the deployment unit (a `.deb` installs them). `.service` and `.target` files live only under `packaging/`, there is no `host/`. A unit loads secrets only through `LoadCredential=`: no `Environment=` or `EnvironmentFile=` that carries a secret, and no PVE API token or AWS access key literal in any file | `packaging/` | `scripts/guard.sh layout` `scripts/guard.sh secrets` |
| R12 | Retired Kubernetes platform and kubeconform pins must not return. Installer images do not render or carry the platform bundle | `scripts/versions.env` `bootstrap/Containerfile` | `scripts/guard.sh bundle` |
| R13 | A PR targets main, its body has `Part of #N`, one `AC: <n>` line per acceptance-criteria box it covers (or `AC: none - <reason>`, vws-deploy#486) and the 9-item Quality checklist, all ticked with evidence or `N/A: reason`, and every PR commit has a `<type>: summary` subject and `Part of #N`; neither the PR body nor any commit has `Closes/Fixes/Resolves #N`; CI reports commits that reached main without a PR. repo-guard.yml is the caller of the reusable repo-guard jobs, pinned by full SHA outside vws-deploy (#402). A PR changes at most the `.github/pr-size.conf` limit of lines outside generated, vendored, lockfile and testdata paths unless its body has `Size-exception: <reason>` (#401). AGENTS.md has Insider-risk guardrails 1-7 | `.github/workflows/repo-guard.yml` `.github/workflows/repo-guard-pr-hygiene.yml` `.github/workflows/repo-guard-verify-gate.yml` `.github/workflows/repo-guard-verify-reset.yml` `.github/workflows/repo-guard-verify-rerun.yml` `.github/drift/caller_pin.py` `.github/pr-size.conf` `.github/workflows/direct-push-watch.yml` `scripts/guard-agents.sh` | `scripts/guard.sh agents` |
| R17 | Every `scripts/vws` command is one file `scripts/vws.d/<name>.sh` with a `# summary:` line, `vws_cmd_main`, `tests/vws/<name>.test.sh` and a `### vws <name>` entry in `docs/vws-cli.md`. Commands call Kubernetes tools only through `vws_kubectl` and `vws_helm`. CI checks layout, shellcheck and the config, region, aws-account and project suites. The legacy Kubernetes harness remains available through `make vws-test` until #775 | `scripts/vws` `scripts/vws.d/lib/common.sh` `tests/vws/run.sh` | `scripts/guard.sh vws` |
| R20 | The only cluster-admin for installation is the `vws-installer-temp` binding, with a TTL (`INSTALLER_TTL_SECONDS`) annotation, an installer pod whose deadline is the TTL, and a reaper Job that deletes the binding at the TTL. The bootstrap image is pinned by digest and has no privilege escalation, privileged mode, host namespaces or hostPath. The rights left after reclaim have no wildcards, Secrets, RBAC or cluster writes, and ClusterRole `vws-operator-read` has only get and list on the groups/resources in the `docs/installer.md` table (anything outside it fails). `vws installer finish` confirms the reclaim with auth can-i and exits 1 on failure | `bootstrap/bootstrap.yaml` `scripts/vws.d/installer.sh` `scripts/vws.d/lib/installer.sh` | `scripts/guard.sh bootstrap` `tests/vws/installer.test.sh` |
| R21 | A corrected mistake becomes an incident issue, and a repeated one is promoted to a check (the /correct loop). AGENTS.md has that section (with a body), the incident form exists, and the `incident` label is defined in `.github/labels.yml`. Whether the label actually exists on GitHub is not checked | `AGENTS.md` `.github/ISSUE_TEMPLATE/incident.yml` `.github/labels.yml` | `scripts/guard.sh agents` |
| R22 | The repo is English only: README, code comments, docs, AGENTS.md, templates, commit messages, PR and issue text and Verified-by lines. A tracked file may contain Hangul only when `.github/english-allowlist` lists it with a reason (test data, user-facing UI text); commit messages and PR bodies have no exception | `.github/english-allowlist` `scripts/guard-agents.sh` | `scripts/guard.sh english` |
| R25 | Collector configuration examples document their rights, mounts and component stability, and pass `otelcol-contrib validate` at `OTELCOL_VERSION` | `docs/observability/collector-examples/` `scripts/versions.env` | `scripts/guard.sh observability` |
| R26 | Installer packages keep their layers: a package imports only the module packages its entry in `layers` lists, and a new package adds an entry. Only `internal/cluster` imports `os/exec` or Kubernetes client types, and no production code imports std `log` or `testing` | `installer/internal/archtest/arch_test.go` `.github/workflows/ci.yml` | `scripts/guard-agents.sh archtest` (ci installer job) `scripts/guard.sh agents` |
| R27 | A DeepSeek or grok task is dispatched only when its issue has an `## Acceptance criteria` section with checkbox items and a `Spec:` line: a draft PR or non-main commit by Grok (`Spec-by: grok`) whose test fails on CI, or `Spec: exempt (<reason>)` with a listed reason. `ORCHESTRATE_SPEC_FIRST` (`off`, `warn` or `enforce`) switches it; only `enforce` holds a task. `spec_state()` and `spec_gate()` in `common.py` decide it | `.github/orchestrate/rules/common.py` `.github/orchestrate/rules/dispatch.py` `.github/orchestrate/rules/grok.py` | `scripts/guard.sh agents` (runs `tests/orchestrate/test_spec_first.py` and the SpecFirst class of `tests/orchestrate/test_decide.py`) |

The structure and enforcer cells point at real paths and real commands. A rule that is only a sentence is not a rule. `scripts/guard-agents.sh rules` reads the last column as the enforcer and prints how many rows say `Judgment` (a rule with no check).

## English only

Write everything in this repo in English: README, code comments, docs, AGENTS.md, QUALITY.md, PR and issue templates, commit messages, PR and issue bodies, review comments and `Verified-by` sections. `scripts/guard.sh english` (R22) fails on a tracked file with Hangul unless `.github/english-allowlist` lists it as `<glob> <reason>`; only test data and user-facing UI strings belong there. `scripts/guard-agents.sh commit-msg` and `pr-body` (run by `repo-guard.yml`) reject Hangul in commit messages and PR bodies outright.

## Branches and atomic commits

Do not commit on main. Fix things on a branch made with `git switch -c <branch>`, and open a pull request when done. Do not push to main directly; the pre-push hook refuses `refs/heads/main` and `refs/heads/master`.

One commit is one change. Only `scripts/scopes` says which path belongs to which scope; do not repeat the scope list in this file. The subject is `scope: summary`, as in `ci: update one guard`. A commit that touches two scopes fails the check, so split it.

`make check` sets `core.hooksPath` to `.githooks`. Do not skip hooks with `--no-verify`.

Open a pull request with its body passed through `scripts/guard-agents.sh pr-normalize`, for example `gh pr create --body-file <(scripts/guard-agents.sh pr-normalize pr.md)`, and check it with `scripts/guard-agents.sh pr-body pr.md` first. `pr-normalize` rewrites a line `Part of popododo0720/vws-x#N` to `Part of vws-x#N`, the form the orchestrator's Rule D merges, and leaves everything else byte for byte. `pr-body` (the pr-hygiene check) fails on any line that starts with `Part of <owner>/<repo>#N` and names the allowed forms, `Part of #N` and `Part of vws-<repo>#N` (vws-deploy#504).

## Spec first

A task is specified before it is implemented. Grok writes the acceptance criteria in the issue
(measurable: a command, a test name, a status and error code with the AWS doc URL, or a number) and,
where feasible, a failing test as a draft PR or branch commit with the trailer `Spec-by: grok`, linked
from the issue as `Spec: <url>`. The implementer keeps that commit unchanged and makes it pass. When a
separate failing test is not practical, the issue says `Spec: exempt (<reason>): <why>` with a reason
from R27 (`docs`, `ci`, `config`, `revert`, `live-only`, `test-in-ac`). The implementer never writes or
edits its own spec. Questions about the spec go to the issue, not into the code. The orchestrator side
is in `docs/factory-orchestrator.md` ("Spec first").

## /correct loop

1. When a bot is corrected (by the user Su Kim, tmp or eggbot, or by a failed `vws verify`), it opens an issue the same day with the `incident` label using the `.github/ISSUE_TEMPLATE/incident.yml` form. It must fill in the correction and "the check that would have caught this".
2. Every few days vws-verify runs a gardener pass. It groups open incidents by cause and promotes repeated ones to a check (test, lint, guard rule, archtest), not a sentence. The check must actually fail on an input that contains the mistake (the `correct` section above).
3. When the promoting PR is merged, close the incident with its link. An incident that will not be promoted is closed with the reason.
4. The milestone report (to tmp) lists the number of incidents in the period and the checks promoted.
5. The `incident` label is defined in `.github/labels.yml` (the only definition). That file is only a definition and does not create the label on GitHub. `scripts/guard-agents.sh correct` checks this section and its body, the required fields of the form and the label definition.

## Way of working

- Write concisely and solve with the smallest code change. Do not add abstraction layers; delete them when you can.
- Decide the shape of the data before writing code. When the same mistake shows up twice, block it with structure or a check instead of a sentence.
- Say something is verified only if you actually ran it. Mark what you could not run as "unverified". `make check` is this repo's T0 check, not evidence that something compiles.
- Write commit messages and PR descriptions as short plain statements. No unsupported claims and no made-up links.
- Do not use Cursor cloud agents (CloudAgent). Usage is exhausted; work with `gh` and `git`. `scripts/guard.sh cursor` keeps `.cursor/` and Cursor agent calls out of the repo.

## Insider-risk guardrails

Bots are insider risks too. The rules below have no exceptions.

1. **Independent verification**: an important result (merge, security, data) is never verified by the bot/model that wrote it. Another bot (or a person) verifies it. (Process rule; CI cannot enforce it.)
2. **Records**: every change leaves a trail of issue → PR (`Part of #N`) → commits. People close issues, so closing keywords such as `Closes #N` never appear in PR bodies or commit messages. Work logs go in the PR body or issue comments in a form people can read. No force-push or history rewriting, except a bot may rebuild its own PR branch on current main and push it with `git push --force-with-lease=<branch>:<old-head>` when that is needed to meet the commit rules (each commit builds and passes tests, no merge commits, one concern per commit). That exception is only for the bot's own PR branch, never main or another branch; every commit must build and pass the repo's tests; the PR must name the old and new heads.
3. **Failure tests**: a new feature is not tested on the happy path only. Include at least one case each for power loss/restart, idempotent re-run and malicious or boundary input, or state in the PR why that is not possible.
4. **Least privilege**: no direct pushes to main. Every change is a single PR from its own branch cut from main. No stacked PRs. No local merges. Tokens are scoped to the repo. RBAC covers only the resources the service code actually calls, as a Role (namespace scope) where possible.
5. **Separated audit**: author bot ≠ verifier bot ≠ merge decision. An author bot never merges its own PR. Merge only when base==main and every CI check on the head commit is success.
6. **Isolation and emergency stop**: a bot/agent is stopped immediately at the user's request or by the orchestrator stopping that bot's session and revoking its token (`gh auth`/PAT revoke). Risky work happens only in a sandbox (the box or a throwaway cluster). No access to production clusters.
7. **Incident disclosure**: rule violations and incidents are never hidden; record them in an issue as `Cause / Impact / Action / Prevention` (label `incident`).

Main is protected by the active `vws-main` ruleset: a pull request is required, only a squash merge is allowed, force push and deletion are off, and the required checks include `pr-hygiene`, `verify-gate` and `vws-verify`. The ruleset has no bypass actors. `.github/drift/drift-check.sh` fails when the ruleset is missing or inactive, no longer applies to main, loses its pull request (squash only), force push or deletion rule, or a required check is dropped. GitHub shows a ruleset's bypass actors only to an admin token, so the scheduled run (`github.token`) reports them as unverified, never ok, and an audit run with an admin `gh` fails on any bypass actor. Guardrails 1, 5 and 6 stay process rules. For parts of 2 and 4, `.github/workflows/repo-guard.yml` checks the PR's base, the `Part of #N` in the body, and that neither the PR body nor the commits have a closing keyword, and `.github/workflows/direct-push-watch.yml` reports after the fact any commit that reached main without a PR.

### Script push, PR and merge

A deterministic script (the factory orchestrator in `docs/factory-orchestrator.md`, the handoff submit) may push a branch, open a pull request and merge one. A script merge happens only when all of these hold:

- every CI check on the head commit is green;
- `verify-gate` passes;
- the `vws-verify` App check run passes;
- the pull request has no conflicts;
- the merge is a squash, made with `--match-head-commit` set to the verified head;
- the pull request is not labeled `blocked`.

`ORCHESTRATE_MERGE=apply` is approved only under these conditions. The lead (dr eggbot) is woken only for a decision or a failure (issue scope, real conflicts, a failed check), not for a routine push, pull request or merge. Guardrails 1, 4 and 2 still hold: the author bot is never the verifier, nothing pushes to main, and every pull request body has `Part of #N` with no closing keyword.

## Commands

The supported check entry point is:

- `make check`: static guards, admin CLI tests and the checks on this file

## Verify gate

`repo-guard / verify-gate` (#249) requires the `verify-approved` label and a PR comment (not the
body) with a `Verified-by: vws-verify` section that names the PR head SHA, with evidence for each
docs/QUALITY.md item. A push voids the approval. The PR author and the head pusher cannot approve
unless they are the vws-verify identity. The gate runs under `pull_request` (never
`pull_request_target`) and uses the guard script from the current tip of the base branch, but the PR can still edit
the workflow YAML; see "Known gaps" in docs/QUALITY.md. It is a required check on main. vws-verify
judges and labels; a script merges only after that, under the rule above. Test it locally
with `scripts/guard-agents.sh verify-gate <sha> <labels> <file>`.

Code review is Kodus (owner, 2026-11-11): it reviews every push on the PR, and the factory posts PASS
only when no critical or high Kodus finding is open on the head. The Verified-by section needs no review
line.
