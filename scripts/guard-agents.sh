#!/usr/bin/env bash
# Insider-risk guards that need no repo-specific paths. Copy this file as-is into another repo.
#
#   guard-agents.sh agents [FILE]           FILE (default AGENTS.md) has the guardrail section and items 1-7
#   guard-agents.sh rules [FILE] [HEADING]  every row of the rule table under HEADING names an enforcer; prints the Judgment count
#   guard-agents.sh judgment [FILE] [HEADING] alias of rules
#   guard-agents.sh identity [SUMFILE]      this file's sha256 equals SUMFILE (default scripts/guard-agents.sh.sha256)
#   guard-agents.sh pr-base BASE_REF        a pull request must target the main branch
#   guard-agents.sh pr-body [FILE]          the pull request text (FILE or $PR_BODY) has "Part of #N" and no closing keyword
#   guard-agents.sh commit-msg FILE         the commit message has no closing keyword (Closes/Fixes/Resolves #N)
#   guard-agents.sh pr-commits BASE HEAD    every commit in BASE..HEAD passes commit-msg
#   guard-agents.sh push-watch BEFORE AFTER every first-parent commit pushed to main came from a pull request
#   guard-agents.sh correct [DIR]           DIR (default .) has the /correct section in AGENTS.md, the incident
#                                           issue template with its required fields, and the incident label
#   guard-agents.sh selftest                run the guards on bad inputs; every one must fail
#
# Environment: GUARD_MAIN_BRANCH (default main), GUARD_HEADING (default "## Insider-risk guardrails"),
# GUARD_ITEMS (default 7), GUARD_RULES_HEADING (default: the first of "## Rules" and "## 규칙표" in FILE),
# GITHUB_REPOSITORY and an authenticated gh for push-watch.
set -euo pipefail
export LC_ALL=C

