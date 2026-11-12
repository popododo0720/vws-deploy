#!/usr/bin/env bash
# The legacy installer entry points do not need chart rendering or Kubernetes calls.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fakebin="${WORK}/vws-installer"
printf '#!/bin/sh\necho "installer $*"\nexit "${INSTALLER_EXIT:-0}"\n' >"$fakebin"
chmod +x "$fakebin"

VWS_INSTALLER_POD=1 VWS_INSTALLER_BIN="$fakebin" run_vws_real init </dev/null
expect_rc "init in the pod runs the TUI" 0
expect_text "init execs the installer TUI" "$OUT" "installer tui"
assert_no_changes "the TUI entry point makes no Kubernetes calls"

VWS_INSTALLER_BIN="$fakebin" run_vws_real init --print-url
expect_rc "--print-url exits 0" 0
expect_text "--print-url execs the installer" "$OUT" "installer print-url"
assert_no_changes "connection details make no Kubernetes calls"

VWS_INSTALLER_BIN="${WORK}/none" run_vws_real init --print-url
expect_rc "a missing installer binary exits 2" 2
VWS_INSTALLER_POD=1 VWS_INSTALLER_BIN="${WORK}/none" run_vws_real init
expect_rc "a missing TUI binary exits 2" 2
VWS_INSTALLER_POD=0 VWS_INSTALLER_BIN="$fakebin" run_vws_real init
expect_rc "the TUI outside the pod exits 2" 2
VWS_INSTALLER_POD=1 VWS_INSTALLER_BIN="$fakebin" run_vws_real init --from "${WORK}/old.yaml"
expect_rc "chart CR initialization is retired" 2
assert_no_changes "retired initialization makes no Kubernetes calls"
VWS_INSTALLER_BIN="$fakebin" run_vws_real init --print-url extra
expect_rc "extra arguments exit 2" 2
INSTALLER_EXIT=7 VWS_INSTALLER_BIN="$fakebin" run_vws_real init --print-url
expect_rc "the installer exit status is preserved" 7

finish_tests
