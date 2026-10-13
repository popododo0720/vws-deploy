#!/usr/bin/env bash
# summary: save the VWSCluster CR from questions or --from FILE (no workload change)
# Sourced by scripts/vws. Usage: vws init [--from FILE]
# Without --from it asks on stdin: profile, storage driver, StorageClass (existing only), image tag,
# elastic IP pool CIDR (required, no default; vws-deploy#148).
# The CR is validated with the same code as vws render. Same CR as in the cluster: unchanged.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq python3 kubectl)
# shellcheck source=scripts/vws.d/lib/installer.sh
source "${VWS_ROOT}/scripts/vws.d/lib/installer.sh"
# shellcheck source=scripts/vws.d/lib/applyrun.sh
source "${VWS_ROOT}/scripts/vws.d/lib/applyrun.sh"

vws_init_ask() { # PROMPT DEFAULT -> answer
  local answer
  printf '%s [%s]: ' "$1" "$2" >&2
  IFS= read -r answer || true
  echo "${answer:-$2}"
}

vws_cmd_main() {
  local from='' file profile driver class version current
  while (($#)); do
    case $1 in
      --from) (($# >= 2)) || vws_usage "--from needs a file"; from=$2; shift 2 ;;
      --from=*) from=${1#*=}; shift ;;
      *) vws_usage "usage: vws init [--from FILE]" ;;
    esac
  done
  vws_installer_namespace
  vws_installer_guard_ttl
  file=$(mktemp)
  if [[ -n $from ]]; then
    [[ -f $from ]] || vws_usage "${from} does not exist"
    cp "$from" "$file"
  else
    profile=$(vws_init_ask "profile (single-node, small, ha)" single-node)
    driver=$(vws_init_ask "storage driver (rook-ceph, existing)" rook-ceph)
    class=''
    [[ $driver != existing ]] || class=$(vws_init_ask "StorageClass to use" "")
    version=$(vws_init_ask "image tag (empty: chart appVersion)" "")
    pool=$(vws_init_ask "elastic IP pool CIDR (required)" "")
    {
      printf 'apiVersion: %s/v1alpha1\nkind: VWSCluster\nmetadata:\n  name: vws\n  namespace: %s\nspec:\n' "$(vws_api_group)" "$VWS_NAMESPACE"
      printf '  profile: %s\n' "$profile"
      [[ -z $version ]] || printf '  version: "%s"\n' "$version"
      printf '  storage:\n    driver: %s\n' "$driver"
      [[ -z $class ]] || printf '    storageClass: %s\n' "$class"
      printf '  network:\n    driver: kube-ovn\n'
      [[ -z $pool ]] || printf '  services:\n    network:\n      flags:\n        pool: %s\n' "$pool"
    } >"$file"
  fi
  if ! python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "$file" /dev/null; then
    rm -f "$file"
    vws_usage "the VWSCluster is invalid (see above)"
  fi
  if current=$(vws_kubectl get vwsclusters vws -o json 2>/dev/null) &&
    python3 - "$current" "$file" <<'PY'
import json, sys, yaml
have = json.loads(sys.argv[1]).get("spec")
want = yaml.safe_load(open(sys.argv[2])).get("spec")
sys.exit(0 if have == want else 1)
PY
  then
    rm -f "$file"
    vws_emit OK init "unchanged: VWSCluster vws already has this spec"
    return 0
  fi
  vws_apply_crd
  vws_kubectl apply --server-side --field-manager=vws -f - <"$file" >/dev/null || { rm -f "$file"; vws_die_env "cannot save VWSCluster vws"; }
  rm -f "$file"
  vws_emit OK init "saved VWSCluster vws in ${VWS_NAMESPACE}"
}
