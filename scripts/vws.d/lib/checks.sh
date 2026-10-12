#!/usr/bin/env bash
# Checks shared by vws preflight, status and doctor (vws-deploy#43 section 4). One function per check;
# the function name after vws_check_ is the check name in the output. Source it after common.sh.
# Every function only reads the cluster (vws_kubectl/vws_helm read verbs). A check that cannot be
# evaluated because the API is unreachable exits 3 through vws_die_env.
# MODE is preflight (things the bundle installs later may be missing: WARN) or doctor (missing: FAIL).

# Minimum Ready nodes per profile (vws-deploy#43 section 2). CPU and memory minima come from the
# profile file itself: the sum of requests over replicas.
declare -A VWS_PROFILE_MIN_NODES=([single-node]=1 [small]=3 [ha]=6)

# vws_versions_env NAME: a value from scripts/versions.env. Versions are never written in this code.
vws_versions_env() {
  sed -n "s/^$1=//p" "${VWS_ROOT}/scripts/versions.env"
}

vws_missing() { # LEVEL_IF_PREFLIGHT MODE
  if [[ $2 == doctor ]]; then echo FAIL; else echo "$1"; fi
}

vws_check_tools() {
  local want have
  want=$(vws_versions_env HELM_VERSION)
  have=$(vws_helm version --short 2>/dev/null) || vws_die_env "helm version failed"
  if [[ $have == "${want}"* ]]; then
    vws_emit OK tools "helm ${have}"
  else
    vws_emit WARN tools "helm is ${have}, scripts/versions.env pins ${want}"
  fi
}

# vws_check_api: the server answers. Prints the context name so a production context stands out.
vws_check_api() {
  local context
  vws_kubectl version -o json >/dev/null 2>&1 || vws_die_env "the Kubernetes API does not answer"
  context=$(vws_kubectl config current-context 2>/dev/null || echo unknown)
  vws_emit OK api "context ${VWS_CONTEXT:-$context}"
}

vws_check_k8s_minor() {
  local want server
  want=$(vws_versions_env KUBERNETES_VERSION | cut -d. -f1,2)
  server=$(vws_kubectl version -o json 2>/dev/null | jq -r '.serverVersion | "\(.major).\(.minor | sub("[^0-9]+$"; ""))"') ||
    vws_die_env "cannot read the server version"
  if [[ $server == "$want" ]]; then
    vws_emit OK k8s-minor "server ${server}"
  else
    vws_emit WARN k8s-minor "server ${server}, the bundle is tested on ${want} (KUBERNETES_VERSION)"
  fi
}

# The kinds an install creates: the bundle (namespaces, CRDs, cluster RBAC) and the chart.
VWS_INSTALL_RIGHTS=(namespaces customresourcedefinitions.apiextensions.k8s.io clusterroles.rbac.authorization.k8s.io
  clusterrolebindings.rbac.authorization.k8s.io roles.rbac.authorization.k8s.io rolebindings.rbac.authorization.k8s.io
  serviceaccounts services deployments.apps statefulsets.apps poddisruptionbudgets.policy leases.coordination.k8s.io)

