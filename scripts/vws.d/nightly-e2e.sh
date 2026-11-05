#!/usr/bin/env bash
# summary: nightly full-stack e2e on vws-test: scope check, apply the CR, every scenario, the feature-map PR, an incident on failure
# Sourced by scripts/vws. Usage: vws [--context CTX] nightly-e2e --cr FILE --log-url URL [--results DIR]
#   [--timeout SECONDS] [--repo OWNER/NAME]
# vws-deploy#253, design docs/design/nightly-e2e.md. Removes every OS_* variable, then runs in order:
#   scope         the cluster's kube-system UID is vwsTest.kubeSystemUID in docs/feature-map.json
#   apply         vws apply --unattended -f CR (install or upgrade to what the CR pins)
#   scenarios     vws nightly --results DIR --timeout SECONDS
#   feature-map-pr vws nightly-pr --results DIR --log-url URL --repo REPO
# A failed step skips the steps after it, except that feature-map-pr still runs after failed scenarios
# so the passes are recorded. Each step has a hard limit (VWS_E2E_LIMITS). DIR (default
# $XDG_STATE_HOME/vws-nightly/<UTC time>) gets the scenario files of vws nightly and e2e/: one log per
# step, steps.jsonl and summary.json. On a failure it opens one incident issue in REPO with the failing
# step and the log tails (secret-looking env values redacted); a rerun of the same run reuses it.
# Exit: 0 every step passed, 1 a step failed or the incident could not be opened, 2 usage,
# 3 environment (tool missing, lock held).
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(jq timeout flock gh kubectl)

# The vws used for the steps. Tests point it at a fake; nothing else sets it.
VWS_NIGHTLY_E2E_VWS=${VWS_NIGHTLY_E2E_VWS:-}
# Seconds before the second incident attempt (doubled before the third). Tests set 0.
VWS_NIGHTLY_E2E_BACKOFF=${VWS_NIGHTLY_E2E_BACKOFF:-5}
declare -A VWS_E2E_LIMITS=([apply]=3600 [scenarios]=21600 [feature-map-pr]=600)

vws_e2e_dir='' vws_e2e_failed=''

vws_e2e_record() { # NAME RESULT EXIT STARTED MESSAGE
  jq -cn --arg n "$1" --arg r "$2" --argjson x "$3" --arg s "$4" --arg t "$(date -u +%FT%TZ)" \
    --arg l "${vws_e2e_dir}/$1.log" --arg m "$5" \
    '{name: $n, result: $r, exit: $x, startedAt: $s, finishedAt: $t, log: $l, message: $m}' \
    >>"${vws_e2e_dir}/steps.jsonl"
  case $2 in
    pass) vws_emit OK "e2e/$1" "$5" ;;
    skipped) vws_emit WARN "e2e/$1" "$5" ;;
    *) vws_emit FAIL "e2e/$1" "$5"; vws_e2e_failed=${vws_e2e_failed:-$1} ;;
  esac
}

vws_e2e_step() { # NAME ARGS...: one vws command under its limit, output to NAME.log
  local name=$1 limit=${VWS_E2E_LIMITS[$1]} rc=0 started
  shift
  started=$(date -u +%FT%TZ)
  timeout --kill-after=30 "$limit" "${vws_e2e_cmd[@]}" "$@" </dev/null >"${vws_e2e_dir}/${name}.log" 2>&1 || rc=$?
  if ((rc == 0)); then
    vws_e2e_record "$name" pass 0 "$started" "vws $1 passed"
  elif ((rc == 124 || rc == 137)); then
    vws_e2e_record "$name" fail "$rc" "$started" "vws $1 timed out after ${limit}s"
  else
    vws_e2e_record "$name" fail "$rc" "$started" "vws $1 exited ${rc}; $(grep -m1 '^FAIL' "${vws_e2e_dir}/${name}.log" | vws_e2e_redact | cut -c1-200)"
  fi
}

