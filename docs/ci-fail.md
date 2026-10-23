# CI-failure marker (vws-deploy#412)

A red PR CI run posts exactly one marker comment per head SHA. The comment names the failed jobs,
shows a bounded and masked log tail, and carries a JSON record that is the source of truth. A
`POST` is the only event that wakes an owner; a rerun of the same SHA edits the comment in place.

This is the GitHub-side producer from #412 stage 1. The orchestrator and the Grok trigger are the
consumers described below; the marker is the same whether or not they exist yet.

## Caller

Copy `.github/ci-fail/caller.yml` into `.github/workflows/ci-fail.yml` in a `vws-*` repo. The
workflow names in `on.workflow_run.workflows` and in `with.workflows` must be the same list.

```yaml
name: ci-fail
on:
  workflow_run:
    workflows: [ci, english]
    types: [completed]

concurrency:
  group: vws-ci-fail-${{ github.repository }}-${{ github.event.workflow_run.head_sha }}
  cancel-in-progress: false

jobs:
  marker:
    uses: popododo0720/vws-deploy/.github/workflows/ci-fail.yml@main
    permissions:
      actions: read
      contents: read
      issues: write
      pull-requests: write
    with:
      workflows: ci,english
```

- `check_suite` cannot be used: suites created by GitHub Actions do not trigger workflows. The
  caller uses `workflow_run`.
- Fire only for `workflow_run.event == "pull_request"`, a head branch other than main, and exactly
  one open PR whose `head.sha` is the run's head SHA. The script looks the PR up through
  `GET /repos/{o}/{r}/commits/{sha}/pulls` because `workflow_run.pull_requests` is empty for some
  events.
- `contents: read` is for the public `vws-deploy` checkout of the script. The comment is written
  with the caller's `GITHUB_TOKEN`, so its author is `github-actions[bot]`.
- Keep `repo-guard`, `direct-push-watch` and `drift-check` out of the list. `verify-gate` is red
  until `vws-verify` approves, so it would produce a rework marker on every PR.
- The `needs-human` label must exist. Add it with
  `gh label create needs-human --color b60205 --description "CI rework stopped after 3 attempts in the head chain (vws-deploy#412); remove it to allow 3 more" --force`.

A third failing workflow can replace the pending run in the concurrency group. That is safe: each
run reads every failed workflow on the SHA, so the surviving run still records all of them.

## Marker line

```
<!-- vws-ci-fail sha=<40 hex> v=1 repo=<vws-x> pr=<N> owner=<owner> n=<k> wake=<true|false> state=<failing|passed> -->
```

Keys are always in that order, single spaces, no spaces in values. The parse regex is:

```text
\A<!-- vws-ci-fail sha=([0-9a-f]{40}) v=(\d+) repo=(vws-[a-z0-9-]+) pr=(\d+) owner=([a-z0-9:-]+) n=(\d+) wake=(true|false) state=(failing|passed) -->$
```

A server-side trigger can filter on a plain substring: `<!-- vws-ci-fail ` plus `owner=<bot>` and
`wake=true`.

The record is the **last** fenced block whose info string is exactly `json`. Line 1 duplicates the
fields a substring trigger needs. The job list and log tails are informative only; parsers must not
read them.

## Record v1

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | int | Schema version, 1 |
| `repo` | string | `vws-x` |
| `pr` | int | PR number |
| `head_sha` | string | 40 hex; the dedupe key |
| `head_branch` | string | PR head branch |
| `owner` | string | see below |
| `owner_source` | string | `label:grok`, `label:deepseek`, `agent_line`, `branch_prefix`, `none` |
| `chain` | int[] | PR numbers in the head chain, oldest first, this PR last |
| `n` | int | Ordinal of this failing head in the chain (1-based) |
| `cap` | int | 3 |
| `wake` | bool | `n <= 3`, owner wakeable, `needs-human` absent |
| `state` | string | `failing`, or `passed` after every failed workflow reran green |
| `runs` | object[] | `{workflow, run_id, run_attempt, conclusion, url}`, newest attempt per failing workflow |
| `failed_jobs` | object[] | `{workflow, job, job_id, conclusion, url, tail_lines, tail_truncated}` |
| `created_at`, `updated_at` | string | RFC 3339 UTC |

Failed conclusions are `failure`, `timed_out`, `startup_failure` and `action_required`. A
`cancelled` run is not a failure to rework.

## Owner

First match wins.

1. Label `grok` gives `owner=<repo>`, the repo's Grok dev bot (`label:grok`). It wins over
   `deepseek` because the orchestrator adds `grok` on a swap.
2. Label `deepseek` gives `owner=deepseek` (`label:deepseek`).
3. The PR body `Agent:` line maps `deepseek...` to `deepseek`, `vws-x...` to `vws-x`, and
   `eggbot`/`dr eggbot` to `eggbot` (`agent_line`).
