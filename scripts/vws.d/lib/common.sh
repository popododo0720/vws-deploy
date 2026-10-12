#!/usr/bin/env bash
# Shared by scripts/vws and every scripts/vws.d/<command>.sh. Source it, do not run it.
#
# Exit codes: 0 ok, 1 a check failed (or warned with --strict), 2 usage error, 3 environment
# (a required tool is missing or the cluster cannot be reached).
# Output: each result is OK, WARN or FAIL with a name and a message. -o json prints the same
# results once at the end as {"command", "status", "checks": [...]}.
# Secrets never go through these functions: callers pass names and counts, not values.

VWS_CONTEXT=${VWS_CONTEXT:-}
VWS_NAMESPACE=${VWS_NAMESPACE:-}
VWS_OUTPUT=${VWS_OUTPUT:-text}
VWS_DRY_RUN=${VWS_DRY_RUN:-}
VWS_YES=${VWS_YES:-}
VWS_STRICT=${VWS_STRICT:-}

vws_results=$(mktemp)
trap 'rm -f "$vws_results"' EXIT
vws_fail_count=0
vws_warn_count=0

vws_usage() {
  echo "vws: $*" >&2
  exit 2
}

vws_die_env() {
  echo "vws: $*" >&2
  exit 3
}

# vws_need TOOL...: exit 3 when a tool is not on PATH.
vws_need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || vws_die_env "required tool ${tool} is not on PATH"
  done
}

# vws_emit OK|WARN|FAIL NAME MESSAGE
vws_emit() {
  local level=$1 name=$2 message=${3:-}
  case $level in
    OK) ;;
    WARN) vws_warn_count=$((vws_warn_count + 1)) ;;
    FAIL) vws_fail_count=$((vws_fail_count + 1)) ;;
    *) vws_usage "internal: bad level ${level}" ;;
  esac
  message=${message//$'\t'/ }
  message=${message//$'\n'/ }
  printf '%s\t%s\t%s\n' "$level" "$name" "$message" >>"$vws_results"
  if [[ $VWS_OUTPUT == text ]]; then
    printf '%-4s  %s  %s\n' "$level" "$name" "$message"
  fi
}

# vws_finish: print the json summary and exit with the code the results call for.
vws_finish() {
  local status=ok code=0
  if ((vws_fail_count > 0)); then
    status=fail code=1
  elif ((vws_warn_count > 0)); then
    status=warn
    [[ -z $VWS_STRICT ]] || code=1
  fi
  if [[ $VWS_OUTPUT == json ]]; then
    vws_need jq
    jq -Rn --arg command "${command_name:-}" --arg status "$status" \
      '{command: $command, status: $status,
        checks: [inputs | split("\t") | {level: .[0], name: .[1], message: .[2]}]}' <"$vws_results"
  fi
  exit "$code"
}

# vws_confirm WHAT: a destructive command asks for --yes. Without it the command stops with 2.
vws_confirm() {
  [[ -n $VWS_YES ]] || vws_usage "$1 needs --yes"
}

vws_kube_flags() {
  [[ -z $VWS_CONTEXT ]] || printf '%s\n' "--context=${VWS_CONTEXT}"
  [[ -z $VWS_NAMESPACE ]] || printf '%s\n' "--namespace=${VWS_NAMESPACE}"
}

# A verb that can change the cluster. --dry-run skips these. Unknown verbs count as changing.
vws_kubectl_reads() {
  case $1 in
    get | describe | logs | top | version | api-resources | api-versions | explain | cluster-info | events | wait | diff) return 0 ;;
    auth) [[ ${2:-} == can-i || ${2:-} == whoami ]] ;;
    config) [[ ${2:-} == current-context || ${2:-} == get-contexts || ${2:-} == view ]] ;;
    *) return 1 ;;
  esac
}

vws_helm_reads() {
  case $1 in
    list | status | get | history | template | lint | show | version | search | repo) return 0 ;;
    *) return 1 ;;
  esac
}

# vws_kubectl VERB ARGS...: runs kubectl with --context and --namespace. With --dry-run a
# changing verb is printed and not run.
vws_kubectl() {
  local flags=()
  mapfile -t flags < <(vws_kube_flags)
  if [[ -n $VWS_DRY_RUN ]] && ! vws_kubectl_reads "$@"; then
    echo "DRYRUN kubectl $* ${flags[*]}" >&2
    return 0
  fi
  kubectl "${flags[@]}" "$@"
}

vws_helm() {
  local flags=()
  [[ -z $VWS_CONTEXT ]] || flags+=("--kube-context=${VWS_CONTEXT}")
  [[ -z $VWS_NAMESPACE ]] || flags+=("--namespace=${VWS_NAMESPACE}")
  if [[ -n $VWS_DRY_RUN ]] && ! vws_helm_reads "$@"; then
    echo "DRYRUN helm $* ${flags[*]}" >&2
    return 0
  fi
  helm "${flags[@]}" "$@"
}
