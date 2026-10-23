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

# The refs guard-agents.sh pr-body accepts, read with its own code (vws-deploy#418): visible_text drops
# HTML comments and code blocks, PART_OF_RE matches #N, name#N and owner/name#N anywhere in a line. Only
# those two definitions are loaded (the script's command dispatch is not run), so there is no second copy.
guard=${GUARD_AGENTS:-$here/../../scripts/guard-agents.sh}
unset PART_OF_RE
unset -f visible_text
if ! defs=$(sed -n -e '/^PART_OF_RE=/p' -e '/^visible_text() {$/,/^}$/p' "$guard" 2>/dev/null) ||
  ! eval "$defs" || [[ -z ${PART_OF_RE:-} ]] || ! declare -F visible_text >/dev/null; then
  echo "cannot load visible_text and PART_OF_RE from guard-agents ($guard); not reading the PR body" >&2
  echo "- cannot load guard-agents ($guard)" >>"$summary"
  exit 1
fi
body=$(cat "$PR_BODY_FILE")
mapfile -t refs < <(LC_ALL=C visible_text <<<"$body" | grep -oiE "$PART_OF_RE" |
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
  # ac-lint exits 0 (no tick without evidence) or 1 (some), after exactly one "N boxes" line.
  # Anything else is a crash: fail closed, label ac-pending so the lead looks, and fail the job.
  lrc=0
  out=$(jq -r '.body // ""' <<<"$json" | "$lint" --file /dev/stdin) || lrc=$?
  if ((lrc > 1)) || [[ $(grep -cE '^[0-9]+ boxes, ' <<<"$out" || true) != 1 ]]; then
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
