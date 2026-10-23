#!/usr/bin/env bash
# After a pull request merges, label each "Part of #N" issue in this repo whose acceptance
# criteria still have an open box or a tick without evidence with ac-pending, so the lead can
# tick them in one batch (vws-deploy#399). Writes labels only: no comments, edits or closes.
# Env: REPO=owner/name, PR_BODY_FILE=the merged PR body, GITHUB_STEP_SUMMARY (optional).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
: "${REPO:?}" "${PR_BODY_FILE:?}"
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
name=${REPO#*/}

# "Part of #N" or "Part of <this-repo>#N" at the start of a line, as guard-agents.sh pr-body asks.
mapfile -t refs < <(tr -d '\r' <"$PR_BODY_FILE" |
  grep -oiE '^[[:space:]]*part of[[:space:]]+([a-z0-9_.-]+)?#[0-9]+' |
  sed -E 's/^[[:space:]]*[Pp][Aa][Rr][Tt] [Oo][Ff][[:space:]]+//' | awk '!seen[$0]++')

{
  echo "### ac-pending"
  ((${#refs[@]})) || echo "No Part of #N in the PR body."
} >>"$summary"

rc=0
for ref in "${refs[@]}"; do
  repo_part=${ref%%#*} n=${ref##*#}
  if [[ -n $repo_part && $repo_part != "$name" ]]; then
    echo "- $ref skipped, other repo (this token labels $REPO only)" >>"$summary"
    continue
  fi
  if ! json=$(gh api "repos/$REPO/issues/$n"); then
    echo "- #$n cannot read the issue" >>"$summary"
    rc=1
    continue
  fi
  if [[ $(jq -r 'has("pull_request")' <<<"$json") == true || $(jq -r .state <<<"$json") != open ]]; then
    echo "- #$n skipped, not an open issue" >>"$summary"
    continue
  fi
  out=$(jq -r '.body // ""' <<<"$json" | "$here/ac-lint.sh" --file /dev/stdin || true)
  open=$(grep -c '^OPEN ' <<<"$out" || true)
  noev=$(grep -c '^NO-EVIDENCE ' <<<"$out" || true)
  if ((open + noev == 0)); then
    echo "- #$n complete (OPEN=0 NO-EVIDENCE=0)" >>"$summary"
    continue
  fi
  if gh api -X POST "repos/$REPO/issues/$n/labels" -f 'labels[]=ac-pending' >/dev/null; then
    echo "- #$n labeled ac-pending (OPEN=$open NO-EVIDENCE=$noev)" >>"$summary"
  else
    echo "- #$n label write failed (OPEN=$open NO-EVIDENCE=$noev)" >>"$summary"
    rc=1
  fi
done
exit "$rc"
