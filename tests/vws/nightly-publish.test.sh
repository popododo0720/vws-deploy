#!/usr/bin/env bash
# vws nightly-publish: pushes a run's scenario results to metrics/nightly/<run>/ on the nightly-results
# branch as one fast-forward commit, never touches main or the checkout, refuses a branch with a commit
# it did not write, and creates the branch only with --init.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t VWS_NIGHTLY_PUBLISH_BACKOFF=0
origin="${WORK}/origin.git" clone="${WORK}/clone" br=refs/heads/nightly-results
git init -q --bare -b main "$origin"
git init -q -b main "$clone"
echo base >"${clone}/README.md"
git -C "$clone" add README.md && git -C "$clone" commit -q -m base
git -C "$clone" remote add origin "$origin" && git -C "$clone" push -q origin main
main_before=$(git -C "$origin" rev-parse refs/heads/main)
idx_before=$(sha256sum "${clone}/.git/index")

run=20261031T130000Z res="${WORK}/res/20261031T130000Z"
mkdir -p "$res"
jq -n '{scenario: "s3-basic", result: "pass", cluster: "vws-test", finishedAt: "2026-10-31T13:05:00Z"}' >"${res}/s3-basic.json"
jq -n '{scenario: "ec2-run", result: "fail", cluster: "vws-test", finishedAt: "2026-10-31T13:09:00Z"}' >"${res}/ec2-run.json"
echo 'log line' >"${res}/s3-basic.log"
publish() { run_vws_real nightly-publish --root "$clone" "$@"; }
head_of() { git -C "$origin" rev-parse -q --verify "$br" || true; }

publish --results "$res"
expect_rc "no branch and no --init: exit 1" 1
expect_text "the missing branch is named" "$OUT" "FAIL  nightly-publish  origin has no nightly-results branch; create it once with --init"
if [[ -z $(head_of) ]]; then ok "without --init no branch is created"; else bad "no branch" "created"; fi

publish --results "$res" --init
expect_rc "--init creates the branch: exit 0" 0
first=$(head_of)
if [[ -n $first && -z $(git -C "$origin" rev-list --parents -n1 "$first" | cut -s -d' ' -f2) ]]; then ok "the first commit is an orphan root"
else bad "orphan" "$(git -C "$origin" log --oneline --all)"; fi
if [[ $(git -C "$origin" ls-tree -r --name-only "$first" | tr '\n' ' ') == "metrics/nightly/${run}/ec2-run.json metrics/nightly/${run}/s3-basic.json " ]]; then
  ok "only the result JSON files are published, under metrics/nightly/<run>/"; else bad "tree" "$(git -C "$origin" ls-tree -r --name-only "$first")"; fi
if [[ $(git -C "$origin" show "${first}:metrics/nightly/${run}/s3-basic.json") == "$(cat "${res}/s3-basic.json")" ]]; then
  ok "a published file equals the result"; else bad "content" "differs"; fi
msg=$(git -C "$origin" log -1 --format=%B "$first")
expect_text "the subject names the run" "$msg" "metrics: nightly results ${run}"
expect_text "the commit says Part of #252" "$msg" "Part of #252"
expect_text "the commit has the generator trailer" "$msg" "Results-Generator: vws-nightly-publish"
if [[ $(git -C "$origin" rev-parse refs/heads/main) == "$main_before" ]]; then ok "main on the remote is untouched"
else bad "main untouched" "main moved"; fi
if [[ $(git -C "$clone" symbolic-ref --short HEAD) == main && $(git -C "$clone" rev-parse HEAD) == "$main_before" &&
  $(sha256sum "${clone}/.git/index") == "$idx_before" && -z $(git -C "$clone" status --porcelain) ]]; then
  ok "the checkout keeps its branch, HEAD, index and files"; else bad "checkout" "$(git -C "$clone" status -sb)"; fi

publish --results "$res"
expect_rc "a rerun of the same run: exit 0" 0
expect_text "a rerun reports the run is there" "$OUT" "already holds run ${run}"
if [[ $(head_of) == "$first" ]]; then ok "a rerun pushes nothing"; else bad "rerun" "branch moved"; fi

jq '.result = "pass"' "${res}/ec2-run.json" >"${res}/x" && mv "${res}/x" "${res}/ec2-run.json"
rm "${res}/s3-basic.json"
publish --results "$res"
expect_rc "a changed rerun: exit 0" 0
second=$(head_of)
if [[ $(git -C "$origin" rev-parse "${second}^") == "$first" ]]; then ok "a changed rerun fast-forwards the branch"; else bad "ff" "$second"; fi
if [[ $(git -C "$origin" ls-tree -r --name-only "$second" | tr '\n' ' ') == "metrics/nightly/${run}/ec2-run.json " ]]; then
  ok "a rerun replaces the run directory exactly"; else bad "replace" "$(git -C "$origin" ls-tree -r --name-only "$second")"; fi

res2="${WORK}/res/20261101T130000Z"
mkdir -p "$res2"
jq -n '{scenario: "s3-basic", result: "pass"}' >"${res2}/s3-basic.json"
publish --results "$res2"
third=$(head_of)
if [[ $(git -C "$origin" ls-tree -r --name-only "$third" | tr '\n' ' ') == "metrics/nightly/${run}/ec2-run.json metrics/nightly/20261101T130000Z/s3-basic.json " ]]; then
  ok "the next night adds its run and keeps the earlier one"; else bad "two runs" "$(git -C "$origin" ls-tree -r --name-only "$third")"; fi