vws_e2e_redact() { # stdin to stdout, the values of secret-looking exported variables replaced
  local name val line
  local -a vals=()
  while IFS= read -r name; do
    val=${!name-}
    ((${#val} < 4)) || vals+=("$val")
  done < <(compgen -e | grep -E 'SECRET|TOKEN|PASSWORD|PASSWD|ACCESS_KEY|CREDENTIAL|PRIVATE' || true)
  while IFS= read -r line || [[ -n $line ]]; do
    for val in "${vals[@]}"; do line=${line//"$val"/[redacted]}; done
    printf '%s\n' "${line:0:300}"
  done
}

vws_e2e_tail() { # TITLE FILE LINES: a log section of the incident body
  printf '\n### %s (last %s lines)\n\n~~~~text\n' "$1" "$3"
  if [[ -f $2 ]]; then tail -n "$3" "$2" | vws_e2e_redact; else echo "(no log at $2)"; fi
  printf '~~~~\n'
}

vws_e2e_incident_body() { # RUN LOG_URL
  local f name
  jq -r --arg run "$1" --arg url "$2" --arg dir "$vws_e2e_dir" --arg step "$vws_e2e_failed" '
    (map(select(.name == $step)) | first) as $f |
    "## What happened\n\nNightly full-stack e2e run \($run) on vws-test failed at step `\($step)` (exit \($f.exit)): \($f.message | gsub("[\r\n`]"; " "))\n\nRun log: \($url)\nSummary on the box: \($dir)/summary.json\n\n| Step | Result | Exit | Started (UTC) | Log |\n|---|---|---|---|---|",
    (.[] | "| \(.name) | \(.result) | \(.exit) | \(.startedAt) | \(.log) |"),
    "\n## Impact\n\nvws-test only. The table shows which steps ran, failed or were skipped.\n\n## How it was detected\n\n`vws nightly-e2e` (vws-deploy#253) opened this issue when the run failed.\n\n## Root cause\n\nNot known yet. Triage from the logs below.\n\n## The check that would have caught this\n\nTo fill in during triage. The step that caught it is `\($step)` of `vws nightly-e2e`.\n\n## Follow-up issue and PR links\n\nNone yet.\n\n## Logs"
  ' -s "${vws_e2e_dir}/steps.jsonl"
  vws_e2e_tail "step ${vws_e2e_failed}" "${vws_e2e_dir}/${vws_e2e_failed}.log" 60
  [[ $vws_e2e_failed == scenarios ]] || return 0
  for f in "${vws_e2e_dir%/e2e}"/*.log; do
    [[ -f $f ]] || continue
    name=$(basename "$f" .log)
    [[ $(jq -r '.result // ""' "${f%.log}.json" 2>/dev/null) == pass ]] || vws_e2e_tail "scenario ${name}" "$f" 30
  done
}

vws_e2e_incident() { # RUN LOG_URL REPO: prints the URL of the open incident for this run
  local title="incident: nightly e2e $1 failed at ${vws_e2e_failed}" body list url i
  body="${vws_e2e_dir}/incident.md"
  vws_e2e_incident_body "$1" "$2" >"$body"
  for i in 1 2 3; do
    # Look before every create, so a retry after a create that timed out finds the issue it made.
    if list=$(timeout 60 gh issue list --repo "$3" --label incident --state open --limit 200 \
      --json title,url) && url=$(jq -r --arg t "$title" 'map(select(.title == $t)) | .[0].url // ""' <<<"$list"); then
      if [[ -n $url ]]; then printf '%s\n' "$url"; return 0; fi
      if url=$(timeout 60 gh issue create --repo "$3" --label incident --title "$title" --body-file "$body"); then
        printf '%s\n' "${url##*$'\n'}"
        return 0
      fi
    fi
    ((i == 3)) || sleep $((VWS_NIGHTLY_E2E_BACKOFF * i))
  done
  return 1
}

vws_cmd_main() {
  local cr='' log_url='' results='' timeout_s=1800 repo=popododo0720/vws-deploy run v want uid started incident
  local map="${VWS_ROOT}/docs/feature-map.json"
  while (($#)); do
    case $1 in
      --cr | --log-url | --results | --timeout | --repo)
        (($# >= 2)) || vws_usage "$1 needs a value"
        case $1 in
          --cr) cr=$2 ;;
          --log-url) log_url=$2 ;;
          --results) results=$2 ;;
          --timeout) timeout_s=$2 ;;
          --repo) repo=$2 ;;
        esac
        shift 2 ;;
      *) vws_usage "nightly-e2e: unknown argument $1" ;;
    esac
  done
  [[ -z $VWS_DRY_RUN ]] || vws_usage "nightly-e2e has no --dry-run: its steps push a branch and open issues"
  [[ -n $cr && -f $cr ]] || vws_usage "nightly-e2e needs --cr FILE (the VWSCluster CR to apply), not '${cr}'"
  [[ $log_url =~ ^https://[^[:space:]\`\<\>]{1,2000}$ ]] || vws_usage "--log-url must be an https URL without spaces"
  [[ $timeout_s =~ ^[1-9][0-9]{0,5}$ ]] || vws_usage "--timeout must be whole seconds from 1 to 999999, not ${timeout_s}"
  [[ $repo =~ ^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$ ]] || vws_usage "--repo must be OWNER/NAME, not ${repo}"
  [[ -n $results ]] || results="${XDG_STATE_HOME:-${HOME}/.local/state}/vws-nightly/$(date -u +%Y%m%dT%H%M%SZ)"
  run=$(basename "$results")
  [[ $run =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || vws_usage "--results must be a run directory named like 20261031T130000Z, not ${run}"

  vws_e2e_dir="${results}/e2e"
  mkdir -p "$vws_e2e_dir" || vws_die_env "cannot create ${vws_e2e_dir}"
  exec {lock_fd}>"$(dirname "$results")/nightly-e2e.lock" || vws_die_env "cannot open the nightly-e2e lock"
  flock -n "$lock_fd" || vws_die_env "another nightly-e2e run holds $(dirname "$results")/nightly-e2e.lock"
  : >"${vws_e2e_dir}/steps.jsonl"

  # No step may reach OpenStack: vws-test is reached only through its cluster API and VWS endpoints.
  for v in "${!OS_@}"; do unset "$v"; done
  local -a vws_e2e_cmd=(bash "${VWS_ROOT}/scripts/vws")
  [[ -z $VWS_NIGHTLY_E2E_VWS ]] || vws_e2e_cmd=("$VWS_NIGHTLY_E2E_VWS")

  started=$(date -u +%FT%TZ)
  want=$(jq -r '.vwsTest.kubeSystemUID // empty' "$map" 2>/dev/null) || want=''
  uid=$(vws_kubectl get ns kube-system -o 'jsonpath={.metadata.uid}' 2>"${vws_e2e_dir}/scope.log") || uid=''
  if [[ -z $want ]]; then
    vws_e2e_record scope fail 3 "$started" "${map} has no vwsTest.kubeSystemUID; nothing ran"
  elif [[ $uid != "$want" ]]; then
    vws_e2e_record scope fail 3 "$started" "cluster kube-system UID '${uid:0:64}' is not vws-test (${want}); nothing ran"
  else
    vws_e2e_record scope pass 0 "$started" "cluster is vws-test (kube-system UID ${uid})"
  fi

  if [[ -z $vws_e2e_failed ]]; then vws_e2e_step apply apply --unattended -f "$cr"; else
    vws_e2e_record apply skipped 0 "$(date -u +%FT%TZ)" "skipped after ${vws_e2e_failed} failed"; fi
  if [[ -z $vws_e2e_failed ]]; then
    vws_e2e_step scenarios nightly --results "$results" --timeout "$timeout_s"
    vws_e2e_step feature-map-pr nightly-pr --results "$results" --log-url "$log_url" --repo "$repo"
  else
    vws_e2e_record scenarios skipped 0 "$(date -u +%FT%TZ)" "skipped after ${vws_e2e_failed} failed"
    vws_e2e_record feature-map-pr skipped 0 "$(date -u +%FT%TZ)" "skipped after ${vws_e2e_failed} failed"
  fi

  incident=''
  if [[ -n $vws_e2e_failed ]]; then
    if incident=$(vws_e2e_incident "$run" "$log_url" "$repo"); then
      vws_emit OK e2e/incident "$incident"
    else
      incident=''
      vws_emit FAIL e2e/incident "could not open the incident issue in ${repo}; body at ${vws_e2e_dir}/incident.md"
    fi
  fi
  jq -s --arg run "$run" --arg s "$started" --arg t "$(date -u +%FT%TZ)" --arg url "$log_url" --arg i "$incident" \
    '{run: $run, cluster: "vws-test", startedAt: $s, finishedAt: $t, logUrl: $url,
      result: (if any(.[]; .result == "fail") then "fail" else "pass" end),
      incident: (if $i == "" then null else $i end), steps: .}' \
    "${vws_e2e_dir}/steps.jsonl" >"${vws_e2e_dir}/summary.json" || vws_die_env "cannot write ${vws_e2e_dir}/summary.json"
  vws_emit OK e2e/summary "${vws_e2e_dir}/summary.json"
}
