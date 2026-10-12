#!/usr/bin/env bash
# vws backup: reads only, 0700/0600, no overwrite, manifest + SHA256SUMS, secret scan blocks, helm denied.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=tests/vws/installer_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/installer_lib.sh"
# shellcheck source=tests/vws/backup_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/backup_lib.sh"

backup_scenario ok
touch "${WORK}/etcd.db"
run_vws_real backup --out "${WORK}/b1" --etcd-snapshot "${WORK}/etcd.db"; expect_rc "backup exits 0" 0
assert_no_changes "backup only reads"
expect_text "the directory is written" "$OUT" "wrote ${WORK}/b1"
expect_text "the limits are printed" "$OUT" "VM disks (Ceph RBD) are not backed up"
expect_text "a fresh etcd snapshot is OK" "$OUT" "OK    etcd-snapshot"
expect_text "the directory is 0700" "$(stat -c %a "${WORK}/b1")" "700"
expect_text "files are 0600" "$(stat -c %a "${WORK}/b1"/* | sort -u | paste -sd,)" "600"
expect_text "SHA256SUMS verifies" "$(cd "${WORK}/b1" && sha256sum -c SHA256SUMS)" "manifest.json: OK"
expect_text "the CR has no status" "$(jq -c 'has("status")' "${WORK}/b1/vwscluster.json")" "false"
expect_text "manifest lists snapshot names" "$(jq -c .volumeSnapshots "${WORK}/b1/manifest.json")" '["state-snap-fake"]'
expect_text "manifest has ceph health" "$(jq -r '.ceph[0].health' "${WORK}/b1/manifest.json")" "HEALTH_OK"
expect_text "manifest records versions" "$(jq -r '.versions | length > 0' "${WORK}/b1/manifest.json")" "true"
expect_text "manifest lists file hashes" "$(jq -r '.files[] | select(.name == "helm-values.json") | .sha256 | length' "${WORK}/b1/manifest.json")" "64"
expect_text "ConfigMaps are kept" "$(jq -r '.items[0].data.vm' "${WORK}/b1/configmaps.json")" "fake-vm"
expect_text "values come from get values" "$(jq -r '.image.tag' "${WORK}/b1/helm-values.json")" "fake-tag"
expect_text "the manifest comes from get manifest" "$(cat "${WORK}/b1/helm-manifest.yaml")" "vws-fake"

run_vws_real backup --out "${WORK}/b1";                 expect_rc "an existing directory exits 2" 2

backup_scenario old
touch -d '-2 days' "${WORK}/old.db"
run_vws_real backup --out "${WORK}/b2" --etcd-snapshot "${WORK}/old.db"; expect_rc "an old snapshot exits 0" 0
expect_text "an old snapshot is a WARN" "$OUT" "over 24h"
run_vws_real --strict backup --out "${WORK}/b3";        expect_rc "--strict makes the missing snapshot exit 1" 1

backup_scenario nocm
echo '{"items":[]}' >"${VWS_FAKE_DIR}/kubectl/get_configmaps.json"
run_vws_real backup --out "${WORK}/b4";                 expect_rc "no ConfigMaps exits 0" 0
expect_text "no ConfigMaps is a WARN" "$OUT" "compute metadata is not in this backup"

backup_scenario secret
echo '{"auth":{"password":"fake-not-a-real-password"}}' >"${VWS_FAKE_DIR}/helm/get_values.json"
run_vws_real backup --out "${WORK}/b5";                 expect_rc "a secret in values exits 1" 1
expect_text "the scan names the hit" "$OUT" "secret-shaped lines"
if [[ -e ${WORK}/b5 ]]; then bad "a scan hit writes nothing" "${WORK}/b5 exists"; else ok "a scan hit writes nothing"; fi
expect_text "the secret is not printed" "$(if [[ $OUT == *fake-not-a-real-password* ]]; then echo leaked; else echo clean; fi)" "clean"

backup_scenario denied
rm "${VWS_FAKE_DIR}/helm/get_values.json"
echo 'Error: secrets is forbidden: User "fake-operator" cannot list resource "secrets"' >"${VWS_FAKE_DIR}/helm/get.err"
run_vws_real backup --out "${WORK}/b6";                 expect_rc "helm denied exits 1" 1
expect_text "helm denied names the cause" "$OUT" "run vws backup with your own kubeconfig"
if [[ -e ${WORK}/b6 ]]; then bad "helm denied writes nothing" "${WORK}/b6 exists"; else ok "helm denied writes nothing"; fi

run_vws_real backup --nosuch;                           expect_rc "an unknown flag exits 2" 2

finish_tests
