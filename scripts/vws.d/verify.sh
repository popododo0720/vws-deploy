#!/usr/bin/env bash
# summary: record vws-test scenario results in the feature map (docs/feature-map.md)
# Sourced by scripts/vws. Usage: vws verify --update-feature-map [--map FILE] RESULT.json...
#        vws verify --check-feature-map [--map FILE]
# vws-deploy#252. RESULT.json is a scenario result:
#   {"scenario": "NAME", "result": "pass"|"fail", "cluster": "vws-test", "finishedAt": RFC3339, "evidence": "link",
#    "clusterUID": "kube-system namespace UID"}
# clusterUID must equal vwsTest.kubeSystemUID in the map, else the result is refused (FAIL).
# Only a pass on vws-test moves a feature's last pass forward. Features without a scenario are WARN unverified.
# Exit: 0 ok/warn, 1 a result refused (FAIL), 2 usage or invalid result/fingerprint ("FAIL: <reason>",
# map unchanged; includes finishedAt > 5 min in the future), 3 map unreadable.
# Changes local files only; never touches the cluster.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3)

vws_cmd_main() {
  local mode='' map="${VWS_ROOT}/docs/feature-map.json" results=() line level name msg rc
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
  [[ -n $mode ]] || vws_usage "verify: give --update-feature-map RESULT.json... or --check-feature-map (scenarios: see docs/feature-map.md)"
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
