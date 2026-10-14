#!/usr/bin/env bash
# summary: install or update VWS from the VWSCluster CR (bundle in order, then the chart)
# Sourced by scripts/vws. Usage: vws apply [--unattended] [-f FILE]
# -f FILE saves FILE as the VWSCluster first (vws init --from FILE: invalid exits 2, the same spec is
# unchanged), then applies it; the unattended path of the installer (#144).
# Steps: TTL guard -> read CR -> render -> unchanged if status matches -> preflight -> secrets ->
# platform bundle in build/platform file order with waits -> vws_helm upgrade --atomic --wait
# -> CR status. --dry-run prints the changing calls and changes nothing. --unattended asks nothing
# (apply asks nothing today; the flag is accepted so automation can state it).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl helm sha256sum)
# shellcheck source=scripts/vws.d/lib/hash.sh
source "${VWS_ROOT}/scripts/vws.d/lib/hash.sh"
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"
# shellcheck source=scripts/vws.d/lib/checks.sh
source "${VWS_ROOT}/scripts/vws.d/lib/checks.sh"
# shellcheck source=scripts/vws.d/lib/applyrun.sh
source "${VWS_ROOT}/scripts/vws.d/lib/applyrun.sh"

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
  local cr tmp values hash generation applied observed profile file from=''
  while (($#)); do
    case $1 in
      --unattended) shift ;;
      -f | --file) (($# >= 2)) || vws_usage "-f needs a file"; from=$2; shift 2 ;;
      -f=* | --file=*) from=${1#*=}; shift ;;
      *) vws_usage "usage: vws apply [--unattended] [-f FILE]" ;;
    esac
  done
  if [[ -n $from ]]; then
    local rc=0
    "${VWS_ROOT}/scripts/vws" init --from "$from" || rc=$?
    ((rc == 0)) || exit "$rc"
  fi
  vws_installer_namespace
  vws_installer_guard_ttl
  # The CRD before the CR is read: an older CRD prunes fields a newer chart added (spec.storage.s3),
  # and the values rendered from that CR would turn them off (#114). Helm never upgrades crds/ (#112).
  if ! vws_crd_current; then
    vws_apply_crd
    vws_emit OK crd "VWSCluster CRD applied from charts/vws/crds"
  fi
  cr=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) || vws_usage "no VWSCluster vws in ${VWS_NAMESPACE}; run vws init first"
  tmp=$(mktemp -d)
  jq '{apiVersion, kind, metadata: {name: .metadata.name}, spec}' <<<"$cr" >"${tmp}/cr.yaml"
  if ! python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "${tmp}/cr.yaml" "${tmp}/values.yaml"; then
    rm -rf "$tmp"
    vws_usage "the VWSCluster in the cluster is invalid (see above)"
  fi
  values="${tmp}/values.yaml"
  hash=$(vws_applied_hash "$values")
  generation=$(jq -r '.metadata.generation // 0' <<<"$cr")
  applied=$(jq -r '.status.appliedValuesHash // ""' <<<"$cr")
  observed=$(jq -r '.status.observedGeneration // -1' <<<"$cr")
  profile=$(jq -r '.spec.profile' <<<"$cr")
  vws_apply_drop_extgw
  if [[ $applied == "$hash" && $observed == "$generation" ]]; then
    rm -rf "$tmp"
    vws_emit OK apply "unchanged: generation ${generation} is applied (values ${hash:0:12})"
    return 0
  fi

  vws_apply_preflight "$profile"
  vws_apply_secrets "$values"

  # VWS_BUNDLE_DIR: the rendered bundle, build/platform from scripts/platform.sh (the harness points it at fixtures).
  local bundle=${VWS_BUNDLE_DIR:-${VWS_ROOT}/build/platform}
  compgen -G "${bundle}/*.yaml" >/dev/null || vws_die_env "${bundle} has no bundle files; the installer image carries them (scripts/platform.sh)"
  # spec.network.nodes picks the Kube-OVN interface and MTU, the Ceph networks and the external gateway (#127).
  python3 "${VWS_ROOT}/scripts/vws.d/lib/platform.py" "${tmp}/cr.yaml" "$bundle" "${tmp}/bundle" ||
    { rm -rf "$tmp"; vws_usage "the bundle does not fit spec.network.nodes (see above)"; }
  bundle="${tmp}/bundle"
  for file in "${bundle}"/*.yaml; do
    # Bundle objects carry their own namespaces (kube-system, kubevirt, cdi, ...). Without -n, so
    # kubectl does not refuse them against the release namespace; the same for the -A wait (vws-deploy#81).
    # --force-conflicts: operators of the bundle (cdi-operator on its CRDs) take fields over after the
    # first apply, so a second apply would conflict; the bundle file is the wanted state (vws-deploy#81).
    VWS_NAMESPACE='' vws_kubectl apply --server-side --field-manager=vws --force-conflicts -f - <"$file" >/dev/null || vws_die_env "applying $(basename "$file") failed"
    vws_kubectl wait --for=condition=Established crd --all --timeout=300s >/dev/null || vws_die_env "CRDs not Established after $(basename "$file")"
    VWS_NAMESPACE='' vws_kubectl wait --for=condition=Available deployment --all -A --timeout=600s >/dev/null || vws_die_env "Deployments not Available after $(basename "$file")"
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
  vws_apply_seed_image "$values"

  vws_kubectl patch vwsclusters vws --subresource=status --type=merge -p "$(jq -nc --argjson g "$generation" --arg h "$hash" --argjson sh "$history" \
    '{status: {observedGeneration: $g, appliedValuesHash: $h, specHistory: $sh, conditions: [{type: "Applied", status: "True", reason: "Applied", message: "vws apply"}]}}')" >/dev/null ||
    vws_die_env "cannot write VWSCluster status"
  rm -rf "$tmp"
  vws_emit OK apply "applied generation ${generation} (values ${hash:0:12})"
}

# vws_apply_drop_extgw deletes the ovn-external-gw-config ConfigMap that an apply before #165 wrote
# (field manager vws). With it, kube-ovn deletes the OVS bridge the external NIC is on (br-vwsul)
# and wants a Subnet named external (#171, vws-test evidence 53). It runs before the unchanged
# check, so an apply of an unchanged CR also removes it. A ConfigMap that vws did not write is left
# alone with a WARN.
vws_apply_drop_extgw() {
  local cm
  cm=$(VWS_NAMESPACE=kube-system vws_kubectl get configmap ovn-external-gw-config -o json --ignore-not-found) ||
    vws_die_env "cannot read kube-system/ovn-external-gw-config"
  [[ -n $cm ]] || return 0
  if ! jq -e 'any(.metadata.managedFields[]?; .manager == "vws")' <<<"$cm" >/dev/null; then
    vws_emit WARN extgw "kube-system/ovn-external-gw-config exists but vws did not write it; left as is (it breaks the underlay bridge, #171)"
    return 0
  fi
  VWS_NAMESPACE=kube-system vws_kubectl delete configmap ovn-external-gw-config --ignore-not-found >/dev/null ||
    vws_die_env "cannot delete kube-system/ovn-external-gw-config"
  vws_emit OK extgw "deleted kube-system/ovn-external-gw-config from an earlier apply (#171)"
}

# VWS_DEFAULT_IMAGE_ID is the image id console-api asks for on every run-instances. vws-storage
# (#88) clones an image volume from the CDI DataSource of that name in the images namespace, and
# vws-compute keeps an image as a DataVolume and a DataSource of the same name there (#232).
VWS_DEFAULT_IMAGE_ID=ami-0123456789abcdef0
VWS_DEFAULT_IMAGE_SIZE=${VWS_DEFAULT_IMAGE_SIZE:-1Gi}

# vws_apply_seed_image VALUES seeds the default image after the chart (CDI is installed by the bundle,
# the images namespace by the chart): a DataVolume importing LAB_VM_IMAGE of scripts/versions.env
# (pinned by digest) and a DataSource pointing at its claim. Each is created only when missing
# (AlreadyExists keeps it as is), so an apply never overwrites or re-imports an image.
vws_apply_seed_image() {
  local values=$1 ns image kind err manifest
  ns=$(python3 -c 'import sys,yaml; print((yaml.safe_load(open(sys.argv[1])) or {}).get("namespaces", {}).get("images", ""))' "$values") ||
    vws_die_env "cannot read namespaces.images from the rendered values"
  [[ -n $ns ]] || ns=vws-images
  image=$(vws_versions_env LAB_VM_IMAGE)
  [[ $image == *@sha256:* ]] || vws_die_env "LAB_VM_IMAGE in scripts/versions.env is not pinned by digest"
  err=$(mktemp)
  for kind in DataVolume DataSource; do
    manifest=$(vws_seed_image_manifest "$kind" "$ns" "$image")
    if [[ -n $VWS_DRY_RUN ]]; then
      VWS_NAMESPACE=$ns vws_kubectl create -f - <<<"$manifest"
      vws_emit OK image "would create ${kind} ${ns}/${VWS_DEFAULT_IMAGE_ID} if it is missing"
    elif VWS_NAMESPACE=$ns vws_kubectl create -f - <<<"$manifest" >/dev/null 2>"$err"; then
      vws_emit OK image "created ${kind} ${ns}/${VWS_DEFAULT_IMAGE_ID}"
    elif grep -q -e 'AlreadyExists' -e 'already exists' "$err"; then
      vws_emit OK image "kept ${kind} ${ns}/${VWS_DEFAULT_IMAGE_ID}: it exists, unchanged"
    else
      rm -f "$err"
      vws_die_env "cannot create ${kind} ${ns}/${VWS_DEFAULT_IMAGE_ID}"
    fi
  done
  rm -f "$err"
}

# vws_seed_image_manifest KIND NAMESPACE IMAGE: the JSON of the seeded DataVolume or DataSource.
vws_seed_image_manifest() {
  jq -nc --arg k "$1" --arg ns "$2" --arg img "$3" --arg id "$VWS_DEFAULT_IMAGE_ID" --arg size "$VWS_DEFAULT_IMAGE_SIZE" '
    {apiVersion: "cdi.kubevirt.io/v1beta1", kind: $k,
     metadata: {name: $id, namespace: $ns, labels: {"app.kubernetes.io/managed-by": "vws"}}}
    + if $k == "DataVolume" then
        {spec: {source: {registry: {url: ("docker://" + $img)}}, storage: {resources: {requests: {storage: $size}}}}}
      else
        {spec: {source: {pvc: {name: $id, namespace: $ns}}}}
      end'
}

# vws_apply_secrets VALUES creates each Secret the chart reads by secretKeyRef and vws makes itself
# (services.*.apiCredentials.existingSecret), once, in the release namespace. The values come from
# lib/apisecret.py on the stdin of vws_kubectl: never in an argument, a file, the output or the CR.
# An existing Secret is kept as is (AlreadyExists), so an apply never rotates a credential and
# needs no read access to Secrets. Only names reach the output.
vws_apply_secrets() {
  local values=$1 name err list
  list=$(python3 "${VWS_ROOT}/scripts/vws.d/lib/apisecret.py" names "$values") || vws_die_env "cannot list the Secrets the chart needs"
  if [[ -z $list ]]; then
    vws_emit OK secrets "none required: no service has apiCredentials"
    return 0
  fi
  err=$(mktemp)
  while IFS= read -r name; do
    # --dry-run: print the call, make no values (vws_kubectl would not read them).
    if [[ -n $VWS_DRY_RUN ]]; then
      vws_kubectl create -f - </dev/null
      vws_emit OK secrets "would create Secret ${name} if it is missing"
    elif python3 "${VWS_ROOT}/scripts/vws.d/lib/apisecret.py" manifest "$name" | vws_kubectl create -f - >/dev/null 2>"$err"; then
      vws_emit OK secrets "created Secret ${name} with fresh random values"
    elif grep -q -e 'AlreadyExists' -e 'already exists' "$err"; then
      vws_emit OK secrets "kept Secret ${name}: it exists, values unchanged"
    else
      rm -f "$err"
      vws_die_env "cannot create Secret ${name} in the release namespace; nothing else was changed"
    fi
  done <<<"$list"
  rm -f "$err"
}
