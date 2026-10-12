#!/usr/bin/env bash
# vws render: VWSCluster CR -> values overlay. The fake helm stands in for the schema check;
# scripts/guard.sh profiles runs the same examples through the real helm.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

EX="${VWS_TEST_ROOT}/examples/vwscluster"

for profile in single-node small ha; do
  run_vws_real render "${EX}/${profile}.yaml" "${WORK}/${profile}.yaml"; expect_rc "render ${profile} exits 0" 0
  expect_text "render ${profile} sets global.profile" "$(cat "${WORK}/${profile}.yaml")" "profile: ${profile}"
  assert_no_changes "render ${profile} changes nothing"
  expect_text "render ${profile} checks the schema with helm template" "$(cat "$VWS_FAKE_LOG")" "template vws"
  run_vws_real render "${EX}/${profile}.yaml" "${WORK}/${profile}.again.yaml"
  if cmp -s "${WORK}/${profile}.yaml" "${WORK}/${profile}.again.yaml"; then ok "render ${profile} twice gives the same bytes"; else bad "render ${profile} is deterministic" "outputs differ"; fi
done
expect_text "existing driver sets the storage class" "$(cat "${WORK}/small.yaml")" "storageClass: fast-rbd"
expect_text "spec.version becomes global.tag" "$(cat "${WORK}/ha.yaml")" "tag: 0.1.0"
expect_text "a CR replicas override wins over the profile" "$(cat "${WORK}/ha.yaml")" "replicas: 4"

# bad NAME SED: render a broken copy of ha.yaml and expect exit 2.
bad_cr() {
  local name=$1 expr=$2
  sed -e "$expr" "${EX}/ha.yaml" >"${WORK}/bad.yaml"
  run_vws_real render "${WORK}/bad.yaml" "${WORK}/bad.out"
  expect_rc "invalid CR: ${name} exits 2" 2
  if [[ -e ${WORK}/bad.out ]]; then bad "invalid CR: ${name} writes nothing" "output exists"; rm -f "${WORK}/bad.out"; fi
}
bad_cr "unknown network driver" 's/driver: kube-ovn/driver: calico/'
bad_cr "unknown storage driver" 's/driver: rook-ceph/driver: nfs/'
bad_cr "unknown profile" 's/profile: ha/profile: huge/'
bad_cr "unknown field" 's/^  version: 0.1.0/  version: 0.1.0\n  colour: blue/'
bad_cr "unknown service field" 's/      replicas: 4/      replicas: 4\n      image: x/'
bad_cr "secret-shaped field" 's/^  version: 0.1.0/  version: 0.1.0\n  adminPassword: x/'
bad_cr "secret-shaped flag" 's/      replicas: 4/      replicas: 4\n      flags:\n        api-token: fake/'
bad_cr "a secret value under secrets" 's/^  version: 0.1.0/  version: 0.1.0\n  secrets:\n    db:\n      secretName: db\n      value: x/'
bad_cr "wrong name" 's/name: vws$/name: other/'
bad_cr "wrong group" 's#apiVersion: .*#apiVersion: example.com/v1alpha1#'
bad_cr "unknown service" 's/console-api:/billing:/'
bad_cr "replicas below one" 's/replicas: 4/replicas: 0/'
bad_cr "otlp endpoint the chart cannot take yet" 's/otlpEndpoint: ""/otlpEndpoint: http:\/\/collector:4317/'
bad_cr "existing driver without a class" 's/driver: rook-ceph/driver: existing/'
bad_cr "version with a slash" 's/version: 0.1.0/version: a\/b/'

# spec.storage.s3.rgw goes to services.storage.s3.rgw so vws apply keeps RGW on (#108).
sed -e 's/    storageClass: fast-rbd/    storageClass: fast-rbd\n    s3:\n      rgw:\n        enabled: true\n        endpoint: http:\/\/10.77.0.101:8080\n        bucketPrefix: vwslab\n        existingSecret: ceph-rgw-s3/' "${EX}/small.yaml" >"${WORK}/rgw.yaml"
run_vws_real render "${WORK}/rgw.yaml" "${WORK}/rgw.out"; expect_rc "s3.rgw renders" 0
if python3 -c 'import sys,yaml; r=yaml.safe_load(open(sys.argv[1]))["services"]["storage"]["s3"]["rgw"]; sys.exit(r != {"enabled": True, "endpoint": "http://10.77.0.101:8080", "bucketPrefix": "vwslab", "existingSecret": "ceph-rgw-s3"})' "${WORK}/rgw.out"; then
  ok "s3.rgw becomes services.storage.s3.rgw"; else bad "s3.rgw values" "$(cat "${WORK}/rgw.out")"; fi
if ! grep -q 's3:' "${WORK}/small.yaml"; then ok "without s3.rgw the overlay leaves the object backend off"; else bad "no s3 by default" "$(cat "${WORK}/small.yaml")"; fi
for prefix in abcdefghij123456789 Acme -acme; do
  sed -e "s/bucketPrefix: vwslab/bucketPrefix: ${prefix}/" "${WORK}/rgw.yaml" >"${WORK}/rgwbad.yaml"
  run_vws_real render "${WORK}/rgwbad.yaml" "${WORK}/rgwbad.out"; expect_rc "bucketPrefix ${prefix} exits 2" 2
done
sed -e 's/        existingSecret: ceph-rgw-s3/        existingSecret: ceph-rgw-s3\n        secretKey: x/' "${WORK}/rgw.yaml" >"${WORK}/rgwkey.yaml"
run_vws_real render "${WORK}/rgwkey.yaml" "${WORK}/rgwkey.out"; expect_rc "a key under s3.rgw exits 2" 2

printf -- '---\na: 1\n---\nb: 2\n' >"${WORK}/two.yaml"
run_vws_real render "${WORK}/two.yaml" "${WORK}/x.yaml"; expect_rc "two documents exit 2" 2
run_vws_real render "${WORK}/missing.yaml" "${WORK}/x.yaml"; expect_rc "a missing file exits 2" 2
run_vws_real render "${EX}/ha.yaml"; expect_rc "a missing output argument exits 2" 2

finish_tests
