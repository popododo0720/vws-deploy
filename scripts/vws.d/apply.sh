#!/usr/bin/env bash
# summary: install or update VWS from the VWSCluster CR (bundle in order, then the chart)
# Sourced by scripts/vws. Usage: vws apply [--unattended]
# Steps: TTL guard -> read CR -> render -> unchanged if status matches -> preflight -> secrets ->
# platform bundle in build/platform file order with waits -> vws_helm upgrade --atomic --wait
# -> CR status. --dry-run prints the changing calls and changes nothing. --unattended asks nothing
# (apply asks nothing today; the flag is accepted so automation can state it).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl helm sha256sum)
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"
# shellcheck source=scripts/vws.d/lib/checks.sh
source "${VWS_ROOT}/scripts/vws.d/lib/checks.sh"

vws_apply_preflight() {
  local profile=$1
  vws_check_api
  vws_check_nodes "$profile"
  vws_check_storage preflight
  vws_check_release
  # shellcheck disable=SC2154 # set by lib/output.sh
  ((vws_fail_count == 0)) || { vws_emit FAIL apply "preflight failed; nothing was changed"; vws_finish; }
}

vws_cmd_main() {
  local cr tmp values hash generation applied observed profile file
  while (($#)); do
    case $1 in
      --unattended) shift ;;
      *) vws_usage "usage: vws apply [--unattended]" ;;
    esac
  done
  vws_installer_namespace
  vws_installer_guard_ttl
  cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) || vws_usage "no VWSCluster vws in ${VWS_NAMESPACE}; run vws init first"
  tmp=$(mktemp -d)
  jq '{apiVersion, kind, metadata: {name: .metadata.name}, spec}' <<<"$cr" >"${tmp}/cr.yaml"
  if ! python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "${tmp}/cr.yaml" "${tmp}/values.yaml"; then
    rm -rf "$tmp"
    vws_usage "the VWSCluster in the cluster is invalid (see above)"
  fi
  values="${tmp}/values.yaml"
  hash=$(sha256sum "$values" | cut -d' ' -f1)
  generation=$(jq -r '.metadata.generation // 0' <<<"$cr")
  applied=$(jq -r '.status.appliedValuesHash // ""' <<<"$cr")
  observed=$(jq -r '.status.observedGeneration // -1' <<<"$cr")
  profile=$(jq -r '.spec.profile' <<<"$cr")
  if [[ $applied == "$hash" && $observed == "$generation" ]]; then
    rm -rf "$tmp"
    vws_emit OK apply "unchanged: generation ${generation} is applied (values ${hash:0:12})"
    return 0
  fi

  vws_apply_preflight "$profile"
  vws_emit OK secrets "none required: the chart takes no Secret from the installer"

  # VWS_BUNDLE_DIR: the rendered bundle, build/platform from scripts/platform.sh (the harness points it at fixtures).
  local bundle=${VWS_BUNDLE_DIR:-${VWS_ROOT}/build/platform}
  compgen -G "${bundle}/*.yaml" >/dev/null || vws_die_env "${bundle} has no bundle files; the installer image carries them (scripts/platform.sh)"
  for file in "${bundle}"/*.yaml; do
    vws_kubectl apply --server-side --field-manager=vws -f - <"$file" >/dev/null || vws_die_env "applying $(basename "$file") failed"
    vws_kubectl wait --for=condition=Established crd --all --timeout=300s >/dev/null || vws_die_env "CRDs not Established after $(basename "$file")"
    vws_kubectl wait --for=condition=Available deployment --all -A --timeout=600s >/dev/null || vws_die_env "Deployments not Available after $(basename "$file")"
    vws_emit OK bundle "$(basename "$file")"
  done

  # --description carries the spec this revision came from, so vws rollback can put the CR back in
  # step with the release (vws-deploy#60). A spec longer than VWS_SPEC_DESCRIPTION_MAX characters is
  # recorded by its sha256 only and kept in full in status.specHistory (newest 10).
  # --history-max 10 bounds the release history (#43 section 5).
  local spec_json spec_hash description history
  spec_json=$(jq -cS '.spec' <<<"$cr")
  spec_hash=$(printf '%s' "$spec_json" | sha256sum | cut -d' ' -f1)
  history=$(jq -c '.status.specHistory // []' <<<"$cr")
  if ((${#spec_json} + 9 <= ${VWS_SPEC_DESCRIPTION_MAX:-1024})); then
    description="vws-spec:${spec_json}"
  else
    description="vws-spec-sha256:${spec_hash}"
    history=$(jq -c --arg h "$spec_hash" --argjson s "$spec_json" '[.[] | select(.specHash != $h)] + [{specHash: $h, spec: $s}] | .[-10:]' <<<"$history")
  fi
  vws_helm upgrade --install vws "${VWS_ROOT}/charts/vws" -f "$values" --atomic --wait --timeout 15m --history-max 10 \
    --description "$description" >/dev/null ||
    { rm -rf "$tmp"; vws_emit FAIL helm "helm release upgrade failed and rolled back (--atomic)"; vws_finish; }
  vws_emit OK helm "release vws upgraded"

  vws_kubectl patch vwsclusters vws --subresource=status --type=merge -p "$(jq -nc --argjson g "$generation" --arg h "$hash" --argjson sh "$history" \
    '{status: {observedGeneration: $g, appliedValuesHash: $h, specHistory: $sh, conditions: [{type: "Applied", status: "True", reason: "Applied", message: "vws apply"}]}}')" >/dev/null ||
    vws_die_env "cannot write VWSCluster status"
  rm -rf "$tmp"
  vws_emit OK apply "applied generation ${generation} (values ${hash:0:12})"
}
