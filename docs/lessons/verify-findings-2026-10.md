# Verify and Codex findings, October 2026

This file is the findings table for vws-deploy#439 AC1. It names the classes of
findings that vws-verify and Codex repeat most often, how often each class
appears, where to see real examples, and the cheapest deterministic check
that would catch each class before a PR reaches verify. The checks themselves
are not built here. They are AC2 and later, in separate PRs.

## Method

The method is pstack `correct` (principle-encode-lessons-in-structure):
cluster the findings, count them, pick the top repeated classes, and choose
the cheapest deterministic encoding for each one (architecture first, then
types, then a lint or guard).

Inputs:

1. Codex review files: `/workspace/handoff/codex/*.md` on the shared box.
   543 files have an mtime from 2026-10-11 on (the mtimes fall between
   2026-10-22 and 2026-10-25, because the files were copied again, so mtime
   is not the review date). Every `[P0]`-`[P3]` finding line was extracted:
   844 lines in 204 files (P1 205, P2 611, P3 28). P3 lines were dropped,
   which leaves 816 P1 and P2 lines.
2. `CHANGES_REQUESTED` reviews on popododo0720/vws-* PRs updated from
   2026-10-11 on, through the GitHub GraphQL search API: 148 reviews, all by
   vws-verify, submitted from 2026-10-14 18:22 to 2026-10-25 14:19 KST
   (vws-deploy 47, vws-console-api 35, vws-iam 13, vws-compute 12,
   vws-console 10, vws-storage 9, vws-conformance 8, vws-proto 7, vws-lib 4,
   vws-network 3).
3. Verdict evidence: `/workspace/handoff/verdicts/*/summary.md` (114 files in
   the window, 42 of them FAIL). These are the drafts of the reviews in input
   2, so they were read for context (for example `verdicts/ca319/summary.md`)
   and not counted a second time.

Each finding line was given to the first class whose keyword pattern matched
it, in the table's order. A line is counted once per review file. "Distinct"
counts the distinct finding titles. The same title often appears in several
files, because one guard file was copied into up to ten repos and reviewed
in each copy. 342 of the 816 lines matched no class. That long tail is mostly
one-off domain bugs, and no single check would catch it. The counts are
keyword-based and approximate. Review counts use a separate, stricter
pattern on the full review body.

The extraction and clustering scripts, with their raw output, are on the box
under `/workspace/handoff/factory/439-ac1/` (`extract.py`, `cluster.py`,
`rc.py`, `rc_tight.py`, `findings.tsv`, `rc-reviews.json`).

## Findings table

Codex counts are P1 and P2 lines (with the P1 count in brackets), distinct
titles and review files. Review counts are `CHANGES_REQUESTED` reviews.

