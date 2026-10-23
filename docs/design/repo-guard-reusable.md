# repo-guard as one reusable workflow (#402)

## Problem

`.github/workflows/repo-guard.yml` was copied into every vws-* repo, so one bug was reviewed and fixed once per repo. The jobs are the verify-gate trust boundary, so moving them must not widen who can satisfy the gate or what a pull request can reach.

## Chosen

- The jobs (`pr-hygiene`, `verify-gate`, `verify-reset`, `verify-rerun`) live in vws-deploy `.github/workflows/repo-guard-reusable.yml`, `on: workflow_call` only. The required input `job` picks the one job a call runs. The workflow takes no secrets and sets top-level `permissions: {}`; each job keeps its own permissions.
- Every repo keeps `.github/workflows/repo-guard.yml` as a caller of at most 40 lines with exactly this shape (`.github/drift/caller_pin.py` checks it):

  | caller job | runs | `if` | permissions |
  | --- | --- | --- | --- |
  | `hygiene` | `pr-hygiene` | `pull_request` | contents read, pull-requests read |
  | `gate` | `verify-gate` | `pull_request` | contents read, pull-requests read, issues read |
  | `reset` | `verify-reset` | `pull_request` synchronize | issues write, pull-requests write |
  | `rerun` | `verify-rerun` | `issue_comment` on a PR | actions write, pull-requests read |
  | `pr-hygiene` | shim, `needs: hygiene` | `always()` and `pull_request` | none |
  | `verify-gate` | shim, `needs: gate` | `always()` and `pull_request` | none |

- The ref is `popododo0720/vws-deploy/.github/workflows/repo-guard-reusable.yml@<40-hex SHA>`, the same SHA in every call and every repo, and that SHA is on vws-deploy main. Never a branch or tag, which can move under every caller at once. vws-deploy itself calls `./.github/workflows/repo-guard-reusable.yml`, which GitHub runs from the same commit as the caller, so a vws-deploy PR that changes the jobs is checked by the changed jobs.

### Required check names

The rulesets require the bare contexts `pr-hygiene` and `verify-gate`. A job in a called workflow reports as `<caller job> / <called job>` (`hygiene / pr-hygiene`), so the caller has two shim jobs with the bare names. A skipped required check counts as passed, so a shim runs `always()` and passes only when `needs.<call>.result` is `success` and the call's `passed` output names its job. The called job writes `passed` in its last step, so a call that was skipped, cancelled or failed before the end fails the shim. The ruleset needs no change; the `x / y` runs are extra, non-required checks.

`verify-rerun` re-runs the called job `gate / verify-gate`. GitHub re-runs a job together with the jobs that depend on it, so the `verify-gate` shim re-runs after it. Re-running only the shim would replay the old `needs` result.

### Trust model (unchanged)

- Event: `pull_request` and `issue_comment` only, never `pull_request_target`. A fork PR gets a read-only token and no secrets, as before. The called workflow runs in the caller's context: `github`, `vars` and `github.token` are the caller repo's, and its token has at most what the caller job grants (GitHub can only lower permissions down the call chain). No secrets are passed (`secrets:` is not allowed in the caller).
- Guard script: `verify-gate` checks out the caller's base commit (`github.event.pull_request.base.sha`) and runs that `scripts/guard-agents.sh`, as before; `pr-hygiene` still runs the identity check against `scripts/guard-agents.sh.sha256`. The callee does not fetch the script from vws-deploy: that would need the called workflow's own SHA, which the `github` context does not carry, and it would bypass the per-repo identity and the accepted variant (#338).
- Who satisfies the gate: unchanged. The `verify-approved` label plus a `Verified-by: vws-verify` comment for the head, with `VWS_VERIFY_ACTORS`, `VWS_VERIFY_IDENTITY` and the author and pusher exclusion read from the caller repo's variables and API.
- What the pin adds: in a repo other than vws-deploy, a PR can no longer edit the job steps; they are fixed at the pinned vws-deploy commit. A PR can still edit its caller (the pin, the shim or the permissions), because `pull_request` runs the PR's copy of the caller. That is detected, not blocked: `caller_pin.py` and the daily drift-check report any caller that changed shape, pins a branch, tag or short SHA, pins a different SHA from the other repos, or pins a SHA that is not on vws-deploy main. In vws-deploy a PR can edit both files, as before. This is the same gap as docs/QUALITY.md "Known gaps"; the real control stays branch protection with review by a second account.

## How it fits #338

#338 moves verify-gate to the vws-verify App check run and a digest allowlist. With this layout that is one vws-deploy PR that changes the called `verify-gate` job (and adds `checks: read` to the `gate` call in `caller_pin.py`), followed by one pin bump per repo, instead of an edit in every repo. vws-conformance keeps its own repo-guard.yml and accepted guard variant (`caller-pending.tsv`, `guard-variants.tsv`) until then. Its variant runs verify-gate on `pull_request_target`; becoming a caller means moving it to `pull_request`, which #338 has to decide.

## Upgrade procedure (bumping the pin)

1. Merge the change to `repo-guard-reusable.yml` on vws-deploy main; note the merge commit SHA `S` (40 hex).
2. In each repo, one PR that replaces the four `@<old>` refs in `.github/workflows/repo-guard.yml` with `@S` (`sed -i "s/@[0-9a-f]\{40\}/@S/" .github/workflows/repo-guard.yml`) and nothing else. `python3 .github/drift/caller_pin.py <file>` (from a vws-deploy checkout) prints `S`.
3. Until the last repo merges, drift-check reports the repos whose pin is not the common one. That is expected during a rollout and clears when every repo pins `S`.
4. First move of a repo from the full copy: the same PR deletes the repo's row from vws-deploy `.github/drift/caller-pending.tsv` (a separate vws-deploy PR).

## Rejected

- A tag or branch ref (`@v1`, `@main`): one merge reaches every repo's required check unreviewed by that repo.
- One caller job that calls the whole workflow: the caller job must grant the union of permissions (actions write, issues write) to every job.
- Renaming the required checks to `hygiene / pr-hygiene`: a ruleset change in every repo, which #402 rules out.
- `pull_request_target` for the callee: it runs with a write token and secrets on fork PRs.

## Not verified

The YAML passes actionlint and the stubbed tests in `tests/workflows`. The GitHub behaviour (the `caller / called` job name, re-running a called job, the output of a failed call) is not exercised until a scratch PR runs it.
