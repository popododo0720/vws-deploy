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

printf -- '---\na: 1\n---\nb: 2\n' >"${WORK}/two.yaml"
run_vws_real render "${WORK}/two.yaml" "${WORK}/x.yaml"; expect_rc "two documents exit 2" 2
run_vws_real render "${WORK}/missing.yaml" "${WORK}/x.yaml"; expect_rc "a missing file exits 2" 2
run_vws_real render "${EX}/ha.yaml"; expect_rc "a missing output argument exits 2" 2

finish_tests
