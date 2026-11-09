#!/usr/bin/env bash
# vws init: --from and questions build the same CR; the same CR is unchanged.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"
EX="${VWS_TEST_ROOT}/examples/vwscluster"

installer_scenario fresh
rm "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
run_vws_real init --from "${EX}/small.yaml";     expect_rc "init --from saves" 0
expect_text "init saves" "$OUT" "saved VWSCluster vws"
crd_line=$(grep -n -m1 "^kind: CustomResourceDefinition" "${VWS_FAKE_LOG}.stdin" | cut -d: -f1 || true)
cr_line=$(grep -n -m1 "^kind: VWSCluster" "${VWS_FAKE_LOG}.stdin" | cut -d: -f1 || true)
if [[ -n $crd_line && -n $cr_line ]] && ((crd_line < cr_line)); then ok "init applies the CRDs before the CR (#112)"; else bad "init CRD before CR" "$(head -5 "${VWS_FAKE_LOG}.stdin")"; fi
assert_changes "the first init applies the CR"

installer_scenario same
run_vws_real init --from "${EX}/single-node.yaml"; expect_rc "init with the same spec exits 0" 0
expect_text "the same spec is unchanged" "$OUT" "unchanged"
assert_no_changes "the same spec changes nothing"

# Questions: the answers that give single-node.yaml give the same spec, so unchanged too.
run_vws_real init <<<$'single-node\nrook-ceph\n\n198.18.0.0/24\nhttps://console.example.invalid\n'; expect_rc "interactive init exits 0" 0
expect_text "interactive answers equal to --from are unchanged" "$OUT" "unchanged"
assert_no_changes "interactive same spec changes nothing"

# #391: the admin page (field manager vws-admin) mapped io1. An init that drops io1 replaces the
# whole spec, so io1 goes; server-side apply as vws would keep a field vws-admin owns.
installer_scenario admin-type
python3 - "${EX}/single-node.yaml" "${WORK}/no-io1.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" <<'PY'
import json, sys, yaml
cr = yaml.safe_load(open(sys.argv[1]))
cr["spec"]["storage"]["volumeTypes"] = {"gp3": {"storageClass": "ceph-block"}}
json.dump(cr, open(sys.argv[2], "w"))
live = json.loads(json.dumps(cr))
live["spec"]["storage"]["volumeTypes"]["io1"] = {"storageClass": "ceph-block", "volumeSnapshotClass": "ceph-block"}
live["metadata"].update(namespace="vws", resourceVersion="4711", managedFields=[{"manager": "vws-admin", "operation": "Update"}])
json.dump(live, open(sys.argv[3], "w"))
PY
run_vws_real init --from "${WORK}/no-io1.json";  expect_rc "dropping a type the admin page mapped exits 0" 0
have=$(saved_specs | jq -c '.storage.volumeTypes')
if [[ $have == '{"gp3":{"storageClass":"ceph-block"}}' ]]; then ok "the saved spec has no io1"; else bad "io1 dropped" "$have"; fi
ops=$(grep $'^kubectl\tpatch\t.* vwsclusters vws --type=json' "$VWS_FAKE_LOG" | sed 's/.* -p //' | jq -c '[.[] | {op, path, value: (if .op == "test" then .value else null end)}]')
if [[ $ops == '[{"op":"test","path":"/metadata/resourceVersion","value":"4711"},{"op":"replace","path":"/spec","value":null}]' ]]; then ok "the save is a resourceVersion test and one whole-spec replace"; else bad "patch ops" "$ops"; fi
if grep -qE '^kind: VWSCluster|"kind": "VWSCluster"' "${VWS_FAKE_LOG}.stdin"; then bad "an existing CR is not saved server-side" "$(grep -m1 -E '^kind: VWSCluster' "${VWS_FAKE_LOG}.stdin")"; else ok "an existing CR is not saved server-side"; fi
echo 'testing value /metadata/resourceVersion failed' >"${VWS_FAKE_DIR}/kubectl/patch_vwsclusters.fail"
run_vws_real init --from "${WORK}/no-io1.json";  expect_rc "a CR changed since it was read exits 3" 3
expect_text "the refusal names the admin page" "$ERR" "for example by the admin page; run the command again"

installer_scenario other
run_vws_real init <<<$'ha\nexisting\nfast-rbd\n0.1.0\n198.18.1.0/24\nhttps://console.example.invalid\n'; expect_rc "interactive init with new answers saves" 0
assert_changes "new answers apply the CR"

run_vws_real init <<<$'single-node\nrook-ceph\n\n\n'; expect_rc "no elastic IP pool answer exits 2" 2
assert_no_changes "a CR without a pool is not saved"
run_vws_real init <<<$'huge\n\n\n';              expect_rc "an invalid answer exits 2" 2
assert_no_changes "an invalid CR is not saved"
sed 's/driver: kube-ovn/driver: calico/' "${EX}/ha.yaml" >"${WORK}/bad.yaml"
run_vws_real init --from "${WORK}/bad.yaml";     expect_rc "an invalid file exits 2" 2
run_vws_real init --from "${WORK}/missing.yaml"; expect_rc "a missing file exits 2" 2

installer_scenario expired
temp_binding 20000 14400
run_vws_real init --from "${EX}/small.yaml";     expect_rc "past the TTL init revokes and exits 1" 1
expect_text "the TTL failure says so" "$OUT" "past its TTL and is revoked"
expect_text "the expired binding is deleted" "$(calls)" "delete clusterrolebinding vws-installer-temp"

# In the installer pod plain vws init is the API TUI and --print-url prints the access (#142).
fakebin="${WORK}/vws-installer"
printf '#!/bin/sh\necho "installer $*"\n' >"$fakebin"; chmod +x "$fakebin"
installer_scenario fresh
VWS_INSTALLER_POD=1 VWS_INSTALLER_BIN="$fakebin" run_vws_real init </dev/null; expect_rc "init in the pod runs the TUI" 0
expect_text "init in the pod execs vws-installer tui" "$OUT" "installer tui"
assert_no_changes "the TUI path changes nothing by itself"
VWS_INSTALLER_POD=1 VWS_INSTALLER_BIN="$fakebin" run_vws_real init --print-url; expect_rc "--print-url exits 0" 0
expect_text "--print-url execs vws-installer print-url" "$OUT" "installer print-url"
VWS_INSTALLER_BIN="${WORK}/none" run_vws_real init --print-url; expect_rc "--print-url without the binary exits 2" 2
rm -f "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
VWS_INSTALLER_POD=1 VWS_INSTALLER_BIN="$fakebin" run_vws_real init --from "${EX}/small.yaml"; expect_rc "init --from in the pod still saves" 0
expect_text "init --from in the pod does not start the TUI" "$OUT" "saved VWSCluster vws"

finish_tests
