#!/usr/bin/env bash
# summary: write a verified vws-etcd snapshot; requires --etcd
# Sourced by scripts/vws. Usage: vws backup --etcd [--out FILE]
# shellcheck source=scripts/vws.d/lib/etcd.sh
source "${VWS_ROOT}/scripts/vws.d/lib/etcd.sh"

vws_cmd_main() {
  [[ ${1:-} == --etcd ]] || vws_usage "usage: vws backup --etcd [--out FILE]"
  shift
  vws_etcd_backup "$@"
}
