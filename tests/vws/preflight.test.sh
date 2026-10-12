#!/usr/bin/env bash
# vws preflight: one scenario per check, built from tests/fixtures/preflight/ok by changing one file.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export VWS_SOURCE_CHECKS=0

# scenario NAME: a fresh copy of the ok fixtures in $WORK/NAME, used by the next run.
scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/preflight/ok" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
}
fx() { echo "${VWS_FAKE_DIR}/$1"; }
edit_json() { # FILE JQ
  local f
  f=$(fx "$1")
  jq "$2" "$f" >"${f}.new" && mv "${f}.new" "$f"
}
line() { grep -E "^$1 +$2 " <<<"$OUT" || true; }
expect_line() { # NAME LEVEL CHECK
  if [[ -n $(line "$2" "$3") ]]; then ok "$1"; else bad "$1" "no '$2 $3' line in: $OUT"; fi
}

scenario ok
run_vws_real preflight --profile single-node;     expect_rc "all pass exits 0" 0
assert_no_changes "preflight changes nothing"
expect_line "ok has the nodes line" OK nodes
run_vws_real -o json preflight --profile single-node; expect_rc "json exits 0" 0
if jq -e '.command == "preflight" and .status == "ok" and (.checks | length) >= 15 and all(.checks[]; .level == "OK")' <<<"$OUT" >/dev/null; then
  ok "json lists every check"; else bad "json lists every check" "$OUT"; fi

run_vws_real preflight;                           expect_rc "no profile exits 2" 2
run_vws_real preflight --profile huge;            expect_rc "unknown profile exits 2" 2

scenario few-nodes
run_vws_real preflight --profile small;           expect_rc "1 node for small exits 1" 1
expect_line "small needs 3 nodes" FAIL nodes
run_vws_real preflight --profile ha;              expect_rc "1 node for ha exits 1" 1
edit_json kubectl/get_nodes.json '.items += [.items[0], .items[0]]'
run_vws_real preflight --profile small;           expect_rc "3 nodes for small exits 0" 0
run_vws_real preflight --profile ha;              expect_rc "3 nodes for ha exits 1" 1
edit_json kubectl/get_nodes.json '.items[1].status.conditions[0].status = "False"'
run_vws_real preflight --profile small;           expect_rc "a NotReady node does not count" 1

scenario small-node
edit_json kubectl/get_nodes.json '.items[0].status.allocatable = {"cpu": "500m", "memory": "1Gi"}'
run_vws_real preflight --profile single-node;     expect_rc "too little cpu and memory exits 1" 1
expect_line "resources fail" FAIL node-resources

scenario no-rights
echo no >"$(fx kubectl/auth_can-i.json)"
run_vws_real preflight --profile single-node;     expect_rc "missing rights exit 1" 1
expect_line "install-rights fail" FAIL install-rights
assert_no_changes "a failing preflight changes nothing"

scenario minor
edit_json kubectl/version_.json '.serverVersion.minor = "34"'
run_vws_real preflight --profile single-node;     expect_rc "another minor is a warning" 0
expect_line "k8s-minor warns" WARN k8s-minor
run_vws_real --strict preflight --profile single-node; expect_rc "--strict turns the warning into 1" 1

scenario helm
echo 'v3.10.0+gdeadbee' >"$(fx helm/version.json)"
run_vws_real preflight --profile single-node;     expect_rc "another helm is a warning" 0
expect_line "tools warns" WARN tools

scenario no-default-class
edit_json kubectl/get_storageclass.json '.items |= map(del(.metadata.annotations))'
run_vws_real preflight --profile single-node;     expect_rc "no default StorageClass exits 1" 1
expect_line "default-storageclass fails" FAIL default-storageclass

scenario before-bundle
edit_json kubectl/get_storageclass.json '.items |= map(select(.metadata.name != "ceph-block"))'
echo '{"items":[]}' >"$(fx kubectl/get_volumesnapshotclass.json)"
echo '{"items":[]}' >"$(fx kubectl/get_crd.json)"
rm "$(fx kubectl/get_kubevirt.json)" "$(fx kubectl/get_cdi.json)"
edit_json kubectl/get_nodes.json '.items |= map(del(.metadata.labels))'
run_vws_real preflight --profile single-node;     expect_rc "a cluster before the bundle only warns" 0
expect_line "missing ceph-block warns" WARN ceph-block
expect_line "missing kubevirt CRDs warn" WARN crd-kubevirt.io
expect_line "missing KubeVirt CR warns" WARN kubevirt-phase
expect_line "kernel modules not confirmed warns" WARN kernel-indirect

scenario crd-not-ready
edit_json kubectl/get_crd.json '.items[0].status.conditions[0].status = "False"'
run_vws_real preflight --profile single-node;     expect_rc "a CRD not Established exits 1" 1
expect_line "crd fails" FAIL crd-kubevirt.io
edit_json kubectl/get_cdi.json '.items[0].status.phase = "Deploying"'
run_vws_real preflight --profile single-node;     expect_rc "CDI not Deployed exits 1" 1
expect_line "cdi phase fails" FAIL cdi-phase

scenario release-failed
echo '[{"name":"vws","namespace":"vws","status":"pending-upgrade","revision":"3"}]' >"$(fx helm/list.json)"
run_vws_real preflight --profile single-node;     expect_rc "a pending release exits 1" 1
expect_line "release fails" FAIL release

scenario api-down
rm "$(fx kubectl/version_.json)"
run_vws_real preflight --profile single-node;     expect_rc "an unreachable API exits 3" 3

finish_tests
