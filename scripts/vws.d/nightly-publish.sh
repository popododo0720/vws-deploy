#!/usr/bin/env bash
# summary: push a nightly run's scenario results to the nightly-results data branch, never to main
# Sourced by scripts/vws. Usage: vws nightly-publish --results DIR [--root DIR] [--remote NAME] [--init]
# vws-deploy#290 (Part of #252), design docs/design/nightly-e2e.md "Results branch". Copies every scenario
# result DIR/NAME.json of a `vws nightly` run to metrics/nightly/<run>/ on the orphan branch nightly-results
# and pushes it as one fast-forward commit; <run> is the basename of DIR (e.g. 20261031T130000Z). The branch
# is never merged and never opened as a PR. The checkout at --root (default this repo) keeps its branch,
# index and files. Before it adds a commit it refuses a branch that has any commit it did not write (a
# merge, no Results-Generator trailer, another subject, or a path outside metrics/nightly/). A missing
# branch is created only with --init (once, approved). A rerun of the same run with the same files pushes
# nothing; a push that loses a race is retried on the new head, three times in all.
# Exit: 0 pushed, already there, or no result in DIR (WARN); 1 the branch is refused or the push failed;
# 2 usage; 3 environment (tool missing, --root not a git checkout).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(git jq timeout)

VWS_PUBLISH_BRANCH=nightly-results
VWS_PUBLISH_TRAILER='Results-Generator: vws-nightly-publish'
# Seconds before the second push attempt (doubled before the third). Tests set 0.
VWS_NIGHTLY_PUBLISH_BACKOFF=${VWS_NIGHTLY_PUBLISH_BACKOFF:-5}

vws_publish_foreign() { # ROOT HEAD: prints the first problem with the branch history, or nothing
  local root=$1 head=$2 bad
  bad=$(git -C "$root" rev-list --min-parents=2 -n1 "$head") || return 1
  [[ -z $bad ]] || { echo "${bad:0:12} is a merge"; return 0; }
  bad=$(git -C "$root" log -n1 --format=%h --invert-grep --grep="^${VWS_PUBLISH_TRAILER}\$" "$head") || return 1
  [[ -z $bad ]] || { echo "${bad} has no '${VWS_PUBLISH_TRAILER}' trailer"; return 0; }
  bad=$(git -C "$root" log --format='%h %s' "$head" | grep -Ev '^[0-9a-f]+ metrics: nightly results [0-9]{8}T[0-9]{6}Z$' | head -n1)
  [[ -z $bad ]] || { echo "${bad:0:120} is not a nightly results subject"; return 0; }
  bad=$(git -C "$root" log --format= --name-only "$head" | grep -v -e '^$' -e '^metrics/nightly/' | head -n1)
  [[ -z $bad ]] || echo "a commit touches ${bad:0:200} outside metrics/nightly/"
}

vws_publish_tree() { # ROOT PARENT RUN FILES...: the tree of PARENT (or empty) with metrics/nightly/RUN set to FILES
  local root=$1 parent=$2 run=$3 idx f blob rc=0
  shift 3
  # A private index, so the checkout's own branch, index and files stay as they are.
  idx=$(mktemp) && rm -f "$idx" || return 1
  if [[ -n $parent ]]; then GIT_INDEX_FILE=$idx git -C "$root" read-tree "$parent" || rc=1
  else GIT_INDEX_FILE=$idx git -C "$root" read-tree --empty || rc=1; fi
  ((rc)) || GIT_INDEX_FILE=$idx git -C "$root" rm -r -q --cached --ignore-unmatch -- "metrics/nightly/${run}" >/dev/null || rc=1
  for f in "$@"; do
    ((rc == 0)) || break
    blob=$(git -C "$root" hash-object -w -- "$f") &&
      GIT_INDEX_FILE=$idx git -C "$root" update-index --add --cacheinfo "100644,${blob},metrics/nightly/${run}/${f##*/}" || rc=1
  done
  ((rc)) || GIT_INDEX_FILE=$idx git -C "$root" write-tree || rc=1
  rm -f "$idx"
  return "$rc"
}

