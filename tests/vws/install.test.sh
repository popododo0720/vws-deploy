#!/usr/bin/env bash
set -euo pipefail
export TMPDIR=${TMPDIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
python3 "${VWS_TEST_ROOT}/tests/vws/install_test.py"
