#!/usr/bin/env bash
# vws nightly-pr: commits the regenerated feature map to a nightly/<run> branch, pushes only that
# branch, and opens one PR (Part of #252) that links the run log. It never pushes to main and never
# touches the checkout it reads from.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
origin="${WORK}/origin.git" clone="${WORK}/clone"
git init -q --bare -b main "$origin"
git init -q -b main "$clone"
mkdir -p "${clone}/docs"
echo '{"features": []}' >"${clone}/docs/feature-map.json"
echo '# Feature map' >"${clone}/docs/feature-map.md"
git -C "$clone" add docs && git -C "$clone" commit -q -m base
git -C "$clone" remote add origin "$origin" && git -C "$clone" push -q origin main
main_before=$(git -C "$origin" rev-parse refs/heads/main)

run="20261031T130000Z" res="${WORK}/res/20261031T130000Z"
mkdir -p "$res"
jq -n '{scenario: "ec2-run", result: "pass", cluster: "vws-test", finishedAt: "2026-10-31T13:05:00Z",
  evidence: "https://example.invalid/ec2-run"}' >"${res}/ec2-run.json"
jq -n '{scenario: "sts-assume-role", result: "fail", cluster: "vws-test", finishedAt: "2026-10-31T13:09:00Z"}' >"${res}/sts-assume-role.json"
echo '{"features": [{"scenario": "ec2-run", "lastPass": "2026-10-31T13:05:00Z"}]}' >"${clone}/docs/feature-map.json"
echo '# Feature map (nightly)' >"${clone}/docs/feature-map.md"

# The fake gh: logs each call; `pr list` prints FAKE_GH_OPEN; `pr create` saves the body.
mkdir -p "${WORK}/bin"
cat >"${WORK}/bin/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_GH_LOG}"
case "$1 $2" in
  "pr list") printf '%s' "${FAKE_GH_OPEN:-}" ;;
  "pr create")
    [[ -z ${FAKE_GH_FAIL:-} ]] || { echo "HTTP 422" >&2; exit 1; }
    while (($#)); do [[ $1 == --body-file ]] && cp "$2" "${FAKE_GH_BODY}"; shift; done
    echo "https://github.com/popododo0720/vws-deploy/pull/9001" ;;
  *) exit 9 ;;
esac
SH
chmod +x "${WORK}/bin/gh"
export PATH="${WORK}/bin:${PATH}" FAKE_GH_LOG="${WORK}/gh.log" FAKE_GH_BODY="${WORK}/body.md"
log_url="https://example.invalid/nightly/${run}/run.log"
gh_calls() { grep -c "^pr $1" "$FAKE_GH_LOG" || true; }

: >"$FAKE_GH_LOG"
run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "$log_url"
expect_rc "a changed map opens a PR: exit 0" 0
expect_text "the PR URL is reported" "$OUT" "pull/9001"
if [[ $(git -C "$origin" rev-parse refs/heads/main) == "$main_before" ]]; then ok "main on the remote is untouched"
else bad "main untouched" "main moved"; fi
pushed=$(git -C "$origin" rev-parse -q --verify "refs/heads/nightly/${run}" || true)
if [[ -n $pushed ]] && [[ $(git -C "$origin" show "${pushed}:docs/feature-map.json") == *lastPass* ]] &&
  [[ $(git -C "$origin" show "${pushed}:docs/feature-map.md") == *nightly* ]] &&
  [[ $(git -C "$origin" rev-parse "${pushed}^") == "$main_before" ]]; then
  ok "the nightly branch holds both regenerated files on top of main"
else bad "nightly branch" "pushed=${pushed}"; fi
if [[ $(git -C "$origin" diff --name-only "$main_before" "$pushed" | sort | tr '\n' ' ') == "docs/feature-map.json docs/feature-map.md " ]]; then
  ok "the commit changes only the feature map"; else bad "only the map" "$(git -C "$origin" diff --name-only "$main_before" "$pushed")"; fi
expect_text "the commit says Part of #252" "$(git -C "$origin" log -1 --format=%B "$pushed")" "Part of #252"
if [[ $(git -C "$clone" symbolic-ref --short HEAD) == main ]] && [[ $(git -C "$clone" rev-parse HEAD) == "$main_before" ]] &&
  [[ -n $(git -C "$clone" status --porcelain -- docs) ]]; then ok "the source checkout keeps its branch, HEAD and edits"
else bad "checkout untouched" "$(git -C "$clone" status -sb)"; fi
expect_text "the PR targets main from the nightly branch" "$(cat "$FAKE_GH_LOG")" "--base main --head nightly/${run}"
body=$(cat "$FAKE_GH_BODY" 2>/dev/null || true)
expect_text "the body says Part of #252" "$body" "Part of #252"
expect_text "the body links the run log" "$body" "$log_url"
expect_text "the body lists a passing scenario with its evidence" "$body" "| ec2-run | pass | 2026-10-31T13:05:00Z | https://example.invalid/ec2-run |"
expect_text "the body lists a failing scenario" "$body" "| sts-assume-role | fail |"
if ! grep -Eiq '\b(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]' <<<"$body"; then ok "the body has no closing keyword"
else bad "closing keyword" "$body"; fi

run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "$log_url"
expect_rc "a rerun with the branch pushed but no PR open: exit 0" 0
if [[ $(git -C "$origin" rev-parse "refs/heads/nightly/${run}") == "$pushed" ]]; then ok "a rerun reuses the pushed commit"
else bad "rerun reuses commit" "branch moved"; fi
FAKE_GH_OPEN="https://github.com/popododo0720/vws-deploy/pull/9001" run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "$log_url"
expect_rc "a rerun with the PR open: exit 0" 0
expect_text "the open PR is reported" "$OUT" "already open"
if [[ $(gh_calls create) == 2 ]]; then ok "an open PR is not opened twice"; else bad "one create per run" "$(cat "$FAKE_GH_LOG")"; fi

FAKE_GH_FAIL=1 run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "$log_url"
expect_rc "a failed PR create: exit 1" 1
expect_text "the failure is named" "$OUT" "FAIL  nightly-pr"

git -C "$clone" stash -q
: >"$FAKE_GH_LOG"
run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "$log_url"
expect_rc "an unchanged map: exit 0" 0
expect_text "an unchanged map is a WARN" "$OUT" "WARN  nightly-pr  feature map unchanged"
if [[ ! -s $FAKE_GH_LOG ]]; then ok "an unchanged map opens no PR"; else bad "no PR" "$(cat "$FAKE_GH_LOG")"; fi
git -C "$clone" stash pop -q

run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "http://example.invalid/log"
expect_rc "a non-https log URL is a usage error" 2
run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "https://x/a b"
expect_rc "a log URL with a space is a usage error" 2
run_vws_real nightly-pr --root "$clone" --log-url "$log_url"
expect_rc "a missing --results is a usage error" 2
mkdir -p "${WORK}/res/main"
run_vws_real nightly-pr --root "$clone" --results "${WORK}/res/main" --log-url "$log_url"
expect_rc "a results dir not named like a run is a usage error" 2
run_vws_real nightly-pr --root "$clone" --results "$res" --log-url "$log_url" --repo "a/b;rm"
expect_rc "a bad --repo is a usage error" 2
mkdir -p "${WORK}/notgit"
run_vws_real nightly-pr --root "${WORK}/notgit" --results "$res" --log-url "$log_url"
expect_rc "a root that is not a git checkout: exit 3" 3

finish_tests
