#!/usr/bin/env bash
# Insider-risk guards that need no repo-specific paths. Copy this file as-is into another repo.
#
#   guard-agents.sh agents [FILE]           FILE (default AGENTS.md) has the guardrail section and items 1-7
#   guard-agents.sh pr-base BASE_REF        a pull request must target the main branch
#   guard-agents.sh pr-body [FILE]          the pull request text (FILE or $PR_BODY) has "Closes #N"
#   guard-agents.sh push-watch BEFORE AFTER every first-parent commit pushed to main came from a pull request
#   guard-agents.sh verify-gate HEAD LABELS [FILE]
#                                           the pull request is verify-approved for HEAD: LABELS (comma list) has
#                                           verify-approved and the text (FILE or $PR_TEXT: body and comments) has a
#                                           "Verified-by: vws-verify" section naming HEAD with evidence for every
#                                           QUALITY.md item
#   guard-agents.sh selftest                run the guards on bad inputs; every one must fail
#
# Environment: GUARD_MAIN_BRANCH (default main), GUARD_HEADING (default "## Insider-risk guardrails"),
# GUARD_ITEMS (default 7), GITHUB_REPOSITORY and an authenticated gh for push-watch,
# GUARD_VERIFY_LABEL (default verify-approved), GUARD_QUALITY_ITEMS (default 9, the QUALITY.md checklist).
set -euo pipefail
export LC_ALL=C

