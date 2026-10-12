#!/usr/bin/env bash
# Scenario helpers of status.test.sh and doctor.test.sh. Source after lib.sh.
GROUP=$(sed -n 's/^  group: //p' "${VWS_TEST_ROOT}/charts/vws/crds/vwsclusters.yaml")

scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/status/ok" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
  edit_json kubectl/get_vwsclusters.json --arg v "${GROUP}/v1alpha1" '.apiVersion = $v'
  edit_json kubectl/get_crd.json --arg g "$GROUP" '(.items[] | select(.spec.group == "FILLED_BY_TEST")) |= (.spec.group = $g | .metadata.name = "vwsclusters." + $g)'
  # Versions live only in scripts/versions.env (R12): the operators report what it pins.
  edit_json kubectl/get_kubevirt.json --arg v "$(sed -n 's/^KUBEVIRT_VERSION=//p' "${VWS_TEST_ROOT}/scripts/versions.env")" '.items[0].status.observedKubeVirtVersion = $v'
  edit_json kubectl/get_cdi.json --arg v "$(sed -n 's/^CDI_VERSION=//p' "${VWS_TEST_ROOT}/scripts/versions.env")" '.items[0].status.observedVersion = $v'
}
edit_json() { # FILE [JQ ARGS...] FILTER
  local f="${VWS_FAKE_DIR}/$1"
  shift
  jq "$@" "$f" >"${f}.new" && mv "${f}.new" "$f"
}
line() { grep -E "^$1 +$2 " <<<"$OUT" || true; }
expect_line() { # NAME LEVEL CHECK [TEXT]
  local l
  l=$(line "$2" "$3")
  if [[ -n $l && $l == *"${4:-}"* ]]; then ok "$1"; else bad "$1" "no '$2 $3 ${4:-}' line in: $OUT"; fi
}
fails_have_next() { # NAME: every FAIL line names a next step
  local missing
  missing=$(grep -E '^FAIL' <<<"$OUT" | grep -v 'next: ' || true)
  if [[ -z $missing ]]; then ok "$1"; else bad "$1" "$missing"; fi
}

