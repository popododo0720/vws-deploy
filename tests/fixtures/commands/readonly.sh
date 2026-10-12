#!/usr/bin/env bash
# summary: fixture, reads the node list and changes nothing
# shellcheck disable=SC2034
VWS_NEED=(kubectl helm)
vws_cmd_main() {
  local count
  count=$(vws_kubectl get nodes -o json | jq '.items | length')
  vws_emit OK nodes "${count} nodes"
  vws_helm list -o json >/dev/null
  vws_emit OK release "listed"
}
