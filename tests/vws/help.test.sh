#!/usr/bin/env bash
# vws help
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

run_vws_real help;                  expect_rc "help exits 0" 0
expect_text "help lists the flags" "$OUT" "--dry-run"
expect_text "help lists the exit codes" "$OUT" "3 environment"
expect_text "help lists every real command" "$OUT" "help "
for file in "${VWS_TEST_ROOT}"/scripts/vws.d/*.sh; do
  name=$(basename "$file" .sh)
  expect_text "help lists ${name}" "$OUT" "  ${name} "
done
run_vws_real -o json help
if jq -e '.checks | length >= 1' <<<"$OUT" >/dev/null; then ok "help json lists commands"; else bad "help json lists commands" "$OUT"; fi

finish_tests
