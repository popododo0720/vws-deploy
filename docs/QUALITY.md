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

The required `repo-guard / verify-gate` check passes only when both of these hold:

- The pull request has the `verify-approved` label.
- The body or a comment has a section like the one below. It must name the current head
  commit and give evidence for every item above. Evidence is a URL, a file path, or
  `N/A: <reason>`.

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
