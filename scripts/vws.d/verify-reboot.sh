#!/usr/bin/env bash
# summary: record the state before a reboot and check recovery after it (read only, never reboots)
# Sourced by scripts/vws. Usage: vws verify-reboot --snapshot FILE | --drained NODE | --check FILE [--timeout 20m]
# #43 section 8. --snapshot writes counts and names only: services ready/desired, pod counts per
# namespace, stateful PVC names and phase, ConfigMap key counts, runStrategy Always VMs, node names
# and boot IDs. No pod UID, no data, no Secret.
# --check passes the gates in boot order, each with its time: api, nodes, cni, csi, bundle, services.
# A gate that cannot pass (a node gone, Ceph HEALTH_ERR, an operator Failed, a service without a
# workload) exits 1; a gate still waiting at --timeout exits 3. Either way the blocked gate is the
# first output line. Then the comparisons with the snapshot (FAIL: 1) and whether a reboot was
# observed (WARN when no node has a new boot ID).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl)

VWS_VERIFY_RESTARTS=${VWS_VERIFY_RESTARTS:-3}
VWS_VERIFY_INTERVAL=${VWS_VERIFY_INTERVAL:-10}
VWS_VERIFY_SELECTOR=app.kubernetes.io/part-of=vws

vws_verify_get() { # RESOURCE [ARGS...]: json, or {"items":[]} when it cannot be read
  vws_kubectl get "$@" -o json 2>/dev/null || echo '{"items":[]}'
}