vws_cmd_main() {
  local root=$VWS_ROOT results='' remote=origin init='' run f name tree parent commit out rc i problem
  local -a files=() parents=()
  while (($#)); do
    case $1 in
      --root | --results | --remote)
        (($# >= 2)) || vws_usage "$1 needs a value"
        case $1 in
          --root) root=$2 ;;
          --results) results=$2 ;;
          --remote) remote=$2 ;;
        esac
        shift 2 ;;
      --init) init=1; shift ;;
      *) vws_usage "nightly-publish: unknown argument $1" ;;
    esac
  done
  [[ -z $VWS_DRY_RUN ]] || vws_usage "nightly-publish has no --dry-run: it pushes a branch"
  [[ -n $results ]] || vws_usage "nightly-publish needs --results DIR (the vws nightly run directory)"
  [[ $remote =~ ^[A-Za-z0-9._-]{1,64}$ ]] || vws_usage "--remote must be a remote name, not ${remote}"
  run=$(basename "$results")
  [[ $run =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || vws_usage "--results must be a run directory named like 20261031T130000Z, not ${run}"
  [[ -d $results ]] || vws_usage "--results ${results} is not a directory"
  [[ $(git -C "$root" rev-parse --show-toplevel 2>/dev/null) == "$(cd "$root" && pwd -P)" ]] ||
    vws_die_env "--root ${root} is not the top of a git checkout"

  for f in "$results"/*.json; do
    [[ -f $f ]] || continue
    name=$(basename "$f")
    if [[ ! $name =~ ^[a-z0-9][a-z0-9-]{0,62}\.json$ ]]; then
      vws_emit WARN nightly-publish "${name:0:80} is not a scenario result name; not published"
    elif (($(stat -c %s "$f") > 1048576)) || ! jq -e 'type == "object"' "$f" >/dev/null 2>&1 ||
      LC_ALL=C grep -q $'[\x80-\xff]' "$f"; then
      vws_emit FAIL nightly-publish "${name} is not an ASCII JSON object of at most 1 MiB; nothing published"
      return 0
    else
      files+=("$f")
    fi
  done
  if ((${#files[@]} == 0)); then
    vws_emit WARN nightly-publish "no scenario result in ${results}; nothing published"
    return 0
  fi

  local ref="refs/remotes/${remote}/${VWS_PUBLISH_BRANCH}"
  export GIT_TERMINAL_PROMPT=0 # a missing credential fails the attempt instead of waiting for a prompt
  for i in 1 2 3; do
    ((i == 1)) || sleep $((VWS_NIGHTLY_PUBLISH_BACKOFF * (i - 1)))
    rc=0; timeout 120 git -C "$root" ls-remote --exit-code "$remote" "refs/heads/${VWS_PUBLISH_BRANCH}" >/dev/null 2>&1 || rc=$?
    parent='' parents=()
    if ((rc == 0)); then
      rc=0; out=$(timeout 120 git -C "$root" fetch -q "$remote" "+refs/heads/${VWS_PUBLISH_BRANCH}:${ref}" 2>&1) || rc=$?
      ((rc == 0)) || { vws_emit WARN nightly-publish "fetch of ${VWS_PUBLISH_BRANCH} failed: ${out:0:300}"; continue; }
      parent=$(git -C "$root" rev-parse "$ref")
      parents=(-p "$parent")
      problem=$(vws_publish_foreign "$root" "$parent") || problem="cannot read the history of ${parent}"
      if [[ -n $problem ]]; then
        vws_emit FAIL nightly-publish "refused: ${VWS_PUBLISH_BRANCH} has a commit not written by vws nightly-publish: ${problem}"
        return 0
      fi
    elif ((rc == 2)); then
      if [[ -z $init ]]; then
        vws_emit FAIL nightly-publish "${remote} has no ${VWS_PUBLISH_BRANCH} branch; create it once with --init"
        return 0
      fi
    else
      vws_emit WARN nightly-publish "cannot reach ${remote} (exit ${rc})"
      continue
    fi

    tree=$(vws_publish_tree "$root" "$parent" "$run" "${files[@]}") || vws_die_env "cannot stage the results in ${root}"
    if [[ -n $parent && $(git -C "$root" rev-parse "${parent}^{tree}") == "$tree" ]]; then
      vws_emit OK nightly-publish "${VWS_PUBLISH_BRANCH} already holds run ${run}"
      return 0
    fi
    commit=$(printf 'metrics: nightly results %s\n\nScenario results of the vws-test nightly run %s, written by vws nightly-publish.\n\nPart of #252\n%s\n' \
      "$run" "$run" "$VWS_PUBLISH_TRAILER" | git -C "$root" commit-tree "$tree" "${parents[@]}") ||
      vws_die_env "cannot create the results commit"
    # Not forced: a branch that moved since the fetch rejects the push, and the loop starts over on it.
    rc=0; out=$(timeout 120 git -C "$root" push -q "$remote" "${commit}:refs/heads/${VWS_PUBLISH_BRANCH}" 2>&1) || rc=$?
    if ((rc == 0)); then
      vws_emit OK nightly-publish "pushed ${commit:0:12} with ${#files[@]} result(s) to ${VWS_PUBLISH_BRANCH}:metrics/nightly/${run}"
      return 0
    fi
    vws_emit WARN nightly-publish "push attempt ${i} failed: ${out:0:300}"
  done
  vws_emit FAIL nightly-publish "could not push run ${run} to ${VWS_PUBLISH_BRANCH} after 3 attempts"
}
