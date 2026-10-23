#!/usr/bin/env bash
# Codex review round cap per issue, not per PR (vws-deploy#403).
# The box skill vws-verify-verdict/codex-review.sh calls this before each review. Counting reads
# local files only (no network): a sidecar index OUT_DIR/rounds.tsv keeps one line per round,
# "<repo>\t<issue>\t<pr>\t<sha7>", so a rebuilt or rework PR for the same issue keeps the count.
# Review file names stay <repo>-<pr>-<sha7>.md (the verify-gate path rule in AGENTS.md).
set -euo pipefail

usage() {
  cat <<'U'
Usage: codex-round-count.sh check  --out-dir DIR --repo REPO --issue N --pr N [--cap N]
       codex-round-count.sh record --out-dir DIR --repo REPO --issue N --pr N --sha SHA
       codex-round-count.sh check-and-record --out-dir DIR --repo REPO --issue N --pr N --sha SHA [--cap N]
       codex-round-count.sh count  --out-dir DIR --repo REPO --issue N --pr N
       codex-round-count.sh issue-from-body < PR_BODY

check   exits 0 while the issue has fewer rounds than the cap, 1 at or past it.
record  adds one round to OUT_DIR/rounds.tsv; recording the same repo/pr/sha again is a no-op.
check-and-record runs check and record under flock on OUT_DIR/rounds.tsv.lock (waits up to 30s);
        at the cap it exits 1 and records nothing. Use it so parallel reviews cannot pass the cap.
count   prints the number of rounds for the issue.
issue-from-body prints N from the first "Part of #N" or "Closes/Fixes/Resolves #N" line; exit 1 if none.
Rounds = index lines for REPO and ISSUE, plus unindexed legacy files OUT_DIR/REPO-PR-*.md for this PR.
Cap: --cap, else CODEX_ROUND_CAP, else 3.
U
}

die() { echo "codex-round-count: $*" >&2; exit 2; }

cmd=${1:-}
[[ -n $cmd ]] || { usage >&2; exit 2; }
shift
case $cmd in
  -h | --help) usage; exit 0 ;;
  issue-from-body)
    n=$(grep -oiE '(part of|closes|fixes|resolves) #[0-9]+' | head -n1 | grep -oE '[0-9]+$' || true)
    [[ $n =~ ^[1-9][0-9]*$ ]] || { echo "codex-round-count: no 'Part of #N' or 'Closes #N' in the PR body" >&2; exit 1; }
    echo "$n"
    exit 0 ;;
  check | record | check-and-record | count) ;;
  *) die "unknown command: $cmd" ;;
esac

out="" repo="" issue="" pr="" sha="" cap=${CODEX_ROUND_CAP:-3}
while (($#)); do
  [[ $# -ge 2 ]] || die "missing value for $1"
  case $1 in
    --out-dir) out=$2 ;; --repo) repo=$2 ;; --issue) issue=$2 ;; --pr) pr=$2 ;;
    --sha) sha=$2 ;; --cap) cap=$2 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift 2
done
[[ -n $out ]] || die "--out-dir is required"
[[ $repo =~ ^[A-Za-z0-9._-]+$ ]] || die "--repo must match [A-Za-z0-9._-]+, got '$repo'"
[[ $issue =~ ^[1-9][0-9]*$ ]] || die "--issue must be a positive integer, got '$issue'"
[[ $pr =~ ^[1-9][0-9]*$ ]] || die "--pr must be a positive integer, got '$pr'"
[[ $cap =~ ^[1-9][0-9]*$ ]] || die "cap must be a positive integer, got '$cap'"
index=$out/rounds.tsv

count_rounds() {
  local indexed=0 legacy=0 f s
  if [[ -f $index ]]; then
    indexed=$(awk -F'\t' -v r="$repo" -v i="$issue" '$1 == r && $2 == i && !seen[$3 FS $4]++' "$index" | wc -l)
  fi
  # Reviews saved before the index existed still count against this PR's issue.
  for f in "$out/$repo-$pr"-*.md; do
    [[ -e $f ]] || continue
    s=${f##*-}; s=${s%.md}
    if [[ ! -f $index ]] || ! awk -F'\t' -v r="$repo" -v p="$pr" -v s="$s" '$1 == r && $3 == p && $4 == s { found = 1 } END { exit !found }' "$index"; then
      legacy=$((legacy + 1))
    fi
  done
  echo $((indexed + legacy))
}

recorded_head() {
  [[ -f $index ]] && awk -F'\t' -v r="$repo" -v p="$pr" -v s="$sha" '$1 == r && $3 == p && $4 == s { found = 1 } END { exit !found }' "$index"
}

do_check() {
  local n
  n=$(count_rounds)
  if ((n >= cap)); then
    echo "issue $repo#$issue already had $n Codex rounds (max $cap) across its PRs: split the work or move the rest to issues" >&2
    return 1
  fi
  echo "issue $repo#$issue: $n of $cap Codex rounds used"
}

do_record() {
  if recorded_head; then
    echo "already recorded: $repo#$pr $sha"
    return 0
  fi
  printf '%s\t%s\t%s\t%s\n' "$repo" "$issue" "$pr" "$sha" >>"$index"
  echo "recorded: $repo issue #$issue PR #$pr $sha"
}

if [[ $cmd == record || $cmd == check-and-record ]]; then
  [[ $sha =~ ^[0-9a-f]{7,40}$ ]] || die "--sha must be 7-40 lowercase hex, got '$sha'"
  sha=${sha:0:7}
fi

case $cmd in
  count) count_rounds ;;
  check) do_check || exit 1 ;;
  record) mkdir -p "$out"; do_record ;;
  check-and-record)
    mkdir -p "$out"
    exec 9>"$index.lock"
    flock -w 30 9 || die "timed out waiting for the lock $index.lock"
    # A head already recorded is a retry of the same round, not a new one.
    if recorded_head; then
      echo "already recorded: $repo#$pr $sha"
      exit 0
    fi
    do_check || exit 1
    do_record ;;
esac
