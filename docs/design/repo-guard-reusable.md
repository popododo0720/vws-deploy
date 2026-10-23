# repo-guard as reusable workflows, one per job (#402)

## Problem

`.github/workflows/repo-guard.yml` was copied into every vws-* repo, so one bug was reviewed and fixed once per repo. The jobs are the verify-gate trust boundary, so moving them must not widen who can satisfy the gate or what a pull request can reach.

## Chosen

- Each job lives in its own vws-deploy file, `on: workflow_call` only: `.github/workflows/repo-guard-pr-hygiene.yml`, `repo-guard-verify-gate.yml`, `repo-guard-verify-reset.yml` and `repo-guard-verify-rerun.yml`. Each file takes no inputs and no secrets, sets top-level `permissions: {}`, and holds only its job with that job's permissions.
- Why one file per job: GitHub validates the permissions of every job in a called file when the run starts, before any `if:` is evaluated, and a called job can only keep or lower what the caller job grants. Round 1 had all four jobs in one file, so the read-only `hygiene` call failed against the write jobs beside it and the whole run ended in `startup_failure` with 0 jobs (run 39904989710). `tests/workflows/test_call_permissions.py` now fails when any caller job grants less than some job of the file it calls.
- Every repo keeps `.github/workflows/repo-guard.yml` as a caller of at most 40 lines with exactly this shape (`.github/drift/caller_pin.py` checks it):

  | caller job | calls | `if` | permissions |
  | --- | --- | --- | --- |
  | `hygiene` | `pr-hygiene` | `pull_request` | contents read, pull-requests read |
  | `gate` | `verify-gate` | `pull_request` | contents read, pull-requests read, issues read |
  | `reset` | `verify-reset` | `pull_request` synchronize | issues write, pull-requests write |
  | `rerun` | `verify-rerun` | `issue_comment` on a PR | actions write, pull-requests read |
  | `pr-hygiene` | shim, `needs: hygiene` | `always()` and `pull_request` | none |
  | `verify-gate` | shim, `needs: gate` | `always()` and `pull_request` | none |

- The top level has exactly `name: repo-guard`, `on`, `permissions: {}` and `jobs`. `defaults`, `env` and `concurrency` are refused because they change how the shims and calls run (a default shell that always exits 0 would pass both shims). `run-name` is refused too: it only sets the run title, which nothing needs. Repeated YAML keys are refused so the check reads what GitHub runs. Call jobs and shims are compared as whole mappings, so any job-level `defaults`, `env`, `continue-on-error`, `container` or step `shell` fails.
- Each ref is `popododo0720/vws-deploy/.github/workflows/repo-guard-<job>.yml@<40-hex SHA>`, the same SHA in every call and every repo, and that SHA is on vws-deploy main. Never a branch or tag, which can move under every caller at once. vws-deploy itself calls `./.github/workflows/repo-guard-<job>.yml`, which GitHub runs from the same commit as the caller, so a vws-deploy PR that changes the jobs is checked by the changed jobs.

### Required check names

The rulesets require the bare contexts `pr-hygiene` and `verify-gate`. A job in a called workflow reports as `<caller job> / <called job>` (`hygiene / pr-hygiene`), so the caller has two shim jobs with the bare names. A skipped required check counts as passed, so a shim runs `always()` and passes only when `needs.<call>.result` is `success` and the call's `passed` output names its job. The called job writes `passed` in its last step, so a call that was skipped, cancelled or failed before the end fails the shim. The ruleset needs no change; the `x / y` runs are extra, non-required checks.

`verify-rerun` re-runs the called job `gate / verify-gate`. GitHub re-runs a job together with the jobs that depend on it, so the `verify-gate` shim re-runs after it. Re-running only the shim would replay the old `needs` result.

### Trust model (unchanged)

