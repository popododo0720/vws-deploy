#!/usr/bin/env bash
# summary: run every feature-map scenario one at a time and record the results in the feature map
# Sourced by scripts/vws. Usage: vws [--context CTX] nightly [--map FILE] [--results DIR] [--timeout SECONDS]
# vws-deploy#290 (Part of #252). Reads the scenario names from the map (docs/feature-map.json by
# default), runs each one in name order with `scripts/vws verify scenario NAME --result FILE`
# (vws-deploy#287), one at a time and each under --timeout (default 1800 s, then SIGTERM, then
# SIGKILL 30 s later), and then records every result written with the same update that
# `vws verify --update-feature-map` runs. Only a vws-test pass moves a feature's last pass forward.
# Writes under DIR (default $XDG_STATE_HOME/vws-nightly/<UTC time>): NAME.json and NAME.log per
# scenario. A second run while one holds DIR/../nightly.lock stops with exit 3, so runs never overlap.
# Exit: 0 every scenario passed (or none exists: WARN), 1 a scenario failed, timed out or wrote no
# valid result, 2 usage, 3 environment (tool missing, lock held, map unreadable).
# Opening the PR, publishing results and alerting are separate steps (vws-deploy#290 ACs 2, 6, 7).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3 jq timeout flock)

# The scenario runner. Tests point it at a fake; nothing else sets it.
VWS_NIGHTLY_RUNNER=${VWS_NIGHTLY_RUNNER:-}

vws_nightly_scenarios() { # MAP: unique scenario names, sorted, one per line; exit 3 when unreadable
  python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    names = {f["scenario"] for f in data["features"] if f.get("scenario")}
except (OSError, ValueError, KeyError, TypeError) as e:
    print(f"unreadable feature map: {e}", file=sys.stderr)
    sys.exit(3)
for n in sorted(names):
    print(n)
PY
}

vws_cmd_main() {
  local map="${VWS_ROOT}/docs/feature-map.json" results='' timeout_s=1800 names name rc out level check msg
  local -a written=()
  while (($#)); do
    case $1 in
      --map | --results | --timeout)
        (($# >= 2)) || vws_usage "$1 needs a value"
        case $1 in
          --map) map=$2 ;;
          --results) results=$2 ;;
          --timeout) timeout_s=$2 ;;
        esac
        shift 2 ;;
      *) vws_usage "nightly: unknown argument $1" ;;
    esac
  done
  [[ $timeout_s =~ ^[1-9][0-9]{0,5}$ ]] || vws_usage "--timeout must be whole seconds from 1 to 999999, not ${timeout_s}"
  [[ -f $map ]] || vws_die_env "feature map ${map} not found"
  if [[ -z $results ]]; then
    results="${XDG_STATE_HOME:-${HOME}/.local/state}/vws-nightly/$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  mkdir -p "$results" || vws_die_env "cannot create results directory ${results}"
  exec {lock_fd}>"$(dirname "$results")/nightly.lock" || vws_die_env "cannot open the nightly lock"
  flock -n "$lock_fd" || vws_die_env "another nightly run holds $(dirname "$results")/nightly.lock"

  rc=0; names=$(vws_nightly_scenarios "$map") || rc=$?
  ((rc == 0)) || vws_die_env "feature map ${map} is unreadable"
  if [[ -z $names ]]; then
    vws_emit WARN nightly "no feature in ${map} has a scenario; nothing ran"
    return 0
  fi
  local -a runner
  if [[ -n $VWS_NIGHTLY_RUNNER ]]; then runner=("$VWS_NIGHTLY_RUNNER"); else
    runner=(bash "${VWS_ROOT}/scripts/vws" verify scenario) # inherits the exported VWS_CONTEXT
  fi
  while IFS= read -r name; do
    if [[ ! $name =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
      vws_emit FAIL "scenario/${name:0:64}" "invalid scenario name in ${map}; skipped"
      continue
    fi
    rm -f "${results}/${name}.json"
    rc=0
    timeout --kill-after=30 "$timeout_s" "${runner[@]}" "$name" --result "${results}/${name}.json" \
      </dev/null >"${results}/${name}.log" 2>&1 || rc=$?
    if ((rc == 124 || rc == 137)); then
      vws_emit FAIL "scenario/${name}" "timed out after ${timeout_s}s; log ${results}/${name}.log"
    elif [[ ! -s ${results}/${name}.json ]] || ! jq -e 'type == "object"' "${results}/${name}.json" >/dev/null 2>&1; then
      vws_emit FAIL "scenario/${name}" "no valid result written (exit ${rc}); log ${results}/${name}.log"
    else
      written+=("${results}/${name}.json")
      if [[ $(jq -r '.result' "${results}/${name}.json") == pass ]]; then
        vws_emit OK "scenario/${name}" "pass; log ${results}/${name}.log"
      else
        vws_emit FAIL "scenario/${name}" "result $(jq -r '.result | tostring' "${results}/${name}.json" | head -c 32) (exit ${rc}); log ${results}/${name}.log"
      fi
    fi
  done <<<"$names"

  ((${#written[@]})) || return 0
  rc=0; out=$(python3 "${VWS_ROOT}/scripts/vws.d/lib/feature_map.py" update "$map" "${written[@]}") || rc=$?
  if ((rc == 2)); then
    vws_emit FAIL feature-map "results not recorded: ${out#FAIL: }"
    return 0
  fi
  ((rc <= 1)) || vws_die_env "feature map ${map} is unreadable"
  while IFS=$'\t' read -r level check msg; do
    [[ -n $level ]] && vws_emit "$level" "$check" "$msg"
  done <<<"$out"
}