MAIN=${GUARD_MAIN_BRANCH:-main}
HEADING=${GUARD_HEADING:-## Insider-risk guardrails}
ITEMS=${GUARD_ITEMS:-7}
VERIFY_LABEL=${GUARD_VERIFY_LABEL:-verify-approved}
QUALITY_ITEMS=${GUARD_QUALITY_ITEMS:-9}

die() {
  echo "$*" >&2
  exit 1
}

cmd_agents() {
  local file=${1:-AGENTS.md} section n
  [[ -f $file ]] || die "agents: ${file} does not exist"
  grep -q -x -F -- "$HEADING" "$file" || die "agents: ${file} has no '${HEADING}' heading"
  section=$(awk -v h="$HEADING" '
    $0 == h { on = 1; next }
    on && /^## / { exit }
    on { print }' "$file")
  for ((n = 1; n <= ITEMS; n++)); do
    [[ $(grep -c -E "^${n}\. +[^ ]" <<<"$section") -eq 1 ]] ||
      die "agents: '${HEADING}' needs exactly one item numbered ${n}"
  done
  if grep -q -E "^$((ITEMS + 1))\. " <<<"$section"; then
    die "agents: '${HEADING}' has more than ${ITEMS} numbered items"
  fi
}

cmd_pr_base() {
  local base=${1:-}
  [[ -n $base ]] || die "pr-base: needs the base ref"
  [[ $base == "$MAIN" ]] || die "pr-base: this pull request targets '${base}', not '${MAIN}'. Do not stack pull requests."
}

cmd_pr_body() {
  local body=${PR_BODY:-}
  if [[ -n ${1:-} ]]; then
    [[ -f $1 ]] || die "pr-body: ${1} does not exist"
    body=$(cat "$1")
  fi
  grep -q -i -E '(^|[^[:alnum:]])(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]+([A-Za-z0-9._/-]+)?#[0-9]+' <<<"$body" ||
    die "pr-body: the pull request text needs 'Closes #N'"
}

pr_count() {
  local sha=$1 i n
  for i in 1 2 3; do
    n=$(gh api "repos/${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}/commits/${sha}/pulls" --jq 'length') || n=0
    [[ $n -gt 0 ]] && break
    # The association appears a few seconds after a merge.
    [[ $i -lt 3 ]] && sleep "${GUARD_RETRY_SECONDS:-5}"
  done
  echo "$n"
}

cmd_push_watch() {
  local before=${1:-} after=${2:-} zero sha
  local -a commits bad=()
  [[ -n $before && -n $after ]] || die "push-watch: needs BEFORE and AFTER"
  zero=$(printf '0%.0s' {1..40})
  if [[ $before == "$zero" ]] || ! git cat-file -e "${before}^{commit}" 2>/dev/null; then
    commits=("$after")
  else
    git merge-base --is-ancestor "$before" "$after" ||
      die "push-watch: history of '${MAIN}' was rewritten (${before} is not an ancestor of ${after})"
    mapfile -t commits < <(git rev-list --first-parent "${before}..${after}")
  fi
  for sha in "${commits[@]}"; do
    [[ $(pr_count "$sha") -gt 0 ]] || bad+=("$sha")
  done
  [[ ${#bad[@]} -eq 0 ]] || die "push-watch: commits on '${MAIN}' without a pull request: ${bad[*]}"
}

# verify-gate: the approval is bound to one head commit. A push changes the head, so the old section no
# longer matches and the gate is red until vws-verify judges the new head (the label is also removed on
# synchronize by the workflow). Evidence per checklist item: a URL, a path, or "N/A: <reason>".
cmd_verify_gate() {
  local head=${1:-} labels=${2:-} text=${PR_TEXT:-} section n line
  [[ $head =~ ^[0-9a-f]{40}$ ]] || die "verify-gate: needs the full head commit SHA"
  if [[ -n ${3:-} ]]; then
    [[ -f $3 ]] || die "verify-gate: ${3} does not exist"
    text=$(cat "$3")
  fi
  [[ ",${labels}," == *",${VERIFY_LABEL},"* ]] || die "verify-gate: the pull request has no '${VERIFY_LABEL}' label"
  # The section that names this head: from its "Verified-by: vws-verify" line to the next such line or heading.
  section=$(tr -d '`\r' <<<"$text" | awk -v h="$head" '
    /Verified-by:[[:space:]]*vws-verify/ { if (keep) exit; on = 1; buf = ""; next }
    on && /^#/ { if (keep) exit; on = 0; next }
    on { buf = buf $0 "\n"; if ($0 ~ "^[[:space:]]*Head:[[:space:]]*" h "[[:space:]]*$") keep = 1 }
    END { if (keep) printf "%s", buf }')
  [[ -n $section ]] || die "verify-gate: no 'Verified-by: vws-verify' section names the head ${head}; a push after approval needs a new verification"
  for ((n = 1; n <= QUALITY_ITEMS; n++)); do
    line=$(grep -m1 -E "^[[:space:]]*(- )?${n}\.[[:space:]]" <<<"$section" || true)
    line=${line#*"${n}."}
    # "N/A:" needs a reason; otherwise a URL or a file path (dir/file.ext) is the evidence.
    if grep -q -E '^[[:space:]]*N/A' <<<"$line"; then
      grep -q -E '^[[:space:]]*N/A:[[:space:]]*[^[:space:]]' <<<"$line"
    else
      grep -q -E '(https?://[^[:space:]]+|[A-Za-z0-9._-]+/[A-Za-z0-9._/-]*\.[A-Za-z0-9]+)' <<<"$line"
    fi ||
      die "verify-gate: the Verified-by section for ${head:0:12} has no evidence (URL, path or 'N/A: reason') for checklist item ${n}"
  done
}

expect_fail() {
  local label=$1
  shift
  if ("$@") >/dev/null 2>&1; then
    die "selftest: ${label} was accepted"
  fi
  echo "ok: ${label} fails"
}

expect_pass() {
  local label=$1
  shift
  "$@" >/dev/null 2>&1 || die "selftest: ${label} was rejected"
  echo "ok: ${label} passes"
}

cmd_selftest() {
  local tmp good i
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN
  good="$tmp/good.md"
  { echo "# t"; echo "$HEADING"; echo; for ((i = 1; i <= ITEMS; i++)); do echo "${i}. **rule ${i}** text"; done; echo; echo "## next"; } >"$good"
  expect_pass "complete section" cmd_agents "$good"
  expect_fail "missing file" cmd_agents "$tmp/none.md"
  printf '# t\n## other\n1. a\n' >"$tmp/nohead.md"
  expect_fail "missing heading" cmd_agents "$tmp/nohead.md"
  grep -v -E "^3\. " "$good" >"$tmp/noitem.md"
  expect_fail "missing item 3" cmd_agents "$tmp/noitem.md"
  { cat "$good"; echo "3. again"; } >"$tmp/dup.md"
  sed -i "/^## next/d" "$tmp/dup.md"
  expect_fail "duplicate item 3" cmd_agents "$tmp/dup.md"
  awk -v h="$HEADING" '$0 == h { print "## Other"; next } { print }' "$good" >"$tmp/renamed.md"
  expect_fail "renamed heading" cmd_agents "$tmp/renamed.md"

  expect_pass "base is main" cmd_pr_base "$MAIN"
  expect_fail "base is a stack branch" cmd_pr_base "feature-stack-base"
  expect_fail "empty base" cmd_pr_base ""

  expect_pass "Closes #N" env PR_BODY="Closes #12" bash "$0" pr-body
  expect_pass "fixes owner/repo#N" env PR_BODY="intro
fixes acme/repo#7" bash "$0" pr-body
  expect_fail "no closing keyword" env PR_BODY="mentions #12 only" bash "$0" pr-body
  expect_fail "empty body" env PR_BODY="" bash "$0" pr-body
  expect_fail "Closes without number" env PR_BODY="Closes the gap" bash "$0" pr-body

  # push-watch against a throwaway repository and a gh that answers from a file.
  local repo="$tmp/repo" bin="$tmp/bin" c1 c2 c3
  mkdir -p "$repo" "$bin"
  git -C "$repo" init -q -b "$MAIN"
  git -C "$repo" config user.email t@example.invalid
  git -C "$repo" config user.name t
  for i in 1 2 3; do
    echo "$i" >"$repo/f"
    git -C "$repo" add f
    git -C "$repo" commit -q -m "c$i"
  done
  c1=$(git -C "$repo" rev-parse HEAD~2)
  c2=$(git -C "$repo" rev-parse HEAD~1)
  c3=$(git -C "$repo" rev-parse HEAD)
  cat >"$bin/gh" <<'GH'
#!/usr/bin/env bash
# fake gh api repos/R/commits/SHA/pulls --jq length: prints the number of lines for SHA in $FAKE_PULLS
sha=$(sed -n 's#.*/commits/\([0-9a-f]*\)/pulls.*#\1#p' <<<"$*")
grep -c "^${sha}$" "$FAKE_PULLS" || true
GH
  chmod +x "$bin/gh"
  : >"$tmp/pulls"
  printf '%s\n%s\n' "$c2" "$c3" >"$tmp/pulls"
  push() { (cd "$repo" && PATH="$bin:$PATH" FAKE_PULLS="$tmp/pulls" GITHUB_REPOSITORY=o/r GUARD_RETRY_SECONDS=0 bash "$OLDPWD_SCRIPT" push-watch "$@"); }
  export OLDPWD_SCRIPT
  OLDPWD_SCRIPT=$(readlink -f "$0")
  expect_pass "every pushed commit has a pull request" push "$c1" "$c3"
  printf '%s\n' "$c3" >"$tmp/pulls"
  expect_fail "a pushed commit without a pull request" push "$c1" "$c3"
  git -C "$repo" checkout -q -b rewritten "$c1"
  echo x >"$repo/g"
  git -C "$repo" add g
  git -C "$repo" commit -q -m rewrite
  expect_fail "rewritten history" push "$c3" "$(git -C "$repo" rev-parse HEAD)"
  expect_fail "new branch whose head has no pull request" push "$(printf '0%.0s' {1..40})" "$c1"

  # verify-gate: label and a section bound to the head with evidence for every item.
  local h1 h2 ev
  h1=$(printf 'a%.0s' {1..40}); h2=$(printf 'b%.0s' {1..40})
  ev="$tmp/ev.md"
  { echo "Closes #1"; echo; echo "## Verified-by: vws-verify"; echo "Head: ${h1}"
    for ((i = 1; i <= QUALITY_ITEMS; i++)); do echo "${i}. https://example.invalid/log/${i}"; done; } >"$ev"
  expect_pass "label and evidence for the head" cmd_verify_gate "$h1" "bug,${VERIFY_LABEL}" "$ev"
  expect_fail "no label" cmd_verify_gate "$h1" "bug" "$ev"
  expect_fail "a push after approval (new head)" cmd_verify_gate "$h2" "$VERIFY_LABEL" "$ev"
  expect_fail "short SHA" cmd_verify_gate "${h1:0:12}" "$VERIFY_LABEL" "$ev"
  grep -v -E "^4\. " "$ev" >"$tmp/ev4.md"
  expect_fail "missing evidence for item 4" cmd_verify_gate "$h1" "$VERIFY_LABEL" "$tmp/ev4.md"
  sed 's#^6\. .*#6. done#' "$ev" >"$tmp/ev6.md"
  expect_fail "item 6 without a link, path or N/A reason" cmd_verify_gate "$h1" "$VERIFY_LABEL" "$tmp/ev6.md"
  sed 's#^7\. .*#7. N/A: no telemetry in a shell script#; s#^2\. .*#2. tests/vws/run.sh output in evidence/run.log#' "$ev" >"$tmp/evna.md"
  expect_pass "N/A with a reason and a path count as evidence" cmd_verify_gate "$h1" "$VERIFY_LABEL" "$tmp/evna.md"
  sed 's#^7\. .*#7. N/A:#' "$ev" >"$tmp/evna2.md"
  expect_fail "N/A without a reason" cmd_verify_gate "$h1" "$VERIFY_LABEL" "$tmp/evna2.md"
  expect_fail "no section" cmd_verify_gate "$h1" "$VERIFY_LABEL" "$tmp/nohead.md"
  { echo "## Verified-by: vws-verify"; echo "Head: ${h2}"; echo "1. stale"; cat "$ev"; } >"$tmp/two.md"
  expect_pass "a stale section and a current one: the current one counts" cmd_verify_gate "$h1" "$VERIFY_LABEL" "$tmp/two.md"
  echo "selftest passed"
}

command_name=${1:-}
shift || true
case "$command_name" in
  agents) cmd_agents "$@" ;;
  pr-base) cmd_pr_base "$@" ;;
  pr-body) cmd_pr_body "$@" ;;
  push-watch) cmd_push_watch "$@" ;;
  verify-gate) cmd_verify_gate "$@" ;;
  selftest) cmd_selftest ;;
  *) die "usage: guard-agents.sh <agents [FILE]|pr-base BASE|pr-body [FILE]|push-watch BEFORE AFTER|verify-gate HEAD LABELS [FILE]|selftest>" ;;
esac
