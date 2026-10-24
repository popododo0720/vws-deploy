#!/usr/bin/env bash
# Rebuild a stale PR branch on the current main by cherry-picking, never rewriting pushed history.
# Vendored from the vws-rebuild-pr workflow (vws-deploy#398); rebuild_job.py runs it for Rule E.
set -euo pipefail

usage() {
  cat <<'U'
Usage: rebuild-pr.sh --repo-dir CLONE --old BRANCH --new BRANCH-vN --worktree DIR
                     [--base origin/main] [--commits "SHA ..."] [--dry-run]

Fetches, creates DIR as a new worktree on BASE, and cherry-picks the old PR's commits (default:
every non-merge commit in BASE..origin/OLD, oldest first) with the project committer identity.
Stops on a conflict and leaves DIR for you to resolve, then `git cherry-pick --continue`.
Never pushes. Prints the push, PR and supersede commands to run after review.
U
}
repo="" old="" new="" wt="" base=origin/main commits="" dry=0
while (($#)); do
  case $1 in
    --repo-dir) repo=$2; shift 2 ;; --old) old=$2; shift 2 ;; --new) new=$2; shift 2 ;;
    --worktree) wt=$2; shift 2 ;; --base) base=$2; shift 2 ;; --commits) commits=$2; shift 2 ;;
    --dry-run) dry=1; shift ;; -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n $repo && -n $old && -n $new && -n $wt ]] || { usage >&2; exit 2; }
[[ $new =~ -v[0-9]+$ ]] || { echo "--new must end in -vN" >&2; exit 2; }
[[ ! -e $wt ]] || { echo "worktree path exists: $wt" >&2; exit 2; }
g() { git -C "$repo" "$@"; }
((dry)) || g fetch -q origin
! g show-ref -q --verify "refs/heads/$new" || { echo "branch exists: $new" >&2; exit 2; }
g rev-parse -q --verify "$base^{commit}" >/dev/null || { echo "bad base: $base" >&2; exit 2; }
src="origin/$old"; g rev-parse -q --verify "$src^{commit}" >/dev/null || src=$old
if [[ -z $commits ]]; then commits=$(g rev-list --reverse --no-merges "$base..$src" | tr '\n' ' '); fi
[[ -n ${commits// /} ]] || { echo "nothing to cherry-pick from $src onto $base" >&2; exit 1; }
echo "base $base $(g rev-parse --short "$base"); picking from $src: $commits"
if ((dry)); then echo "dry-run: git worktree add -b $new $wt $base; git cherry-pick $commits"; exit 0; fi

export GIT_COMMITTER_NAME=popododo0720 GIT_COMMITTER_EMAIL=78542988+popododo0720@users.noreply.github.com
g worktree add -q -b "$new" "$wt" "$base"
# shellcheck disable=SC2086 # word splitting of the SHA list is intended
git -C "$wt" cherry-pick $commits || { echo "conflict: resolve in $wt, then git -C $wt cherry-pick --continue" >&2; exit 1; }
git -C "$wt" log --format='%h %s' "$base..HEAD"
cat <<N
Next, after the handoff checks pass (no force-push; the old branch stays as it is):
  git -C $wt push -u origin $new
  gh pr create -R OWNER/REPO --base main --head $new --title "..." --body-file BODY.md
  gh pr close OLD_PR -R OWNER/REPO --comment "Superseded by #NEW_PR (same commits cherry-picked onto $(g rev-parse --short "$base"))."
N
