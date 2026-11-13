#!/usr/bin/env bash
# summary: restore a vws-etcd member from a snapshot into a fresh data directory
# shellcheck source=scripts/vws.d/lib/etcd.sh
source "${VWS_ROOT}/scripts/vws.d/lib/etcd.sh"

vws_cmd_main() {
  [[ ${1:-} == --etcd ]] || vws_usage "usage: vws restore --etcd SNAPSHOT --data-dir DIR --name NAME --peer-url URL [--initial-cluster LIST]"
  shift
  vws_etcd_restore "$@"
}