vws_check_install_rights() {
  local resource answer missing=()
  for resource in "${VWS_INSTALL_RIGHTS[@]}"; do
    answer=$(vws_kubectl auth can-i create "$resource" 2>/dev/null || true)
    [[ $answer == yes ]] || missing+=("create ${resource}")
  done
  if ((${#missing[@]})); then
    vws_emit FAIL install-rights "cannot: ${missing[*]}"
  else
    vws_emit OK install-rights "create on ${#VWS_INSTALL_RIGHTS[@]} kinds"
  fi
}

# vws_profile_needs PROFILE: "<millicores> <MiB>" the profile requests in total.
vws_profile_needs() {
  python3 - "${VWS_ROOT}/charts/vws/values.yaml" "${VWS_ROOT}/profiles/$1.yaml" <<'PY'
import sys, yaml
import os
base = yaml.safe_load(open(sys.argv[1]))["services"]
# Without the profile file (profiles/ arrives with vws-deploy#53), the chart defaults count.
prof = yaml.safe_load(open(sys.argv[2]))["services"] if os.path.exists(sys.argv[2]) else {}
cpu = mem = 0
for name, spec in base.items():
    spec = {**spec, **prof.get(name, {})}
    n = spec.get("replicas", 1 if "state" in spec else 2)
    c, m = spec.get("cpu", "100m"), spec.get("memory", "256Mi")
    cpu += n * int(c[:-1])
    mem += n * (int(m[:-2]) * (1024 if m.endswith("Gi") else 1))
print(cpu, mem)
PY
}

vws_check_nodes() {
  local profile=$1 json ready need_cpu need_mem have_cpu have_mem min
  min=${VWS_PROFILE_MIN_NODES[$profile]:-}
  [[ -n $min ]] || vws_usage "unknown profile ${profile}"
  json=$(vws_kubectl get nodes -o json 2>/dev/null) || vws_die_env "cannot list nodes"
  ready=$(jq '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' <<<"$json")
  if ((ready >= min)); then
    vws_emit OK nodes "${ready} Ready, profile ${profile} needs ${min}"
  else
    vws_emit FAIL nodes "${ready} Ready, profile ${profile} needs ${min}"
  fi
  read -r need_cpu need_mem < <(vws_profile_needs "$profile")
  read -r have_cpu have_mem < <(jq -r '
    def cpu: if endswith("m") then .[:-1] | tonumber else tonumber * 1000 end;
    def mib: if endswith("Ki") then .[:-2] | tonumber / 1024 elif endswith("Mi") then .[:-2] | tonumber
             elif endswith("Gi") then .[:-2] | tonumber * 1024 else tonumber / 1048576 end;
    [.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True")) | .status.allocatable]
    | "\(map(.cpu | cpu) | add // 0 | floor) \(map(.memory | mib) | add // 0 | floor)"' <<<"$json")
  if ((have_cpu >= need_cpu && have_mem >= need_mem)); then
    vws_emit OK node-resources "allocatable ${have_cpu}m/${have_mem}Mi, VWS requests ${need_cpu}m/${need_mem}Mi"
  else
    vws_emit FAIL node-resources "allocatable ${have_cpu}m/${have_mem}Mi, VWS requests ${need_cpu}m/${need_mem}Mi"
  fi
  vws_check_node_ip "$json"
}

# vws_check_node_ip NODES_JSON: node addresses must be static (docs/quickstart.md). A k3s node that
# does not pin --node-ip follows whatever DHCP hands out; after a lease change the node, the CNI
# tunnels and every certificate SAN point at the old address (vws-deploy#118). Only k3s exposes the
# flags (annotation k3s.io/node-args); other distributions are not checked.
vws_check_node_ip() {
  local json=$1 bad k3s
  k3s=$(jq '[.items[] | select(.metadata.annotations["k3s.io/node-args"])] | length' <<<"$json")
  bad=$(jq -r '[.items[] | select(.metadata.annotations["k3s.io/node-args"]) |
      (.metadata.annotations["k3s.io/node-args"] | try fromjson catch []) as $a |
      ([range(0; $a | length) as $i | if $a[$i] == "--node-ip" then $a[$i + 1]
        elif ($a[$i] | startswith("--node-ip=")) then $a[$i][10:] else empty end] | first // "") as $pin |
      ([.status.addresses[]? | select(.type == "InternalIP") | .address] | first // "") as $ip |
      select($pin == "" or $pin != $ip) |
      "\(.metadata.name) (InternalIP \($ip), --node-ip \(if $pin == "" then "not set" else $pin end))"] | join(", ")' <<<"$json")
  if ((k3s == 0)); then
    vws_emit OK node-ip "not k3s; keep node addresses static (docs/quickstart.md)"
  elif [[ -z $bad ]]; then
    vws_emit OK node-ip "${k3s} k3s nodes pin --node-ip to their InternalIP"
  else
    vws_emit WARN node-ip "${bad}: pin a static address with --node-ip, a DHCP change breaks the node (docs/quickstart.md)"
  fi
}

vws_check_storage() {
  local mode=$1 classes snaps
  classes=$(vws_kubectl get storageclass -o json 2>/dev/null) || vws_die_env "cannot list StorageClasses"
  if jq -e '[.items[] | select(.metadata.annotations["storageclass.kubernetes.io/is-default-class"] == "true")] | length > 0' <<<"$classes" >/dev/null; then
    vws_emit OK default-storageclass "$(jq -r '[.items[] | select(.metadata.annotations["storageclass.kubernetes.io/is-default-class"] == "true") | .metadata.name] | join(",")' <<<"$classes")"
  else
    vws_emit FAIL default-storageclass "no default StorageClass"
  fi
  if jq -e '.items[] | select(.metadata.name == "ceph-block")' <<<"$classes" >/dev/null; then
    vws_emit OK ceph-block "StorageClass ceph-block exists"
  else
    vws_emit "$(vws_missing WARN "$mode")" ceph-block "StorageClass ceph-block is missing (the bundle creates it, docs/platform-bundle.md)"
  fi
  snaps=$(vws_kubectl get volumesnapshotclass -o json 2>/dev/null || echo '{"items":[]}')
  if jq -e '.items[] | select(.metadata.name == "ceph-block")' <<<"$snaps" >/dev/null; then
    vws_emit OK ceph-block-snapshot "VolumeSnapshotClass ceph-block exists"
  else
    vws_emit "$(vws_missing WARN "$mode")" ceph-block-snapshot "VolumeSnapshotClass ceph-block is missing (the bundle creates it)"
  fi
}

# Groups whose CRDs the bundle installs.
VWS_BUNDLE_GROUPS=(kubevirt.io cdi.kubevirt.io kubeovn.io k8s.cni.cncf.io snapshot.storage.k8s.io)

vws_check_crds() {
  local mode=$1 json group total bad
  json=$(vws_kubectl get crd -o json 2>/dev/null) || vws_die_env "cannot list CRDs"
  for group in "${VWS_BUNDLE_GROUPS[@]}"; do
    total=$(jq --arg g "$group" '[.items[] | select(.spec.group == $g)] | length' <<<"$json")
    bad=$(jq -r --arg g "$group" '[.items[] | select(.spec.group == $g)
      | select(any(.status.conditions[]?; .type == "Established" and .status == "True") | not) | .metadata.name] | join(",")' <<<"$json")
    if ((total == 0)); then
      vws_emit "$(vws_missing WARN "$mode")" "crd-${group}" "no CRDs of ${group} yet (the bundle installs them)"
    elif [[ -n $bad ]]; then
      vws_emit FAIL "crd-${group}" "not Established: ${bad}"
    else
      vws_emit OK "crd-${group}" "${total} Established"
    fi
  done
  vws_check_bundle_phase "$mode" kubevirt KubeVirt
  vws_check_bundle_phase "$mode" cdi CDI
}

# vws_check_bundle_phase MODE RESOURCE KIND: the operator CR reports phase Deployed.
vws_check_bundle_phase() {
  local mode=$1 resource=$2 kind=$3 json phase
  json=$(vws_kubectl get "$resource" -A -o json 2>/dev/null || echo '{"items":[]}')
  phase=$(jq -r '[.items[] | .status.phase // "unknown"] | join(",")' <<<"$json")
  if [[ -z $phase ]]; then
    vws_emit "$(vws_missing WARN "$mode")" "${resource}-phase" "no ${kind} CR yet"
  elif [[ $phase == Deployed ]]; then
    vws_emit OK "${resource}-phase" "${kind} Deployed"
  else
    vws_emit FAIL "${resource}-phase" "${kind} phase ${phase}"
  fi
}

# Kernel modules are checked indirectly (no --node-probe in the MVP, vws-deploy#43 decision 3):
# KubeVirt marks nodes that can run VMs (kvm) with kubevirt.io/schedulable.
vws_check_kernel_indirect() {
  local mode=$1 json schedulable
  json=$(vws_kubectl get nodes -o json 2>/dev/null) || vws_die_env "cannot list nodes"
  schedulable=$(jq '[.items[] | select(.metadata.labels["kubevirt.io/schedulable"] == "true")] | length' <<<"$json")
  if ((schedulable > 0)); then
    vws_emit OK kernel-indirect "${schedulable} nodes kubevirt.io/schedulable"
  else
    vws_emit "$(vws_missing WARN "$mode")" kernel-indirect "no node is kubevirt.io/schedulable yet; rbd, openvswitch, geneve, kvm are not confirmed"
  fi
}

vws_check_release() {
  local json status release=vws
  json=$(vws_helm list -a -o json 2>/dev/null) || vws_die_env "helm list failed"
  status=$(jq -r --arg r "$release" '[.[] | select(.name == $r) | .status] | first // ""' <<<"$json")
  case $status in
    "") vws_emit OK release "release ${release} is not installed" ;;
    deployed) vws_emit OK release "release ${release} deployed" ;;
    pending-* | failed) vws_emit FAIL release "release ${release} is ${status}; resolve it (helm history ${release}) before installing" ;;
    *) vws_emit WARN release "release ${release} is ${status}" ;;
  esac
}

# Source integrity, only in a checkout: reuse the guard checks. VWS_SOURCE_CHECKS=0 skips (the harness).
vws_check_source() {
  if [[ ${VWS_SOURCE_CHECKS:-1} == 0 || ! -d ${VWS_ROOT}/.git ]]; then
    vws_emit OK source "not a checkout; skipped"
    return 0
  fi
  local name
  for name in bundle recovery config; do
    if (cd "$VWS_ROOT" && scripts/guard.sh "$name" >/dev/null 2>&1); then
      vws_emit OK "source-${name}" "scripts/guard.sh ${name}"
    else
      vws_emit FAIL "source-${name}" "scripts/guard.sh ${name} fails; run it for the reason"
    fi
  done
}
