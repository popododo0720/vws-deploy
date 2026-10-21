# Factory orchestrator (vws-deploy#333)

A deterministic script that runs four routine factory steps on GitHub state. It never calls an LLM.

| rule | what it does |
|---|---|
| A, verify wake | When at least 5 open, non-draft PRs on `main` lack `verify-approved` and their CI is not failing, it posts **one** comment on the status thread, draft PR vws-deploy#339 (`ORCHESTRATE_STATUS_PR`; PR comments go through the issues comments API). The comment's first line is `<!-- vws-orchestrate:verify-wake -->`. Each pending set (sorted `repo#number@head_sha`, sha256) is posted only once. After a rising edge the next comment waits at least 1h; while the count stays at or above the threshold, at least 6h. |
| B, DeepSeek queue | When no `@deepseek-dev` dispatch is in flight in any repo, it comments `@deepseek-dev please take this issue ...` on the first open issue or PR that has `ready` and `deepseek`, has no `grok` or `blocked` label, is not a draft, and has every `Depends on` met. Order: `priority/blocker` first, then the oldest `ready` label, then repo and number. |
| C, grok fallback | Each dispatch fails once on any of: a `[deepseek-dev] Failed for <dispatch url>` report, no report within 3h, or a Done PR closed without merge. At 2 counted failures it removes `deepseek`, adds `grok` and posts one notice. |
| D, serialized merge (vws-deploy#346) | Squash-merges a PR that passed vws-verify, **at most one per repo per tick**, and only while the push CI on `main` is green for the current main head. After a merge the repo is locked until the push CI of the new squash commit is green. Red stops merges in that repo and posts one failure comment on #339. See "Rule D" below. |

Code: `.github/orchestrate/orchestrate.py` (Python 3 stdlib), `.github/orchestrate/exclusions.tsv`,
`.github/orchestrate/box/orchestrate-loop.sh`, `.github/orchestrate/box/orchestrate-ensure.sh`.
Tests: `tests/orchestrate/` (run by `.github/workflows/drift-check.yml`).

## How it decides

- **Pipeline:** collect (gh reads into one snapshot), then `decide()` (pure, table-tested), then apply
  (writes). Writes happen only with `ORCHESTRATE_MODE=apply`.
- **Repos:** the entries of `.github/drift/repos.tsv` plus `ORCHESTRATE_EXTRA_REPOS` (default
  `vws-conformance`). Archived repos (today `vws-hypervisor`) are skipped.
- **Dependencies:** a body line `Depends on #N` or `Depends on vws-<repo>#N`, comma separated.
  - A dependency is met when the issue closed as completed or the PR merged.
  - An open, unreadable, outside, archived or cyclic reference blocks the task. The orchestrator adds
    `blocked` and logs a `note`.
  - It removes `blocked` only if it added the label itself.
- **History:** candidates (`ready`, `deepseek` or `grok`) get their full comment history, so old
  dispatches and failures are never forgotten. Other items use the repo-wide comment list for the
  last 14 days.
- **Completion signal:** only the poller's `[deepseek-dev] Done for <url>` and
  `[deepseek-dev] Failed for <url>` comments. A Done report with an open PR adds `in-review`.
- **Done report with a PR outside `popododo0720/vws-*`:** that PR is never read (the gh allowlist
  refuses it). The attempt counts as one failure (kind `pr_outside_vws`) and each tick logs a
  `note` with kind `report_pr_outside_vws`, so the task is retried or moved to grok instead of
  waiting for review forever. The tick itself does not fail. If such a report was a real mistake,
  `/orchestrate reset-failures` or an exclusions.tsv entry clears it.
- **Move to grok and `in-review`:** when a task moves to grok, the orchestrator also removes
  `in-review` if it added that label itself; a human-set `in-review` stays. A task already on grok
  that still has an orchestrator-owned `in-review` loses it on the next tick.
- **Failure resets** (failures before them do not count):
  - `deepseek` re-added after `grok`;
  - a comment starting `/orchestrate reset-failures`;
  - a report or dispatch URL listed in `exclusions.tsv` (vws-iam#123's token failure is the first entry).
- **Labels:** labels are never created. An action whose label is missing on the repo is skipped and
  logged.

## Rule D: serialized merges (vws-deploy#346)

A GitHub merge queue needs an organization-owned repo, and the vws repos are user-owned, so the
orchestrator serializes merges itself. `decide()` lists every reason a PR cannot merge. The log has
one `note` with kind `merge_check` per open PR (`eligible`, `reasons`, head and main state), so each
tick records why each PR would or would not be merged.

A PR is eligible only when all of these hold:

| rule | reason when it fails |
|---|---|
| not a draft, base `main` | `draft`, `base_not_main` (stacked PRs never merge) |
| label `verify-approved` | `no_verify_approved` |
| not opted out: no `do-not-merge` or `hold` label, no `<!-- vws-orchestrate:no-merge -->` in the body, not the status thread | `opted_out` |
| the newest `vws-verify` check run from the vws-verify App (id 5313004) on the **current head** is success | `verify_check_missing`, `verify_check_<conclusion>` |
| every required check of `main` (classic protection plus rulesets, bound to its app id) is success on the head | `required_missing:<name>`, `required_pending:<name>`, `required_<conclusion>:<name>`, `required_checks_unreadable` |
| every other check run on the head is success, skipped or neutral, and every commit status is success (AGENTS.md rule 5) | `check_failed:<name>`, `check_pending:<name>`, `status_<state>:<context>`, `combined_status_<state>`, `checks_truncated`, `statuses_truncated` |
| `mergeable` is true and `mergeable_state` is `clean`, `has_hooks` or `unstable` | `mergeable_unknown` (GitHub is still computing; next tick), `conflicts`, `mergeable_state:<state>` |
| the head did not move during collect, and the PR is still open | `head_moved_during_collect`, `not_open` |
| the body has `Part of #N`, and the title has no closing keyword | `no_part_of`, `closing_keyword_in_title` |
| every `Depends on` is met (same rules as the DeepSeek queue) | `dep_unmet:<ref>` |
| fewer than `ORCHESTRATE_MERGE_MAX_ATTEMPTS` (2) failed merge attempts for this head | `merge_attempts_exhausted` |
| the repo is not locked, and main's push CI is green | `repo_locked:<why>`, `main_ci_pending`, `main_ci_none`, `main_ci_red` |
| no other PR of the repo is chosen this tick (lowest number first) | `one_merge_per_repo:<pr>` |

The `Agent:` line of the PR body is copied into the `merged` log event (`agent`, empty when absent).
No rule reads it, so a PR without that line merges the same way.

**Merge call.** `PUT repos/O/R/pulls/N/merge` with `{"merge_method": "squash", "sha": "<verified head>",
"commit_title": "<PR title> (#N)"}`. The squash message stays GitHub's default (the commit messages),
so the `Part of #N` lines are kept. GitHub refuses the merge if the head moved after verification. The
gh wrapper accepts no other PUT and no other payload. The code has no `update-branch` and no admin
merge. Right before the PUT the orchestrator reads again: main must still be at the collected head
with a green push CI, the head checks must still pass every check rule above, and the PR must still be
open, at the same head, not a draft, on base `main`, with `verify-approved` and no opt-out. The pinned
`sha` protects only the head, so the CI verdicts are re-read too.

**Rulesets.** Required checks are the union of classic protection and `rules/branches/main`, so the
`vws-main` ruleset (squash only, required checks such as `english`) is honored. Each requirement is
bound to its app id. A commit status can satisfy only a requirement that has no app id. The
orchestrator always squashes, never updates the branch (no merge commits for guard R2), and never
bypasses a ruleset or protection.

**Main push CI.** The Actions runs for one commit with `event=push` on `main`, newest run per
workflow: `green` when every run completed as success, skipped or neutral; `red` when any failed or
was cancelled; `pending` while any is running; `none` when there is no run yet.

**Lock per repo** (`merge_locks` in `state.json`):

| status | set when | released when |
|---|---|---|
| `merging` | journaled right **before** the PUT, with the attempt count | next tick: the PR is merged (the lock becomes `waiting` with GitHub's `merge_commit_sha`), or it is still open and unmerged (the lock is dropped; the attempt counts) |
| `waiting` | the PUT answered `merged` with the squash sha | the squash commit's push CI is green, or a newer main head that contains it (compare status `ahead`) is green |
| `red` | the squash commit's push CI is red | a newer main head that contains the red commit has a green push CI (a fix landed). A green re-run of the same commit does not clear it. |

A different main sha alone never releases a lock, because `branches/main` may still show the
pre-merge head right after a merge. While a repo is locked, nothing in it merges (`repo_locked`).

**Status comments.** A red main (after our merge, or a red current main head in a repo with a
candidate) posts **one** comment on #339 per repo and commit. Its first line is
`<!-- vws-orchestrate:failure -->` and it has `Kind: merge-main-CI-red`. A lock still waiting after
`ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS` (2) posts one `Kind: merge-main-CI-wait-timeout` comment and
stays locked. The key (sha256 of kind, repo and commit) is checked against #339 before posting, so
there is no retry loop. These comments count toward the write cap and are skipped while
`ORCHESTRATE_SCOPE` is set.

**Switches.** Merges need both `ORCHESTRATE_MODE=apply` and `ORCHESTRATE_MERGE=apply`. The default
`ORCHESTRATE_MERGE=dry-run` plans and logs merges but never calls the merge API, even in apply mode.
`off` skips rule D and its reads. `ORCHESTRATE_SCOPE=vws-x#N` limits merges to that one PR. The STOP
file and the write cap apply as for every write (one merge is one write).

**Reads.** Rule D reads only repos with a `verify-approved` candidate or a lock: `branches/main`, the
push Actions runs for one main commit, `branches/main/protection/required_status_checks` (404 means
no classic protection), `rules/branches/main`, and for each candidate the PR, its check runs and its
combined status. A lock also reads `compare/<old>...<main>`. A read error in one repo means no merge
there this tick (`merge_repo_unreadable`), not a failed tick.

## Safety limits

- **gh login:** the box's existing `gh` login, with `GH_TOKEN` and `GITHUB_TOKEN` removed from every
  gh call. There are no other secrets.
- **Allowlist:** every call goes through an allowlist:
  - GET: repo, issues, pulls, labels, repo comments, one issue or PR, issue events and comments,
    check runs, combined status, `rate_limit`; for rule D also `branches/main`, its required status
    checks, `rules/branches/main`, push Actions runs for one main commit, and `compare/<sha>...<sha>`;
  - POST: issue comments and issue labels;
  - DELETE: only the labels `blocked`, `in-review`, `deepseek`;
  - PUT: only `pulls/N/merge` with `merge_method` squash and a 40-hex `sha` (rule D).
- **Conditional reads:** reads send `If-None-Match`. A 304 is served from
  `~/.local/state/vws-orchestrate/etag-cache.json` and does not use the primary rate limit. The
  comment `since` value is floored to the UTC day, so its URL and ETag stay stable.
- **Rate floor:** a tick is skipped when `core.remaining` < `ORCHESTRATE_RATE_FLOOR` (1000), because
  other agents share the login.
- **Write cap:** at most `ORCHESTRATE_MAX_WRITES` (5) GitHub writes per tick. The verify-wake comment
  counts toward it.
- **No retried writes:** a write is never retried, because a timed-out POST may have landed. Every
  attempt uses the write budget, failed ones included. Reads retry 5xx and 429 twice.
- **Failed writes:** any failed write makes the tick fail (exit 4, `health.json` result
  `apply_failed`), so backoff and the failure comment start.
- **Crash recovery:**
  - An interrupted grok swap (`grok` added, `deepseek` still there) is finished on the next tick.
  - Ownership of `blocked` and `in-review` is journaled in `state.json` before the write, and pruned
    when the label is not on the item.
- **Re-check before posting:** a dispatch is skipped if the target got a new dispatch, lost its
  labels, or got `blocked` or `grok` since collect. A wake is skipped if #339 already has a comment with its key.
- **One tick at a time:** each tick takes `run.lock` (non-blocking), so overlapping or manual runs log
  `skipped` and exit 0.

## Failure handling and the failure comment

`orchestrate-loop.sh` runs every tick as a new process, so a crash ends one tick, not the loop.
Stopping the loop (`kill` of `loop.pid`) also stops a running tick, and children never hold
`loop.lock`.
- **Backoff:** after a failed tick (exit status not 0) the loop waits `ORCHESTRATE_BACKOFF_BASE`
  (60 s) times 2^(failures-1), capped at `ORCHESTRATE_BACKOFF_MAX` (1800 s).
- **Failure comment:** after each failed tick the loop runs `orchestrate.py --notify-failure`. At
  `ORCHESTRATE_FAILURE_THRESHOLD` (3) failures in a row, it posts one comment on #339 whose first line
  is `<!-- vws-orchestrate:failure -->`, with the count, host, last exit code and the last 3 errors
  (redacted).
- **Scoped trials:** while `ORCHESTRATE_SCOPE` is set, the failure comment is skipped and logged
  (`failure_alert_skipped`), like every other write outside the scope, unless the scope is the status
  thread itself. Watch `health.json` during a scoped trial.
- **Once per streak:** the key is sha256 of host and streak start, stored in `failure.json` only after
  a successful POST. A good tick deletes `failure.json`. A 1h minimum gap stops a restarted loop from
  posting again for the same outage.
- **GitHub-wide failures:** the failure comment ignores the write cap and the rate floor, because it is
  one call, so it still goes out when every tick fails. If GitHub itself is down, the POST fails, is
  logged, and the next failed tick tries again.
- **When it posts:** `ORCHESTRATE_FAILURE_ALERT=apply` (default) posts only in apply mode, `always`
  also in dry-run, `never` turns it off.

## Files on the box

| path | content |
|---|---|
| `/home/box/.local/opt/vws-orchestrate/releases/<sha7>/` | `.github/orchestrate/` and `.github/drift/repos.tsv` from merged `main` |
| `/home/box/.local/opt/vws-orchestrate/current` | symlink to the active release |
| `/home/box/.config/vws-orchestrate/env` | `KEY=VALUE` config, re-read every tick (no shell evaluation) |
| `/home/box/.config/vws-orchestrate/STOP` | if present, ticks are skipped |
| `/home/box/.local/state/vws-orchestrate/` | `state.json`, `health.json`, `failure.json`, `etag-cache.json`, `run.lock`, `loop.lock`, `loop.pid`, all 0600 |
| `/home/box/.local/state/vws-orchestrate/log/orchestrate.jsonl` | one JSON object per event; rotated at 5 MiB, keeps 5 |
| `/home/box/.local/state/vws-orchestrate/log/loop.log` | loop output; rotated at 1 MiB, keeps 1 |

Config keys and defaults:
- `ORCHESTRATE_MODE=dry-run`
- `ORCHESTRATE_SCOPE=` (for example `vws-deploy#999` limits writes to one target and skips the wake and the failure comment)
- `ORCHESTRATE_INTERVAL=600`
- `ORCHESTRATE_MAX_WRITES=5`
- `ORCHESTRATE_RATE_FLOOR=1000`
- `ORCHESTRATE_STATUS_PR=339` (the status thread: a never-merged draft PR in vws-deploy; a positive integer)
- `ORCHESTRATE_FAILURE_ALERT=apply`
- `ORCHESTRATE_FAILURE_THRESHOLD=3`
- `DEEPSEEK_TIMEOUT_HOURS=3`
- `DEEPSEEK_MAX_FAILURES=2`
- `VERIFY_WAKE_THRESHOLD=5`
- `VERIFY_WAKE_EDGE_MIN_HOURS=1`
- `VERIFY_WAKE_COOLDOWN_HOURS=6`
- `ORCHESTRATE_MERGE=dry-run` (rule D: `off`, `dry-run` or `apply`; merges need `apply` here and in `ORCHESTRATE_MODE`)
- `ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS=2`
- `ORCHESTRATE_MERGE_MAX_ATTEMPTS=2`

## Install (after this PR merges; from merged main)

The box has no systemd and no cron, and nothing restarts user processes after a box restart. The loop
is started with `setsid nohup`.

```bash
unset GH_TOKEN
cd /workspace/vws-deploy && git fetch origin main
SHA=$(git rev-parse --short=7 origin/main)
umask 077
install -d -m 0700 /home/box/.local/opt/vws-orchestrate/releases /home/box/.config/vws-orchestrate \
  /home/box/.local/state/vws-orchestrate/log
REL=/home/box/.local/opt/vws-orchestrate/releases/$SHA
install -d -m 0700 "$REL"
git archive --format=tar origin/main .github/orchestrate .github/drift/repos.tsv | tar -x -C "$REL"
ln -sfn "releases/$SHA" /home/box/.local/opt/vws-orchestrate/current
printf 'ORCHESTRATE_MODE=dry-run\nORCHESTRATE_INTERVAL=600\n' > /home/box/.config/vws-orchestrate/env
# one manual dry-run tick, then start the loop
python3 /home/box/.local/opt/vws-orchestrate/current/.github/orchestrate/orchestrate.py --once --print-plan
/home/box/.local/opt/vws-orchestrate/current/.github/orchestrate/box/orchestrate-ensure.sh
```

### After a box restart (one-liner)

dr eggbot runs this when it wakes for VWS work. Any agent may run it; it is idempotent and locked:

```bash
/home/box/.local/opt/vws-orchestrate/current/.github/orchestrate/box/orchestrate-ensure.sh
```

It prints `started (pid N)`, `running (pid N)`, or an error with the loop log path.

### Verify

```bash
S=/home/box/.local/state/vws-orchestrate
pgrep -fc orchestrate-loop.sh                     # 1
python3 -m json.tool "$S/health.json"             # last_tick within 15 min, result ok
tail -n 5 "$S/log/orchestrate.jsonl"
rg -c 'gho_|ghp_|github_pat_|Bearer ' "$S/log"    # no matches
```

Merging (rule D) is switched on separately with `ORCHESTRATE_MERGE=apply`, also only with dr eggbot's
approval, and first with `ORCHESTRATE_SCOPE` set to one verified PR.

Switch to apply only with dr eggbot's approval, first scoped to one throwaway issue
(`ORCHESTRATE_SCOPE=vws-deploy#<n>`, `ORCHESTRATE_MODE=apply` in the env file), then clear the scope.

### Roll back

| goal | action |
|---|---|
| pause | `touch /home/box/.config/vws-orchestrate/STOP` |
| read-only | `ORCHESTRATE_MODE=dry-run` in the env file (next tick) |
| previous release | `ln -sfn releases/<old sha7> /home/box/.local/opt/vws-orchestrate/current` |
| stop | `kill "$(cat /home/box/.local/state/vws-orchestrate/loop.pid)"` |
| uninstall | stop, then `rm -rf /home/box/.local/opt/vws-orchestrate /home/box/.local/state/vws-orchestrate /home/box/.config/vws-orchestrate` |

To unlock a repo by hand, remove its entry from `merge_locks` in `state.json` while the loop is
stopped or STOP is present. GitHub writes are listed by `"event": "applied"` lines in the log, and each
merge also has a `"event": "merged"` line. Reverting them is a manual,
approved step.
