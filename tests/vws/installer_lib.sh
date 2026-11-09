#!/usr/bin/env bash
# Scenario setup for init, apply and installer tests. Source after lib.sh.
GROUP=$(sed -n 's/^  group: //p' "${VWS_TEST_ROOT}/charts/vws/crds/vwsclusters.yaml")
export VWS_SOURCE_CHECKS=0
INSTALLER_REAL_HELM=$(command -v helm)

# installer_template: the fake helm answers "template" with the real chart rendered from the
# scenario CR as kubectl/get_vwsclusters.json holds it, so the check_render.py gate of vws apply
# and reconfigure (#246) sees real manifests. installer_scenario calls it; a test that edits the CR calls it again.
installer_template() {
  python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/render.py" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" "${WORK}/tmpl-values.yaml"
  "$INSTALLER_REAL_HELM" template vws "${VWS_TEST_ROOT}/charts/vws" --namespace vws -f "${WORK}/tmpl-values.yaml" >"${VWS_FAKE_DIR}/helm/template.json"
}

# installer_scenario NAME: a fresh copy of tests/fixtures/installer in $WORK/NAME.
installer_scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/installer" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
  export VWS_BUNDLE_DIR="${WORK}/$1/bundle"
  jq --arg v "${GROUP}/v1alpha1" '.apiVersion = $v' "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json" >"${WORK}/cr.json"
  mv "${WORK}/cr.json" "${VWS_FAKE_DIR}/kubectl/get_vwsclusters.json"
  # The cluster already has every chart CRD; a test edits this list for an older one (#114).
  python3 - "${VWS_TEST_ROOT}/charts/vws/crds" "${VWS_FAKE_DIR}/kubectl/get_crd.json" <<'PY'
import json, sys
from pathlib import Path
import yaml

items = []
for path in sorted(Path(sys.argv[1]).glob("*.yaml")):
    items += [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
json.dump({"apiVersion": "v1", "kind": "List", "items": items}, open(sys.argv[2], "w"))
PY
  installer_template
}

# temp_binding AGE_SECONDS TTL: the temporary binding as kubectl get returns it.
temp_binding() {
  jq -n --arg t "$(date -u -d "-$1 seconds" +%Y-%m-%dT%H:%M:%SZ)" --arg ttl "$2" \
    '{metadata: {name: "vws-installer-temp", creationTimestamp: $t, annotations: {"vws-installer-ttl-seconds": $ttl}}}' \
    >"${VWS_FAKE_DIR}/kubectl/get_clusterrolebinding.json"
}

calls() { cat "$VWS_FAKE_LOG"; }

# saved_specs: the spec of every save of an existing VWSCluster vws (the JSON patch of
# vws_save_spec), one sorted JSON line each, in call order.
saved_specs() {
  python3 - "$VWS_FAKE_LOG" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    tool, verb, args = (line.rstrip("\n").split("\t", 2) + ["", ""])[:3]
    if tool == "kubectl" and verb == "patch" and " vwsclusters vws --type=json --field-manager=vws -p " in args:
        ops = json.loads(args.split(" -p ", 1)[1])
        print(json.dumps(next(o["value"] for o in ops if o["op"] == "replace" and o["path"] == "/spec"), sort_keys=True))
PY
}

# save_tests: the resourceVersion each save of an existing VWSCluster vws tests, one per line.
save_tests() {
  grep $'^kubectl\tpatch\t.* vwsclusters vws --type=json --field-manager=vws -p ' "$VWS_FAKE_LOG" | sed 's/.* -p //' |
    jq -r '.[0] | select(.op == "test" and .path == "/metadata/resourceVersion") | .value' || true
}
