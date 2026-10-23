#!/usr/bin/env bash
# After a pull request merges, label each "Part of #N" issue in this repo whose acceptance
# criteria still have an open box or a tick without evidence with ac-pending, so the lead can
# tick them in one batch (vws-deploy#399). Writes labels only: no comments, edits or closes.
# Env: REPO=owner/name, PR_BODY_FILE=the merged PR body, GITHUB_STEP_SUMMARY (optional),
# HEAD_REPO=owner/name of the PR head (optional; set but different or empty = a fork PR),
# AC_LINT=the linter (default: ac-lint.sh next to this script).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
: "${REPO:?}" "${PR_BODY_FILE:?}"
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
lint=${AC_LINT:-$here/ac-lint.sh}
name=${REPO#*/}
lc() { tr '[:upper:]' '[:lower:]' <<<"$1"; }

echo "### ac-pending" >>"$summary"
# A fork PR runs with a read-only token: say so instead of failing on the first label write.
head_repo=${HEAD_REPO-$REPO}
if [[ $(lc "$head_repo") != $(lc "$REPO") ]]; then
  msg="skipped: fork pull request (head ${head_repo:-deleted fork}); its read-only token cannot label issues in $REPO"
  echo "$msg"
  echo "$msg" >>"$summary"
  exit 0
fi

# The refs guard-agents.sh pr-body accepts (PART_OF_RE, case-insensitive, anywhere in a line), in the
# text a reader sees: HTML comments and fenced code blocks are dropped first, as visible_text does.
mapfile -t refs < <(tr -d '\r' <"$PR_BODY_FILE" |
  awk '/^[[:space:]]*(```|~~~)/ { fence = !fence; next } !fence' |
  perl -0pe 's/<!--.*?(-->|\z)//gs' |
  grep -oiE '(^|[^[:alnum:]])part of[[:space:]]+([A-Za-z0-9._/-]+)?#[1-9][0-9]*' |
  sed -E 's/^[^Pp]?[Pp][Aa][Rr][Tt] [Oo][Ff][[:space:]]+//' | awk '!seen[tolower($0)]++')

((${#refs[@]})) || echo "No Part of #N in the PR body." >>"$summary"

rc=0
declare -A done_n=()
for ref in "${refs[@]}"; do
  repo_part=$(lc "${ref%%#*}") n=${ref##*#}
  if [[ -n $repo_part && $repo_part != $(lc "$name") && $repo_part != $(lc "$REPO") ]]; then
    echo "- $ref skipped, other repo (this token labels $REPO only)" >>"$summary"
    continue
  fi
  [[ -z ${done_n[$n]:-} ]] || continue # #N, name#N and owner/name#N are the same issue
  done_n[$n]=1
  if ! json=$(gh api "repos/$REPO/issues/$n"); then
    echo "- #$n cannot read the issue" >>"$summary"
    rc=1
    continue
  fi
  if [[ $(jq -r 'has("pull_request")' <<<"$json") == true || $(jq -r .state <<<"$json") != open ]]; then
    echo "- #$n skipped, not an open issue" >>"$summary"
    continue
  fi
  # ac-lint exits 0 (no tick without evidence) or 1 (some), always after its "N boxes" line.
  # Anything else is a crash: fail closed, label ac-pending so the lead looks, and fail the job.
  lrc=0
  out=$(jq -r '.body // ""' <<<"$json" | "$lint" --file /dev/stdin) || lrc=$?
  if ((lrc > 1)) || ! grep -qE '^[0-9]+ boxes, ' <<<"$out"; then
    echo "#$n: ac-lint failed (exit $lrc); marking ac-pending" >&2
    if gh api -X POST "repos/$REPO/issues/$n/labels" -f 'labels[]=ac-pending' >/dev/null; then
      echo "- #$n ac-lint failed (exit $lrc), labeled ac-pending" >>"$summary"
    else
      echo "- #$n ac-lint failed (exit $lrc), label write failed" >>"$summary"
    fi
    rc=1
    continue
  fi
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