- Event: `pull_request` and `issue_comment` only, never `pull_request_target`. A fork PR gets a read-only token and no secrets, as before. The called workflow runs in the caller's context: `github`, `vars` and `github.token` are the caller repo's, and its token has at most what the caller job grants (GitHub can only lower permissions down the call chain). No secrets are passed (`secrets:` is not allowed in the caller).
- Guard script: `verify-gate` checks out the caller's base commit (`github.event.pull_request.base.sha`) and runs that `scripts/guard-agents.sh`, as before; `pr-hygiene` still runs the identity check against `scripts/guard-agents.sh.sha256`. The callee does not fetch the script from vws-deploy: that would need the called workflow's own SHA, which the `github` context does not carry, and it would bypass the per-repo identity and the accepted variant (#338).
- Who satisfies the gate: unchanged. The `verify-approved` label plus a `Verified-by: vws-verify` comment for the head, with `VWS_VERIFY_ACTORS`, `VWS_VERIFY_IDENTITY` and the author and pusher exclusion read from the caller repo's variables and API.
- What the pin adds: in a repo other than vws-deploy, a PR can no longer edit the job steps; they are fixed at the pinned vws-deploy commit. A PR can still edit its caller (the pin, the shim or the permissions), because `pull_request` runs the PR's copy of the caller. That is detected, not blocked: `caller_pin.py` and the daily drift-check report any caller that changed shape, pins a branch, tag or short SHA, pins a different SHA from the other repos, or pins a SHA that is not on vws-deploy main. drift-check reads the caller on the default branch and at the head of every open PR, so a PR that changes the pin is flagged while it is open: the daily run fails on it, and `workflow_dispatch` with `repo` and `sha` checks one PR head on demand. On a vws-deploy pull request drift-check also checks that PR's head (`--head vws-deploy=<head sha>` from the event) but, as before, only warns on drift. In vws-deploy a PR can edit both files, as before. This is the same gap as docs/QUALITY.md "Known gaps"; the real control stays branch protection with review by a second account.

### What drift-check checks at each ref

drift-check applies the same rules on the default branch and at every PR head. It lists open PRs page by page; a page it cannot read is drift.

- Pending repos: a repo in `.github/drift/caller-pending.tsv` (`repo<TAB>sha256<TAB>reason`) is exempt only while its `repo-guard.yml` has exactly the pinned sha256, the unmigrated full copy. Any other content is checked by `caller_pin.py` and its errors are drift. A migration PR with a `defaults.run.shell` bypass or an `@main` pin is flagged. The hash is a pin, not "same as the default branch", so a full copy edited by a PR that merged between two drift runs is also flagged. Editing a full copy on purpose needs a vws-deploy PR that updates its hash first.
- Other workflow files: every `.yml` and `.yaml` file in `.github/workflows` is read. No file but `repo-guard.yml` may call a `repo-guard-*.yml` file (by pin or local path), so a second caller or a renamed one is flagged. No file but `repo-guard.yml` may have a job that can report the bare `pr-hygiene` or `verify-gate` check name. That name is the job `name`, or the job id when it has no name. The match ignores case. Such a job could satisfy a required check from another file, so it is flagged. A job name with `${{` anywhere is flagged too: expressions are never parsed or evaluated, because a quoted `}}`, `format()` or a `||` fallback can still produce a bare name (#436). Files that run only on `workflow_call` are skipped, because their jobs report as `caller / job`. An unreadable or unparsable file, or a symlink named like a workflow, is drift. A listing of 1,000 or more entries counts as unreadable, because the Contents API returns at most 1,000 and a file past the cap would never be read (#436).
- A missing `repo-guard.yml` is drift.

## Rollout notes

- Comment-triggered rerun after this merges: `issue_comment` runs the default-branch workflow. Once vws-deploy main has the caller, `verify-rerun` looks for the job `gate / verify-gate`. An open PR whose head still carries the old full `repo-guard.yml` has no such job, so the rerun prints "has no verify-gate job" and exits 0 without re-running anything. For those PRs, merge main into the branch (or push a new commit) so the head has the caller, or toggle the `verify-approved` label to start a `pull_request` run. The same applies in each repo when its caller PR lands. verify r2 saw the reverse before the merge: on PR #413 a comment ran main's old `verify-rerun`, which re-ran only the shim.

## How it fits #338

#338 moves verify-gate to the vws-verify App check run and a digest allowlist. With this layout that is one vws-deploy PR that changes the called `verify-gate` job (and adds `checks: read` to the `gate` call in `caller_pin.py`), followed by one pin bump per repo, instead of an edit in every repo. vws-conformance keeps its own repo-guard.yml and accepted guard variant (`caller-pending.tsv`, `guard-variants.tsv`) until then. Its variant runs verify-gate on `pull_request_target`; becoming a caller means moving it to `pull_request`, which #338 has to decide.

## Upgrade procedure (bumping the pin)

1. Merge the change to the `repo-guard-<job>.yml` files on vws-deploy main; note the merge commit SHA `S` (40 hex).
2. In each repo, one PR that replaces the four `@<old>` refs in `.github/workflows/repo-guard.yml` with `@S` (`sed -i "s/@[0-9a-f]\{40\}/@S/" .github/workflows/repo-guard.yml`) and nothing else. `python3 .github/drift/caller_pin.py <file>` (from a vws-deploy checkout) prints `S`.
3. Until the last repo merges, drift-check reports the repos whose pin is not the common one. That is expected during a rollout and clears when every repo pins `S`.
4. First move of a repo from the full copy: drift-check checks the migration PR's caller like any other, because it is no longer the pinned full copy. After it merges, a separate vws-deploy PR deletes the repo's row from `.github/drift/caller-pending.tsv`. Until then drift-check shows the caller as ok with "drop its caller-pending.tsv row".

## Rejected

- A tag or branch ref (`@v1`, `@main`): one merge reaches every repo's required check unreviewed by that repo.
- One caller job that calls the whole workflow: the caller job must grant the union of permissions (actions write, issues write) to every job.
- One file for all four jobs with a `job` input (round 1): GitHub validates every job's permissions before `if:`, so every call would have to grant the union, the same as above.
- Renaming the required checks to `hygiene / pr-hygiene`: a ruleset change in every repo, which #402 rules out.
- `pull_request_target` for the callee: it runs with a write token and secrets on fork PRs.

## Not verified

The YAML passes actionlint and the stubbed tests in `tests/workflows`, and the permission superset test reads the real files. verify r2 observed these on PR #413's head 390ca0e (run 39906944900): the run starts on the per-job layout, the bare `pr-hygiene` check passes, and the bare `verify-gate` check fails closed without a verdict. Not yet seen: a green `verify-gate`, re-running `gate / verify-gate` together with its shim (only possible after merge, see Rollout notes), the output of a failed call, and drift-check's pending-repo and other-file rules against the real API.
