#!/usr/bin/env bash
# Snapshot restore is exercised against real etcd in etcd.test.sh.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

run_vws_real restore
expect_rc "restore requires --etcd" 2
run_vws_real restore "${WORK}/legacy-backup" --yes
expect_rc "chart backup restore is retired" 2
assert_no_changes "a retired restore changes nothing"

finish_tests
