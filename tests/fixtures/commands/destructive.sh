#!/usr/bin/env bash
# summary: fixture, deletes a ConfigMap and needs --yes
# shellcheck disable=SC2034
VWS_NEED=(kubectl)
vws_cmd_main() {
  vws_confirm "deleting the ConfigMap"
  vws_kubectl delete configmap x >/dev/null
  vws_emit OK delete "deleted"
}
