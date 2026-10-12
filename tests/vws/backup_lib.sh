#!/usr/bin/env bash
# Scenario for backup and restore tests. Source after installer_lib.sh.

# backup_scenario NAME: installer_scenario plus the reads vws backup makes.
backup_scenario() {
  installer_scenario "$1"
  local h="${VWS_FAKE_DIR}/helm" k="${VWS_FAKE_DIR}/kubectl"
  echo '{"profile":"single-node","image":{"tag":"fake-tag"}}' >"$h/get_values.json"
  printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: vws-fake\n' >"$h/get_manifest.json"
  echo '[{"revision":1,"status":"deployed","description":"Install complete"}]' >"$h/history.json"
  echo 'v0.0.0-fake' >"$h/version.json"
  jq -n '{items: [{metadata: {name: "vm-meta-fake", namespace: "vws", labels: {"app.kubernetes.io/part-of": "vws"}, resourceVersion: "9"}, data: {vm: "fake-vm"}}]}' >"$k/get_configmaps.json"
  echo '{"items":[{"metadata":{"name":"state-snap-fake"}}]}' >"$k/get_volumesnapshots.json"
  echo '{"items":[{"metadata":{"name":"rook-ceph"},"status":{"ceph":{"health":"HEALTH_OK"}}}]}' >"$k/get_cephcluster.json"
}