MAIN=${GUARD_MAIN_BRANCH:-main}
HEADING=${GUARD_HEADING:-## Insider-risk guardrails}
ITEMS=${GUARD_ITEMS:-7}

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

# The rule table under HEADING has two columns, rule | enforcer. An enforcer cell is never empty;
# a rule without a test, lint or CI step says so with a cell that starts with "Judgment".
# HEADING: the argument, else GUARD_RULES_HEADING, else "## Rules" or "## 규칙표", whichever FILE has.
cmd_rules() {
  local file=${1:-AGENTS.md} heading=${2:-${GUARD_RULES_HEADING:-}} h rows
  [[ -f $file ]] || die "rules: ${file} does not exist"
  if [[ -z $heading ]]; then
    for h in "## Rules" "## 규칙표"; do
      if grep -q -x -F -- "$h" "$file"; then heading=$h; break; fi
    done
    [[ -n $heading ]] || die "rules: ${file} has no '## Rules' or '## 규칙표' heading"
  fi
  grep -q -x -F -- "$heading" "$file" || die "rules: ${file} has no '${heading}' heading"
  rows=$(awk -v h="$heading" '
    $0 == h { on = 1; next }
    on && /^## / { exit }
    on && /^\|/ { print }' "$file")
  [[ -n $rows ]] || die "rules: '${heading}' has no table"
  awk -F'|' 'NR == 1 && NF != 4 { exit 1 } NR == 2 && $2 !~ /^[[:space:]]*:?-+:?[[:space:]]*$/ { exit 1 }' <<<"$rows" ||
    die "rules: '${heading}' needs a two-column '| Rule | Enforcer |' table"
  rows=$(tail -n +3 <<<"$rows")
  [[ -n $rows ]] || die "rules: '${heading}' has no rule rows"
  awk -F'|' 'NF != 4 || $3 ~ /^[[:space:]]*$/ { print "rules: row without an enforcer: " $0; bad = 1 } END { exit bad }' <<<"$rows" >&2 ||
    die "rules: every row needs an enforcer (test, lint, CI step) or 'Judgment'"
  echo "rules: $(wc -l <<<"$rows") rows, $(awk -F'|' '$3 ~ /^[[:space:]]*Judgment/' <<<"$rows" | wc -l) Judgment"
}

# Every repo carries a byte-identical copy of the vws-deploy file. SUMFILE pins its sha256 (sha256sum format).
# vws-deploy CI checks SUMFILE against the file, so a change to the canonical file updates SUMFILE in the
# same pull request; other repos copy both files unchanged.
cmd_identity() {
  local sumfile=${1:-scripts/guard-agents.sh.sha256} self want got
  self=${GUARD_SELF:-${BASH_SOURCE[0]}}
  [[ -f $sumfile ]] || die "identity: ${sumfile} does not exist"
  want=$(awk 'NR == 1 { print $1 }' "$sumfile")
  [[ $want =~ ^[0-9a-f]{64}$ ]] || die "identity: ${sumfile} has no sha256"
  got=$(sha256sum "$self" | cut -d' ' -f1)
  [[ $got == "$want" ]] ||
    die "identity: ${self} (sha256 ${got}) is not the canonical vws-deploy copy (${want}); copy scripts/guard-agents.sh and its .sha256 from vws-deploy main, do not edit them here"
  echo "identity: ${self} is the canonical copy (${want:0:12})"
}

cmd_pr_base() {
  local base=${1:-}
  [[ -n $base ]] || die "pr-base: needs the base ref"
  [[ $base == "$MAIN" ]] || die "pr-base: this pull request targets '${base}', not '${MAIN}'. Do not stack pull requests."
}

# A closing keyword followed by an issue reference: GitHub would close that issue on merge.
CLOSING_RE='(^|[^[:alnum:]])(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]*:?[[:space:]]+([A-Za-z0-9._/-]+)?#[0-9]+'
# "Part of #N" or "Part of owner/repo#N". #TBD is allowed in commits only, until the issue exists.
PART_OF_RE='(^|[^[:alnum:]])part of[[:space:]]+([A-Za-z0-9._/-]+)?#[0-9]+'

# Work items are tracked on parent issues that a human closes. No PR or commit may close an
# issue automatically, so closing keywords are banned outright. Telling a parent issue from a
# leaf would need the GitHub API (sub-issues or a tracking label) in every hook and run; a
# plain ban needs no token, works offline and cannot be fooled by a missing label.
cmd_pr_body() {
  local body=${PR_BODY:-}
  if [[ -n ${1:-} ]]; then
    [[ -f $1 ]] || die "pr-body: ${1} does not exist"
    body=$(cat "$1")
  fi
  ! grep -q -i -E "$CLOSING_RE" <<<"$body" ||
    die "pr-body: do not close issues from a pull request (found Closes/Fixes/Resolves #N); write 'Part of #N'"
  grep -q -i -E "$PART_OF_RE" <<<"$body" ||
    die "pr-body: the pull request text needs 'Part of #N'"
}

cmd_commit_msg() {
  local file=${1:-} msg
  [[ -n $file && -f $file ]] || die "commit-msg: needs the message file"
  # Lines starting with # are git's comments and never reach the commit.
  msg=$(grep -v '^#' "$file" || true)
  ! grep -q -i -E "$CLOSING_RE" <<<"$msg" ||
    die "commit-msg: do not close issues from a commit (found Closes/Fixes/Resolves #N); write 'Part of #N'"
}

cmd_pr_commits() {
  local base=${1:-} head=${2:-} sha tmp
  [[ -n $base && -n $head ]] || die "pr-commits: needs BASE and HEAD"
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' RETURN
  for sha in $(git rev-list --no-merges "${base}..${head}"); do
    git log -1 --format=%B "$sha" >"$tmp"
    (cmd_commit_msg "$tmp") 2>/dev/null || die "pr-commits: ${sha:0:12} closes an issue; write 'Part of #N'"
  done
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

CORRECT_HEADING="## /correct 교정 루프"
CORRECT_FIELDS="what-happened impact detection root-cause catching-check follow-ups"

cmd_correct() {
  local dir=${1:-.} tpl labels id
  tpl="${dir}/.github/ISSUE_TEMPLATE/incident.yml"
  labels="${dir}/.github/labels.yml"
  [[ -f ${dir}/AGENTS.md ]] || die "correct: ${dir}/AGENTS.md does not exist"
  grep -q -x -F -- "$CORRECT_HEADING" "${dir}/AGENTS.md" || die "correct: AGENTS.md has no '${CORRECT_HEADING}' heading"
  # Empty: no non-blank line before the next # or ## heading; ### subheadings and code fences are content.
  awk -v h="$CORRECT_HEADING" '$0 == h { s = 1; next } s && /^##?([[:space:]]|$)/ { exit } s && /[^[:space:]]/ { f = 1; exit } END { exit !f }' \
    "${dir}/AGENTS.md" || die "correct: AGENTS.md '${CORRECT_HEADING}' section is empty"
  [[ -f $tpl ]] || die "correct: ${tpl} does not exist"
  grep -q -E '^labels:.*\bincident\b' "$tpl" || die "correct: ${tpl} does not apply the incident label"
  for id in $CORRECT_FIELDS; do
    grep -q -E "^[[:space:]]+id:[[:space:]]+${id}[[:space:]]*$" "$tpl" || die "correct: ${tpl} has no field '${id}'"
  done
  [[ -f $labels ]] || die "correct: ${labels} does not exist"
  grep -q -E '^- name:[[:space:]]+incident[[:space:]]*$' "$labels" || die "correct: ${labels} does not define the incident label"
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

  local rules="$tmp/rules.md"
  printf '# t\n## Rules\n\n| Rule | Enforcer |\n| --- | --- |\n| a | `TestA` |\n| b | Judgment. why |\n\n## next\n| x | |\n' >"$rules"
  expect_pass "every rule has an enforcer" cmd_rules "$rules"
  [[ $(cmd_rules "$rules") == "rules: 2 rows, 1 Judgment" ]] || die "selftest: Judgment count is wrong"
  echo "ok: Judgment count"
  [[ $(bash "$0" judgment "$rules") == "rules: 2 rows, 1 Judgment" ]] || die "selftest: judgment is not an alias of rules"
  echo "ok: judgment is an alias of rules"
  sed 's/^## Rules$/## 규칙표/; s/| Rule | Enforcer |/| 규칙 | 강제 |/' "$rules" >"$tmp/rules-ko.md"
  expect_pass "the Korean heading is found" cmd_rules "$tmp/rules-ko.md"
  sed 's/^## Rules$/## Policy/' "$rules" >"$tmp/rules-arg.md"
  expect_pass "heading from the argument" cmd_rules "$tmp/rules-arg.md" "## Policy"
  expect_pass "heading from GUARD_RULES_HEADING" env GUARD_RULES_HEADING="## Policy" bash "$0" rules "$tmp/rules-arg.md"
  expect_fail "a heading the file does not have" cmd_rules "$rules" "## Policy"
  sed 's/^| a | `TestA` |$/| a |  |/' "$rules" >"$tmp/norule.md"
  expect_fail "a rule without an enforcer" cmd_rules "$tmp/norule.md"
  sed 's/^| a | `TestA` |$/| a | `TestA` | extra |/' "$rules" >"$tmp/threecol.md"
  expect_fail "a row with three columns" cmd_rules "$tmp/threecol.md"
  sed 's/| Rule | Enforcer |/| Rule | Enforcer | Note |/' "$rules" >"$tmp/header.md"
  expect_fail "a three-column header" cmd_rules "$tmp/header.md"
  sed 's/^## Rules$/## other/' "$rules" >"$tmp/norules.md"
  expect_fail "no rule table heading" cmd_rules "$tmp/norules.md"
  printf '# t\n## Rules\n\n| Rule | Enforcer |\n| --- | --- |\n' >"$tmp/emptyrules.md"
  expect_fail "an empty rule table" cmd_rules "$tmp/emptyrules.md"

  cp "$0" "$tmp/copy.sh"
  sha256sum "$tmp/copy.sh" >"$tmp/copy.sha256"
  expect_pass "an identical copy" env GUARD_SELF="$tmp/copy.sh" bash "$0" identity "$tmp/copy.sha256"
  echo "# local edit" >>"$tmp/copy.sh"
  expect_fail "an edited copy" env GUARD_SELF="$tmp/copy.sh" bash "$0" identity "$tmp/copy.sha256"
  echo "not-a-hash  x" >"$tmp/bad.sha256"
  expect_fail "a sum file without a hash" bash "$0" identity "$tmp/bad.sha256"
  expect_fail "a missing sum file" bash "$0" identity "$tmp/none.sha256"

  expect_pass "base is main" cmd_pr_base "$MAIN"
  expect_fail "base is a stack branch" cmd_pr_base "feature-stack-base"
  expect_fail "empty base" cmd_pr_base ""

  expect_pass "Part of #N" env PR_BODY="Part of #12" bash "$0" pr-body
  expect_pass "part of owner/repo#N" env PR_BODY="intro
part of acme/repo#7" bash "$0" pr-body
  expect_fail "Closes #N" env PR_BODY="Part of #3
Closes #12" bash "$0" pr-body
  expect_fail "fixes owner/repo#N" env PR_BODY="Part of #3
fixes acme/repo#7" bash "$0" pr-body
  expect_fail "Resolves: #N" env PR_BODY="Part of #3 Resolves: #7" bash "$0" pr-body
  expect_fail "no Part of" env PR_BODY="mentions #12 only" bash "$0" pr-body
  expect_fail "empty body" env PR_BODY="" bash "$0" pr-body
  expect_fail "Part of without number" env PR_BODY="Part of the gap" bash "$0" pr-body
  expect_fail "Part of #TBD in a pull request" env PR_BODY="Part of #TBD" bash "$0" pr-body

  printf 'ci: x\n\nPart of #12\n' >"$tmp/msg-ok"
  expect_pass "commit with Part of #N" cmd_commit_msg "$tmp/msg-ok"
  printf 'ci: x\n\nPart of #TBD\n' >"$tmp/msg-tbd"
  expect_pass "commit with Part of #TBD" cmd_commit_msg "$tmp/msg-tbd"
  printf 'ci: x\n\nCloses #12\n' >"$tmp/msg-closes"
  expect_fail "commit with Closes #N" cmd_commit_msg "$tmp/msg-closes"
  printf 'docs: y (fixes acme/repo#7)\n' >"$tmp/msg-fixes"
  expect_fail "subject with fixes owner/repo#N" cmd_commit_msg "$tmp/msg-fixes"
  printf 'ci: x\n# Closes #12 in a git comment line\n' >"$tmp/msg-comment"
  expect_pass "Closes in a git comment line" cmd_commit_msg "$tmp/msg-comment"

  # correct: a complete repo passes; each missing piece fails.
  local cr="$tmp/correct"
  mkdir -p "$cr/.github/ISSUE_TEMPLATE"
  printf '# t\n%s\n1. x\n' "$CORRECT_HEADING" >"$cr/AGENTS.md"
  { echo "name: incident"; echo "labels: [incident]"; echo "body:"; for id in $CORRECT_FIELDS; do echo "  - type: textarea"; echo "    id: ${id}"; done; } >"$cr/.github/ISSUE_TEMPLATE/incident.yml"
  printf -- '- name: incident\n  color: "b60205"\n' >"$cr/.github/labels.yml"
  expect_pass "complete /correct loop" cmd_correct "$cr"
  local cb="$tmp/correct-bad"
  rm -rf "$cb"; cp -r "$cr" "$cb"; sed -i '/correct/d' "$cb/AGENTS.md"
  expect_fail "no /correct section" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; printf '# t\n%s\n\n## next\n1. x\n' "$CORRECT_HEADING" >"$cb/AGENTS.md"
  expect_fail "/correct heading with an empty body" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; printf '# t\n%s\n\n### 절차\n\n## next\n' "$CORRECT_HEADING" >"$cb/AGENTS.md"
  expect_pass "/correct body with only a ### subheading" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; printf '# t\n%s\n```sh\n# gh issue create --template incident.yml\n```\n## next\n' "$CORRECT_HEADING" >"$cb/AGENTS.md"
  expect_pass "/correct body with a fenced '#' line" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; printf '# t\n%s\n\n\n# top\n1. x\n' "$CORRECT_HEADING" >"$cb/AGENTS.md"
  expect_fail "/correct body empty before a # heading" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; sed -i '/^labels:/d' "$cb/.github/ISSUE_TEMPLATE/incident.yml"
  expect_fail "template missing only the labels: line" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; sed -i '/id: catching-check/d' "$cb/.github/ISSUE_TEMPLATE/incident.yml"
  expect_fail "template without the catching-check field" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; sed -i 's/^labels:.*/labels: [bug]/' "$cb/.github/ISSUE_TEMPLATE/incident.yml"
  expect_fail "template without the incident label" cmd_correct "$cb"
  rm -rf "$cb"; cp -r "$cr" "$cb"; rm "$cb/.github/labels.yml"
  expect_fail "no labels file" cmd_correct "$cb"

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
  git -C "$repo" checkout -q -b commits "$c3"
  echo y >"$repo/h"
  git -C "$repo" add h
  git -C "$repo" commit -q -m "ci: y" -m "Part of #4"
  expect_pass "commits with Part of" bash -c 'cd "$1" && bash "$2" pr-commits "$3" HEAD' _ "$repo" "$OLDPWD_SCRIPT" "$c3"
  git -C "$repo" commit -q --allow-empty -m "ci: z" -m "Closes #4"
  expect_fail "a commit that closes an issue" bash -c 'cd "$1" && bash "$2" pr-commits "$3" HEAD' _ "$repo" "$OLDPWD_SCRIPT" "$c3"
  echo "selftest passed"
}

command_name=${1:-}
shift || true
case "$command_name" in
  agents) cmd_agents "$@" ;;
  pr-base) cmd_pr_base "$@" ;;
  pr-body) cmd_pr_body "$@" ;;
  rules | judgment) cmd_rules "$@" ;;
  identity) cmd_identity "$@" ;;
  commit-msg) cmd_commit_msg "$@" ;;
  pr-commits) cmd_pr_commits "$@" ;;
  push-watch) cmd_push_watch "$@" ;;
  correct) cmd_correct "$@" ;;
  selftest) cmd_selftest ;;
  *) die "usage: guard-agents.sh <agents [FILE]|pr-base BASE|rules [FILE] [HEADING]|judgment [FILE] [HEADING]|identity [SUMFILE]|pr-body [FILE]|commit-msg FILE|pr-commits BASE HEAD|push-watch BEFORE AFTER|correct [DIR]|selftest>" ;;
esac
