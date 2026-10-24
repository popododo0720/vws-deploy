# Copying the insider-risk guards to another repo

The design is https://github.com/popododo0720/vws-deploy/issues/33. This repo is private on a free plan, so branch protection and rulesets are unavailable; CI and rules take their place. The PR checks stop bad merges, and the main push watch only reports what has already happened (it does not block).

## The three files

| File | What it does |
| --- | --- |
| `.github/workflows/repo-guard.yml` | Runs on PRs only: the base is the default branch; the body has `Part of #N` (N of 1 or more, outside HTML comments `<!-- -->` and code fences), `AC: <n>` lines naming the boxes of the linked issue's acceptance criteria it covers (n from 1; or one `AC: none - <reason>`; vws-deploy#486) and the 9-item Quality checklist, all ticked with evidence or `N/A: <reason>`; every PR commit has a `<type>: summary` subject and `Part of #N`; neither the PR body nor the commits have a closing keyword (`Closes/Fixes/Resolves` followed by `#N`, `owner/repo#N` or a full issue or PR URL); AGENTS.md has the guard section. The linked issue's acceptance criteria are printed to the job summary (read-only, never failing) |
| `.github/workflows/direct-push-watch.yml` | Runs only on `push` to main: every new first-parent commit has a linked PR, and main's history was not rewritten. It is a separate file so a PR run has no skipped check |
| `scripts/guard-agents.sh` | The actual code of the checks above. It uses no repo paths. `selftest` feeds bad and good inputs directly and checks that every bad one fails |

## How to copy

1. Copy the two workflow files and the script to the same paths unchanged, and make `scripts/guard-agents.sh` executable.
2. Add a `## Insider-risk guardrails` section (items 1-7) to the target repo's AGENTS.md. Copy the body from the same section of this repo's AGENTS.md. The heading text and numbering must match for the `agents` check to pass.
3. If the default branch is not `main`, change only the `branches: [main]` line in `direct-push-watch.yml`. Everything else uses `github.event.repository.default_branch`. On the script side, set `GUARD_MAIN_BRANCH`.
4. Even if the repo already has CI, keep these workflows as separate files. Do not run the same job in two places.
5. Local check: `scripts/guard-agents.sh selftest`, `scripts/guard-agents.sh agents`. If the repo has its own guard (such as `guard.sh`), call `agents` and `selftest` from there. In this repo `scripts/guard.sh agents` does it.

Environment variables: `GUARD_MAIN_BRANCH` (default `main`), `GUARD_HEADING`, `GUARD_ITEMS` (default 7), `GUARD_QUALITY_ITEMS` (default 9, the checklist items), `GUARD_RETRY_SECONDS` (retry interval for push-watch's PR lookup, default 5).

## Limits

- A PR can bypass these checks by editing the script or workflow in the same PR. They are not server-side required checks. Whoever merges (`mergepr.sh` and the like) must check the base and the head CI.
- The push watch (`direct-push-watch.yml`) is detection after the fact. It cannot stop a commit that landed without a PR; it only marks it as failed. The link between a commit and its PR can lag a few seconds after a merge, so it retries 3 times.
- When the author bot, the verifier bot and the merger share one GitHub account, an "author and merger differ" check means nothing, so there is none. Design principles 1 and 5 are process rules.
- Results on the GitHub side are seen only through the PR's CI. `direct-push-watch.yml` first runs only after it is merged to main.

## The canonical guard-agents.sh and the identity check

- The canonical copy is `scripts/guard-agents.sh` in vws-deploy, and its sha256 is pinned in `scripts/guard-agents.sh.sha256` (sha256sum format). Other repos copy both files byte for byte and do not edit them.
- Each repo's repo-guard checks with `scripts/guard-agents.sh identity` that its copy's sha256 equals the pinned value, and fails otherwise.
- To change the canonical copy: edit the script in a vws-deploy PR and update the hash in the same commit with `(cd scripts && sha256sum guard-agents.sh > guard-agents.sh.sha256)`. vws-deploy's `scripts/guard.sh agents` (R13) fails when the two disagree. After the merge, open a PR in each repo that copies both files unchanged.
- Limit: the hash file is also inside the repo, so a copy that edits both files together passes this check. It catches accidental differences. To stop deliberate changes as well, CI would have to read the hash from vws-deploy main directly, which needs a token that can read a private repo (needs checking).
- `rules [FILE] [HEADING]` (alias `judgment`) checks that every row of the rule table has an enforcer and prints the number of Judgment rows. The heading comes from the argument, then `GUARD_RULES_HEADING`, then whichever of `## Rules` and the legacy Korean heading the file has.
- `archtest [FILE] [HEADING]` is the single command that runs the architecture tests. It first prints the Judgment row count line from `rules` (`rules: 50 rows, 2 Judgment`), then runs `go test -v -count=1 ${GUARD_ARCHTEST_PKG:-./internal/archtest/...}`. The row count stays in the CI log even when the tests fail. A repo without the directory prints that and passes. Each repo's CI calls `scripts/guard-agents.sh archtest` instead of `go test ./internal/archtest/...` (vws-network#99).
- `english [ALLOWLIST]` fails on a tracked file with Hangul unless a `<glob> <reason>` line in ALLOWLIST (default `.github/english-allowlist`) covers it. `pr-body` and `commit-msg` reject Hangul with no exception. A repo with Korean docs must translate them (or allowlist test data and UI strings) before it takes this copy.
- `pr-checklist [FILE]` checks the pull request text (FILE or `$PR_BODY`): exactly one `## Quality checklist (required)` section in the visible text (HTML comments, also across lines, and code fences removed) with each of the 9 template item titles (`QUALITY_TITLES`) exactly once; a ticked item has evidence after `Evidence:` or `N/A: <reason>`, an unticked item passes only with `N/A: <reason>`. The untouched template line, an `N/A:` with no reason, an unknown or repeated title, and evidence hidden in an HTML comment all fail, and the error names each bad, missing or duplicated item.
- `pr-issue [FILE]` prints the `Part of #N` reference (`#N`, or `owner/repo#N`). The repo-guard step uses it to pick the issue for `.github/ac/ac-lint.sh`.
- `.github/ac/ac-lint.sh --file BODY` (or `--repo OWNER/REPO --issue N`) lists each acceptance-criteria box as OPEN, OK or NO-EVIDENCE (#399). `pr-hygiene` sends its output to the job summary and continues even when the script finds a ticked box without evidence.

- `verify-gate HEAD LABELS [FILE]`: the verdict is read only from `@@comment-by:` comments (never the PR body). Comments and labels by `GUARD_VERIFY_EXCLUDE` (the PR author and the account that pushed the head) do not count; the only exception is `GUARD_VERIFY_IDENTITY` (the vws-verify login). Known gap: when all bots use one account, the author cannot be told apart from vws-verify. The workflow lands separately, after this script is on main (#261).
