#!/usr/bin/env bash
# The dispatcher, the shared library and the fake tools themselves.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Exit codes.
run_vws_real help;                  expect_rc "help exits 0" 0
run_vws_real --help;                expect_rc "--help exits 0" 0
run_vws;                            expect_rc "no command exits 2" 2
run_vws nope;                       expect_rc "unknown command exits 2" 2
expect_text "unknown command is named" "$ERR" "unknown command nope"
run_vws '../etc/passwd';            expect_rc "a path as command exits 2" 2
run_vws --bogus readonly;           expect_rc "unknown flag before the command exits 2" 2
run_vws readonly --context;         expect_rc "flag without value exits 2" 2
run_vws -o xml readonly;            expect_rc "bad -o value exits 2" 2

# A missing tool exits 3. PATH has the system tools and the fake kubectl, but no helm.
mkdir -p "${WORK}/nohelm"
for tool in bash env jq cat grep sed awk mktemp rm dirname basename sort tr wc head date cut printf mkdir; do
  path=$(command -v "$tool") && ln -sf "$path" "${WORK}/nohelm/${tool}"
done
ln -sf "${FIXTURES}/bin/kubectl" "${WORK}/nohelm/kubectl"
reset_log
set +e
OUT=$(PATH="${WORK}/nohelm" VWS_COMMAND_DIR="${FIXTURES}/commands" bash "$VWS_BIN" readonly 2>"${WORK}/err")
RC=$?
set -e
ERR=$(cat "${WORK}/err")
expect_rc "helm missing exits 3" 3
expect_text "missing tool is named" "$ERR" "helm"
OUT=$(PATH="${WORK}/nohelm" bash "$VWS_BIN" help 2>&1) && ok "help needs no cluster tool" || bad "help needs no cluster tool" "$OUT"

# Output.
run_vws -o json readonly;           expect_rc "json readonly exits 0" 0
if jq -e '.command == "readonly" and .status == "ok" and (.checks | length) == 2' <<<"$OUT" >/dev/null; then
  ok "json output parses with jq"
else bad "json output parses with jq" "$OUT"; fi
run_vws readonly;                   expect_text "text output has OK lines" "$OUT" "OK    nodes  2 nodes"
run_vws_real -o json help
if jq -e '.checks[] | select(.name == "help")' <<<"$OUT" >/dev/null; then ok "help lists itself in json"; else bad "help lists itself in json" "$OUT"; fi

# A read scenario changes nothing, and the assertion can tell (control cases below).
run_vws readonly
assert_no_changes "read scenario makes no changing call"
if [[ $(wc -l <"$VWS_FAKE_LOG") -eq 2 ]]; then ok "calls are logged"; else bad "calls are logged" "$(cat "$VWS_FAKE_LOG")"; fi
run_vws mutating;                   assert_changes "control: apply is seen as a changing call"
run_vws --dry-run mutating;         expect_rc "dry-run exits 0" 0
assert_no_changes "dry-run makes no changing call"
expect_text "dry-run says what it skipped" "$ERR" "DRYRUN kubectl apply"
run_vws --dry-run readonly
if [[ $(wc -l <"$VWS_FAKE_LOG") -eq 2 ]]; then ok "dry-run still runs reads"; else bad "dry-run still runs reads" "$(cat "$VWS_FAKE_LOG")"; fi

# Common flags reach the tools.
run_vws --context lab -n vws readonly
expect_text "context reaches kubectl" "$(cat "$VWS_FAKE_LOG")" "--context=lab"
expect_text "namespace reaches kubectl" "$(cat "$VWS_FAKE_LOG")" "--namespace=vws"
expect_text "context reaches helm" "$(cat "$VWS_FAKE_LOG")" "--kube-context=lab"
run_vws readonly --context=lab
expect_text "flags work after the command" "$(cat "$VWS_FAKE_LOG")" "--context=lab"

# Destructive commands.
run_vws destructive;                expect_rc "destructive without --yes exits 2" 2
assert_no_changes "destructive without --yes makes no call"
run_vws --yes destructive;          expect_rc "destructive with --yes exits 0" 0
assert_changes "destructive with --yes deletes"

# WARN, FAIL and --strict.
run_vws warn;                       expect_rc "WARN exits 0" 0
run_vws --strict warn;              expect_rc "WARN with --strict exits 1" 1
run_vws fail;                       expect_rc "FAIL exits 1" 1
run_vws -o json fail
if jq -e '.status == "fail"' <<<"$OUT" >/dev/null; then ok "json status is fail"; else bad "json status is fail" "$OUT"; fi

# The fake tools fail on a call nobody planned.
run_vws unfixtured;                 expect_rc "a call without fixture fails" 1
expect_text "the missing fixture is named" "$ERR" "no fixture"

# The harness catches a changing call when one is logged.
printf 'kubectl\tdelete\tdelete pod x\n' >"$VWS_FAKE_LOG"
if [[ -n $(changing_calls) ]]; then ok "control: a logged delete is found"; else bad "control: a logged delete is found" "not found"; fi

finish_tests
