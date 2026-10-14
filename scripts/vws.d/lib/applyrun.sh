#!/usr/bin/env bash
# Shared by vws reconfigure, upgrade, init, apply and restore. Source after common.sh.

# vws_apply_crd: server-side apply charts/vws/crds/vwsclusters.yaml. Helm installs crds/ on the first
# install only and never upgrades them, so a cluster keeps the CRD of the chart it was installed with
# and rejects a VWSCluster field a newer chart added ("field not declared in schema", #112). Every
# command that writes the CR (or applies a newer chart) calls this first. --force-conflicts takes
# .spec over from whoever created the CRD (helm, or kubectl client-side apply). Cluster-scoped: no -n.
# vws_crd_current: 0 when the cluster's VWSCluster CRD has the group and versions (schemas
# included) of charts/vws/crds/vwsclusters.yaml. Read only; vws apply uses it to stay at zero
# writes when nothing changed (#114).
vws_crd_current() {
  local crd have
  crd=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["metadata"]["name"])' "${VWS_ROOT}/charts/vws/crds/vwsclusters.yaml")
  have=$(VWS_NAMESPACE='' vws_kubectl get crd "$crd" -o json 2>/dev/null) || return 1
  python3 - "$have" "${VWS_ROOT}/charts/vws/crds/vwsclusters.yaml" <<'PY'
import json, sys, yaml
have = json.loads(sys.argv[1]).get("spec") or {}
want = yaml.safe_load(open(sys.argv[2]))["spec"]
def key(s):
    return s.get("group"), [(v.get("name"), v.get("served"), v.get("storage"), v.get("schema"), v.get("subresources")) for v in s.get("versions") or []]
sys.exit(0 if key(have) == key(want) else 1)
PY
}

vws_apply_crd() {
  VWS_NAMESPACE='' vws_kubectl apply --server-side --field-manager=vws --force-conflicts -f - \
    <"${VWS_ROOT}/charts/vws/crds/vwsclusters.yaml" >/dev/null ||
    vws_die_env "cannot apply the VWSCluster CRD from charts/vws/crds/vwsclusters.yaml"
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
