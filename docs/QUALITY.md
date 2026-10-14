# Quality standard

Adopted 2026-10-14. Every change in this repo must be AWS-grade production quality, not demo quality. A PR is done only when the "Quality checklist (required)" in the pull request template is filled with evidence for each item.

1. **Error handling**: every error is wrapped with context and returned. No panic or crash on any input.
2. **Retries and timeouts**: every external call has a timeout or context deadline and retries transient failures with bounded backoff.
3. **Security**: validate all input, grant least privilege (RBAC and permissions), and keep secrets only in Kubernetes Secrets. Secrets are never logged and never committed.
4. **Unit tests** for new behavior and failure paths.
5. **Integration tests** at real or faithfully faked boundaries.
6. **Live evidence on vws-test**: commands and output from the vws-test cluster, linked or pathed in the PR. Without live evidence the work is not done.
7. **Observability**: OpenTelemetry traces and metrics plus structured logs.
8. **Idempotency**: safe on retry, duplicate request and restart.
9. **AWS compatibility** where applicable: API behavior and error codes match AWS.

Reviewers do not merge a PR whose checklist lacks evidence or a stated reason for N/A.

## Verify gate (#249)

The `repo-guard / verify-gate` check passes only when all of these hold. It is not a required
check yet; making it required is a separate admin step.

- The pull request has the `verify-approved` label.
- A PR comment (never the PR body) has a section like the one below. It must name the
  current head commit and give evidence for every item above, all in that one comment.
  Evidence is a URL, a file path, or `N/A: <reason>`.
- When the repo variable `VWS_VERIFY_ACTORS` (comma list of logins) is set, the comment
  author and whoever last added the label must both be in it.
- The PR author and the head commit's pusher can neither label nor approve, unless the login
  is the vws-verify identity in the repo variable `VWS_VERIFY_IDENTITY`.

```
Verified-by: vws-verify
Head: <full 40-character head SHA>
1. https://...
2. tests/vws/run.sh log: build/verify/run.log
...
9. N/A: no user-visible change
```

A push changes the head, so the old section stops counting and the gate turns red.
`verify-reset` also removes the label. To approve again, re-verify and post a new section
for the new head.

### Known gaps

The gate records that a verdict was posted. It cannot prove who judged it.

- **One account.** All bots, vws-verify included, push and comment as the same GitHub
  account. While that account is also `VWS_VERIFY_IDENTITY`, the author/pusher exclusion
  exempts it, so a bot can label and approve its own PR. The fix is a separate account or
  GitHub App for vws-verify, set alone in `VWS_VERIFY_ACTORS` and `VWS_VERIFY_IDENTITY`.
- **The PR can edit the gate's YAML.** On `pull_request`, GitHub runs the workflow from the
  PR's merge commit. The job checks out the base commit, so the guard script that judges is
  the one on main, but a PR can still change `repo-guard.yml` itself (for example, the
  checkout ref or the step). The real control is branch protection with required review
  from CODEOWNERS for `.github/` and `scripts/`, by an account other than the author.
- **Evidence is checked for form, not truth.** The gate checks that each item has a URL,
  path or `N/A: reason`. It does not open them.
