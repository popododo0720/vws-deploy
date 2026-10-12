#!/usr/bin/env bash
# vws diag: the bundle masks planted fake secrets, never holds Secrets or ConfigMap data, is 0600,
# and is not written when the scanner cannot run.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
GROUP=$(sed -n 's/^  group: //p' "${VWS_TEST_ROOT}/charts/vws/crds/vwsclusters.yaml")

scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/diag/ok" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
  jq --arg v "${GROUP}/v1alpha1" '.apiVersion = $v' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
  mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
}
unpack() { rm -rf "${WORK}/x"; mkdir "${WORK}/x"; tar -C "${WORK}/x" -xzf "$1"; }
bundle_text() { find "${WORK}/x/vws-diag" -type f -exec cat {} +; }

scenario ok
run_vws_real diag --out "${WORK}/b.tgz";       expect_rc "diag exits 0" 0
assert_no_changes "diag changes nothing"
if [[ -f ${WORK}/b.tgz ]]; then ok "the bundle is written"; else bad "bundle written" "$OUT $ERR"; fi
mode=$(stat -c %a "${WORK}/b.tgz")
if [[ $mode == 600 ]]; then ok "the bundle is 0600"; else bad "bundle mode" "$mode"; fi
unpack "${WORK}/b.tgz"
text=$(bundle_text)
for fake in FAKEFAKEFAKEFAKETOKEN0000 fakepass FAKEFAKEFAKEFAKE0000 000000000000 FAKEKEYFAKEKEY RkFLRUZBS0VGQUtF; do
  if [[ $text != *"$fake"* ]]; then ok "masked: ${fake:0:12}"; else bad "masked ${fake}" "$(grep -rl "$fake" "${WORK}/x")"; fi
done
if [[ $text != *FAKE-CONFIGMAP-CONTENT* ]]; then ok "no ConfigMap data"; else bad "ConfigMap data" "present"; fi
expect_text "ConfigMap names and sizes are there" "$(cat "${WORK}/x/vws-diag/configmaps.json")" '"vm-1": 40'
expect_text "the CR spec and status are there" "$(cat "${WORK}/x/vws-diag/vwscluster.json")" '"observedGeneration": 2'
if ! grep -q managedFields "${WORK}/x/vws-diag/vwscluster.json"; then ok "no managedFields"; else bad "managedFields" "present"; fi
expect_text "logs of an unready pod are there" "$(cat "${WORK}/x/vws-diag/pods/vws_compute-0.log")" "start"
if [[ ! -e ${WORK}/x/vws-diag/pods/vws_console-1.log && ! -e ${WORK}/x/vws-diag/pods/default_other.log ]]; then ok "only unhealthy pods in watched namespaces"; else bad "pod selection" "$(ls "${WORK}/x/vws-diag/pods")"; fi
report=$(cat "${WORK}/x/vws-diag/REDACTION_REPORT")
expect_text "the report counts" "$report" "pem-private-key 1"
if [[ $(grep -cvE '^[a-z0-9-]+ [0-9]+$' <<<"$report" || true) == 0 ]]; then ok "the report holds counts only"; else bad "report shape" "$report"; fi
expect_text "the README warns to review" "$(cat "${WORK}/x/vws-diag/README")" "read the bundle before you share it"
if ! grep -qE $'\t(get|describe|logs)\t.*\\bsecrets?\\b' "$VWS_FAKE_LOG"; then ok "no Secret is read"; else bad "secret read" "$(cat "$VWS_FAKE_LOG")"; fi
if ! grep -qE 'upload|curl|wget' "$VWS_FAKE_LOG"; then ok "no upload call"; else bad "upload" "$(cat "$VWS_FAKE_LOG")"; fi
run_vws_real diag --out "${WORK}/b.tgz";       expect_rc "an existing --out is a usage error" 2

# Operator credentials: helm cannot read the release; the bundle still forms.
scenario operator
rm "${VWS_FAKE_DIR}"/helm/{status,history,get}.json
run_vws_real diag --out "${WORK}/op.tgz";      expect_rc "diag without helm access exits 0" 0
unpack "${WORK}/op.tgz"
expect_text "unreadable helm is noted" "$(cat "${WORK}/x/vws-diag/helm-status.json")" "unavailable:"

# No scanner on PATH: no bundle, exit 3. The PATH keeps every tool diag needs except python3.
scenario no-scanner
nopy="${WORK}/nopy"
mkdir -p "$nopy"
for t in bash sh jq tar gzip mktemp cat head sed awk date chmod mv rm cp mkdir dirname stat env grep tr cut sort printf ls basename; do
  p=$(command -v "$t" || true); [[ -z $p ]] || ln -sf "$p" "$nopy/$t"
done
reset_log
set +e
OUT=$(PATH="${FIXTURES}/bin:${nopy}" VWS_COMMAND_DIR="${VWS_TEST_ROOT}/scripts/vws.d" "$nopy/bash" "$VWS_BIN" diag --out "${WORK}/none.tgz" 2>"${WORK}/err")
RC=$?
set -e
ERR=$(cat "${WORK}/err")
expect_rc "no scanner exits 3" 3
if [[ ! -e ${WORK}/none.tgz ]]; then ok "no scanner writes no bundle"; else bad "no bundle" "written"; fi

scenario api-down
rm "${VWS_FAKE_DIR}/kubectl/version_.json"
run_vws_real diag --out "${WORK}/down.tgz";    expect_rc "no API exits 3" 3
if [[ ! -e ${WORK}/down.tgz ]]; then ok "no API writes no bundle"; else bad "no bundle" "written"; fi
run_vws_real diag --since forever;             expect_rc "a bad --since is a usage error" 2

# The scanner on its own: canary, digests are kept, counts only.
printf 'image: x@sha256:%s\n' "$(printf 'a%.0s' {1..64})" >"${WORK}/digest.txt"
mkdir -p "${WORK}/scan" && mv "${WORK}/digest.txt" "${WORK}/scan/"
python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/redact.py" "${WORK}/scan" "${WORK}/rep"
expect_text "an image digest is not masked" "$(cat "${WORK}/scan/digest.txt")" "sha256:aaaa"

finish_tests