# vws_verify_snapshot FILE RELEASE: every read must succeed, or exit 3 and leave no FILE, so no caller
# reboots without the krbd check. The lists go to jq as files: a real cluster's pods -A JSON is larger
# than the kernel's argument limit (vws-deploy#150).
vws_verify_snapshot() {
  local file=$1 release=$2 dir
  vws_kubectl version -o json >/dev/null 2>&1 || vws_die_env "the Kubernetes API does not answer"
  vws_verify_tmp=$(mktemp -d)
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "${vws_verify_tmp:-}"; rm -f "$vws_results"' EXIT
  dir=$vws_verify_tmp
  vws_verify_save "${dir}/nodes" nodes
  vws_verify_save "${dir}/pods" pods -A
  vws_verify_save "${dir}/w" deployments,statefulsets -n "$release" -l "$VWS_VERIFY_SELECTOR"
  vws_verify_save "${dir}/pvc" pvc -n "$release"
  vws_verify_save "${dir}/cm" configmaps -n "$release" -l "$VWS_VERIFY_SELECTOR"
  vws_verify_save "${dir}/vm" virtualmachines -A
  vws_verify_save "${dir}/va" volumeattachments
  umask 077
  if ! jq -n --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg ns "$release" \
    --slurpfile nodes "${dir}/nodes" --slurpfile pods "${dir}/pods" --slurpfile w "${dir}/w" \
    --slurpfile pvc "${dir}/pvc" --slurpfile cm "${dir}/cm" --slurpfile vm "${dir}/vm" '{
      takenAt: $at, namespace: $ns,
      nodes: [$nodes[0].items[] | {name: .metadata.name, bootID: (.status.nodeInfo.bootID // "")}],
      services: [$w[0].items[] | {name: .metadata.name, kind, desired: (.spec.replicas // 1), ready: (.status.readyReplicas // 0)}],
      podCounts: ([$pods[0].items[] | .metadata.namespace] | group_by(.) | map({key: .[0], value: length}) | from_entries),
      statefulPVCs: [$pvc[0].items[] | {name: .metadata.name, phase: .status.phase}],
      configMapKeys: [$cm[0].items[] | {name: .metadata.name, keys: ((.data // {}) | length)}],
      alwaysVMs: [$vm[0].items[] | select(.spec.runStrategy == "Always" or .spec.running == true) | {namespace: .metadata.namespace, name: .metadata.name, status: (.status.printableStatus // "unknown")}]
    }' >"${dir}/snap" || ! mv "${dir}/snap" "$file"; then
    vws_die_env "snapshot failed: cannot write ${file}; do not reboot"
  fi
  vws_emit OK snapshot "wrote ${file}: $(jq -r '"\(.nodes | length) nodes, \(.services | length) services, \(.statefulPVCs | length) PVCs, \(.alwaysVMs | length) VMs"' "$file")"
  vws_verify_krbd "${dir}/va"
}

# vws_verify_save FILE RESOURCE [ARGS...]: the list as JSON in FILE, or exit 3.
vws_verify_save() {
  local out=$1
  shift
  vws_kubectl get "$@" -o json >"$out" 2>/dev/null && jq -e '.items | type == "array"' "$out" >/dev/null 2>&1 ||
    vws_die_env "snapshot failed: cannot list ${*}; do not reboot (the krbd check did not run)"
}

# vws_verify_krbd: a node with RBD volumes mapped by ceph-csi (krbd) can hang in shutdown when the
# network goes down before the unmap (libceph error -101, vws-deploy#116). WARN per such node so it
# is drained first (docs/platform-bundle.md "노드 재부팅 절차"). Read only.
vws_verify_krbd() { # VOLUMEATTACHMENTS_FILE
  local attached node count
  attached=$(jq -c '[.items[] | select(.spec.attacher == "rbd.csi.ceph.com" and .status.attached == true) | .spec.nodeName]' "$1") ||
    vws_die_env "snapshot failed: cannot read volumeattachments; do not reboot"
  while IFS=$'\t' read -r node count; do
    [[ -n $node ]] && vws_emit WARN krbd "${node}: ${count} RBD volumes attached; drain it before rebooting with: kubectl drain ${node} --ignore-daemonsets --delete-emptydir-data, krbd can hang the shutdown"
  done < <(jq -r 'group_by(.) | .[] | [.[0], (length | tostring)] | @tsv' <<<"$attached")
  [[ $attached != "[]" ]] || vws_emit OK krbd "no RBD volume attached"
}

# vws_verify_drained NODE: the gate between drain and reboot. Exit 0 only when NODE is cordoned and no
# rbd.csi.ceph.com VolumeAttachment is attached to it; 1 when the drain is not done; 3 when it cannot
# be read. A reboot goes ahead only on 0 (vws-deploy#150). Read only.
vws_verify_drained() {
  local node=$1 json attached
  json=$(vws_kubectl get nodes "$node" -o json 2>/dev/null) || vws_die_env "cannot read node ${node}; do not reboot"
  attached=$(vws_kubectl get volumeattachments -o json 2>/dev/null |
    jq -er --arg n "$node" '[.items[] | select(.spec.nodeName == $n and .spec.attacher == "rbd.csi.ceph.com" and .status.attached == true)] | length') ||
    vws_die_env "cannot read volumeattachments; do not reboot ${node}"
  if [[ $(jq -r '.spec.unschedulable // false' <<<"$json") != true ]]; then
    vws_emit FAIL drained "${node} is not cordoned: the drain did not run or failed; do not reboot"
  elif ((attached > 0)); then
    vws_emit FAIL drained "${node} still has ${attached} RBD volumes attached: the drain did not finish; do not reboot"
  else
    vws_emit OK drained "${node} is cordoned and has no RBD volume attached; safe to reboot"
  fi
}

# Each gate prints "pass", "wait <reason>" or "fail <reason>".
vws_gate_api() { if vws_kubectl version -o json >/dev/null 2>&1; then echo pass; else echo "wait the API does not answer"; fi; }

vws_gate_nodes() {
  local json
  json=$(vws_kubectl get nodes -o json 2>/dev/null) || { echo "wait cannot list nodes"; return; }
  jq -r --slurpfile s "$VWS_VERIFY_FILE" '
    [$s[0].nodes[].name] as $want
    | (.items | map({key: .metadata.name, value: any(.status.conditions[]?; .type == "Ready" and .status == "True")}) | from_entries) as $have
    | ([$want[] | select($have[.] == null)]) as $gone
    | ([$want[] | select($have[.] == false)]) as $notready
    | if ($gone | length) > 0 then "fail node gone: \($gone | join(","))"
      elif ($notready | length) > 0 then "wait not Ready: \($notready | join(","))"
      else "pass" end' <<<"$json"
}

vws_gate_cni() {
  jq -r '[.items[] | select(.metadata.name | startswith("kube-ovn-"))] as $p
    | if ($p | length) == 0 then "wait no kube-ovn pods yet"
      elif all($p[]; .status.phase == "Running" and all(.status.containerStatuses[]?; .ready)) then "pass"
      else "wait kube-ovn pods not Ready: \([$p[] | select((.status.phase != "Running") or any(.status.containerStatuses[]?; .ready | not)) | .metadata.name] | join(","))" end' \
    <<<"$(vws_verify_get pods -A)"
}

vws_gate_csi() {
  local profile=$1
  jq -r --arg p "$profile" '[.items[] | .status.ceph.health // "unknown"] as $h
    | if ($h | length) == 0 then "wait no CephCluster yet"
      elif all($h[]; . == "HEALTH_OK") then "pass"
      elif all($h[]; . == "HEALTH_OK" or . == "HEALTH_WARN") and $p == "single-node" then "pass"
      elif any($h[]; . == "HEALTH_ERR") then "fail Ceph \($h | join(","))"
      else "wait Ceph \($h | join(","))" end' <<<"$(vws_verify_get cephcluster -A)"
}

vws_gate_bundle() {
  local r phase out=pass
  for r in kubevirt cdi; do
    phase=$(jq -r '[.items[] | .status.phase // "unknown"] | join(",")' <<<"$(vws_verify_get "$r" -A)")
    case $phase in
      Deployed) ;;
      *Failed* | *Error*) echo "fail ${r} phase ${phase}"; return ;;
      "") out="wait no ${r} CR yet" ;;
      *) out="wait ${r} phase ${phase}" ;;
    esac
  done
  echo "$out"
}

vws_gate_services() {
  local release=$1
  jq -r --slurpfile s "$VWS_VERIFY_FILE" '
    (.items | map({key: .metadata.name, value: .}) | from_entries) as $have
    | [$s[0].services[] | .name as $n | select($have[$n] == null) | $n] as $gone
    | [$s[0].services[] | .name as $n | select($have[$n] != null and (($have[$n].status.readyReplicas // 0) < ($have[$n].spec.replicas // 1))) | $n] as $short
    | if ($gone | length) > 0 then "fail no workload: \($gone | join(","))"
      elif ($short | length) > 0 then "wait not ready: \($short | join(","))"
      else "pass" end' <<<"$(vws_verify_get deployments,statefulsets -n "$release" -l "$VWS_VERIFY_SELECTOR")"
}

# vws_verify_exit CODE: print the results (json too) and exit with CODE.
vws_verify_exit() {
  if [[ $VWS_OUTPUT == json ]]; then
    # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
    jq -Rn --arg status "$([[ $1 == 3 ]] && echo timeout || echo fail)" '{command: "verify-reboot", status: $status,
      checks: [inputs | split("\t") | {level: .[0], name: .[1], message: .[2]}]}' <"$vws_results"
  fi
  exit "$1"
}

vws_verify_check() {
  local file=$1 release=$2 timeout=$3 deadline start gate result t0 profile passed=() nodes pods pvcs cms vms
  [[ -f $file ]] || vws_usage "${file} does not exist"
  jq -e '.nodes and .services' "$file" >/dev/null 2>&1 || vws_usage "${file} is not a vws verify-reboot snapshot"
  VWS_VERIFY_FILE=$file
  profile=$(vws_kubectl get vwsclusters vws -n "$release" -o json 2>/dev/null | jq -r '.spec.profile // "unknown"') || profile=unknown
  start=$(date +%s)
  deadline=$((start + timeout))
  for gate in api nodes cni csi bundle services; do
    t0=$(date +%s)
    while :; do
      case $gate in
        csi) result=$(vws_gate_csi "$profile") ;;
        services) result=$(vws_gate_services "$release") ;;
        *) result=$("vws_gate_${gate}") ;;
      esac
      [[ $result == pass || $result == fail* ]] && break
      if (($(date +%s) >= deadline)); then
        vws_emit FAIL "gate-${gate}" "blocked at gate ${gate} after ${timeout}s (timeout): ${result#wait }"
        for p in "${passed[@]}"; do vws_emit OK "${p%%=*}" "${p#*=}"; done
        vws_verify_exit 3
      fi
      sleep "$VWS_VERIFY_INTERVAL"
    done
    if [[ $result == fail* ]]; then
      vws_emit FAIL "gate-${gate}" "blocked at gate ${gate}: ${result#fail }"
      for p in "${passed[@]}"; do vws_emit OK "${p%%=*}" "${p#*=}"; done
      vws_verify_exit 1
    fi
    passed+=("gate-${gate}=passed in $(($(date +%s) - t0))s")
  done
  for p in "${passed[@]}"; do vws_emit OK "${p%%=*}" "${p#*=}"; done

  # Comparisons with the snapshot.
  pvcs=$(vws_verify_get pvc -n "$release")
  result=$(jq -r --slurpfile s "$file" '(.items | map({key: .metadata.name, value: .status.phase}) | from_entries) as $h
    | [$s[0].statefulPVCs[] | select(.phase == "Bound") | .name | select($h[.] != "Bound")] | join(",")' <<<"$pvcs")
  if [[ -z $result ]]; then vws_emit OK pvcs "every snapshot PVC is Bound under the same name"; else vws_emit FAIL pvcs "not Bound or gone: ${result}"; fi
  cms=$(vws_verify_get configmaps -n "$release" -l "$VWS_VERIFY_SELECTOR")
  result=$(jq -r --slurpfile s "$file" '(.items | map({key: .metadata.name, value: ((.data // {}) | length)}) | from_entries) as $h
    | [$s[0].configMapKeys[] | select(($h[.name] // -1) < .keys) | "\(.name) \($h[.name] // "gone")/\(.keys)"] | join(", ")' <<<"$cms")
  if [[ -z $result ]]; then vws_emit OK configmaps "every ConfigMap has as many entries as before or more"; else vws_emit FAIL configmaps "fewer entries: ${result}"; fi
  vms=$(vws_verify_get virtualmachines -A)
  result=$(jq -r --slurpfile s "$file" '(.items | map({key: "\(.metadata.namespace)/\(.metadata.name)", value: (.status.printableStatus // "unknown")}) | from_entries) as $h
    | [$s[0].alwaysVMs[] | "\(.namespace)/\(.name)" | select($h[.] != "Running") | "\(.) \($h[.] // "gone")"] | join(", ")' <<<"$vms")
  if [[ -z $result ]]; then vws_emit OK vms "every runStrategy Always VM is Running"; else vws_emit FAIL vms "not Running: ${result}"; fi
  pods=$(vws_verify_get pods -A)
  result=$(jq -r --arg ns "$release" --argjson max "$VWS_VERIFY_RESTARTS" '[.items[] | select(.metadata.namespace == $ns) | . as $p
    | .status.containerStatuses[]? | select((.restartCount // 0) > $max) | "\($p.metadata.name) \(.restartCount)"] | unique | join(", ")' <<<"$pods")
  if [[ -z $result ]]; then vws_emit OK restarts "no pod over ${VWS_VERIFY_RESTARTS} restarts"; else vws_emit FAIL restarts "over ${VWS_VERIFY_RESTARTS} restarts: ${result}"; fi
  nodes=$(vws_kubectl get nodes -o json 2>/dev/null || echo '{"items":[]}')
  result=$(jq -r --slurpfile s "$file" '(.items | map({key: .metadata.name, value: (.status.nodeInfo.bootID // "")}) | from_entries) as $h
    | [$s[0].nodes[] | select($h[.name] != null and $h[.name] != .bootID) | .name] | join(",")' <<<"$nodes")
  if [[ -n $result ]]; then vws_emit OK reboot "rebooted since the snapshot: ${result}"; else vws_emit WARN reboot "reboot not observed: no node has a new boot ID since the snapshot"; fi
}

vws_cmd_main() {
  local snapshot='' check='' drained='' timeout=20m seconds release usage="usage: vws verify-reboot --snapshot FILE | --drained NODE | --check FILE [--timeout 20m]"
  while (($#)); do
    case $1 in
      --snapshot) (($# >= 2)) || vws_usage "--snapshot needs a file"; snapshot=$2; shift 2 ;;
      --check) (($# >= 2)) || vws_usage "--check needs a file"; check=$2; shift 2 ;;
      --drained) (($# >= 2)) || vws_usage "--drained needs a node"; drained=$2; shift 2 ;;
      --timeout) (($# >= 2)) || vws_usage "--timeout needs a duration"; timeout=$2; shift 2 ;;
      *) vws_usage "$usage" ;;
    esac
  done
  [[ $(printf '%s' "${snapshot:+x}${check:+x}${drained:+x}") == x ]] || vws_usage "$usage"
  [[ $timeout =~ ^([0-9]+)([smh])$ ]] || vws_usage "--timeout must look like 90s, 20m or 1h"
  case ${BASH_REMATCH[2]} in s) seconds=${BASH_REMATCH[1]} ;; m) seconds=$((BASH_REMATCH[1] * 60)) ;; h) seconds=$((BASH_REMATCH[1] * 3600)) ;; esac
  release=${VWS_NAMESPACE:-vws}
  VWS_NAMESPACE=''
  if [[ -n $drained ]]; then
    vws_verify_drained "$drained"
  elif [[ -n $snapshot ]]; then
    [[ ! -e $snapshot ]] || vws_usage "${snapshot} exists; verify-reboot never overwrites"
    vws_verify_snapshot "$snapshot" "$release"
  else
    vws_verify_check "$check" "$release" "$seconds"
  fi
}
