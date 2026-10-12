#!/usr/bin/env bash
# summary: fixture, makes a kubectl call that has no fixture
# shellcheck disable=SC2034
VWS_NEED=(kubectl)
vws_cmd_main() {
  vws_kubectl get pods >/dev/null
  vws_emit OK pods "listed"
}
