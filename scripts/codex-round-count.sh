#!/usr/bin/env bash
# Codex review round cap per PR (vws-deploy#579, which supersedes the per-issue cap of #403).
# The box skill vws-verify-verdict/codex-review.sh calls this around each review. Counting reads
# local files only (no network): a sidecar index OUT_DIR/rounds.tsv has a header line and then one
# line per round, "<repo>\t<issue>\t<pr>\t<sha7>". The cap counts unique heads per (repo, pr), so a
# retry of the same head counts once and a new PR for the same issue starts at 0. The issue column
# is for reporting only. Rows written by the per-issue version need no migration: they already
# carry the PR. Review file names stay <repo>-<pr>-<sha7>.md (the verify-gate path rule in AGENTS.md).
# The cap fails closed: a missing, unreadable or corrupt index, or a review file the index does not
# cover, is exit 2, never "0 rounds used".
set -euo pipefail

usage() {
  cat <<'U'
Usage: codex-round-count.sh init     --out-dir DIR
       codex-round-count.sh backfill --out-dir DIR [--repo REPO] [--map REPO#PR=ISSUE]... [--owner OWNER] [--dry-run]
       codex-round-count.sh check    --out-dir DIR --repo REPO --pr N [--issue N] [--cap N]
       codex-round-count.sh count    --out-dir DIR --repo REPO --pr N [--issue N]
       codex-round-count.sh check-and-record --out-dir DIR --repo REPO --issue N --pr N --sha SHA [--cap N]
       codex-round-count.sh record   --out-dir DIR --repo REPO --issue N --pr N --sha SHA
       codex-round-count.sh release  --out-dir DIR --repo REPO --pr N --sha SHA
       codex-round-count.sh issue-from-body < PR_BODY

init     creates OUT_DIR/rounds.tsv (one time). Every other index command needs it.
backfill indexes existing OUT_DIR/<repo>-<pr>-<sha7>.md review files with their issue: --map first,
         else "gh pr view PR -R OWNER/vws-NAME" and issue-from-body (OWNER defaults to popododo0720).
         All files must resolve or nothing is written. --dry-run prints the lines only.
check    exit 0 below the cap, 1 at or past it, for the PR.
count    prints the number of rounds (unique heads) for the PR.
--issue  is only written to new rows (record, check-and-record) for reporting; check and count ignore it.
check-and-record checks and records in one step, under the lock: exit 1 at the cap without a record.
         A head already recorded is a retry: exit 0, no new line.
record   adds one round without the cap check.
release  removes the round for REPO/PR/SHA (when Codex failed before writing a review).
issue-from-body prints N from the first "Part of #N" or "Closes/Fixes/Resolves #N" (also OWNER/REPO#N).
Exit 2: bad input, missing/unreadable/corrupt index, unindexed review files for REPO, lock timeout.
Writes take an exclusive flock on OUT_DIR/rounds.tsv.lock, reads a shared one (30 s wait).
Cap: --cap, else CODEX_ROUND_CAP, else 3.
U
}

die() { echo "codex-round-count: $*" >&2; exit 2; }
header='# codex-round-index v1'
row_re='^[A-Za-z0-9._-]+\t[1-9][0-9]*\t[1-9][0-9]*\t[0-9a-f]{7}$'

issue_from_body() {
  local n
  n=$(grep -oiE '(part of|closes|fixes|resolves) ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+' | head -n1 | grep -oE '[0-9]+$' || true)
  [[ $n =~ ^[1-9][0-9]*$ ]] || { echo "codex-round-count: no 'Part of #N' or 'Closes #N' in the PR body" >&2; return 1; }
  echo "$n"
}

cmd=${1:-}
[[ -n $cmd ]] || { usage >&2; exit 2; }
shift
case $cmd in
  -h | --help) usage; exit 0 ;;
  issue-from-body) issue_from_body; exit $? ;;
  init | backfill | check | count | check-and-record | record | release) ;;
  *) die "unknown command: $cmd" ;;
esac