# A push that loses a race: the hook moves the branch once, so the first push is not a fast-forward.
other="${WORK}/other"
git clone -q -b nightly-results "$origin" "$other"
mkdir -p "${other}/metrics/nightly/20261102T130000Z"
echo '{}' >"${other}/metrics/nightly/20261102T130000Z/s3-basic.json"
git -C "$other" add metrics && git -C "$other" commit -q -m $'metrics: nightly results 20261102T130000Z\n\nResults-Generator: vws-nightly-publish'
cat >"${clone}/.git/hooks/pre-push" <<SH
#!/usr/bin/env bash
[[ -e ${WORK}/raced ]] || { touch ${WORK}/raced; git -C ${other} push -q origin nightly-results; }
SH
chmod +x "${clone}/.git/hooks/pre-push"
res3="${WORK}/res/20261103T130000Z"
mkdir -p "$res3"; echo '{"result": "pass"}' >"${res3}/s3-basic.json"
publish --results "$res3"
expect_rc "a lost race is retried: exit 0" 0
expect_text "the first attempt is a WARN" "$OUT" "WARN  nightly-publish  push attempt 1 failed"
if [[ $(git -C "$origin" ls-tree -r --name-only "$(head_of)" | grep -c '/s3-basic.json$') == 3 &&
  -n $(git -C "$origin" ls-tree -r --name-only "$(head_of)" | grep 20261102T130000Z) ]]; then
  ok "the retry builds on the moved branch and keeps its commit"; else bad "race" "$(git -C "$origin" ls-tree -r --name-only "$(head_of)")"; fi
rm "${clone}/.git/hooks/pre-push"

# A foreign commit on the branch is refused, and nothing is pushed on top of it.
git -C "$other" pull -q --ff-only origin nightly-results
echo x >"${other}/README.md"
git -C "$other" add README.md && git -C "$other" commit -q -m $'metrics: nightly results 20261104T130000Z\n\nResults-Generator: vws-nightly-publish'
git -C "$other" push -q origin nightly-results
foreign=$(head_of)
publish --results "$res2"
expect_rc "a commit outside metrics/nightly/: exit 1" 1
expect_text "the foreign path is named" "$OUT" "touches README.md outside metrics/nightly/"
if [[ $(head_of) == "$foreign" ]]; then ok "nothing is pushed on a refused branch"; else bad "refused" "moved"; fi
git -C "$other" reset -q --hard HEAD^
git -C "$other" commit -q --allow-empty -m 'metrics: nightly results 20261104T130000Z'
git -C "$other" push -q -f origin nightly-results
publish --results "$res2"
expect_rc "a commit with no trailer: exit 1" 1
expect_text "the missing trailer is named" "$OUT" "has no 'Results-Generator: vws-nightly-publish' trailer"
git -C "$other" commit -q --amend --allow-empty -m $'docs: something else\n\nResults-Generator: vws-nightly-publish'
git -C "$other" push -q -f origin nightly-results
publish --results "$res2"
expect_rc "another subject: exit 1" 1
expect_text "the subject is named" "$OUT" "is not a nightly results subject"
git -C "$other" reset -q --hard HEAD^
git -C "$other" push -q -f origin nightly-results

res4="${WORK}/res/20261105T130000Z"
mkdir -p "$res4"
printf '{"result": "pass", "note": "\xed\x95\x9c"}\n' >"${res4}/s3-basic.json"
publish --results "$res4"
expect_rc "a non-ASCII result: exit 1" 1
echo 'not json' >"${res4}/s3-basic.json"
publish --results "$res4"
expect_rc "a result that is not JSON: exit 1" 1
expect_text "the bad file is named" "$OUT" "s3-basic.json is not an ASCII JSON object"
rm "${res4}/s3-basic.json"; echo '{}' >"${res4}/Bad_Name.json"
publish --results "$res4"
expect_rc "no scenario result: exit 0" 0
expect_text "a bad name is skipped with a WARN" "$OUT" "WARN  nightly-publish  Bad_Name.json is not a scenario result name"
expect_text "an empty run is a WARN" "$OUT" "WARN  nightly-publish  no scenario result in"

publish --results "${WORK}/res/latest"; expect_rc "a results dir not named like a run: usage" 2
publish; expect_rc "a missing --results: usage" 2
publish --results "$res" --remote 'a;b'; expect_rc "a bad --remote: usage" 2
run_vws_real --dry-run nightly-publish --root "$clone" --results "$res"; expect_rc "--dry-run is refused" 2
mkdir -p "${WORK}/notgit"
run_vws_real nightly-publish --root "${WORK}/notgit" --results "$res"; expect_rc "a root that is not a git checkout: exit 3" 3
git -C "$clone" remote set-url origin "${WORK}/missing.git"
publish --results "$res"
expect_rc "an unreachable remote after three attempts: exit 1" 1
expect_text "the unreachable remote is named" "$OUT" "could not push run ${run} to nightly-results after 3 attempts"
if [[ $(git -C "$origin" rev-parse refs/heads/main) == "$main_before" ]]; then ok "main is still untouched at the end"
else bad "main at end" "moved"; fi

finish_tests
