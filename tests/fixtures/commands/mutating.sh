#!/usr/bin/env bash
# summary: fixture, applies a ConfigMap (a changing call)
# shellcheck disable=SC2034
VWS_NEED=(kubectl)
vws_cmd_main() {
  vws_kubectl apply configmap -f - </dev/null >/dev/null
  vws_emit OK apply "applied"
}
