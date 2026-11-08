# verify-gate: comment text or the vws-verify App check run (#338)

## Problem

Two verify-gate models run today.

- vws-deploy and every repo that calls `repo-guard-verify-gate.yml` (the comment model). The gate
  needs the `verify-approved` label and a PR comment with a `Verified-by: vws-verify` section that names
  the head SHA, has evidence for the 9 docs/QUALITY.md items and a `Codex review: ... blocking=0` line.
  The job walks every comment and every labeled event over GraphQL, then reads the head pusher.
- vws-conformance (its own `repo-guard.yml` on `pull_request_target`, guard sha256 `7070d89b...`, from
  vws-conformance#3, the check-run model). The gate needs the label, a labeler in a nonempty
  `VWS_VERIFY_ACTORS`, and the newest `vws-verify` check run on the head from the `vws-verify` App with
  conclusion `success`. It reads no comment. It adds `allow`, which refuses a head whose
  `repo-guard.yml`, `guard-agents.sh` or `guard-agents.sh.sha256` digest is not in
  `scripts/guard-allowed.sha256` on the default branch.

drift-check accepts the second guard as a variant (`.github/drift/guard-variants.tsv`) until #338 picks
one model for every repo.

## Facts the choice rests on

Read on 2026-11-08 with `gh api`.

1. The `vws-main` ruleset of vws-deploy and of vws-conformance already requires the context
   `vws-verify` with `integration_id` 5313004 (`repos/<repo>/rules/branches/main`). GitHub accepts that
   check only from the vws-verify App. Only the holder of the App's private key can create it. A pull
   request cannot edit it.
2. The App's check run on a head links the verdict comment. On vws-deploy#717, #719 and #720 the
   `output.title` is `vws-verify PASS` and `output.summary` is exactly
   `https://github.com/popododo0720/vws-deploy/pull/<N>#issuecomment-<id>`. That is the newest
   `Verified-by` comment, written by `vws-verify[bot]`.
3. vws-verify writes in this order: the comment, then the label (about 1 s later), then the check run
   (about 3 s after the label). Example from #720: comment 15:52:18, label 15:52:18, check run completed
   15:52:21. A gate run started by the `labeled` event can therefore start before the check run exists.

## Security comparison

| Question | Comment model | Check-run model (vws-conformance) |
| --- | --- | --- |
| Who can produce a passing verdict | Anyone whose comment passes the login checks. With `VWS_VERIFY_ACTORS` unset, any commenter. All bots but vws-verify share one account, so the author and pusher exclusion is the only thing that tells them apart | Only the vws-verify App (its key). The label still has to come from a `VWS_VERIFY_ACTORS` login |
| Bound to the head | The comment text names a SHA; the gate compares it with the head | The check run's `head_sha` is set by GitHub when the App creates it |
| Verdict edited or deleted after the gate passed | The gate is stale until `verify-rerun` re-runs it on the `issue_comment` event | The App's newer check run replaces the old one; the ruleset reads the newest |
| Evidence form and Codex `blocking=0` checked in CI | Yes | No. Left to vws-verify |
| A PR edits the gate | In vws-deploy, the callee YAML; elsewhere, the caller and its pin. drift-check reports it after the push (docs/QUALITY.md "Known gaps") | `pull_request_target` runs the default-branch file, and `allow` refuses changed locked files. But the ruleset check (fact 1) is what a PR can never edit |
| API reads per gate run | Labels, a GraphQL bundle plus comment and labeled-event pages, the activity of the head ref | Labels, check runs, every issue event, activity, the commit |

The App check run is the stronger proof of authorship: GitHub, not text parsing, says who wrote it and
for which commit. Fact 1 means the ruleset already enforces it in every repo, so checking only its
conclusion again in verify-gate adds no protection. What verify-gate adds over the ruleset is the label
and the form check on the evidence and the Codex line. The check run is useful to the gate as a signed
pointer to the one comment those checks should read.

## Chosen

One model for every repo: **the check run names the comment, and the gate checks that comment.**

- The gate reads the check runs named `vws-verify` on the head (`commits/<head>/check-runs?check_name=vws-verify&filter=latest`) and keeps those from app id 5313004. The id is a constant in `guard-agents.sh`, not a repo variable, so a repo setting cannot widen it. The newest one by `completed_at`, then by id (vws-conformance's `check-run` rule), must be `success`.
- Its `output.summary` must be exactly `https://github.com/<this repo>/pull/<this PR>#issuecomment-<id>`. The gate reads that one comment (`issues/comments/<id>`). Its `user.login` must be the vws-verify identity and its `issue_url` must be this pull request. The existing `verify-gate` rules then run on that body alone: the head SHA, the 9 items with evidence, and the Codex line.
- The label rule stays: `verify-approved` is on the PR. The author and pusher exclusion stays for the comment author, so a PR opened or pushed by the vws-verify identity cannot approve itself.
- No check run yet (fact 3): the gate re-reads the check runs every 10 s for up to 60 s, then fails closed with "no vws-verify check run on <head>". A follow-up in vws-verify posts the check run before it adds the label, which removes the wait.
- `verify-rerun` keeps the `issue_comment` trigger. It covers a verdict comment that was edited after the gate ran.
- vws-conformance becomes a caller like every other repo: `pull_request`, the pinned callee, the canonical guard. Its `repo-guard.yml` variant, `verify-rerun.yml` and `scripts/guard-allowed.sha256` are deleted.

What goes away: walking every comment, the labeled-event walk and its labeler login check, `VWS_VERIFY_ACTORS` as the only defence against a forged comment, and the `vws-verify[bot]` login normalisation between GraphQL and REST. The API reads become labels, check runs, one comment and the head activity.

## Rejected

- **Keep the comment model.** Authorship rests on login strings and on `VWS_VERIFY_ACTORS` being set. The App check run already proves it.
- **The vws-conformance model as is.** It drops the evidence and Codex checks from CI, which docs/QUALITY.md and AGENTS.md require of the gate. Its check-run read repeats what the ruleset already enforces (fact 1).
- **The digest allowlist (`allow`) in the shared callee.** It protects a gate a PR cannot edit only on `pull_request_target`. On `pull_request` a PR can edit its caller to skip the call or the shim, so a step in the callee cannot stop that. The pin rules in `caller_pin.py`, drift-check at every open PR head and the ruleset's App check already cover the caller. `pull_request_target` stays out (docs/design/repo-guard-reusable.md "One event per required check").
- **A `check_run` event trigger instead of the wait.** A `check_run` run is not tied to a pull request, so its result is not the `verify-gate` check of the head. It would need a second workflow that re-runs the gate, as `verify-rerun` does.
- **The verdict in the check run's `output.text`.** It would move the evidence out of the PR conversation that people read, and vws-verify would have to change its output first.

## Rollout, one small PR each

1. vws-deploy (this PR): this record.
2. vws-deploy: `guard-agents.sh` gains the check-run and comment-link rules with selftest cases, the
   callee `repo-guard-verify-gate.yml` reads check runs and the one comment, `caller_pin.py` and the
   caller `gate` job add `checks: read`, and docs/QUALITY.md and AGENTS.md describe the new gate.
   Negative tests: no check run, a check run from another app, on another SHA, `failure` after
   `success`, a summary that names another PR or repo, a comment by another login, a comment without the
   head SHA, a Codex line with `blocking=1`. Each must fail.
3. Each caller repo (vws-lib, vws-proto, vws-iam, vws-network, vws-storage, vws-compute,
   vws-console-api, vws-console, vws-hypervisor): one PR that bumps the four pins and the `checks: read`
   permission and copies the canonical guard and its `.sha256`.
4. vws-conformance: first a PR that adds the digests of the new caller, guard and `.sha256` to
   `scripts/guard-allowed.sha256`, since its current gate refuses them otherwise. Then the PR that
   replaces its `repo-guard.yml` with the caller, copies the canonical guard and deletes
   `verify-rerun.yml` and `scripts/guard-allowed.sha256`.
5. vws-deploy: delete the vws-conformance rows from `.github/drift/guard-variants.tsv` and
   `.github/drift/caller-pending.tsv`.
6. vws-verify (outside this repo): post the check run before the label.

The negative tests of step 2 run in every repo through the shared guard selftest, so "verify-gate still
blocks unverified heads" holds for each repo once it carries the canonical guard.

## Not verified

The facts above were read from the GitHub API. Nothing in this record ran in a workflow. The 60 s wait
is a guess from three PRs' timings (3 to 4 s).
