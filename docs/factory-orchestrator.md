# Factory orchestrator (vws-deploy#333)

A deterministic script that runs three routine factory steps on GitHub state. It never calls an LLM.

| rule | what it does |
|---|---|
| A, verify wake | When at least 5 open, non-draft PRs on `main` lack `verify-approved` and their CI is not failing, it posts **one** comment on the status thread, draft PR vws-deploy#339 (`ORCHESTRATE_STATUS_PR`; PR comments go through the issues comments API). The comment's first line is `<!-- vws-orchestrate:verify-wake -->`. Each pending set (sorted `repo#number@head_sha`, sha256) is posted only once. After a rising edge the next comment waits at least 1h; while the count stays at or above the threshold, at least 6h. |
| B, DeepSeek queue | When no `@deepseek-dev` dispatch is in flight in any repo, it comments `@deepseek-dev please take this issue ...` on the first open issue or PR that has `ready` and `deepseek`, has no `grok` or `blocked` label, is not a draft, and has every `Depends on` met. Order: `priority/blocker` first, then the oldest `ready` label, then repo and number. |
| C, grok fallback | Each dispatch fails once on any of: a `[deepseek-dev] Failed for <dispatch url>` report, no report within 3h, or a Done PR closed without merge. At 2 counted failures it removes `deepseek`, adds `grok` and posts one notice. |

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
- **Failure resets** (failures before them do not count):
  - `deepseek` re-added after `grok`;
  - a comment starting `/orchestrate reset-failures`;
  - a report or dispatch URL listed in `exclusions.tsv` (vws-iam#123's token failure is the first entry).
- **Labels:** labels are never created. An action whose label is missing on the repo is skipped and
  logged.

## Safety limits

- **gh login:** the box's existing `gh` login, with `GH_TOKEN` and `GITHUB_TOKEN` removed from every
  gh call. There are no other secrets.
- **Allowlist:** every call goes through an allowlist:
  - GET: repo, issues, pulls, labels, repo comments, one issue or PR, issue events and comments,
    check runs, combined status, `rate_limit`;
  - POST: issue comments and issue labels;
  - DELETE: only the labels `blocked`, `in-review`, `deepseek`.
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
- `ORCHESTRATE_SCOPE=` (for example `vws-deploy#999` limits writes to one target and skips the wake)
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

GitHub writes are listed by `"event": "applied"` lines in the log. Reverting them is a manual,
approved step.
