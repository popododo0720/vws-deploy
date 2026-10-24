# CI-failure marker comment (vws-deploy#412)

When CI fails on a pull request head, `.github/workflows/ci-fail.yml` posts one comment per head
SHA. The comment wakes the PR's owner: the orchestrator queues a DeepSeek rework, and a Grok dev
bot's own GitHub-trigger routine wakes that bot. Nobody polls. After 3 auto-reworks per PR head
chain, the 4th failure adds `needs-human` and wakes nobody.

The logic is `.github/ci-fail/ci_fail.py` (stdlib, `gh api` with the caller's `GITHUB_TOKEN`);
tests are `tests/ci_fail/` (fake gh, no network).

## When it runs

- The caller fires on `workflow_run` `completed` for every CI workflow of its repo.
- It acts only when the run's event is `pull_request`, the conclusion is `failure`, `timed_out`,
  `startup_failure` or `action_required` (not `cancelled`), the head branch is in the same repo and
  is not `main`/`master`, and exactly one open PR has that head SHA. Push runs on main never post.
- A green run does nothing. There is no passed state; consumers compare `sha` with the PR head.
- Permissions: `actions: read` (jobs and logs), `contents: read`, `pull-requests: write` (comment
  and label). The PR's code is never checked out. The script is checked out from
  `job.workflow_repository` at `job.workflow_sha`, the vws-deploy commit the caller pinned, so a
  push to vws-deploy main changes nothing until the caller bumps its pin.

## Comment format (v=1)

Line 1 is the marker, keys always in this order:

```
<!-- vws-ci-fail sha=<40 hex> v=1 repo=<vws-x> pr=<N> owner=<deepseek|grok|none> agent=<name|-> n=<k> wake=<true|false> -->
```

Then a summary line, the failed jobs (`workflow / job: conclusion (url)`), one collapsed log tail per
job, an optional escalation line, and a fenced `json` block. The JSON record is the source of truth;
a comment counts only when line 1 matches the record exactly (`parse_comment`). Fields: `v`, `repo`,
`pr`, `head_sha`, `head_branch`, `owner`, `agent` (`""` when none; `-` on line 1), `owner_source`
(`label:grok`, `label:deepseek`, `agent-line`, `branch-prefix`, `none`), `chain`, `n`, `cap` (3),
`wake`, `runs` (`workflow`, `run_id`, `run_attempt`, `conclusion`, `url`), `failed_jobs` (`workflow`,
`job`, `job_id`, `conclusion`, `url`, `tail_lines`, `tail_truncated`), `created_at`, `updated_at`.
`<`, `>`, `@`, backtick and `~` are written as `\u` escapes in the JSON.

A `failure` or `timed_out` run always has a failed job, but right after the run the jobs API can
list none yet (#443). The script then reads the job list up to 3 times, 5 s and 10 s apart. If it is
still empty, `failed_jobs` is `[]` and the comment says `Failed jobs: unavailable (...)` with the run
URL. `Failed jobs: none reported` is kept for runs that failed before any job ran, such as
`startup_failure`. The needs-human label POST is idempotent and retries like a read; comment writes
are never retried.

## One comment per head SHA

- No marker by `github-actions[bot]` for the SHA on the PR: post a new one. Only this wakes anyone.
- A marker exists (a rerun, or another workflow failing on the same SHA): edit it. The workflow's
  own `runs` and `failed_jobs` entries are replaced, other workflows' entries are kept, `n` and
  `wake` never change. A per-repo-and-SHA concurrency group serializes the runs.
- A new push is a new SHA: a new comment with `n + 1`.

## Owner

First match wins:

1. Label `grok`: `owner=grok`; `agent` is the `Agent:` name if it matched and is not `deepseek-dev`,
   else the repo name.
2. Label `deepseek`: `owner=deepseek`, `agent=deepseek-dev`.
3. `Agent:` line in the PR body: regex `^Agent:[ \t]*([a-z0-9][a-z0-9-]{0,38})[ \t]*$`, multiline,
   case-sensitive, first match. `deepseek-dev` gives `owner=deepseek`; any other name gives
   `owner=grok`, `agent=<name>`. `Agent: dr eggbot` does not match (space), so lead-owned PRs fall
   through.
4. Branch prefix `deepseek/`: `owner=deepseek`, `agent=deepseek-dev`.
5. Otherwise `owner=none`, `agent=-`, never woken.

## Cap and needs-human

- Chain: same-repo PRs whose head branch minus a trailing `-v<N>` equals this one's (`fix/x`,
  `fix/x-v2`), plus PRs named by a `Supersedes #N` line in a chain PR's body (optional).
- `n` = 1 + the distinct SHAs of earlier markers by `github-actions[bot]` on the chain, counted after
  the latest reset on any chain PR. A reset is a `needs-human` `unlabeled` event, or a
  `/orchestrate reset-failures` comment whose `author_association` is `OWNER`, `MEMBER` or
  `COLLABORATOR`. Other authors' resets are ignored. The cap is CI-only.
- `wake` = `n <= 3`, owner is `deepseek` or `grok`, and the PR has no `needs-human`.
- `n == 4` without `needs-human`: add `needs-human` and one escalation line. Later failures post
  markers with `wake=false` and no new escalation.

## Log tail

Per failed job (first 5 jobs): the job log with ANSI codes, timestamps and `##[group]` /
`::add-mask::` lines removed, then masked, then the last 40 lines, each cut to 200 characters, at
most 4 KiB; all tails together at most 12 KiB. Masking, on top of GitHub's own `***`: private key
blocks, URL credentials, `gh*_`/`github_pat_` tokens and `Bearer` values (orchestrate.py's
`TOKEN_RE`), JWTs, AWS key IDs, and values after `password`, `secret`, `token`, `api_key`,
`access_key`, `private_key`, `authorization` with `:` or `=`. Tails are defused: `<!--`, `-->`, `@`,
runs of backticks and of tildes get a zero-width space, so a log cannot forge a marker, mention a
bot or break the fence.

## Grok dev bot: pr-comment trigger routine

Set up one GitHub-trigger routine per Grok dev bot, on the repos it works in:

- Event: issue comment **created** on a pull request. Ignore `edited`: dedupe edits the comment on
  reruns, and an edit is never a new failure.
- Author: `comment.user.login == "github-actions[bot]"`. Anyone else can paste a marker.
- Body: starts with `<!-- vws-ci-fail ` and line 1 contains ` owner=grok agent=<your name> ` and
  ` wake=true `. Use the sidebar name exactly, e.g. ` agent=vws-storage `.
- Before working: read the PR (`gh pr view <N> --json headRefOid,state`) and continue only when it is
  open and `headRefOid` equals the marker's `sha`. Otherwise a newer push already superseded it.
- Then fix on the same branch and PR, using the failed jobs and tails as a starting point (the tail
  is bounded; open the job URL for the full log).

DeepSeek is woken by the orchestrator, which queues only `owner=deepseek` and logs `owner=grok` until
#398(a). CI reworks count toward its swap to grok after 2 failures; a head with both a CI FAIL and a
verify FAIL counts once.

## Caller (one per vws-* repo, after this merges)

List every CI workflow of the repo by its `name:`. Keep the list in sync when a workflow is added.
Pin `uses:` to a full commit SHA on vws-deploy main (the SHA below is a placeholder).

```yaml
# .github/workflows/ci-fail.yml in a vws-* repo
name: ci-fail-caller

on:
  workflow_run:
    workflows: [ci, english, repo-guard]   # every CI workflow name of this repo
    types: [completed]

permissions: {}

jobs:
  marker:
    permissions:
      actions: read
      contents: read
      pull-requests: write
    # A full 40-hex commit SHA on vws-deploy main, never a branch (#402); the script runs from it too.
    uses: popododo0720/vws-deploy/.github/workflows/ci-fail.yml@0123456789abcdef0123456789abcdef01234567
```

vws-deploy's own caller is `.github/workflows/ci-fail-caller.yml` (`ci-fail.yml` here is the reusable
workflow). It lists `ci`, `drift-check` and `installer-image`; `tests/workflows/test_ci_fail_caller.py`
fails when a pull_request CI workflow is missing from it.

`workflow_run` runs the caller from the default branch, so a PR cannot change it. `check_suite` is
not used: suites created by GitHub Actions do not trigger workflows.