| # | Class | Count | Examples (opened) | Chosen encoding |
|---|---|---|---|---|
| 1 | **Untrusted or stale GitHub state.** A decision uses data that an untrusted actor can write, or that was read before the action it guards: the event snapshot instead of the live labels, a PR body edited after the trusted verdict, a marker or "claim" comment from any commenter, the commit committer instead of the pusher, an older verdict for the head. | Codex 89 [P1 15], 85 distinct, 63 files. Reviews 20. | Codex: `vws-deploy-499-f879cd7.md` and `vws-deploy-496-cc28e1f.md` (collect_ac trusts the live PR body; the dedupe marker is accepted from any commenter), `vws-deploy-484-1b64245.md` (revalidate expired tasks against fresh occupancy), `compute-v2-6472546.md` (use the latest verdict for the head). Reviews: [vws-deploy#499](https://github.com/popododo0720/vws-deploy/pull/499#pullrequestreview-5640903019), [vws-deploy#507](https://github.com/popododo0720/vws-deploy/pull/507#pullrequestreview-5641696302) (any non-marker comment counts as a claim, whoever wrote it). | **Architecture, then a guard.** Give orchestrate.py one trusted-read module: comments filtered by an author allowlist, PR state read again at the pinned head right before each write, and PR body text taken only from the trusted Verified-by snapshot. Then add a `vws-drift-check` lint that fails when workflow YAML reads `github.event.pull_request.labels` or `head_commit.committer`, or when orchestrate.py matches a marker regex outside that module (an AST check). |
| 2 | **Guard text parsing and scan gaps.** Guards parse PR bodies, comments and commit messages in different ways: a multi-line HTML comment hides a checklist, a fence closes on the wrong delimiter, `#` lines are lost when commit messages are cleaned, merge commits are skipped in the closing-keyword scan, an empty commit range passes. | Codex 79 [P1 7], 49 distinct, 49 files. | Codex: `compute-v2-586d673.md` (keep hash-prefixed lines when checking stored commits; the same title appears in 10 files across the repo copies), `lib-v2-c6e11b2.md` (include merge commits in the closing-keyword check). Review: [vws-deploy#430](https://github.com/popododo0720/vws-deploy/pull/430#pullrequestreview-5629555715) (`guard-agents.sh` removes HTML comments only within one line). | **Architecture, then a guard.** Use one shared normalizer (strip multi-line HTML comments, match fences by delimiter and length, read raw commit bodies including merge commits, fail on a range that cannot be listed) and call it from `guard-agents.sh`, `check-english.sh`, `.github/ac/post-merge.sh` and orchestrate's parsers. Then add an adversarial fixture corpus to `guard-agents.sh` tests, with each past bypass as one fixture, so every parser must give the same answer. |
| 3 | **Partial failure and rerun recovery.** A multi-step write does not converge after a crash, a retry or a rerun: the issue is closed but the marker comment never lands, a rerun of a label removal hits a 404, a recovery resend prunes records that another write added, a gate rerun loses the race while the gate is still running. | Codex 41 [P1 8], 39 distinct, 37 files. | Codex: `vws-deploy-495-04c7517.md` (recover the final comment after a successful issue close), `lib-v2-654da9a.md` (make approval-label removal safe to rerun), `vws-console-api-319-b985dfb.md` (detach recovery loses the original result). Review: [vws-console-api#319](https://github.com/popododo0720/vws-console-api/pull/319#pullrequestreview-5622625900) (the recovery resend loses B's original result when another write lands first). | **Guard, backed by a test harness.** Add a fault-injecting fake for orchestrate's GitHub writes that stops the tick after write N, then runs the tick again and checks that the end state matches one clean run. Add a `pr-hygiene` or test-suite lint that every orchestrate function making two or more GitHub writes has a restart test using that fake. (The console-api detach path needs the same idea in Go, as a later per-repo step.) |
| 4 | **Bootstrap order and copy drift of shared guard files.** A base-pinned gate calls a script subcommand that main does not have yet, or a "copy" of a shared file is not the same as the source in vws-deploy. | Codex 22 [P1 19], 21 distinct, 21 files. Reviews 26. | Codex: `lib-v2-c6e11b2.md` (bootstrap the trusted script before enabling the base-pinned gate), `console-api-v2-6e550d3.md` (bootstrap the trusted script before enabling verify-gate). Reviews: the nine verify-queue copy PRs, for example [vws-storage#140](https://github.com/popododo0720/vws-storage/pull/140#pullrequestreview-5638072542) and [vws-lib#50](https://github.com/popododo0720/vws-lib/pull/50#pullrequestreview-5638072331) ("the unchanged copy claim is false, and the copy drops a CI-gate fix"), and [vws-lib#49](https://github.com/popododo0720/vws-lib/pull/49#pullrequestreview-5638075948) (keep the shared repo-guard workflow byte-identical). | **Architecture, then a guard.** Remove copies: shared logic runs from the reusable workflow pinned to a vws-deploy SHA (#476 and AC3). For files that still have to be vendored, extend `vws-drift-check`, which already pins `guard-agents.sh` by sha256 (`scripts/guard-agents.sh.sha256`, `.github/drift/guard-variants.tsv`), so that it also pins `verify-queue.yml` and `check-english.sh`. Also add a bootstrap rule: a workflow may call a guard subcommand only if the base-branch script already has it. |
| 5 | **Authorization bound to the wrong principal.** A console-api IAM action is authorized against one identity (a name, `*`, or a stale ARN) and then runs against another, or it fails open when the lookup misses. | Codex 10 [P1 8], 10 distinct, 10 files. Reviews: at least 2 (the two cited, both blocking). | Codex: `vws-console-api-318-4577f95.md` and `vws-console-api-330-f155593.md` (ListMFADevices is not bound to the authorized user ARN), `vws-console-api-346-c2eb943.md` (bind policy operations to the authorized entity's unique ID), `vws-console-api-355-0950255.md` (the ARN hygiene lint misses aliases and field assignments). Reviews: [vws-console-api#330](https://github.com/popododo0720/vws-console-api/pull/330#pullrequestreview-5623282027), [vws-console-api#325](https://github.com/popododo0720/vws-console-api/pull/325#pullrequestreview-5623030210). | **Types.** Add an `AuthorizedPrincipal` value type in vws-console-api that only the authorizer can create (unexported fields), and make every IAM backend call take it instead of a string. The existing lint (`internal/hygiene/authorized_arn_test.go`) can be bypassed with aliases, as `vws-console-api-355-0950255.md` shows, so a type is the stronger encoding. |

### Considered, not chosen as a top class

- **AWS-parity input validation** (Codex 25 [P1 5], 23 distinct): duplicate
  tag keys, raw pagination markers, description length counted in bytes
  instead of characters. Example: `console-api-226-ac937d9.md`. Each finding
  is a rule for one API, so one CI check cannot catch the class. vws-conformance
  parity runs are the right encoding, and they already exist.
- **Unmet AC while claiming done** (8 reviews, for example
  [vws-deploy#494](https://github.com/popododo0720/vws-deploy/pull/494#pullrequestreview-5638458208)
  and [vws-console-api#374](https://github.com/popododo0720/vws-console-api/pull/374#pullrequestreview-5638303904)),
  and **a blocker carried unchanged into a rebuild** (6 reviews, for example
  [vws-deploy#500](https://github.com/popododo0720/vws-deploy/pull/500#pullrequestreview-5639059828),
  a cherry-pick of a FAIL head with the blocker still there). Both are manual
  checks that verify repeats. They belong to the AC4 project verification
  script: AC coverage per `AC:` line, and a `git range-diff` against the last
  FAIL head that must change non-docs code.
- **Mixed commit scopes** (13 reviews, for example
  [vws-deploy#424](https://github.com/popododo0720/vws-deploy/pull/424#pullrequestreview-5625101056)).
  `scripts/guard.sh` rule R2 already catches this in CI. Those reviews cite
  the red check, so nothing new is needed.

## Next PRs (AC2 and later, not in this PR)

One class per PR, each with a failing-first fixture taken from the cited real
finding and a passing fixture (AC2, AC5):

1. Class 4: extend `vws-drift-check` to pin `verify-queue.yml` and
   `check-english.sh` by sha256, and add the base-script subcommand bootstrap
   rule. Fixture: the vws-storage#140 copy.
   Done in the class 4 PR: `.github/drift/vendored.tsv`, the `vendored copies` and `bootstrap` cells of
   `vws-drift-check`, and `tests/drift/test_class4.py` with real fixtures in `tests/drift/fixtures/class4/`.
2. Class 2: shared text normalizer and adversarial fixture corpus in
   `guard-agents.sh`. Fixtures: the multi-line HTML comment (vws-deploy#430),
   hash-prefixed commit lines, a merge commit with a closing keyword.
3. Class 1: orchestrate trusted-read module and the `vws-drift-check` lint
   for event-snapshot labels, committer identity and stray marker matches.
   Fixture: the #499 edited PR body and the #507 foreign claim comment.
4. Class 3: fault-injecting GitHub fake and the restart-test lint for
   orchestrate. Fixture: the #495 close-then-marker failure.
5. Class 5: `AuthorizedPrincipal` type in vws-console-api, as a console-api PR
   with the 9-item checklist. AC2 lists `pr-hygiene`, `guard-agents.sh` and
   `vws-drift-check` as the places for checks, so the lead should confirm
   that a type change in console-api counts for this class.
6. AC3: run the new checks from the shared reusable workflow in every vws-*
   repo. AC4: the project verification script for the manual checks listed
   above.
