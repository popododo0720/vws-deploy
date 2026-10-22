#!/usr/bin/env bash
# Shared by vws reconfigure, upgrade, init, apply and restore. Source after common.sh.

# vws_crd_files: every CRD file of the chart, in name order. Helm installs crds/ on the first install
# only and never upgrades them, so a cluster keeps the CRDs of the chart it was installed with (#112,
# #382).
vws_crd_files() {
  local file
  for file in "${VWS_ROOT}"/charts/vws/crds/*.yaml; do
    [[ -f $file ]] && printf '%s\n' "$file"
  done
}

# vws_crd_current: 0 when every CRD in charts/vws/crds/*.yaml has the group and versions (schemas
# included) the chart ships. Read only; vws apply uses it to stay at zero writes when nothing changed
# (#114).
vws_crd_current() {
  local have
  have=$(VWS_NAMESPACE='' vws_kubectl get crd -o json 2>/dev/null) || return 1
  python3 - "$have" "${VWS_ROOT}/charts/vws/crds" <<'PY'
import json, sys
from pathlib import Path
import yaml

have = {item["metadata"]["name"]: item.get("spec") or {} for item in json.loads(sys.argv[1]).get("items") or []}
want = {}
for path in sorted(Path(sys.argv[2]).glob("*.yaml")):
    for doc in yaml.safe_load_all(path.read_text()):
        if doc and doc.get("kind") == "CustomResourceDefinition":
            want[doc["metadata"]["name"]] = doc.get("spec") or {}

def key(spec):
    return spec.get("group"), [
        (v.get("name"), v.get("served"), v.get("storage"), v.get("schema"), v.get("subresources"))
        for v in spec.get("versions") or []
    ]

sys.exit(0 if want and all(name in have and key(have[name]) == key(spec) for name, spec in want.items()) else 1)
PY
}

# vws_apply_crd: server-side apply every charts/vws/crds/*.yaml, then wait for the CRDs to be
# Established. A cluster installed before a newer CRD file was added has no such CRD, and Helm never
# installs crds/ on upgrade (#382). --force-conflicts takes .spec over from whoever created the CRD
# (helm, or kubectl client-side apply). Cluster-scoped: no -n. Never deletes a CRD.
vws_apply_crd() {
  local file
  local -a files
  mapfile -t files < <(vws_crd_files)
  ((${#files[@]} > 0)) || vws_die_env "charts/vws/crds holds no CRD files"
  for file in "${files[@]}"; do
    VWS_NAMESPACE='' vws_kubectl apply --server-side --field-manager=vws --force-conflicts -f - <"$file" >/dev/null ||
      vws_die_env "cannot apply $(basename "$file") from charts/vws/crds"
  done
  [[ -z $VWS_DRY_RUN ]] || return 0
  VWS_NAMESPACE='' vws_kubectl wait --for=condition=Established crd --all --timeout=300s >/dev/null ||
    vws_die_env "CRDs from charts/vws/crds are not Established"
}

# vws_save_spec CR_JSON SPEC_JSON: server-side apply the VWSCluster of CR_JSON with that spec.
vws_save_spec() {
  jq --argjson s "$2" '{apiVersion, kind, metadata: {name: .metadata.name, namespace: .metadata.namespace}, spec: $s}' <<<"$1" |
    vws_kubectl apply --server-side --field-manager=vws -f - >/dev/null
}

# vws_run_apply: run vws apply --unattended as a child and fold its result lines into this
# command's results (text and -o json alike). Exit 2 or 3 of the child is passed on; returns the
# child's exit code otherwise.
vws_run_apply() {
  local json rc=0 level name message
  json=$("${VWS_ROOT}/scripts/vws" -o json apply --unattended) || rc=$?
  if ((rc == 2 || rc == 3)); then
    [[ -z $json ]] || jq -r '.checks[] | "\(.level) \(.name) \(.message)"' <<<"$json" >&2
    ((rc == 2)) && vws_usage "vws apply rejected the saved CR"
    vws_die_env "vws apply could not evaluate the cluster"
  fi
  while IFS=$'\t' read -r level name message; do
    [[ -n $level ]] && vws_emit "$level" "$name" "$message"
  done < <(jq -r '.checks[] | [.level, .name, .message] | @tsv' <<<"$json")
  return "$rc"
}

# vws_apply_spec NAME CR_JSON NEW_SPEC_JSON: save the new spec and run vws apply. When apply fails
# (helm --atomic has rolled the release back), the previous spec of CR_JSON is saved again, a FAIL
# line is emitted and the command exits 1 through vws_finish, so CR and release agree.
vws_apply_spec() {
  local name=$1 cr=$2 new=$3 rc=0
  vws_apply_crd
  vws_save_spec "$cr" "$new" || vws_die_env "cannot save VWSCluster vws"
  vws_emit OK "$name" "saved VWSCluster vws; applying"
  vws_run_apply || rc=$?
  # shellcheck disable=SC2154 # vws_fail_count is set by lib/common.sh
  if ((rc != 0 || vws_fail_count > 0)); then
    vws_save_spec "$cr" "$(jq -c '.spec' <<<"$cr")" ||
      vws_die_env "apply failed and the previous VWSCluster spec could not be saved again; restore it by hand"
    vws_emit FAIL "$name" "apply failed (the release was rolled back by --atomic); the VWSCluster spec is back to the previous value; next: vws status, then fix and retry"
  fi
}

# vws_validate_rendered VALUES DIR WHAT: the chart schema (helm template) and scripts/check_render.py
# on rendered values, the same gate for vws apply and vws reconfigure (#246). Exit 2 with WHAT
# ("nothing was written", "nothing was applied") when either fails.
vws_validate_rendered() {
  local values=$1 dir=$2 what=$3
  vws_helm template vws "${VWS_ROOT}/charts/vws" --namespace "$VWS_NAMESPACE" -f "$values" >"${dir}/rendered.yaml" 2>"${dir}/helm.err" ||
    vws_usage "the rendered values fail charts/vws/values.schema.json: $(head -1 "${dir}/helm.err"); ${what}"
  python3 "${VWS_ROOT}/scripts/check_render.py" "${dir}/rendered.yaml" "${VWS_ROOT}/charts/vws/values.yaml" "$VWS_NAMESPACE" "$values" >"${dir}/check.out" 2>&1 ||
    vws_usage "scripts/check_render.py rejects the result: $(paste -sd";" "${dir}/check.out" | sed "s/;/; /g"); ${what}"
}
