# Daily factory metrics (vws-deploy#345)

A deterministic script on the shared box writes one JSON file of factory metrics per KST day. It
never calls an LLM, never writes `main`, and never opens a PR. Design: vws-deploy#345.

Nothing polls the metrics. On a normal day the script is silent and only appends to the
`factory-metrics` branch. On an abnormal day it posts one alert comment on the status issue (vws-deploy#415)
and, when configured, POSTs a short JSON to the alert webhook (see [Alerts](#alerts)).

Code: `.github/metrics/metrics.py` (Python 3 stdlib). It imports the orchestrator
(`.github/orchestrate/orchestrate.py`) for the gh wrapper, log format, config file and comment parsers.
Tests: `tests/metrics/` (run by `.github/workflows/drift-check.yml`, stub gh, no network).

## Where the data goes

- **Branch:** `factory-metrics` in vws-deploy, an orphan branch that is never merged and never opened
  as a PR.
- **Files:**
  - `metrics/daily/YYYY-MM-DD.json`: one per KST day;
  - `metrics/summary.json`: headline numbers for the last 28 days;
  - `metrics/SUMMARY.md`: 7-day and 28-day totals and a per-day table.
- **One commit per day,** made through the Git Data API with the box gh login:
  1. `POST git/trees` on the old tree, with the three files inline;
  2. `POST git/commits` with the branch head as its only parent;
  3. `PATCH git/refs/heads/factory-metrics` with `force: false`.

  A ref that moved in between makes the PATCH fail; nothing is forced.
- **Commit message:** subject `metrics: daily YYYY-MM-DD`, with the trailer
  `Metrics-Generator: vws-metrics`. `metrics` is a scope in `scripts/scopes` that owns `metrics/`, so
  these commits also pass `scripts/guard.sh commits` (R2).
- **No workflow runs on this branch:** every `push` trigger in vws-deploy filters on `main`.
- **Self-check:** before every write the script reads the branch history since the last head it
  wrote or checked. It refuses (exit 5, `metrics_foreign_commit`) if any commit is:
  - missing the trailer,
  - not `metrics: daily ...` with one parent (or the root commit from `--init`),
  - touching a path outside `metrics/`.

  All bots share one login, so this check, not GitHub, is what keeps the branch to this script.
- **English (R22):** output is JSON and Markdown with English keys. Output with any non-ASCII
  character is refused (exit 5, `metrics_non_ascii`) and not written, not even locally.

## What it measures

For KST day D (`[D 00:00, D+1 00:00)` KST, `window_utc` in the file), per repo and in total. Repos
are `.github/drift/repos.tsv` plus `ORCHESTRATE_EXTRA_REPOS`, and archived repos are skipped.

| key | meaning |
|---|---|
| `prs.opened`, `prs.merged` | `{repo: {agent: n}}`; the agent is the PR body line `Agent: <name>` (lower case), else `unattributed` |
| `prs.closed_unmerged`, `prs.totals`, `prs.by_agent` | as named |
| `lead_time_hours` | created to merged, for PRs merged in D: n, median, p90 (nearest rank), max |
| `verify` | `vws-verify` check runs (App 5313004) completed in D, on the heads of PRs updated in D and on every commit of PRs merged in D: runs, success, failure, `pass_rate`. `first_pass_rate`: merged PRs whose first vws-verify verdict was success. `approved_labels`: `verify-approved` labels added in D |
| `deepseek` | `@deepseek-dev` dispatches and `[deepseek-dev] Done/Failed for` reports created in D. `timeout`: dispatches whose `DEEPSEEK_TIMEOUT_HOURS` deadline fell in D with no report before it, judged by the orchestrator's `dispatch_history` (exclusions apply). Reports follow `dispatch_history` too (vws-deploy#462): only the first report per dispatch URL counts (a later one is `duplicate_reports`); a Failed report with a provider error (`INFRA_ERROR_RE`) is `infra`, not `failed`; a Failed report whose dispatch or report URL is in `exclusions.tsv` is `excluded`. `dispatched_by_orchestrator`: apply-mode `applied` dispatch lines in the orchestrator log |
| `grok_swaps` | `grok` labels added in D; `by_orchestrator` from the log |
| `ci` | completed `pull_request`/`push`/`merge_group` workflow runs created in D: success, failure (failure, timed_out, startup_failure) and `failure_rate`, per repo and workflow. Cancelled, skipped and neutral runs are not verdicts. `repo-guard` is in `ci.separate`, because verify-gate is red by design until approval |
| `codex` | `merged_prs_with_codex` (body mentions a Codex review), `rounds_declared` (`Codex review, N rounds`), `verify_reports` and `blocking_sum` (`Codex review: <path> blocking=N` lines in comments created in D) |
| `orchestrator` | ticks, failed ticks, `apply_failed`, `rate_low`, failure alerts and gh read/304/write totals from the orchestrator log. `null` when the log does not reach back to the start of D (`source_gaps`) |
| `stalls` | open tasks (labelled `ready`, `deepseek`, `grok` or `in-review`, not `blocked`, not parked) and open non-draft PRs on main without `verify-approved` whose `updated_at` is at least `STALL_HOURS` before `snapshot_at`: `[{target, idle_hours}]`. Snapshot only, like `queue_depth` |
| `parked` | vws-deploy#463. Open issues (not PRs) that wait by design, so they are not stalls: an opt-out label (`do-not-merge`, `hold`, `blocked`) or `needs-human`; or `grok` with no `deepseek`, no assignee and not on `ORCHESTRATE_GROK_ALLOWLIST` (Rule G never assigns them; an assignee holds a Grok slot, so an assigned issue can still stall). Snapshot only, like `stalls`; `null` for a back-filled day. The snapshot keeps each issue's assignee count, never the logins |
| `tokens` | `{total, by_agent}` from the daily usage input (see [Daily usage input](#daily-usage-input)); `null` and a `source_gaps` entry when there is none, never 0 |
| `wakes` | vws-deploy#400. `verify_wake_comments`: comments created in D whose first line is `<!-- vws-orchestrate:verify-wake -->` (the verify-wake thread, `ORCHESTRATE_WAKE_THREAD`). `dispatches.deepseek`: the same number as `deepseek.dispatched` (comments starting with `@deepseek-dev`, reworks included). `dispatches.grok`: comments created in D that carry an orchestrator marker in `GROK_DISPATCH_MARKERS` (the Rule C swap notice `<!-- orchestrate:swap -->` and the grok wake last line `<!-- orchestrate:grok-wake item=... -->`, vws-deploy#378). `rework_dispatches`: comments created in D with `<!-- orchestrate:rework head=`. `lead_wakes`: from the daily usage input; `null` and a `source_gaps` entry when absent |
| `alerts` | the anomalies found for D (empty on a normal day) |
| `queue_depth` | open items per label (`ready`, `deepseek`, `grok`, `blocked`, `in-review`, `priority/blocker`) and `verify_backlog` (open non-draft PRs on main without `verify-approved`). A live snapshot taken when the day is computed (`snapshot_at`), so only for yesterday. A back-filled day has `null` |

`content_sha256` covers everything except `generated_at`, `release`, `snapshot_at`, `queue_depth`,
`stalls`, `parked` and `alerts`, and ignores the "live snapshot" entry of `source_gaps`: these depend on when
the day is computed or on earlier days, not on its data.
The same GitHub data and log always give the same hash.

## When it runs

- **Trigger:** the orchestrator loop (`.github/orchestrate/box/orchestrate-loop.sh`) runs
  `metrics.py --if-due` after every tick when the env file has `METRICS_ENABLED=1`.
- **Isolation:** it runs in its own process group with its own lock (`metrics.lock`) and log
  (`log/metrics.jsonl` for events, `log/metrics.log` for output). Its exit status never counts as a
  failed tick: no backoff, no failure comment.
- **Not due:** the call returns without any gh call or log line.
- **Due day:** day D is due from `METRICS_TIME` (default `00:20`) KST on D+1.
- **Backfill:** the last `METRICS_BACKFILL_DAYS` (7) days that are not yet done in the current mode,
  oldest first, one day per tick.
- **Idempotent:**
  - A published day whose content hash matches makes 0 writes (`metrics_unchanged`).
  - A different hash makes 0 writes and logs `metrics_drift`. Only `--recompute D` replaces it,
    with a new commit.

## Alerts

The thresholds are constants at the top of `metrics.py`, each tested at and just below its value
(`tests/metrics/test_alerts.py`, which also checks that the recorded fixture day raises nothing).

| kind | raised when | constants |
|---|---|---|
| `stall` | at least one entry in `stalls` (only yesterday has a snapshot) | `STALL_HOURS = 48`, `STALL_LABELS`, `PARK_LABELS` |
| `ci_failure_rate` | `ci` failure / (success + failure) | rate rule below |
| `deepseek_failure_rate` | (failed + timeout) / (done + failed + timeout) | rate rule below |
| `grok_escalation_rate` | `grok` labels added / DeepSeek dispatches | rate rule below |
| `token_spike` | the day's tokens are at least `TOKEN_SPIKE_RATIO = 3.0` times the median of the baseline days with token data, and at least `TOKEN_MIN_DAILY = 1000000` | `BASELINE_MIN_DAYS` |

Rate rule: the baseline is the pooled rate over the `BASELINE_DAYS = 7` days before D (never later
days). It needs at least `BASELINE_MIN_DAYS = 3` such days and at least `RATE_MIN_EVENTS = 5` events
in D's denominator. It alerts when D's rate is at least baseline + `RATE_MIN_RISE = 0.20` and, if the
baseline is above 0, at least `RATE_MIN_RATIO = 2.0` times it. Rates are compared as exact fractions,
so a rate exactly at a threshold alerts.

**Token-usage input.** See [Daily usage input](#daily-usage-input). Without it, `tokens` is `null`
and no token alert is raised.

### Daily usage input

GitHub has no token counts and does not see the lead's wakes, so both come from one optional file
per KST day: `$METRICS_TOKEN_DIR/YYYY-MM-DD.json` (default
`metrics-input/tokens/YYYY-MM-DD.json` under the state directory, `ORCHESTRATE_STATE_DIR`). Whoever runs an agent
can write it before `METRICS_TIME` on D+1 (a later write needs `--recompute D`).

```json
{"by_agent": {"<agent>": <tokens>}, "lead_wakes": <wakes>}
```

- `by_agent`: agent names match `[A-Za-z0-9._-]{1,40}` (lower-cased, summed per name); values are
  integers >= 0. Any bad entry makes `tokens` `null`. Without the key, `tokens` is `null`, never 0.
- `lead_wakes`: an integer >= 0, the number of times the lead (dr eggbot) was woken in D. Missing or
  invalid: `null`, never 0.
- Each key is read on its own: a file with only `lead_wakes` leaves `tokens` `null`.

`SUMMARY.md` adds, for the last 7 and 28 days, the verify wakes, DeepSeek and grok dispatches,
reworks and lead wakes, and tokens per merged PR (the sum of `tokens.total` over the days with token
input, divided by the PRs merged on those days). A total over no day with input is `-`, never 0;
this holds for every 7/28-day total, including a window with no daily file. When only some days in
the window have a `wakes` block (days written before vws-deploy#400 have none), the verify-wake,
grok-dispatch and rework totals are followed by `(N days with data)`.

**Delivery.** In apply mode, after the day is on the branch, the script posts one comment on the
status issue (`ORCHESTRATE_STATUS_ISSUE`, default vws-deploy#415; vws-deploy#416) whose first line is:

```
<!-- vws-orchestrate:metrics-alert -->
```

It lists each anomaly, the daily file and commit, the thresholds, and an idempotency key (hash of
the day and the alert kinds). Before any branch write the alert is journaled in `metrics-state.json`
(`pending_alerts`). A failed ref update or POST fails the run (exit 4) and the day stays due; the
next tick posts the same journaled alert, even if a fresh computation no longer sees the anomaly. Posted keys are kept
in `alerted` and searched in the thread before posting, so a re-run, a timed-out POST that landed, or
`--recompute` never posts the same alert twice. The gh wrapper refuses any other
comment: another issue or repo, or a body that does not start with the marker. Comments on the status
issue are metrics alerts only; the orchestrator edits its body and posts no comments there.

**Alert webhook (vws-deploy#416).** Right after the alert comment is posted, the script POSTs

```json
{"kind": "metrics-alert", "summary": "1 anomalies for the KST day 2026-10-20: stall", "comment_url": "https://github.com/..."}
```

to `VWS_ALERT_WEBHOOK_URL` with the header `Authorization: Bearer <VWS_ALERT_WEBHOOK_KEY>` (a Cursor
automation webhook key; a value that already starts with `Bearer ` is sent as is). Rules:
- both values come from the **process environment** of the loop only, never from the env file, and
  are never logged or printed; if either is unset the POST is skipped (`metrics_webhook_skipped`) and
  the run still succeeds;
- `https://` only (plain `http://` only to `127.0.0.1`/`localhost`, for tests); redirects are not
  followed, so the key is never sent to another URL;
- 10 s timeout per try, at most 3 tries (1 s, then 2 s backoff) on a network error, 429 or 5xx; no
  retry on another 4xx;
- a URL that urllib cannot parse (for example a bad bracketed host) is logged as
  `metrics_webhook_failed` with the error class only and `attempts` 0, and is not retried;
- a failed POST is logged as `metrics_webhook_failed` with the HTTP status or the error class only,
  and never fails the run; it is not retried on a later run (the comment on the status issue is the
  durable record);
- no alert, no POST; dry-run never POSTs.

In dry-run the alert is only written to `metrics/dry-run/alerts/YYYY-MM-DD.md` and logged as
`metrics_alert_planned`.

## Reads, cache and limits

- **ETag cache:** `If-None-Match` on every read, cached in `metrics-etag-cache.json`. This is not the
  orchestrator's `etag-cache.json`, which keeps only the entries of its own last tick.
- **Rate floor:** the same `ORCHESTRATE_RATE_FLOOR` (1000), checked before the first read and every
  100 reads. Below it the run stops with exit 0 (`metrics_rate_low`) and is due again next tick.
- **Read budget:** `METRICS_MAX_GETS` (1500) reads per run; over it the run fails (exit 4) without writing.
- **Allowlist:**
  - reads of pulls, issues, comments, issue events, PR commits, check runs and Actions runs in
    `vws-*` repos, plus the `factory-metrics` ref, trees, blobs and commits and the status issue's
    comments in vws-deploy;
  - writes only `POST git/trees`, `POST git/commits`, `PATCH git/refs/heads/factory-metrics`
    with `force: false`, and the alert comment on the status issue;
  - `POST git/refs` only under `--init` and only for `refs/heads/factory-metrics`.

## Commands and config

```bash
M=/home/box/.local/opt/vws-orchestrate/current/.github/metrics/metrics.py
python3 "$M" --date 2026-10-20 --print      # compute one day (dry-run: local file only)
python3 "$M" --recompute 2026-10-20         # apply mode: replace a drifted day
python3 "$M" --init                         # apply mode, once: create the orphan branch
python3 "$M" --date 2026-10-20 --dump-raw raw.json && python3 "$M" --date 2026-10-20 --fixture raw.json
```

Env file keys (`/home/box/.config/vws-orchestrate/env`):
- `METRICS_ENABLED=0|1`: read by the loop;
- `METRICS_MODE=dry-run|apply` (default dry-run);
- `METRICS_TIME=00:20`;
- `METRICS_BACKFILL_DAYS=7`;
- `METRICS_MAX_GETS=1500`;
- `METRICS_TOKEN_DIR` (optional token-usage input).

`ORCHESTRATE_RATE_FLOOR`, `ORCHESTRATE_EXTRA_REPOS`, `ORCHESTRATE_STATUS_ISSUE`, `DEEPSEEK_TIMEOUT_HOURS`
and the `STOP` file are shared with the orchestrator.

Process environment only (export it where `orchestrate-ensure.sh` starts the loop, then restart the
loop; not the env file): `VWS_ALERT_WEBHOOK_URL`, `VWS_ALERT_WEBHOOK_KEY`.

| path under `/home/box/.local/state/vws-orchestrate/` | content |
|---|---|
| `metrics-state.json` | days done per mode, last checked branch head, pending alerts, posted alert keys |
| `metrics/dry-run/alerts/YYYY-MM-DD.md` | the alert a dry-run would have posted |
| `metrics/<mode>/daily/YYYY-MM-DD.json` | local copy of every computed day |
| `metrics-etag-cache.json`, `metrics.lock` | cache and lock |
| `log/metrics.jsonl`, `log/metrics.log` | events and loop output |

Exit status: 0 done, skipped or not due; 2 usage or config; 4 failed (GitHub, budget, no branch, alert POST);
5 refused (foreign commit, non-ASCII).

## Rollout

1. After this PR merges, reinstall the release (the `git archive` line in
   [factory-orchestrator.md](factory-orchestrator.md) includes `.github/metrics`).
2. Turn it on in dry-run (`METRICS_ENABLED=1`). Check `metrics/dry-run/daily/` and `log/metrics.jsonl`.
3. Once only, with the approval recorded on #345: set `METRICS_MODE=apply`, then run
   `python3 "$M" --init`.
4. Leave it on apply. The next due day is published by the loop.
5. Alerts arrive as comments on the status issue (vws-deploy#415) and, with `VWS_ALERT_WEBHOOK_URL` and
   `VWS_ALERT_WEBHOOK_KEY` in the loop's environment, at the alert webhook.

## Roll back

| goal | action |
|---|---|
| pause metrics only | `METRICS_ENABLED=0` in the env file (next iteration) |
| read-only | `METRICS_MODE=dry-run` (no branch write, no alert comment) |
| remove the data | deleting the `factory-metrics` branch is a manual, approved GitHub step; `main` is never involved |
