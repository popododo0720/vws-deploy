# Factory orchestrator (vws-deploy#333)

A deterministic script that runs five routine factory steps on GitHub state. It never calls an LLM.

## Roles

- A deterministic script (this orchestrator, the handoff submit) may push a branch, open a pull
  request and merge one.
- A script merge happens only when all of these hold: every CI check on the head is green,
  `verify-gate` passes, the `vws-verify` App check run passes, the pull request has no conflicts,
  the merge is a squash with `--match-head-commit` set to the verified head, and the pull request
  is not labeled `blocked`. Rule D lists each check.
- The lead (dr eggbot) is woken only for a decision or a failure (issue scope, a real conflict, a
  failed check), not for a routine push, pull request or merge.
- The author bot is never the verifier, nothing pushes to main directly, and every pull request
  body has `Part of #N` with no closing keyword.

| rule | what it does |
|---|---|
| A, verify wake (vws-deploy#397) | Counts only open, non-draft PRs on `main` that carry `needs-verify`, lack `verify-approved` and whose CI is not failing. The `needs-verify` label is kept by `.github/workflows/verify-queue.yml` (see "The needs-verify queue" below). When the oldest of them has waited at least 30 min since its latest `needs-verify` label (`VERIFY_WAKE_MAX_WAIT_MIN`) or at least 3 wait (`VERIFY_WAKE_THRESHOLD`), it posts **one** comment on the verify-wake thread (`ORCHESTRATE_WAKE_THREAD`, a vws-deploy issue or PR number; unset means no wake and the note `wake_off`). It never posts on the status issue. PR comments go through the issues comments API. The comment's first line is `<!-- vws-orchestrate:verify-wake -->`, and it lists every waiting PR as `- repo#N@head_sha url (CI pre-check: green, waited Nm)`. Each pending set (sorted `repo#number@head_sha`, sha256) is posted only once; there is no cooldown, so a changed set (a new PR, or a new head after a push and a relabel) wakes again on the next tick. |
| B, DeepSeek queue | While fewer than `ORCHESTRATE_DEEPSEEK_CAP` (2) `@deepseek-dev` dispatches are in flight across all repos, it comments `@deepseek-dev please take this issue ...` on the next open issues or PRs, one per free slot and at most one per repo, that have `ready` and `deepseek`, have no `grok` or `blocked` label, are not drafts, and have every `Depends on` met. Order: `priority/blocker` first, then the oldest `ready` label, then repo and number. |
| B2, verify rework (vws-deploy#373) | A DeepSeek PR (an open PR labeled `ready`+`deepseek`, the open PR a `ready`+`deepseek` issue's Done report named, or (vws-deploy#457) an open PR whose `Part of` names that issue and whose owner resolves to `deepseek`) whose newest vws-verify verdict on its current head is a FAIL (a `CHANGES_REQUESTED` review by `vws-verify[bot]`, or a `# repo#N @ <head>` / `Result: FAIL` comment) gets `@deepseek-dev please rework this PR ...` on the PR, with the verdict quoted, through the same queue, cap and `ORCHESTRATE_DISPATCH` switch, ahead of new tasks. A `<!-- orchestrate:rework head=<sha> ... -->` marker, or any dispatch after the verdict, means that head is done: one rework per head. Conflict-only verdicts are skipped for an eggbot rebuild (note `rework_skipped`). Each rework, and a pending FAIL, counts as one failure toward the grok swap below. |
| B2 owner and unrouted FAILs (vws-deploy#457) | A PR's owner is, in order: the PR body `Agent:` line, the PR's `grok` or `deepseek` label, then the `grok` or `deepseek` label of the item its `Part of #N` (or `Part of vws-x#N`) names (`grok` wins when both are set, as in rule B2). An open non-draft PR into `main` whose newest vws-verify verdict on its current head is a FAIL that neither rule B2 nor rule B4 picks up gets one `verify_fail_unrouted` note per head (target, head, verdict URL, owner, reason `no_owner`, `owner_grok`, `owner_agent` or `owner_deepseek_not_ready`). The heads already noted are kept in state (`verify_fail_unrouted`), so the next tick does not repeat it and a new head is noted again. A conflict-only FAIL is left to rule R and the lead. `collect` reads vws-verify reviews (ETag-cached) for every open non-draft PR into `main`; a read error leaves that PR's verdict comments only. |
| B3, CI-failure rework (vws-deploy#412) | The same DeepSeek PR gets a rework when line 1 of a PR comment by `github-actions[bot]` is the ci-fail marker `<!-- vws-ci-fail sha=<40 hex> v=1 repo=<vws-x> pr=<N> owner=<owner> agent=<agent> n=<k> wake=<true\|false> -->` (#420 `MARKER_RE`, fixed key order, no `state=` key), and only when the comment passes #420's `parse_comment` (ported as `ci_fail_record`: CRLF normalized, the last fenced `json` block is the record, the record's marker line must equal line 1, and its field types must hold), for this repo#N and the current head, with `owner=deepseek`. A marker by any other login is ignored (note `ci_fail_untrusted`); the orchestrator records each comment's author for this. It recomputes n itself: the distinct failing shas on the PR since the last CI reset (an `/orchestrate reset-failures` by an OWNER, MEMBER or COLLABORATOR, `authorized_reset`, or `needs-human` removed; the swap count's reset still takes any author until #419), raised to the marker's `n` when the marker is newer than the reset. Skipped (note `ci_fail_skipped`): `wake=false`, `needs-human` or `blocked` on the PR or its issue, or n above 3. The rework quotes the failed jobs and ends `<!-- orchestrate:rework head=<sha> ci=<comment id> -->` (`verdict=<id> ci=<id>` when the head also has a verify FAIL). The rework is shared with B2: one rework and one swap count per head. `owner=grok` is only logged (note `ci_fail_grok_pending`) until #398(a). |
| B4, grok rework wake (vws-deploy#398) | An open non-draft PR into `main` whose newest vws-verify verdict on its current head is a FAIL that is not conflict-only, that rule B2 does not rework, that is not labeled `blocked`, `hold` or `do-not-merge`, and whose owner (as in the row above) resolves to anyone but `deepseek` (`grok`, or the bot named on the `Agent:` line) gets **one** comment on the verify-wake thread (`ORCHESTRATE_WAKE_THREAD`) per head. Its first line is `<!-- vws-orchestrate:grok-wake -->`, so the Grok dev bots' routines match it. It names the PR, the head, the owner and the verdict URL, quotes the verdict, and ends with `<!-- orchestrate:rework head=<sha> verdict=<id> item=<repo#N> -->` (metrics counts it as a rework). The idempotency key is sha256 of the PR and its head, so repeated ticks post nothing new and a new head gets a new wake. `apply` re-checks the key on the thread and the PR's state and head before posting (the rule R checks), skips it under `ORCHESTRATE_SCOPE`, and logs `grok_rework_posted`. No wake thread: no wake, the note `grok_rework_off`, and the FAIL stays `verify_fail_unrouted`. `ORCHESTRATE_DISPATCH=off` pauses it (note `grok_rework_paused`). It has no Grok slot cap and does not read `ORCHESTRATE_GROK_ALLOWLIST`: the PR is already in flight. |
| C, grok fallback | Each dispatch fails once on any of: a `[deepseek-dev] Failed for <dispatch url>` report, no report within 3h, or a Done PR closed without merge. At 2 counted failures it removes `deepseek`, adds `grok` and posts one notice. A Failed report that shows a provider error is an infra failure instead: never counted, re-mentioned after 5 minutes, at most 3 times. See "Infra retries" below. |
| G, Grok wake (vws-deploy#378) | **Only issues listed in `ORCHESTRATE_GROK_ALLOWLIST` are ever assigned**; the default is empty, so nothing is assigned until someone lists an issue (all other work stays parked). While fewer than `ORCHESTRATE_GROK_CAP` (1) Grok tasks are in flight, it posts one comment on the verify-wake thread (`ORCHESTRATE_WAKE_THREAD`; unset means no Grok wake and the note `grok_wake_off`; never the status issue) whose first line is `<!-- vws-orchestrate:grok-wake -->`, naming the next allowlisted open issue or non-draft PR that has `ready` and `grok`, has no `deepseek` label and no opt-out label (`blocked`, `hold`, `do-not-merge`), has no assignee and has every `Depends on` met; at most one per repo. The owning Grok dev bot's GitHub-trigger routine on that thread matches that marker. Order: `priority/blocker`, then security (a `security` label or the word in the title), then the oldest `ready` label. The idempotency key is sha256 of the issue and its last failure reset, so an issue is woken once; while it stays open with `grok` it holds the slot (note `grok_in_flight`). Grok work started without a wake holds a slot too: an open `grok` issue or PR with an assignee, and the issue an open `grok` PR (draft too) names with `Part of #N` or `Part of repo#N`; a PR and its issue count as one task. A repo is busy while it holds a task or an open `grok` PR that names an issue in any repo. **A human assignee holds the slot too:** any assignee on an open `grok` issue, a person included, holds a Grok slot until they are unassigned (or `grok` is removed, or the issue closes); with the default cap of 1 that pauses all Grok wakes, and the note `grok_in_flight` lists the issue. The repos have no in-progress label, so assignment and the linked PR are the in-flight signals; and `/orchestrate reset-failures` on it re-arms one new wake. After a rule C swap the issue is picked up on a later tick if it is allowlisted. `ORCHESTRATE_DISPATCH=off` pauses these wakes too. Bad allowlist entries are ignored and logged (note `grok_allowlist_invalid`). Posted wakes are logged as `grok_wake_posted`, and the comment's last line `<!-- orchestrate:grok-wake item=<repo#N> -->` lets metrics count it. |
| D, serialized merge (vws-deploy#346) | Squash-merges a PR that passed vws-verify, **at most one per repo per tick**, and only while the push CI on `main` is green for the current main head. After a merge the repo is locked until the push CI of the new squash commit is green. Red stops merges in that repo and adds one entry to the status issue body. See "Rule D" below. |
| R, rebuild wake (vws-deploy#460) | An open non-draft PR into `main` that is labeled `needs-rebuild`, or whose `mergeable_state` is `dirty` (it conflicts with main), gets **one** comment on the verify-wake thread (`ORCHESTRATE_WAKE_THREAD`; unset means no wake and the note `rebuild_wake_off`) whose first line is `<!-- vws-orchestrate:rebuild-wake -->`. It names the PR, its head SHA, why it waits, and the owner from the PR body's `Agent:` line (no `Agent:` line: the lead). The owner rebuilds it on current main with `vws-rebuild-pr` as a new `-vN` branch and PR. The idempotency key is sha256 of the PR and its head SHA, so repeated ticks post nothing new and only a new head gets a new wake. `collect` reads `mergeable_state` with one ETag-cached `GET pulls/N` per open non-draft PR into main; GitHub computes it lazily, so `unknown` (or a read error) is not a conflict and the next tick decides. `apply` re-checks the key on the thread and the PR's state and head before posting, and logs `rebuild_wake_posted`. `ORCHESTRATE_DISPATCH=off` does not pause it: it only notifies. The status issue body lists every PR waiting for a rebuild. |
| L, lead wake (vws-deploy#397) | Posts **one** comment on the wake thread (`ORCHESTRATE_WAKE_THREAD`; unset means no lead wake and the note `lead_wake_off`; never the status issue) per PR head that no rule moves on its own: an open, non-draft PR on `main` that is `verify-approved` but conflicts with main (`mergeable` false), or whose newest vws-verify verdict on its current head is a FAIL with no B2 rework and no B4 Grok rework wake this tick: a conflict-only verdict, a Grok PR (`grok` on the PR or on the issue its `Part of` names) that B4 does not wake, or a PR no `ready`+`deepseek` item owns. The first line is `<!-- vws-orchestrate:lead-wake -->`; the comment names `repo#N`, the head sha and the reason. The idempotency key is sha256 of `repo#N@head`, so a head is woken once and a new push can wake again; apply re-reads the thread and skips a key that is already there. dr eggbot's GitHub-trigger routine on that thread matches the marker and rebuilds the PR onto main as a new -vN PR (no force-push) or routes the FAIL to its owner. Logged as `lead_wake_posted`. |
| T, AC tick (vws-deploy#486) | A PR merged into `main` after the rule started, with a `Part of` issue and `AC: <n>` lines, gets box n of that issue ticked with `(evidence: <PR URL> merged <sha>)`. One comment carries the marker. The issue gets `ac-partial` while boxes stay open and is closed as completed when none are left. `ORCHESTRATE_AC_TICK` (`dry-run` by default) switches it. See "The AC tick rule" below. |
| V, verify labels (vws-deploy#487) | `needs-verify` and `verify-approved` follow each open PR's current head in every vws-* repo, with or without `verify-queue.yml`. See "The verify labels rule" below. Runs after every other rule, so it takes the writes left under `ORCHESTRATE_MAX_WRITES`; `ORCHESTRATE_VERIFY_LABELS=off` stops it. |

Code: `.github/orchestrate/orchestrate.py` (Python 3 stdlib), `.github/orchestrate/exclusions.tsv`,
`.github/orchestrate/box/orchestrate-loop.sh`, `.github/orchestrate/box/orchestrate-ensure.sh`.
Tests: `tests/orchestrate/` (run by `.github/workflows/drift-check.yml`).

## How it decides

- **Pipeline:** collect (gh reads into one snapshot), then `decide()` (pure, table-tested), then apply
  (writes). Writes happen only with `ORCHESTRATE_MODE=apply`.
- **Repos:** the entries of `.github/drift/repos.tsv` plus `ORCHESTRATE_EXTRA_REPOS` (default
  `vws-conformance`). Archived repos (today `vws-hypervisor`) are skipped.
- **`Part of` references:** a PR body line that starts with `Part of #N` or `Part of vws-x#N`
  (leading whitespace is allowed). The owner/repo form `Part of popododo0720/vws-x#N` is not
  accepted, and Rule D reports `part_of_owner_repo` instead of the generic `no_part_of`
  (vws-deploy#477).
- **Dependencies:** a body line `Depends on #N` or `Depends on vws-<repo>#N`, comma separated.
  - A dependency is met when the issue closed as completed or the PR merged.
  - An open, unreadable, outside, archived or cyclic reference blocks the task. The orchestrator adds
    `blocked` and logs a `note`.
  - It removes `blocked` only if it added the label itself.
  - Dependent tasks never run at the same time. A task in flight, in review or dispatched in the same
    tick is an open item, so every task that depends on it is blocked until it closes as completed or
    merges (for example vws-console-api#227 waits for vws-proto#51).
- **DeepSeek slots:** a dispatch is in flight until its Done or Failed report lands or it times out
  (3h). A tick dispatches at most `ORCHESTRATE_DEEPSEEK_CAP` minus the in-flight count, and the write
  cap still applies. Slots count running dispatches, not items. A running target is never dispatched
  again, even after `/orchestrate reset-failures`. A task in review (Done report, PR open) does not
  hold a slot.
- **One task per repo:** dsh runs one task per repo at a time and queues the rest, and a queued task
  could reach the 3h timeout and count as a DeepSeek failure. So a repo with a DeepSeek task in flight
  or picked in the same tick is skipped (a `note` with kind `repo_busy`), and the free slot goes to the
  next eligible task from another repo. The skipped task goes out on a later tick, once its repo is free.
- **History:** candidates (`ready`, `deepseek` or `grok`) get their full comment history, so old
  dispatches and failures are never forgotten. Other items use the repo-wide comment list for the
  last 14 days.
- **Completion signal:** only the poller's `[deepseek-dev] Done for <url>` and
  `[deepseek-dev] Failed for <url>` comments. A Done report with an open PR adds `in-review`.
- **Trusted comments (vws-deploy#419):** the repos are public, so anyone can post a comment. The
  orchestrator acts on a comment only when its author is trusted:
  - `/orchestrate reset-failures` and `@deepseek-dev` dispatch comments: the owner (`popododo0720`) or
    an `OWNER`, `MEMBER` or `COLLABORATOR` author association (`is_trusted_reset_author`, the same
    rule as `.github/ci-fail`, #412);
  - `[deepseek-dev] Done/Failed` results: only a login in `ORCHESTRATE_DEEPSEEK_LOGINS` (the poller's
    account, the VM PAT user), whatever its association;
  - verify verdict comments (`# repo#N @ <sha>` + `Result:`) and `<!-- orchestrate:... -->` marker
    comments (vws-deploy#433, #434): only `ORCHESTRATE_VERIFY_LOGIN` (default `vws-verify[bot]`,
    compared case-insensitively) or an `OWNER` association. When `ORCHESTRATE_VERIFY_USER_ID` is set,
    the verify login counts only with that numeric user id, so a renamed and re-registered login does
    not match. `vws-verify[bot]` reviews use the same login and id check;
  - a comment without an author login or association is ignored (fail closed).

  Other comments are dropped before the decision and logged as a `comment_ignored` event with
  `target`, `comment` (URL), `kind`, `author`, `association` and `reason`
  (`reset_author_not_trusted`, `dispatch_author_not_trusted`, `report_author_not_deepseek_poller`,
  `verdict_author_not_trusted`, `marker_author_not_trusted` or `author_missing`). The comment body is
  never logged. So a forged FAIL posts no rework prompt and counts no failure toward the grok swap,
  and a forged PASS cannot hide a trusted FAIL. A new dispatch found by the pre-dispatch re-read
  blocks only when its author is trusted.
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
  - a comment starting `/orchestrate reset-failures` from a trusted author (see Trusted comments);
  - a report or dispatch URL listed in `exclusions.tsv` (vws-iam#123's token failure is the first entry).
- **Labels:** labels are never created. An action whose label is missing on the repo is skipped and
  logged.

## The needs-verify queue (vws-deploy#397)

`.github/workflows/verify-queue.yml` ships unchanged to every vws-* repo with `repo-guard.yml`. It keeps
the `needs-verify` label on a PR exactly while the PR waits for vws-verify:

- It runs when a PR workflow completes on a PR head (`workflow_run`, from the default branch) and on
  `pull_request` `ready_for_review` and `synchronize`. The `workflow_run` list must name every workflow
  that runs on `pull_request`, because any of them can finish last: vws-deploy's `ci`, `drift-check`,
  `installer-image`, `post-merge` (closed PRs; it only clears a stale label) and `repo-guard`, plus
  `CI` (vws-console), `english` (most repos), `buf` (vws-proto), `conformance` (vws-conformance) and
  `tla` (vws-console-api). `tests/workflows/test_verify_queue.py` fails when a `pull_request` workflow
  in this repo, or one of those names, is missing; a repo that adds a PR workflow under a new name must
  add it to the list here and copy the file again. The drift check (`.github/drift/drift-check.sh`)
  reports a repo without `verify-queue.yml` as drift (vws-deploy#460). `check_suite` is not used because GitHub starts no workflow for check suites
  created by GitHub Actions.
- Once `gate / verify-gate` (vws-deploy#413) is on `main`, it joins the skip list next to `verify-gate`
  and `vws-verify`; whichever of #413 and #397 merges second adds it.
- It adds `needs-verify` when the PR is open, not a draft, on base `main`, has no `verify-approved`, and
  the newest run of every check on its head (and every commit status) is green, except `verify-gate`
  and `vws-verify`, which wait for the verdict by design. Pending checks, or no checks yet, leave the
  PR unlabeled; the run for the last workflow to finish adds the label.
- It skips a PR whose current head already has a vws-verify FAIL (vws-deploy#440), and removes
  `needs-verify` if it is on: the newest verdict on that exact head is a `vws-verify[bot]`
  `CHANGES_REQUESTED` review, or a `# <repo>#N @ <sha>` / `Result: FAIL` comment by `vws-verify[bot]` or
  a repo member, or the newest `vws-verify` check run on the head failed. A later PASS on the same head
  supersedes it. A new push (a new head) re-enters the queue. `pull_request_review` `submitted` also
  runs the queue, so a FAIL review takes the label off at once and Rule A stops waking verify for it.
  A `pull_request_review` run uses the workflow file of the PR's merge ref, so a PR branched before
  that trigger existed keeps a stale label after a FAIL review. Rule A therefore also skips a labeled PR
  whose newest vws-verify verdict on its head is FAIL (note `needs_verify_failed_head`); seen on
  vws-deploy#430 and vws-console-api#362.
- It removes the label on a red check, on a new push (`synchronize`), and when the PR stops waiting
  (draft, other base, closed or approved). A run for an old head writes nothing.
- Permissions: none at workflow level; the `needs-verify` job has `issues: write` and
  `pull-requests: write` (adding or removing a label on a pull request goes through the issues API but
  GitHub also requires `pull-requests: write`; without it the call fails with 403 "Resource not accessible
  by integration"), plus `checks: read` and `statuses: read`. It writes nothing but the `needs-verify`
  label, checks out nothing and runs no PR code. Removing a label that is already gone (HTTP 404)
  counts as done; any other error fails the job.

The vws-pr-pipeline "Open" step 4 (the dev agent waking vws-verify) and vws-verify-verdict step 8 are
replaced by this label and Rule A.

## The verify labels rule (vws-deploy#487)

The orchestrator owns `needs-verify` and `verify-approved` (rule V, `decide_verify_labels`). `collect`
reads, for every open non-draft PR into `main`, the head's check runs and commit status and the
vws-verify reviews (all ETag-cached). It then sets both labels from this table:

| The PR's current head | `needs-verify` | `verify-approved` |
|---|---|---|
| Every check green except `verify-gate`, `gate / verify-gate`, `vws-verify` (and verify-queue's `needs-verify` job), and no vws-verify verdict on the head | add | remove |
| The newest verdict on the head is PASS (an `APPROVED` review by the verify login on the head sha) and the newest `vws-verify` check run on the head did not fail | remove | keep (never added, see below) |
| The newest verdict on the head is FAIL (a `CHANGES_REQUESTED` review, a `# repo#N @ <sha>` / `Result: FAIL` comment), or the newest `vws-verify` check run on the head failed (even after a PASS review) | remove | remove |
| A check red or pending, or no checks yet (or more than one page); or a draft, another base, or closed | remove | remove |

- The rule never adds `verify-approved`; it only removes it. `verify-gate` (`scripts/guard-agents.sh
  verify-gate`) rejects a `verify-approved` label added by the PR author or pusher, and the orchestrator
  writes as the repo owner, who authors most PRs (vws-verify FAIL on vws-deploy#488 head 7ded926).
  vws-verify adds the label with its own identity when it posts a PASS.
- A PASS on an older head does not count: after a push both labels come off until the new head is green
  (then `needs-verify`) or has a verdict.
- A PR labeled `blocked`, `hold` or `do-not-merge` gets removals only: `needs-verify` comes off and no
  label is added (note `verify_label_parked` when the table wanted one). `verify-queue.yml` skips such a
  PR the same way while it still runs (PR #483 got `needs-verify` back after the lead removed it).
  Rule A also skips a parked PR that still carries `needs-verify` (note `needs_verify_parked`), so it
  never wakes vws-verify for it.
- A label someone added by hand that contradicts the table is removed on the next tick. A PR already
  matching writes nothing, so ticks are idempotent.
- Longest-waiting PRs (oldest `created_at`) come first. Each label write counts toward
  `ORCHESTRATE_MAX_WRITES`; what the cap stops is written on a later tick. `ORCHESTRATE_SCOPE` limits it
  to one PR like every other write. A repo without the label skips the write (logged).
- A read error on a read Rule A already made before #487 (checks of a PR without `verify-approved`,
  reviews of a `needs-verify` PR) still fails the tick. Any other read error skips only that PR in this
  rule for the tick; Rule D reads a `verify-approved` PR again and fails closed itself.

Retiring `verify-queue.yml` (deleting it from vws-deploy and vws-console-api, closing the copy PRs, and
dropping it from the drift `WORKFLOWS` list) is a follow-up of vws-deploy#487 once this rule runs live.

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
| not opted out: no `do-not-merge`, `hold` or `blocked` label, no `<!-- vws-orchestrate:no-merge -->` in the body, not the status issue or the verify-wake thread | `opted_out` |
| the newest `vws-verify` check run from the vws-verify App (id 5313004) on the **current head** is success | `verify_check_missing`, `verify_check_<conclusion>` |
| a `verify-gate` check run from GitHub Actions (app id 15368) on the **current head**, and every such run is success, even when `main`'s protection does not require it | `verify_gate_missing`, `verify_gate_pending`, `verify_gate_<conclusion>` |
| every required check of `main`'s rulesets and classic branch protection (bound to its app id) is success on the head, so a skipped or neutral required check never merges; the classic list comes from the `protection` field of `branches/main`, never the protection API (the PAT gets 403 there, #408) | `required_missing:<name>`, `required_pending:<name>`, `required_<conclusion>:<name>`, `required_checks_unreadable` |
| every other check run on the head is success, skipped or neutral, and every commit status is success (AGENTS.md rule 5) | `check_failed:<name>`, `check_pending:<name>`, `status_<state>:<context>`, `combined_status_<state>`, `checks_truncated`, `statuses_truncated` |
| `mergeable` is true and `mergeable_state` (GraphQL `mergeStateStatus`) is `clean`, GitHub's own verdict that counts main's protection and rulesets; `blocked`, `behind`, `dirty`, `unknown`, `unstable`, `has_hooks`, `draft` or no value never merge | `mergeable_unknown` (GitHub is still computing; next tick), `conflicts`, `mergeable_state:<state>` |
| the head did not move during collect, and the PR is still open | `head_moved_during_collect`, `not_open` |
| the body has `Part of #N`, and the title has no closing keyword | `no_part_of`, `closing_keyword_in_title` |
| every `Depends on` is met (same rules as the DeepSeek queue) | `dep_unmet:<ref>` |
| fewer than `ORCHESTRATE_MERGE_MAX_ATTEMPTS` (2) failed merge attempts for this head | `merge_attempts_exhausted` |
| the repo is not locked, and main's push CI is green | `repo_locked:<why>`, `main_ci_pending`, `main_ci_none`, `main_ci_red` |
| serial set (vws-deploy#397): a PR that changes `.github/orchestrate/orchestrate.py`, a `.github/workflows/repo-guard*.yml` or `AGENTS.md` (paths from `pulls/N/files`, read in collect; unread counts as serial) merges only if it is the oldest-approved eligible serial PR across all repos this tick (newest successful `vws-verify` check run on the head); the others wait a tick | `serial_after:<pr>` |
| no other PR of the repo is chosen this tick (the serial pick first, then lowest number) | `one_merge_per_repo:<pr>` |

The `Agent:` line of the PR body is copied into the `merged` log event (`agent`, empty when absent).
It follows the #412 spec: the first unindented, case-sensitive line matching
`^Agent:[ \t]*([a-z0-9][a-z0-9-]{0,38})[ \t]*$`, so `Agent: dr eggbot` or `agent: x` is not an agent.
No rule reads it, so a PR without that line merges the same way.

**Merge call.** `PUT repos/O/R/pulls/N/merge` with `{"merge_method": "squash", "sha": "<verified head>",
"commit_title": "<PR title> (#N)"}`. The squash message stays GitHub's default (the commit messages),
so the `Part of #N` lines are kept. GitHub refuses the merge if the head moved after verification. The
gh wrapper accepts no other PUT and no other payload. The code has no `update-branch` and no admin
merge. Right before the PUT the orchestrator reads again: main must still be at the collected head
with a green push CI, the head checks must still pass every check rule above, and the PR must still be
open, at the same head, not a draft, on base `main`, `mergeable` and `mergeable_state` `clean`, with
`verify-approved` and no opt-out. The pinned
`sha` protects only the head, so the CI verdicts are re-read too.

**Rulesets.** Required checks are read from `rules/branches/main`, so the `vws-main` ruleset (squash
only, required checks such as `english`) is honored. The protection API is never called: it
needs Administration read, which the PAT does not have (403 on vws-storage and vws-console-api, #408).
Classic required checks come from the `protection.required_status_checks` field of the
`branches/main` body instead (`checks` with `app_id`, plus `contexts` with no app), and are joined
with the ruleset checks, deduped by context and app id. GitHub counts a skipped or neutral required
check as passing, so `mergeable_state` alone is not enough: each one must be success. Classic
protection off (`protected` or `protection.enabled` false) adds nothing; a missing or malformed
`protection` field fails closed (`merge_repo_unreadable`). Only `clean` merges. Each requirement is
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

**Status entries.** A red main (after our merge, or a red current main head in a repo with a
candidate) adds **one** entry per repo and commit to "Recent entries" in the status issue body
(`merge: main CI red in <repo> at <sha7> (key ...)`). A lock still waiting after
`ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS` (2) adds one `main CI wait timeout` entry and stays locked. The
key (sha256 of kind, repo and commit) is kept in `state.json` and in the body, so there is no retry
loop. No comment is posted. Entries are skipped while `ORCHESTRATE_SCOPE` is set.

**Switches.** Merges need both `ORCHESTRATE_MODE=apply` and `ORCHESTRATE_MERGE=apply`. The default
`ORCHESTRATE_MERGE=dry-run` plans and logs merges but never calls the merge API, even in apply mode.
`off` skips rule D and its reads. `ORCHESTRATE_SCOPE=vws-x#N` limits merges to that one PR. The STOP
file and the write cap apply as for every write (one merge is one write).

**Reads.** Rule D reads only repos with a `verify-approved` candidate or a lock: `branches/main`, the
push Actions runs for one main commit, `rules/branches/main` (never the branch-protection API), and for each candidate the PR, its check runs and its
combined status. A lock also reads `compare/<old>...<main>`. A read error in one repo means no merge
there this tick (`merge_repo_unreadable`), not a failed tick.

## The AC tick rule (vws-deploy#486)

A PR body names the acceptance criteria boxes it covers with one `AC: <n>` line per box (n counts the
`- [ ]` / `- [x]` lines under the `Part of` issue's `## Acceptance criteria` heading, from 1, up to the
next heading), or `AC: none - <reason>`. `scripts/guard-agents.sh pr-body` requires one of the two
(vws-deploy#492). Rule T acts on them after the merge:

- `collect` reads each watched repo's newest 100 closed PRs (one ETag-cached `GET pulls?state=closed`).
  It keeps a PR that was merged into `main` after `ac_tick_since`, within `ORCHESTRATE_COMMENT_WINDOW_DAYS`,
  and that is not yet in `ac_ticked` (both in `state.json`). For one with a `Part of` issue (the first
  `Part of #N` or `Part of vws-<repo>#N` line, as rule D reads it) and `AC: <n>` lines, it reads that issue
  and its comments. A PR merged in another vws-* repo with `Part of vws-deploy#N` ticks the vws-deploy
  issue. A read error skips that PR for this tick (note `ac_read_error`); the tick does not fail.
- The first tick with the rule on only sets `ac_tick_since` to now (note `ac_tick_start`), so PRs merged
  before it are never ticked.
- `decide` (oldest merge first) plans one `ac_tick` per PR. It plans nothing and notes `ac_none`,
  `ac_unparsed`, `ac_no_part_of`, `ac_issue_closed`, `ac_no_section` (no box under the heading),
  `ac_index_invalid` (an `n` beyond the box count: no box is ticked) or `ac_tick_done` (the issue already
  has the PR's marker). These PRs go into `ac_ticked` and are not read again.
- `apply` (only with `ORCHESTRATE_AC_TICK=apply` and `ORCHESTRATE_MODE=apply`) reads the issue's comments
  and body again. If the marker `<!-- vws-orchestrate:ac-tick pr=<repo>#<pr> -->` is there, it writes
  nothing. Otherwise it computes the edit from the fresh body, so an edit made since `collect` is kept,
  and writes in this order:
  1. the body, with each listed unticked box ticked and the evidence suffix appended;
  2. `ac-partial` added while a box stays open (if the repo has the label; else note `ac_label_missing`);
  3. `ac-pending` removed, and `ac-partial` too when no box is left;
  4. the issue closed as completed when no box is left;
  5. one comment with the PR link, the merge sha, the boxes, the boxes still open (index and text) and
     the marker.
  The marker goes last, so a crash part way is finished by the next tick: a box already ticked is left
  alone. The PR then goes into `ac_ticked`.
- Every write counts toward `ORCHESTRATE_MAX_WRITES`. A tick that cannot fit all of one PR's writes skips
  that PR (`write cap N reached`), and a later tick does it. `ORCHESTRATE_SCOPE` limits the rule to one issue.
- The gh wrapper refuses any other issue body edit: a body `PATCH` must differ from the body just read
  only in unticked boxes under the heading, turned `[x]` with the evidence suffix
  (`ac_tick_only`). The only other issue edit allowed besides the status issue's body is
  `{"state": "closed", "state_reason": "completed"}`, never on the status issue.
- The rule does not judge evidence (vws-verify does that on the PR). It ticks no box for an `AC: none`
  PR or for a box no PR names; the lead ticks those by hand. Retiring `post-merge.yml`'s `ac-pending`
  job and creating `ac-partial` in every repo are separate steps (#486 AC8).

## Infra retries (provider errors)

DeepSeek's provider sometimes fails before the task starts (2026-10-21: a 429 at 07:12Z and a 503 at
08:37Z killed vws-proto#51 jobs on the first turn). Those are not task failures.

- **Classification:** a `[deepseek-dev] Failed for <dispatch url>: ...` report is an infra failure
  when its body matches `INFRA_ERROR_RE` (one multiline constant in `orchestrate.py`):
  - a line `dsh: RATE_LIMIT: <code>:` or `dsh: SERVER: <code>:` (`^dsh: (RATE_LIMIT|SERVER): (\d{3}):`).
    The poller's first line is only `dsh exited with code 1.`; the provider error is the last dsh
    line inside the `Last output` block, for a first-turn death and a mid-job death alike;
  - or the first-line form `Failed for <url>: provider 503 (SERVER)` (not posted yet; accepted for when
    the poller adopts it).

  Real examples (vws-proto#51 comments 6310291529 and 6311725351):
  `dsh: RATE_LIMIT: 429: {"type":"server_error","message":"Upstream request failed: Endpoint is unavailable."}`
  and `dsh: SERVER: 503: {...}`. A plain `dsh exited with code 1` without that line is a task failure
  and counts as before. HTTP statuses or `Upstream request failed` elsewhere in the output are not
  matched, because a task's own test output can print them. Other dsh codes (for example `TIMEOUT` or
  an auth error) are task failures until evidence says otherwise. An `exclusions.tsv` entry still wins
  over the classification.
- **Never counted:** infra failures do not count toward the 2-failure grok swap. Task failures
  around them still do.
- **Retry:** while the newest attempt on an item is an infra failure, the item is dispatched again
  `ORCHESTRATE_INFRA_RETRY_DELAY_MIN` (5) minutes after the failure report, as a normal dispatch
  comment (same text and marker) with a `Retry N/3 after provider error.` line. After a mid-job death
  DeepSeek continues on the same item, including a PR branch the partial run already pushed. Until then each tick
  logs a `note` with kind `infra_wait` and the due time. Ticks run every `ORCHESTRATE_INTERVAL`
  (600 s), so the effective delay is 5 to 10 minutes. A retry is subject to the DeepSeek cap, one task
  per repo, dependencies and the write cap, like any dispatch.
- **Count:** N is the number of infra failures since the last failure reset, read from the comments
  every tick, so a restart or a rerun never repeats or skips a retry.
- **Exhausted:** when the newest attempt is the 4th infra failure since the last reset (the first
  dispatch plus 3 retries), the item is not dispatched again. The orchestrator adds one entry to the status issue body
  (`infra: <item>: infra retries exhausted (key ...)`), with an idempotency key (sha256 of the item and
  the last report URL) kept in `state.json` and in the body, so a rerun never adds it twice. It also adds `blocked` (owned, so it stays until a reset). It never
  swaps to grok. To retry after the provider recovers, comment `/orchestrate reset-failures` on the
  item: the next tick removes `blocked`, and the tick after that dispatches it. Removing `blocked` by
  hand without a reset does not help; the next tick adds it again. The entry is skipped while
  `ORCHESTRATE_SCOPE` is set, like the merge entries.

## Board sync (vws-deploy#347)

A one-way mirror of labels and bodies to the [VWS factory queue](https://github.com/users/popododo0720/projects/2).
Labels stay the source of truth: `decide()` never reads the board, and a field edited by hand on the
board is overwritten on the next sync. Design: vws-deploy#347.

- **When.** After `apply()`, on the first tick, every `ORCHESTRATE_BOARD_EVERY` (6) ticks, and on the
  tick after a planned change of `blocked`, `in-review` or `deepseek`. Cadence state is
  `~/.local/state/vws-orchestrate/board.json`. The STOP file, a GraphQL `rateLimit.remaining` below
  `ORCHESTRATE_RATE_FLOOR` (read in the same query) and `ORCHESTRATE_SCOPE` are respected.
- **What.** Status, Owner, Priority and Depends on, per the mapping in `board_values()`; open items not
  on the board are added; board items whose issue or PR is closed or merged and not Done get Done
  (fallback for the built-in workflows). Rank and Agent are never touched. Fields and options are
  not created; a missing one is a `board_sync_error` (run `board-apply.sh`).
- **Writes.** Only `addProjectV2ItemById`, `updateProjectV2ItemFieldValue` and
  `clearProjectV2ItemFieldValue`, at most `ORCHESTRATE_BOARD_WRITES` (50) per sync, in aliased batches of
  20 with a 3 s pause. Order: Status of existing items, adds with Status, Owner and Priority, Depends on;
  the rest waits for the next sync. `Gh.graphql` refuses any query but the board read and any
  mutation that is not built verbatim from the three templates (values travel only as variables). It
  never writes issues, labels, comments or repos.
- **Modes.** Dry-run by default: planned mutations are logged as `board_plan` and nothing is written.
  Writes need `ORCHESTRATE_MODE=apply` and `ORCHESTRATE_BOARD=apply`. Errors are logged as
  `board_sync_error`, counted in `board.json`, and never fail the tick.
- **Token.** Board GraphQL calls (and only those) run with `GH_TOKEN` set to a classic PAT with only
  the `project` scope, read each sync from `ORCHESTRATE_BOARD_TOKEN_FILE`; every REST call keeps the
  box's gh login. A missing, empty or unreadable file skips the sync for that tick with one
  `board_sync_skipped` (`reason: token_missing`) log line, not an error. The token is never logged.

## Safety limits

- **gh login:** the box's existing `gh` login, with `GH_TOKEN` and `GITHUB_TOKEN` removed from every
  gh call. There are no other secrets.
- **Allowlist:** every call goes through an allowlist:
  - GET: repo, issues, pulls, labels, repo comments, one issue or PR, issue events and comments,
    check runs, combined status, `rate_limit`; for rule D also `branches/main`,
    `rules/branches/main`, push Actions runs for one main commit, and `compare/<sha>...<sha>`;
  - POST: issue comments and issue labels;
  - DELETE: only the labels `blocked`, `in-review`, `deepseek`, for rule V `needs-verify`, `verify-approved`, and for rule T `ac-partial`, `ac-pending`;
  - PATCH: the status issue body; for rule T an issue body that only ticks criteria boxes, and a completed close;
  - PUT: only `pulls/N/merge` with `merge_method` squash and a 40-hex `sha` (rule D).
- **Conditional reads:** reads send `If-None-Match`. A 304 is served from
  `~/.local/state/vws-orchestrate/etag-cache.json` and does not use the primary rate limit. The
  comment `since` value is floored to the UTC day, so its URL and ETag stay stable.
- **History cache:** a candidate's (`ready`/`deepseek`/`grok`) comments and label events are read
  only when its `updated_at` in the issue list moved since the copy in `history-cache.json` (a new
  comment or label change bumps it), so a quiet tick makes no per-item reads. An entry is re-read in
  full after 6 hours (plus up to 1 hour of per-item jitter) to catch edits and deletes, when it was
  updated in the last 2 minutes, and when the file is missing or has another `version`. Items that
  lose their labels or close are dropped. Deleting the file forces a full read on the next tick.
  Version 3 (vws-deploy#419) stores each comment's `author` and `association` and keeps untrusted
  comments, so trust is decided on every tick; an older file is discarded and the first tick
  re-reads every candidate.
- **Rate floor:** a tick is skipped when `core.remaining` < `ORCHESTRATE_RATE_FLOOR` (1000), because
  other agents share the login.
- **Write cap:** at most `ORCHESTRATE_MAX_WRITES` (5) GitHub writes per tick. The verify-wake comment
  counts toward it.
- **No retried writes:** a write is never retried, because a timed-out POST may have landed. Every
  attempt uses the write budget, failed ones included. Reads retry 5xx and 429 twice.
- **Failed writes:** any failed write makes the tick fail (exit 4, `health.json` result
  `apply_failed`), so backoff and the failure section start.
- **Crash recovery:**
  - An interrupted grok swap (`grok` added, `deepseek` still there) is finished on the next tick.
  - Ownership of `blocked` and `in-review` is journaled in `state.json` before the write, and pruned
    when the label is not on the item.
- **Re-check before a Grok wake:** right before each wake comment, it is skipped if the verify-wake thread already has
  its key, or the issue closed, lost `ready` or `grok`, or got an assignee, `deepseek` or an opt-out
  label since collect. It then re-reads `issues?state=open&labels=grok` in every scoped repo and
  counts the Grok tasks in flight now, the same way as at decide time: open `grok` items with an
  assignee, issues named by an open `grok` PR, tasks this tick saw as woken that are still open with
  `grok`, and wakes already posted in this tick. The wake is skipped when that count has reached
  `ORCHESTRATE_GROK_CAP`, when the issue is already one of them, or when its repo is busy (the repo of
  a task, or of an open `grok` PR that names an issue in any repo). A failed read skips the wake.
- **Re-check before posting:** a dispatch is skipped if the target got a new dispatch, lost its
  labels, or got `blocked` or `grok` since collect. A wake is skipped if the verify-wake thread
  already has a comment with its key; a merge or infra entry if its key is in `state.json` or the
  status issue body.
- **One tick at a time:** each tick takes `run.lock` (non-blocking), so overlapping or manual runs log
  `skipped` and exit 0.

## The status issue (vws-deploy#416)

The status lives in the **body** of one pinned issue, vws-deploy#415 (`ORCHESTRATE_STATUS_ISSUE`). The
orchestrator owns only one block in that body, from the line `<!-- vws-orchestrate:status -->` to the
line `<!-- /vws-orchestrate:status -->`. Text above and below the block belongs to people: every write
replaces only the block and keeps the rest byte for byte. Each apply tick renders the block (mode and switches,
PRs waiting for verify and where wakes go, DeepSeek in flight, PRs waiting for a rebuild (rule R), merge locks, the last 20 merge and infra
entries, and a "Failed ticks" section during a failure streak) and edits it with one
`PATCH repos/popododo0720/vws-deploy/issues/415` only when the block's content changed: its
`_Updated ... by run ..._` line and line endings are ignored when comparing, so a quiet tick writes
nothing. The read is ETag-cached, so an unchanged issue costs a 304. The edit is outside the write cap
(at most one per tick); a failed edit fails the tick. Dry-run writes the rendered body to
`status-body.md` in the state directory instead (the block only). While `ORCHESTRATE_SCOPE` is set, the
edit is skipped unless the scope is the status issue itself.

**Fail closed.** If the current body does not have exactly one block (no start marker, no end marker,
two blocks, or the end marker before the start marker), the orchestrator writes nothing: the tick logs
`status_body_unowned` with the reason and counts a tick error, and the failure section logs
`failure_alert_failed` (`status_body_unowned: ...`). Fix the body by hand; nothing is overwritten. When
seeding the issue (or after an edit removed a marker), the body must contain, each on its own line:

```
<!-- vws-orchestrate:status -->
<!-- /vws-orchestrate:status -->
```

The gh wrapper allows only this edit of the status issue: `PATCH` with `body` as the only
field and exactly one status block in it (rule T's edits never target it). The orchestrator posts no comments on the status issue
and no status comments anywhere; comments there are metrics alerts only (see
[factory-metrics.md](factory-metrics.md#alerts)). verify-wake never posts there:
`ORCHESTRATE_WAKE_THREAD` equal to `ORCHESTRATE_STATUS_ISSUE` is refused.

## Failure handling and the failure section

`orchestrate-loop.sh` runs every tick as a new process, so a crash ends one tick, not the loop.
Stopping the loop (`kill` of `loop.pid`) also stops a running tick, and children never hold
`loop.lock`.
With `METRICS_ENABLED=1` in the env file, each iteration also runs the daily metrics script
(`.github/metrics/metrics.py --if-due`, see [factory-metrics.md](factory-metrics.md)). Its exit status
never counts as a failed tick. On an abnormal day that script posts its own alert comment on the status issue (first line
`<!-- vws-orchestrate:metrics-alert -->`); see [factory-metrics.md](factory-metrics.md#alerts).
- **Backoff:** after a failed tick (exit status not 0) the loop waits `ORCHESTRATE_BACKOFF_BASE`
  (60 s) times 2^(failures-1), capped at `ORCHESTRATE_BACKOFF_MAX` (1800 s).
- **Failure section:** after each failed tick the loop runs `orchestrate.py --notify-failure`. At
  `ORCHESTRATE_FAILURE_THRESHOLD` (3) failures in a row, it writes one "## Failed ticks" section into
  the status block of the status issue body (no comment; text outside the block is kept; a body without
  one block is not written), starting with `<!-- vws-orchestrate:failure -->`, with the count,
  host, last exit code and the last 3 errors (redacted). The next good tick renders the body without it.
- **Scoped trials:** while `ORCHESTRATE_SCOPE` is set, the failure section is skipped and logged
  (`failure_alert_skipped`), like every other write outside the scope, unless the scope is the status
  issue itself. Watch `health.json` during a scoped trial.
- **Once per streak:** the key is sha256 of host and streak start, stored in `failure.json` only after
  a successful edit; a key already in the body is not written again. A good tick deletes `failure.json`. A 1h minimum gap stops a restarted loop from
  posting again for the same outage.
- **GitHub-wide failures:** the failure section ignores the write cap and the rate floor, because it is
  one read and one edit, so it still goes out when every tick fails. If GitHub itself is down, the edit
  fails, is logged, and the next failed tick tries again.
- **When it writes:** `ORCHESTRATE_FAILURE_ALERT=apply` (default) writes only in apply mode, `always`
  also in dry-run, `never` turns it off.

## Files on the box

| path | content |
|---|---|
| `/home/box/.local/opt/vws-orchestrate/releases/<sha7>/` | `.github/orchestrate/`, `.github/metrics/` and `.github/drift/repos.tsv` from merged `main` |
| `/home/box/.local/opt/vws-orchestrate/current` | symlink to the active release |
| `/home/box/.config/vws-orchestrate/env` | `KEY=VALUE` config, re-read every tick (no shell evaluation) |
| `/home/box/.config/vws-orchestrate/STOP` | if present, ticks are skipped |
| `/home/box/.local/state/vws-orchestrate/` | `state.json`, `health.json`, `failure.json`, `status-body.md`, `etag-cache.json`, `history-cache.json`, `run.lock`, `loop.lock`, `loop.pid`, all 0600 |
| `/home/box/.local/state/vws-orchestrate/log/orchestrate.jsonl` | one JSON object per event; rotated at 5 MiB, keeps 5 |
| `/home/box/.local/state/vws-orchestrate/log/loop.log` | loop output; rotated at 1 MiB, keeps 1 |

Config keys and defaults:
- `ORCHESTRATE_MODE=dry-run`
- `ORCHESTRATE_SCOPE=` (for example `vws-deploy#999` limits writes to one target and skips the wake, the status entries and the failure section)
- `ORCHESTRATE_INTERVAL=600`
- `ORCHESTRATE_MAX_WRITES=5`
- `ORCHESTRATE_RATE_FLOOR=1000`
- `ORCHESTRATE_STATUS_ISSUE=415` (the status issue in vws-deploy whose body the orchestrator edits; a positive integer)
- `ORCHESTRATE_WAKE_THREAD=` (the vws-deploy issue or PR that gets verify-wake and Grok wake comments; empty turns the wake off; never the status issue). `ORCHESTRATE_STATUS_PR` from before vws-deploy#416 is ignored
- `ORCHESTRATE_FAILURE_ALERT=apply`
- `ORCHESTRATE_FAILURE_THRESHOLD=3`
- `DEEPSEEK_TIMEOUT_HOURS=3`
- `DEEPSEEK_MAX_FAILURES=2`
- `ORCHESTRATE_DEEPSEEK_CAP=2` (DeepSeek dispatches in flight at once; a positive integer)
- `ORCHESTRATE_GROK_CAP=1` (rule G: Grok tasks in flight at once; a positive integer)
- `ORCHESTRATE_GROK_ALLOWLIST=` (rule G: `repo#N` entries such as `vws-deploy#378,vws-iam#12`, split on commas or whitespace; empty assigns nothing; anything else is ignored with a `grok_allowlist_invalid` note)
- `ORCHESTRATE_DEEPSEEK_LOGINS=popododo0720` (comma-separated GitHub logins whose `[deepseek-dev]` results count: the poller's account; each must be a valid login, optionally ending in `[bot]`)
- `ORCHESTRATE_VERIFY_LOGIN=vws-verify[bot]` (the one GitHub login whose verdict comments, markers and reviews count besides an `OWNER` comment; a valid login, optionally ending in `[bot]`)
- `ORCHESTRATE_VERIFY_USER_ID=` (empty: not pinned; else a positive integer that the verify login's `user.id` must equal; get it with `gh api 'users/vws-verify[bot]' -q .id`)
- `ORCHESTRATE_INFRA_RETRY_DELAY_MIN=5` (minutes after a provider-error report before the retry; a positive integer; with the 600 s tick the effective delay is 5 to 10 minutes)
- `VERIFY_WAKE_THRESHOLD=3` (needs-verify PRs waiting that wake vws-verify at once)
- `VERIFY_WAKE_MAX_WAIT_MIN=30` (minutes the oldest needs-verify PR may wait before a wake; `VERIFY_WAKE_EDGE_MIN_HOURS` and `VERIFY_WAKE_COOLDOWN_HOURS` are gone and ignored)
- `ORCHESTRATE_VERIFY_LABELS=on` (`on` or `off`; `off` stops rule V's `needs-verify` and `verify-approved` writes, vws-deploy#487)
- `ORCHESTRATE_DISPATCH=on` (`on` or `off`; `off` pauses new DeepSeek dispatches, including infra retries, while in-flight tracking, verify-wake and merges continue)
- `ORCHESTRATE_MERGE=dry-run` (rule D: `off`, `dry-run` or `apply`; merges need `apply` here and in `ORCHESTRATE_MODE`)
- `ORCHESTRATE_MERGE_CI_TIMEOUT_HOURS=2`
- `ORCHESTRATE_MERGE_MAX_ATTEMPTS=2`
- `ORCHESTRATE_AC_TICK=dry-run` (rule T: `off` reads nothing, `dry-run` plans and logs, `apply` writes; writes need `apply` here and in `ORCHESTRATE_MODE`)
- `ORCHESTRATE_BOARD=dry-run` (board_sync: `off`, `dry-run` or `apply`; writes need `apply` here and in `ORCHESTRATE_MODE`)
- `ORCHESTRATE_BOARD_PROJECT=PVT_kwHOBK54jM4BoB_Z` (the ProjectV2 node id of the board)
- `ORCHESTRATE_BOARD_EVERY=6` (sync at most every N ticks, plus the tick after a managed label change)
- `ORCHESTRATE_BOARD_WRITES=50` (board mutations per sync, separate from `ORCHESTRATE_MAX_WRITES`)
- `ORCHESTRATE_BOARD_TOKEN_FILE=/root/.config/vws/board-sync-token` (absolute path to the project-scope classic PAT used only for board_sync GraphQL calls)

## Install (after this PR merges; from merged main)

The box has no systemd and no cron, and nothing restarts user processes after a box restart. The loop
is started with `setsid nohup`.

Before the install or an upgrade, check the GitHub accounts the trust rules depend on (vws-deploy#419,
#433):

1. On the DeepSeek poller VM, run `gh api user -q .login`. It must print the expected poller account
   (`popododo0720` by default). If it prints another login, put `ORCHESTRATE_DEEPSEEK_LOGINS=<that login>`
   in the env file below; otherwise every real result is ignored, dispatches time out and tasks move
   to grok.
2. On the box, run `gh api user -q .login` with `GH_TOKEN` unset. It must print `popododo0720`: the
   orchestrator's own dispatches, rework prompts and swap markers are trusted through that account's
   `OWNER` association.
3. Optional: pin the verify bot with `ORCHESTRATE_VERIFY_USER_ID=$(gh api 'users/vws-verify[bot]' -q .id)`
   in the env file.

```bash
unset GH_TOKEN
cd /workspace/vws-deploy && git fetch origin main
SHA=$(git rev-parse --short=7 origin/main)
umask 077
install -d -m 0700 /home/box/.local/opt/vws-orchestrate/releases /home/box/.config/vws-orchestrate \
  /home/box/.local/state/vws-orchestrate/log
REL=/home/box/.local/opt/vws-orchestrate/releases/$SHA
install -d -m 0700 "$REL"
git archive --format=tar origin/main .github/orchestrate .github/metrics .github/drift/repos.tsv | tar -x -C "$REL"
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