4. A `deepseek/` branch prefix gives `deepseek` (`branch_prefix`). `fix/`, `test/` and `feat/`
   carry no owner because dev bots and the lead share them.
5. Otherwise `owner=none`, `wake=false` (`none`).

Wakeable owners are `deepseek` (the orchestrator) and the repo's own `vws-*` Grok bot. `eggbot`
and `none` are recorded with `wake=false`. Only the PR's own labels and body count; the workflow
does not read the `Part of` issue.

## Dedupe and reruns

The key is `(repo, pr, head_sha)`. Before writing, the script lists the PR's issue comments and
takes the one by `github-actions[bot]` whose line 1 matches this SHA.

- None: POST a comment. This is the only wake.
- Found: PATCH it. Merge `runs`/`failed_jobs` so another workflow failing on the same SHA adds its
  entry and a new attempt replaces its own workflow's entry. Refresh the tails. Keep `n` and
  `wake` unchanged. An edit never re-wakes.
- A later green rerun of every workflow in the record sets `state=passed` in line 1 and in the
  record. Consumers skip `passed`. The caller must fire on success for this to happen; it does
  work only when a marker for the SHA exists.
- A new push is a new SHA, so a new comment and `n + 1`.

## Cap and reset

The chain is the open and closed PRs in the repo whose head branch, with a trailing `-v<N>`
removed, equals this PR's head branch with `-v<N>` removed (`fix/x`, `fix/x-v2`, `fix/x-v3`). This
matches the `vws-rebuild-pr` skill. A `Supersedes #N` line in the PR body also links a rebuild that
renamed.

`n` is 1 plus the number of distinct `sha` values in `state=failing` markers by
`github-actions[bot]` on all chain PRs, counted only after the newest `needs-human` `unlabeled`
event on this PR.

- `n <= 3`: `wake=true` when the owner is wakeable.
- `n == 4`: `wake=false`, add `needs-human`, and put exactly one line above the JSON block:
  `**needs-human**: 4th failing head in this chain (<sha7 list>); auto-rework stopped. Remove the label to allow 3 more.`
- `n > 4`, or `needs-human` already on the PR: the marker is still posted for the record,
  `wake=false`, no escalation line, no label write.
- Removing `needs-human` is the only reset. The next failure counts from 1 again.

The orchestrator recomputes `n` and `wake` from the comments; the record is not trusted alone.

## Log tail

- Source: `GET /repos/{o}/{r}/actions/jobs/{job_id}/logs`, with the caller token.
- At most 5 jobs get a tail. The rest are listed only with `tail_lines: 0`.
- Per job: the last 40 lines of the whole job log (the Actions API does not split a log into
  steps reliably), each line cut to 200 characters plus `…`, at most 4 KiB after masking.
- The whole comment is kept under 16 KiB. Tails are dropped first, then job list lines; the JSON
  record is never dropped.
- Cleanup before masking: strip ANSI escapes and the leading Actions timestamp, and drop
  `##[group]`, `##[endgroup]` and `::add-mask::` lines.
- Masking replaces with `[redacted]`: `gh[opsu]_…`, `github_pat_…`, `Bearer <x>`, JWTs,
  `AKIA`/`ASIA` key ids, values after `password|passwd|secret|token|api_key|access_key|private_key|authorization`
  and `:` or `=`, URL credentials, and `BEGIN … PRIVATE KEY` blocks.
- A log cannot forge or ping: `<!--` becomes `<!-​-`, `-->` becomes `-​->`, `@` becomes `@​`, and a
  run of 4 or more `~` becomes 3, so the `~~~~text` fence cannot be closed by log text.

## Grok trigger

Each Grok dev bot needs one server-side GitHub trigger in its own repo:

- Event: `issue_comment`, action `created`. If the platform only offers "created or edited", the
  routine must dedupe on `sha` itself.
- `comment.user.login == "github-actions[bot]"`.
- `comment.body` starts with `<!-- vws-ci-fail `, contains `owner=<own repo>` and `wake=true`.
- Re-read the comment, parse the JSON record, and check that `head_sha` is still the PR head before
  working.

## DeepSeek consumer (stage 2)

The orchestrator's 600 s tick is the poller #412 allows for DeepSeek. It adds a `classify_comment`
kind `ci_fail`, accepts a marker only from `github-actions[bot]`, and for `owner=deepseek`,
`wake=true`, `state=failing`, `sha == PR head`, queues
`@deepseek-dev please rework this PR ...` on the same PR with the failed jobs quoted and the
existing rework marker extended with `ci=<comment id>`. One rework per head; the orchestrator
recomputes `n` from the comments. That consumer is not part of this stage-1 change.
