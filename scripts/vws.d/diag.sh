#!/usr/bin/env bash
# summary: write a diagnostic bundle (tar.gz, 0600) with secret-shaped lines masked (read only)
# Sourced by scripts/vws. Usage: vws diag [--out FILE] [--since DURATION]
# Collects only vws-deploy#43 section 7: the VWSCluster (spec and status), helm status/history/values,
# nodes, pods, PVCs, PDBs, events, describe and recent logs of unhealthy pods, CRD names, the status
# of the KubeVirt, CDI and CephCluster CRs, ConfigMap names and sizes, versions.env, tool versions,
# the context name. Never: Secrets, ConfigMap data, environment variables, host files, VM content.
# Every file is scanned by lib/redact.py before it is packed; when the scanner cannot run, no bundle
# is written and the exit code is 3. Nothing is uploaded.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 tar gzip kubectl helm)

# Namespaces whose unhealthy pods get describe and logs: the release and the platform bundle.
VWS_DIAG_NAMESPACES=(kubevirt cdi kube-system rook-ceph)
VWS_DIAG_LOG_LINES=500

# vws_diag_try FILE CMD...: run a read command into FILE; a failure writes the reason instead.
vws_diag_try() {
  local file=$1 err
  shift
  err=$(mktemp)
  if ! "$@" >"$file" 2>"$err"; then
    printf 'unavailable: %s\n' "$(head -1 "$err")" >"$file"
  fi
  rm -f "$err"
}

