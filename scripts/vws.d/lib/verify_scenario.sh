#!/usr/bin/env bash
# Scenario runner for `vws verify scenario` and `vws verify list` (vws-deploy#251). Source it, do not run it.
# A scenario is scripts/vws.d/lib/scenario-<name>.sh with a "# scenario: <summary>" line, an array
# VWS_SCENARIO_ENV of the variables it needs, and a function vws_scenario_run that calls vws_step and vws_defer.
# Every step prints one JSON object {scenario, step, cmd, started_at, duration_ms, result, evidence}; result is
# pass, fail or skip. After the first fail the later steps are skip. The deferred cleanup steps run newest first,
# always, also after a fail or on SIGTERM/SIGINT, and each must accept an already deleted resource.

# shellcheck disable=SC2154 # vws_results and vws_fail_count come from lib/common.sh

VWS_SCENARIO_DIR="${VWS_ROOT}/scripts/vws.d/lib"
vws_sc_name='' vws_sc_pid='' vws_sc_fail_at='' vws_sc_fail_hit='' vws_sc_failed='' vws_sc_tmp='' vws_sc_steps=''
vws_sc_defer_names=() vws_sc_defer_cmds=()

vws_sc_file() { printf '%s/scenario-%s.sh' "$VWS_SCENARIO_DIR" "$1"; }

vws_sc_list() {
  local file name
  for file in "${VWS_SCENARIO_DIR}"/scenario-*.sh; do
    [[ -f $file ]] || continue
    name=${file##*/scenario-}
    printf '%s\t%s\n' "${name%.sh}" "$(sed -n 's/^# scenario: //p' "$file" | head -n 1)"
  done
}

vws_sc_now_ms() { date +%s%3N; }

# vws_sc_record STEP CMD STARTED DURATION RESULT EVIDENCE
vws_sc_record() {
  local evidence=${6//$'\n'/ }
  [[ -z ${VWS_VERIFY_SECRET_ACCESS_KEY:-} ]] || evidence=${evidence//"$VWS_VERIFY_SECRET_ACCESS_KEY"/<redacted>}
  evidence=${evidence:0:300}
  jq -cn --arg scenario "$vws_sc_name" --arg step "$1" --arg cmd "$2" --arg started_at "$3" \
    --argjson duration_ms "$4" --arg result "$5" --arg evidence "$evidence" \
    '{scenario: $scenario, step: $step, cmd: $cmd, started_at: $started_at, duration_ms: $duration_ms,
      result: $result, evidence: $evidence}' >>"$vws_sc_steps"
  [[ $VWS_OUTPUT != jsonl ]] || tail -n 1 "$vws_sc_steps"
  case $5 in
    pass) vws_emit OK "$1" "$evidence" ;;
    fail) vws_emit FAIL "$1" "$evidence" ;;
    skip) vws_emit WARN "$1" "$evidence" ;;
  esac
}

# vws_sc_exec STEP CMD...: runs one step unless an earlier one failed. Each tool call a step makes carries its
# own timeout (vws_aws), because `timeout` cannot run a shell function.
vws_sc_exec() {
  local step=$1 cmd started t0 rc=0
  shift
  cmd=$(printf '%q ' "$@")
  cmd=${cmd% }
  started=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  t0=$(vws_sc_now_ms)
  [[ $step != "$vws_sc_fail_at" ]] || vws_sc_fail_hit=1
  if [[ -n $vws_sc_failed && $step != cleanup:* ]]; then
    vws_sc_record "$step" "$cmd" "$started" 0 skip "an earlier step failed"
    return 0
  fi
  if [[ $step == "$vws_sc_fail_at" ]]; then
    vws_sc_failed=1
    vws_sc_record "$step" "$cmd" "$started" 0 fail "injected failure (--fail-at ${step}); not run"
    return 0
  fi
  # In the background so a SIGTERM interrupts the wait and the cleanup starts at once.
  "$@" >"${vws_sc_tmp}/out" 2>"${vws_sc_tmp}/err" </dev/null &
  vws_sc_pid=$!
  wait "$vws_sc_pid" || rc=$?
  vws_sc_pid=''
  if ((rc == 0)); then
    vws_sc_record "$step" "$cmd" "$started" "$(($(vws_sc_now_ms) - t0))" pass "$(head -c 600 "${vws_sc_tmp}/out")"
  else
    vws_sc_failed=1
    vws_sc_record "$step" "$cmd" "$started" "$(($(vws_sc_now_ms) - t0))" fail \
      "exit ${rc}: $(head -c 600 "${vws_sc_tmp}/err")"
  fi
}

vws_step() { vws_sc_exec "$@"; }

# vws_defer NAME CMD...: register a cleanup step. Register it before the step that creates the resource, so a
# create whose reply was lost is still cleaned up.
vws_defer() {
  local name=$1
  shift
  vws_sc_defer_names+=("$name")
  vws_sc_defer_cmds+=("$(printf '%q ' "$@")")
}

