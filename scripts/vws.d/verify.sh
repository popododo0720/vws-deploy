#!/usr/bin/env bash
# summary: run vws-test scenarios with JSON step evidence and record their results in the feature map
# Sourced by scripts/vws. Usage: vws verify list
#        vws verify scenario NAME [--json] [--fail-at STEP] [--result FILE] [--map FILE]
#        vws verify --update-feature-map [--map FILE] RESULT.json...
#        vws verify --check-feature-map [--map FILE]
# scenario and list: vws-deploy#251, lib/verify_scenario.sh, docs/vws-cli.md. scenario runs only on the
# cluster whose kube-system UID is vwsTest.kubeSystemUID in the map.
# --update-feature-map, vws-deploy#252. RESULT.json is a scenario result:
#   {"scenario": "NAME", "result": "pass"|"fail", "cluster": "vws-test", "finishedAt": RFC3339, "evidence": "link",
#    "clusterUID": "kube-system namespace UID"}
# clusterUID must equal vwsTest.kubeSystemUID in the map, else the result is refused (FAIL).
# Only a pass on vws-test moves a feature's last pass forward. Features without a scenario are WARN unverified.
# Exit: 0 ok/warn, 1 a result refused (FAIL), 2 usage or invalid result/fingerprint ("FAIL: <reason>",
# map unchanged; includes finishedAt > 5 min in the future), 3 map unreadable.
# The feature-map modes change local files only and never touch the cluster.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3)

vws_verify_scenario() { # NAME ARGS...
  local name=${1:-} map="${VWS_ROOT}/docs/feature-map.json" fail_at='' result=''
  [[ -n $name && $name != -* ]] || vws_usage "verify scenario needs a NAME; try: scripts/vws verify list"
  shift
  while (($#)); do
    case $1 in
      --json) VWS_OUTPUT=json; shift ;;
      --map | --fail-at | --result)
        (($# >= 2)) || vws_usage "$1 needs a value"
        case $1 in
          --map) map=$2 ;;
          --fail-at) fail_at=$2 ;;
          --result) result=$2 ;;
        esac
        shift 2 ;;
      *) vws_usage "verify scenario: unknown argument $1" ;;
    esac
  done
  vws_need jq timeout aws
  # JSON lines, one per step: the -o json summary at exit would be a second shape in the same stream.
  [[ $VWS_OUTPUT != json ]] || VWS_OUTPUT=jsonl
  vws_sc_main "$name" "$map" "$fail_at" "$result"
}

vws_cmd_main() {
  local mode='' map="${VWS_ROOT}/docs/feature-map.json" results=() line level name msg rc
  # shellcheck source=scripts/vws.d/lib/verify_scenario.sh
  source "${VWS_ROOT}/scripts/vws.d/lib/verify_scenario.sh"
  case ${1:-} in
    scenario) shift; vws_verify_scenario "$@"; return 0 ;;
    list)
      (($# == 1)) || vws_usage "verify list takes no arguments"
      while IFS=$'\t' read -r name msg; do vws_emit OK "$name" "$msg"; done < <(vws_sc_list)
      return 0 ;;
  esac
  while (($#)); do
    case $1 in
      --update-feature-map) mode=update; shift ;;
      --check-feature-map) mode=check; shift ;;
      --map)
        (($# >= 2)) || vws_usage "--map needs a file"
        map=$2; shift 2 ;;
      -*) vws_usage "verify: unknown flag $1" ;;
      *) results+=("$1"); shift ;;
    esac
  done
  [[ -n $mode ]] || vws_usage "verify: give list, scenario NAME, --update-feature-map RESULT.json... or --check-feature-map"
  [[ -f $map ]] || vws_die_env "feature map ${map} not found"
  if [[ $mode == check ]]; then
    ((${#results[@]} == 0)) || vws_usage "verify --check-feature-map takes no result files"
    rc=0; python3 "${VWS_ROOT}/scripts/vws.d/lib/feature_map.py" check "$map" || rc=$?
    case $rc in
      0) vws_emit OK feature-map "${map%.json}.md matches ${map}" ;;
      1) vws_emit FAIL feature-map "${map%.json}.md is stale; run scripts/vws verify --update-feature-map" ;;
      *) vws_die_env "feature map ${map} is unreadable" ;;
    esac
    return 0
  fi
  ((${#results[@]})) || vws_usage "verify --update-feature-map needs at least one RESULT.json"
  local out
  rc=0; out=$(python3 "${VWS_ROOT}/scripts/vws.d/lib/feature_map.py" update "$map" "${results[@]}") || rc=$?
  if ((rc == 2)); then # invalid input: one "FAIL: <reason>" line, nothing written
    printf '%s\n' "$out"
    exit 2
  fi
  ((rc <= 1)) || vws_die_env "feature map ${map} is unreadable"
  while IFS=$'\t' read -r level name msg; do
    [[ -n $level ]] && vws_emit "$level" "$name" "$msg"
  done <<<"$out"
}
