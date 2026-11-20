#!/usr/bin/env bash
# The dispatcher and the shared library.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Exit codes.
run_vws_real help;                  expect_rc "help exits 0" 0
run_vws_real --help;                expect_rc "--help exits 0" 0
run_vws;                            expect_rc "no command exits 2" 2
run_vws nope;                       expect_rc "unknown command exits 2" 2
expect_text "unknown command is reported" "$ERR" "unknown command"
run_vws '../etc/passwd';            expect_rc "a path as command exits 2" 2
run_vws --bogus readonly;           expect_rc "unknown flag before the command exits 2" 2
run_vws readonly -o;              expect_rc "flag without value exits 2" 2
run_vws -o xml readonly;            expect_rc "bad -o value exits 2" 2

canary=gate-canary-768
for command in region aws-account; do
  for flag in --token-secret --external-id --token-secret-extra --external-id-extra; do
    for form in separate equals; do
      secret_args=("$flag" "$canary")
      [[ $form != equals ]] || secret_args=("${flag}=${canary}")
      for position in before after; do
        if [[ $position == before ]]; then
          run_vws_real "${secret_args[@]}" "$command" add pve-test-1
        else
          run_vws_real "$command" add pve-test-1 "${secret_args[@]}"
        fi
        expect_rc "$command $flag $form $position is rejected" 2
        if [[ $OUT != *"$canary"* && $ERR != *"$canary"* ]]; then
          ok "$command $flag $form $position keeps the canary private"
        else
          bad "$command $flag $form $position keeps the canary private" "secret appeared in output"
        fi
      done
    done
  done
done
run_vws --bogus="$canary" readonly
expect_rc "unknown flag value is rejected" 2
expect_text "unknown flag is reported" "$ERR" "unknown flag"
if [[ $OUT != *"$canary"* && $ERR != *"$canary"* ]]; then
  ok "unknown flag value stays private"
else
  bad "unknown flag value stays private" "value appeared in output"
fi

for value in "$canary" "token-secret=$canary" "external-id=$canary"; do
  for command in region aws-account; do
    for form in separate equals; do
      output_args=(-o "$value")
      [[ $form != equals ]] || output_args=("-o=$value")
      for position in before after; do
        if [[ $position == before ]]; then
          run_vws_real "${output_args[@]}" "$command" list
        else
          run_vws_real "$command" list "${output_args[@]}"
        fi
        expect_rc "$command output format $form $position is rejected" 2
        if [[ $OUT != *"$canary"* && $ERR != *"$canary"* ]]; then
          ok "$command output format $form $position keeps the canary private"
        else
          bad "$command output format $form $position keeps the canary private" "value appeared in output"
        fi
      done
    done
  done
  run_vws_real "$value"
  expect_rc "unknown command value is rejected" 2
  if [[ $OUT != *"$canary"* && $ERR != *"$canary"* ]]; then
    ok "unknown command value stays private"
  else
    bad "unknown command value stays private" "value appeared in output"
  fi
done

# A missing tool exits 3, and help needs none.
mkdir -p "${WORK}/notool"
for tool in bash env jq cat grep sed awk mktemp rm dirname basename sort tr wc head date cut printf mkdir; do
  path=$(command -v "$tool") && ln -sf "$path" "${WORK}/notool/${tool}"
done
cat >"${WORK}/needs-tool.sh" <<'FIXTURE'
#!/usr/bin/env bash
# summary: fixture, needs a tool that is not installed
# shellcheck disable=SC2034
VWS_NEED=(vws-absent-tool)
vws_cmd_main() { :; }
FIXTURE
mkdir -p "${WORK}/needs"
cp "${WORK}/needs-tool.sh" "${WORK}/needs/needs.sh"
set +e
OUT=$(PATH="${WORK}/notool" VWS_COMMAND_DIR="${WORK}/needs" bash "$VWS_BIN" needs 2>"${WORK}/err")
RC=$?
set -e
ERR=$(cat "${WORK}/err")
expect_rc "a missing tool exits 3" 3
expect_text "missing tool is named" "$ERR" "vws-absent-tool"
OUT=$(PATH="${WORK}/notool" bash "$VWS_BIN" help 2>&1) && ok "help needs no external tool" || bad "help needs no external tool" "$OUT"

# Output.
run_vws -o json readonly;           expect_rc "json readonly exits 0" 0
if jq -e '.command == "readonly" and .status == "ok" and (.checks | length) == 2' <<<"$OUT" >/dev/null; then
  ok "json output parses with jq"
else bad "json output parses with jq" "$OUT"; fi
run_vws readonly;                   expect_text "text output has OK lines" "$OUT" "OK    nodes  2 nodes"
run_vws_real -o json help
if jq -e '.checks[] | select(.name == "help")' <<<"$OUT" >/dev/null; then ok "help lists itself in json"; else bad "help lists itself in json" "$OUT"; fi

run_vws readonly
if [[ $(wc -l <"$VWS_FAKE_LOG") -eq 0 ]]; then ok "a read scenario makes no tool call"; else bad "a read scenario makes no tool call" "$(cat "$VWS_FAKE_LOG")"; fi
run_vws --dry-run readonly;         expect_rc "dry-run exits 0" 0
run_vws readonly --dry-run;         expect_rc "flags work after the command" 0

# Destructive commands.
run_vws destructive;                expect_rc "destructive without --yes exits 2" 2
expect_text "the refusal names the action" "$ERR" "needs --yes"
run_vws --yes destructive;          expect_rc "destructive with --yes exits 0" 0
expect_text "destructive with --yes runs" "$OUT" "deleted"

# WARN, FAIL and --strict.
run_vws warn;                       expect_rc "WARN exits 0" 0
run_vws --strict warn;              expect_rc "WARN with --strict exits 1" 1
run_vws fail;                       expect_rc "FAIL exits 1" 1
run_vws -o json fail
if jq -e '.status == "fail"' <<<"$OUT" >/dev/null; then ok "json status is fail"; else bad "json status is fail" "$OUT"; fi

finish_tests