out="" repo="" issue="" pr="" sha="" cap=${CODEX_ROUND_CAP:-3} owner=popododo0720 dry=0
declare -A map=()
while (($#)); do
  if [[ $1 == --dry-run ]]; then dry=1; shift; continue; fi
  [[ $# -ge 2 ]] || die "missing value for $1"
  case $1 in
    --out-dir) out=$2 ;; --repo) repo=$2 ;; --issue) issue=$2 ;; --pr) pr=$2 ;;
    --sha) sha=$2 ;; --cap) cap=$2 ;; --owner) owner=$2 ;;
    --map)
      [[ $2 =~ ^([A-Za-z0-9._-]+)#([1-9][0-9]*)=([1-9][0-9]*)$ ]] || die "--map must be REPO#PR=ISSUE, got '$2'"
      map["${BASH_REMATCH[1]}#${BASH_REMATCH[2]}"]=${BASH_REMATCH[3]} ;;
    *) die "unknown argument: $1" ;;
  esac
  shift 2
done
[[ -n $out ]] || die "--out-dir is required"
need() { # need repo|issue|pr|sha
  local a
  for a in "$@"; do
    case $a in
      repo) [[ $repo =~ ^[A-Za-z0-9._-]+$ ]] || die "--repo must match [A-Za-z0-9._-]+, got '$repo'" ;;
      issue) [[ $issue =~ ^[1-9][0-9]*$ ]] || die "--issue must be a positive integer, got '$issue'" ;;
      pr) [[ $pr =~ ^[1-9][0-9]*$ ]] || die "--pr must be a positive integer, got '$pr'" ;;
      sha) [[ $sha =~ ^[0-9a-f]{7,40}$ ]] || die "--sha must be 7-40 lowercase hex, got '$sha'"; sha=${sha:0:7} ;;
    esac
  done
}
[[ $cap =~ ^[1-9][0-9]*$ ]] || die "cap must be a positive integer, got '$cap'"
[[ $owner =~ ^[A-Za-z0-9_.-]+$ ]] || die "--owner must match [A-Za-z0-9_.-]+, got '$owner'"
[[ -z $repo ]] || need repo
index=$out/rounds.tsv

lock() { # lock -s|-x
  [[ -d $out ]] || die "output directory missing: $out (run init)"
  exec 9>>"$index.lock" || die "cannot open the lock $index.lock"
  flock "$1" -w 30 9 || die "timed out waiting for the lock $index.lock"
}

# Fails closed on a missing, unreadable or corrupt index (header, row shape, final newline).
validate() {
  [[ -e $index ]] || die "index missing: $index (run 'init' once, then 'backfill')"
  [[ -f $index ]] || die "index is not a regular file: $index"
  [[ -r $index ]] || die "index is not readable: $index"
  local rc=0
  awk -v h="$header" -v re="$row_re" 'NR == 1 { if ($0 != h) { print "line 1: missing header"; exit 3 } next }
    $0 !~ re { print "line " NR ": bad row"; exit 3 }
    END { if (NR == 0) { print "empty file, no header"; exit 3 } }' "$index" >&2 || rc=$?
  ((rc == 0)) || die "index is corrupt or unreadable: $index (awk exit $rc)"
  [[ -z $(tail -c1 "$index") ]] || die "index is corrupt: $index (last line has no newline)"
}

