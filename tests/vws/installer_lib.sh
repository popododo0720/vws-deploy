#!/usr/bin/env bash
# Scenario setup for init, apply and installer tests. Source after lib.sh.
GROUP=$(sed -n 's/^  group: //p' "${VWS_TEST_ROOT}/charts/vws/crds/vwsclusters.yaml")
export VWS_SOURCE_CHECKS=0

# installer_scenario NAME: a fresh copy of tests/fixtures/installer in $WORK/NAME.
installer_scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/installer" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
  export VWS_BUNDLE_DIR="${WORK}/$1/bundle"
  jq --arg v "${GROUP}/v1alpha1" '.apiVersion = $v' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
  mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
}

# temp_binding AGE_SECONDS TTL: the temporary binding as kubectl get returns it.
temp_binding() {
  jq -n --arg t "$(date -u -d "-$1 seconds" +%Y-%m-%dT%H:%M:%SZ)" --arg ttl "$2" \
    '{metadata: {name: "vws-installer-temp", creationTimestamp: $t, annotations: {"vws-installer-ttl-seconds": $ttl}}}' \
    >"${VWS_FAKE_DIR}/kubectl/get_clusterrolebinding.json"
}

calls() { cat "$VWS_FAKE_LOG"; }