vws_cmd_main() {
  local out='' since=1h dir release ns pod pods json
  while (($#)); do
    case $1 in
      --out) (($# >= 2)) || vws_usage "--out needs a file"; out=$2; shift 2 ;;
      --out=*) out=${1#*=}; shift ;;
      --since) (($# >= 2)) || vws_usage "--since needs a duration"; since=$2; shift 2 ;;
      --since=*) since=${1#*=}; shift ;;
      *) vws_usage "usage: vws diag [--out FILE] [--since DURATION]" ;;
    esac
  done
  [[ $since =~ ^[0-9]+[smh]$ ]] || vws_usage "--since must look like 30m or 1h"
  out=${out:-vws-diag-$(date -u +%Y%m%dT%H%M%SZ).tar.gz}
  [[ ! -e $out ]] || vws_usage "${out} exists; diag never overwrites"
  release=${VWS_NAMESPACE:-vws}
  VWS_NAMESPACE=''

  umask 077
  vws_diag_work=$(mktemp -d)
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "${vws_diag_work:-}"; rm -f "$vws_results"' EXIT
  dir="${vws_diag_work}/vws-diag"
  mkdir -p "$dir/pods"

  vws_kubectl version -o json >/dev/null 2>&1 || vws_die_env "the Kubernetes API does not answer"
  vws_diag_try "$dir/context.txt" vws_kubectl config current-context
  vws_diag_try "$dir/kubectl-version.txt" vws_kubectl version --client -o yaml
  vws_diag_try "$dir/helm-version.txt" vws_helm version --short
  cp "${VWS_ROOT}/scripts/versions.env" "$dir/versions.env"

  # The CR without managedFields. No Secret is read anywhere in this command.
  if json=$(vws_kubectl get vwsclusters vws -n "$release" -o json 2>/dev/null); then
    jq 'del(.metadata.managedFields)' <<<"$json" >"$dir/vwscluster.json"
  else
    echo "unavailable: no VWSCluster vws in ${release} (or no right to read it)" >"$dir/vwscluster.json"
  fi
  vws_diag_try "$dir/helm-status.json" vws_helm status vws -n "$release" -o json
  vws_diag_try "$dir/helm-history.json" vws_helm history vws -n "$release" -o json
  vws_diag_try "$dir/helm-values.json" vws_helm get values vws -n "$release" -a -o json
  vws_diag_try "$dir/cluster.txt" vws_kubectl get nodes,pods,pvc,pdb,events -A -o wide
  if json=$(vws_kubectl get crd -o json 2>/dev/null); then
    jq -r '.items[].metadata.name' <<<"$json" >"$dir/crds.txt"
  else
    echo "unavailable: cannot list CRDs" >"$dir/crds.txt"
  fi
  local resource
  for resource in kubevirt cdi cephcluster; do
    if json=$(vws_kubectl get "$resource" -A -o json 2>/dev/null); then
      jq '[.items[] | {namespace: .metadata.namespace, name: .metadata.name, status}]' <<<"$json" >"$dir/${resource}-status.json"
    else
      echo "unavailable: cannot read ${resource}" >"$dir/${resource}-status.json"
    fi
  done
  # ConfigMaps: names and the size of each key, never the data.
  if json=$(vws_kubectl get configmaps -n "$release" -o json 2>/dev/null); then
    jq '[.items[] | {name: .metadata.name, keys: ((.data // {}) | with_entries(.value |= length))}]' <<<"$json" >"$dir/configmaps.json"
  else
    echo "unavailable: cannot list ConfigMaps in ${release}" >"$dir/configmaps.json"
  fi

  # Unhealthy pods (not Running/Succeeded, or a container not ready) in the release and bundle namespaces.
  json=$(vws_kubectl get pods -A -o json 2>/dev/null || echo '{"items":[]}')
  pods=$(jq -r --args '[$ARGS.positional[]] as $ns | .items[]
    | select(.metadata.namespace as $n | $ns | index($n))
    | select((.status.phase != "Running" and .status.phase != "Succeeded") or any(.status.containerStatuses[]?; .ready == false))
    | "\(.metadata.namespace) \(.metadata.name)"' "$release" "${VWS_DIAG_NAMESPACES[@]}" <<<"$json")
  while read -r ns pod; do
    [[ -n $pod ]] || continue
    vws_diag_try "$dir/pods/${ns}_${pod}.describe.txt" vws_kubectl describe pod "$pod" -n "$ns"
    vws_diag_try "$dir/pods/${ns}_${pod}.log" vws_kubectl logs "$pod" -n "$ns" --all-containers --since="$since" --tail="$VWS_DIAG_LOG_LINES"
  done <<<"$pods"

  # status and doctor output, when this build has them (vws-deploy#56).
  local cmd
  for cmd in status doctor; do
    if [[ -f ${VWS_COMMAND_DIR:-${VWS_ROOT}/scripts/vws.d}/${cmd}.sh ]]; then
      VWS_NAMESPACE=$release "${VWS_ROOT}/scripts/vws" -o json "$cmd" >"$dir/${cmd}.json" 2>&1 || true
    else
      echo "unavailable: vws ${cmd} is not in this build" >"$dir/${cmd}.json"
    fi
  done

  cat >"$dir/README" <<'TXT'
vws diag bundle (vws-deploy#58). Secret resources, ConfigMap data and environment variables were not
collected. Every line that looked like a private key, bearer token, token value, base64 under data:,
a 12+ digit number or a URL with credentials was replaced with [REDACTED]; REDACTION_REPORT has the
counts only. Masking cannot catch everything a log may contain: read the bundle before you share it.
TXT

  # Fail closed: no scanner, no bundle.
  command -v python3 >/dev/null 2>&1 || vws_die_env "the redaction scanner (python3) is not on PATH; no bundle written"
  python3 "${VWS_ROOT}/scripts/vws.d/lib/redact.py" "$dir" "${vws_diag_work}/REDACTION_REPORT" ||
    vws_die_env "the redaction scanner failed; no bundle written"
  mv "${vws_diag_work}/REDACTION_REPORT" "$dir/REDACTION_REPORT"

  tar -C "$vws_diag_work" -czf "${vws_diag_work}/bundle.tar.gz" vws-diag || vws_die_env "tar failed"
  chmod 0600 "${vws_diag_work}/bundle.tar.gz"
  mv "${vws_diag_work}/bundle.tar.gz" "$out"
  vws_emit OK diag "wrote ${out} (0600), $(awk '$1 == "total" {print $2}' "$dir/REDACTION_REPORT") lines redacted; read it before sharing"
}