# Review files OUT_DIR/<repo>-<pr>-<sha7>.md for one repo, as "pr<TAB>sha7" lines.
review_files() {
  local r=$1 f name rest
  for f in "$out/$r"-*.md; do
    [[ -e $f ]] || continue
    name=${f##*/}; rest=${name#"$r-"}
    [[ $rest =~ ^([1-9][0-9]*)-([0-9a-f]{7})\.md$ ]] && printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  done
  return 0
}

indexed() { # indexed REPO PR SHA7
  awk -F'\t' -v r="$1" -v p="$2" -v s="$3" 'NR > 1 && $1 == r && $3 == p && $4 == s { f = 1 } END { exit !f }' "$index"
}

# Refuses (exit 2) while any review file for REPO is not in the index: run backfill first.
require_covered() {
  local p s missing=""
  while IFS=$'\t' read -r p s; do
    indexed "$repo" "$p" "$s" || missing+=" $repo-$p-$s.md"
  done < <(review_files "$repo")
  [[ -z $missing ]] || die "review files not in the index:$missing (run backfill)"
}

# Unique heads for (repo, pr). The issue column is never read: the cap is per PR (#579).
count_rounds() {
  awk -F'\t' -v r="$repo" -v p="$pr" 'NR > 1 && $1 == r && $3 == p && !seen[$4]++ { n++ } END { print n + 0 }' "$index"
}

append() { # append REPO ISSUE PR SHA7
  printf '%s\t%s\t%s\t%s\n' "$@" >>"$index" || die "cannot write the index $index"
}

cap_check() { # sets n; exit 1 at the cap
  n=$(count_rounds) || die "cannot read the index $index"
  if ((n >= cap)); then
    echo "PR $repo#$pr already had $n Codex rounds (max $cap): split the work into a new PR or move the rest to issues" >&2
    exit 1
  fi
}

case $cmd in
  init)
    mkdir -p "$out" || die "cannot create $out"
    lock -x
    if [[ -e $index ]]; then validate; echo "index exists: $index"; exit 0; fi
    printf '%s\n' "$header" >"$index" || die "cannot write the index $index"
    echo "created: $index" ;;
  check | count)
    need repo pr
    [[ -z $issue ]] || need issue
    lock -s; validate; require_covered
    if [[ $cmd == count ]]; then count_rounds; exit 0; fi
    cap_check
    echo "PR $repo#$pr: $n of $cap Codex rounds used" ;;
  check-and-record | record)
    need repo issue pr sha
    lock -x; validate; require_covered
    if indexed "$repo" "$pr" "$sha"; then echo "already recorded: $repo#$pr $sha"; exit 0; fi
    if [[ $cmd == check-and-record ]]; then
      cap_check
      echo "PR $repo#$pr: $n of $cap Codex rounds used"
    fi
    append "$repo" "$issue" "$pr" "$sha"
    echo "recorded: $repo issue #$issue PR #$pr $sha" ;;
  release)
    need repo pr sha
    lock -x; validate
    [[ ! -e $out/$repo-$pr-$sha.md ]] || die "review file exists, not releasing: $out/$repo-$pr-$sha.md"
    tmp=$(mktemp "$index.XXXXXX") || die "cannot create a temp file next to $index"
    awk -F'\t' -v r="$repo" -v p="$pr" -v s="$sha" '!(NR > 1 && $1 == r && $3 == p && $4 == s)' "$index" >"$tmp" ||
      { rm -f "$tmp"; die "cannot rewrite the index $index"; }
    mv "$tmp" "$index" || die "cannot replace the index $index"
    echo "released: $repo#$pr $sha" ;;
  backfill)
    lock -x; validate
    if [[ -n $repo ]]; then repos=("$repo"); else
      mapfile -t repos < <(for f in "$out"/*.md; do [[ -e $f ]] || continue; n=${f##*/}
        [[ $n =~ ^([A-Za-z0-9._-]+)-[1-9][0-9]*-[0-9a-f]{7}\.md$ ]] && echo "${BASH_REMATCH[1]}"; done | sort -u)
    fi
    lines=() failed=0
    declare -A seen_issue=()
    for r in "${repos[@]}"; do
      while IFS=$'\t' read -r p s; do
        indexed "$r" "$p" "$s" && continue
        key="$r#$p"
        if [[ -n ${map[$key]:-} ]]; then i=${map[$key]}
        elif [[ -n ${seen_issue[$key]:-} ]]; then i=${seen_issue[$key]}
        else
          gh_repo=$r; [[ $gh_repo == vws-* ]] || gh_repo=vws-$r
          if ! i=$(timeout 30 gh pr view "$p" -R "$owner/$gh_repo" --json body -q .body | issue_from_body); then
            echo "unresolved: $r-$p-$s.md (add --map $r#$p=ISSUE)" >&2; failed=1; continue
          fi
        fi
        seen_issue[$key]=$i
        lines+=("$r"$'\t'"$i"$'\t'"$p"$'\t'"$s")
      done < <(review_files "$r")
    done
    ((failed == 0)) || die "backfill wrote nothing: map the unresolved files and run it again"
    for l in "${lines[@]}"; do
      echo "backfill: $l"
      ((dry)) || printf '%s\n' "$l" >>"$index" || die "cannot write the index $index"
    done
    echo "backfill: ${#lines[@]} rounds $( ((dry)) && echo "would be indexed (dry run)" || echo indexed)" ;;
esac