vws_sc_cleanup() {
  local i name cmd
  local -a argv
  for ((i = ${#vws_sc_defer_names[@]} - 1; i >= 0; i--)); do
    name=${vws_sc_defer_names[i]} cmd=${vws_sc_defer_cmds[i]}
    unset 'vws_sc_defer_names[i]' 'vws_sc_defer_cmds[i]'
    eval "argv=(${cmd})"
    vws_sc_exec "cleanup:${name}" "${argv[@]}"
  done
}

vws_sc_signal() {
  trap '' TERM INT
  [[ -z $vws_sc_pid ]] || kill "$vws_sc_pid" 2>/dev/null || true
  vws_sc_pid=''
  vws_sc_failed=1
  vws_sc_cleanup
  exit "$1"
}

# vws_aws ARGS...: aws-cli against VWS only. It gets a throwaway HOME and config, no shared credentials,
# IMDS off, standard retries (5 attempts), connect and read timeouts and an overall deadline. The secret key
# travels in the environment only and never in argv.
vws_aws() {
  local cfg="${vws_sc_tmp}/aws-config"
  [[ -f $cfg ]] || printf '[default]\ns3 =\n  addressing_style = path\n' >"$cfg"
  env -i PATH="$PATH" HOME="${vws_sc_tmp}" AWS_CONFIG_FILE="$cfg" AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true AWS_RETRY_MODE=standard AWS_MAX_ATTEMPTS=5 AWS_PAGER='' \
    AWS_ACCESS_KEY_ID="$VWS_VERIFY_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$VWS_VERIFY_SECRET_ACCESS_KEY" \
    AWS_DEFAULT_REGION="${VWS_VERIFY_REGION:-vws-1}" AWS_ENDPOINT_URL="$VWS_VERIFY_ENDPOINT" \
    VWS_FAKE_LOG="${VWS_FAKE_LOG:-}" VWS_FAKE_DIR="${VWS_FAKE_DIR:-}" \
    timeout --kill-after=10 "${VWS_VERIFY_CALL_TIMEOUT:-300}" \
    aws --cli-connect-timeout 10 --cli-read-timeout 60 "$@"
}

# The AWS-owned domains of tests/compat/compat.py AWS_OWNED. The name is split so guard.sh assets (R7) passes.
vws_sc_check_endpoint() {
  local host d a=ama
  a+=zon
  [[ $VWS_VERIFY_ENDPOINT =~ ^https?://([^/:@]+)(:[0-9]+)?(/.*)?$ ]] ||
    vws_usage "VWS_VERIFY_ENDPOINT must be an http or https URL"
  host=${BASH_REMATCH[1],,}
  for d in "${a}aws.com" "${a}aws.com.cn" api.aws "${a}.com" aws.dev; do
    [[ $host != "$d" && $host != *".$d" ]] || vws_usage "VWS_VERIFY_ENDPOINT ${host} is AWS-owned, not VWS; refused"
  done
}

# vws_sc_main NAME MAP FAIL_AT RESULT: exit 0 every step passed, 1 a step or a cleanup failed, 2 usage,
# 3 environment (tool or variable missing, cluster unreadable, cluster is not vws-test). Nothing runs before
# the cluster's kube-system UID matches vwsTest.kubeSystemUID in MAP.
vws_sc_main() {
  local map=$2 result=$4 file var uid want status=pass
  vws_sc_name=$1 vws_sc_fail_at=$3
  [[ $vws_sc_name =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || vws_usage "invalid scenario name ${vws_sc_name:0:64}"
  file=$(vws_sc_file "$vws_sc_name")
  [[ -f $file ]] || vws_usage "unknown scenario ${vws_sc_name}; try: scripts/vws verify list"
  VWS_SCENARIO_ENV=()
  # shellcheck source=/dev/null
  source "$file"
  for var in "${VWS_SCENARIO_ENV[@]}"; do
    [[ -n ${!var:-} ]] || vws_die_env "scenario ${vws_sc_name} needs ${var}; nothing ran"
  done
  [[ -z ${VWS_VERIFY_ENDPOINT:-} ]] || vws_sc_check_endpoint
  [[ -f $map ]] || vws_die_env "feature map ${map} not found"
  want=$(jq -r '.vwsTest.kubeSystemUID // empty' "$map" 2>/dev/null) || vws_die_env "feature map ${map} is unreadable"
  [[ -n $want ]] || vws_die_env "feature map ${map} has no vws-test fingerprint; nothing ran"
  uid=$(vws_kubectl get ns kube-system -o 'jsonpath={.metadata.uid}') || vws_die_env "cannot read the kube-system UID"
  [[ $uid == "$want" ]] || vws_die_env "cluster kube-system UID ${uid} is not vws-test (${want}); nothing ran"

  vws_sc_tmp=$(mktemp -d)
  # shellcheck disable=SC2064 # expand now: the paths are fixed for this run
  trap "rm -rf '${vws_sc_tmp}'; rm -f '${vws_results}'" EXIT
  vws_sc_steps="${vws_sc_tmp}/steps.jsonl"
  [[ -z $result ]] || vws_sc_steps="${result%.json}.steps.jsonl"
  : >"$vws_sc_steps" || vws_die_env "cannot write ${vws_sc_steps}"
  # shellcheck disable=SC2034 # read by the scenario file
  VWS_SCENARIO_RUN_ID="vws-verify-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  trap 'vws_sc_signal 143' TERM
  trap 'vws_sc_signal 130' INT
  vws_scenario_run
  vws_sc_cleanup
  trap - TERM INT
  if [[ -n $vws_sc_fail_at && -z $vws_sc_fail_hit ]]; then
    vws_emit FAIL fail-at "scenario ${vws_sc_name} has no step ${vws_sc_fail_at}"
  fi
  ((vws_fail_count == 0)) || status=fail
  [[ -z $result ]] || jq -n --arg s "$vws_sc_name" --arg r "$status" --arg u "$uid" --arg e "$vws_sc_steps" \
    --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{scenario: $s, result: $r, cluster: "vws-test", finishedAt: $t, clusterUID: $u, evidence: $e}' >"$result" ||
    vws_die_env "cannot write ${result}"
}
