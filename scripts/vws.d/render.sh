#!/usr/bin/env bash
# summary: turn the VWSCluster CR into a values file for charts/vws (no cluster change)
# Sourced by scripts/vws. Usage: vws render CR.yaml OUT.yaml (OUT "-" is stdout, then use -o json
# or redirect; text results go to stderr in that case). Exit 2 when the CR is invalid.
# The output is checked against charts/vws/values.schema.json with helm template.
# shellcheck disable=SC2034 # read by scripts/vws
VWS_NEED=(python3 helm)

vws_cmd_main() {
  local cr=${1:-} out=${2:-} tmp rc=0
  [[ -n $cr && -n $out && $# -eq 2 ]] || vws_usage "usage: vws render CR.yaml OUT.yaml"
  tmp=$(mktemp)
  python3 "${VWS_ROOT}/scripts/vws.d/lib/render.py" "$cr" "$tmp" || rc=$?
  if ((rc != 0)); then
    rm -f "$tmp"
    ((rc == 2)) && vws_usage "the VWSCluster in ${cr} is invalid (see above)"
    vws_die_env "render failed (exit ${rc})"
  fi
  if ! vws_helm template vws "${VWS_ROOT}/charts/vws" -f "$tmp" >/dev/null; then
    rm -f "$tmp"
    vws_usage "the rendered values fail charts/vws/values.schema.json"
  fi
  if [[ $out == - ]]; then
    cat "$tmp"
    rm -f "$tmp"
    return 0
  fi
  mv "$tmp" "$out"
  vws_emit OK render "wrote ${out} from ${cr}"
}
