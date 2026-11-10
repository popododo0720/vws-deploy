#!/usr/bin/env bash
# vws backup --etcd and vws restore --etcd: snapshots of the vws-etcd cluster, which holds IAM and the
# admin registry. VM disks and Proxmox-side data are not in them. Sourced by backup.sh and restore.sh.
# etcdctl reads ETCDCTL_ENDPOINTS, ETCDCTL_CACERT, ETCDCTL_CERT and ETCDCTL_KEY itself; the members
# accept only TLS with a client certificate (packaging/vws-etcd/vws-etcd-tls). ETCDCTL_COMMAND_TIMEOUT
# bounds the save (default 60s).

# vws_etcd_backup [--out FILE]: one snapshot of the member in ETCDCTL_ENDPOINTS, 0600, never overwritten.
vws_etcd_backup() {
  local out='' status err
  while (($#)); do
    case $1 in
      --out) (($# >= 2)) || vws_usage "--out needs a file"; out=$2; shift 2 ;;
      *) vws_usage "usage: vws backup --etcd [--out FILE]" ;;
    esac
  done
  vws_need etcdctl etcdutl jq
  [[ ${ETCDCTL_ENDPOINTS:-} =~ ^https://[^,]+$ ]] || vws_usage "ETCDCTL_ENDPOINTS must be one https URL"
  [[ -f ${ETCDCTL_CACERT:-} && -r $ETCDCTL_CACERT && -f ${ETCDCTL_CERT:-} && -r $ETCDCTL_CERT && -f ${ETCDCTL_KEY:-} && -r $ETCDCTL_KEY ]] ||
    vws_usage "ETCDCTL_CACERT, ETCDCTL_CERT and ETCDCTL_KEY must name readable files of the vws-etcd client certificate"
  out=${out:-vws-etcd-$(date -u +%Y%m%dT%H%M%SZ).db}
  [[ ! -e $out ]] || vws_usage "${out} exists; backup never overwrites"
  umask 077
  vws_etcd_part=$(mktemp "${out}.XXXXXX.part") || vws_die_env "cannot write next to ${out}"
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -f "$vws_etcd_part" "$vws_results"' EXIT
  if ! err=$(ETCDCTL_COMMAND_TIMEOUT=${ETCDCTL_COMMAND_TIMEOUT:-60s} etcdctl snapshot save "$vws_etcd_part" 2>&1 >/dev/null); then
    vws_emit FAIL etcd-snapshot "etcdctl snapshot save failed: $(tail -n 1 <<<"$err"); nothing was written"
    return 0
  fi
  if ! status=$(etcdutl snapshot status "$vws_etcd_part" -w json 2>/dev/null); then
    vws_emit FAIL etcd-snapshot "the saved snapshot does not verify; nothing was written"
    return 0
  fi
  if ! ln "$vws_etcd_part" "$out" 2>/dev/null; then
    vws_emit FAIL etcd-snapshot "${out} appeared during the backup; nothing was overwritten"
    return 0
  fi
  vws_emit OK etcd-snapshot "wrote ${out} (0600, revision $(jq -r .revision <<<"$status"), $(jq -r .totalKey <<<"$status") keys)"
}

# vws_etcd_restore SNAPSHOT --data-dir DIR --name NAME --peer-url URL [--initial-cluster LIST]: the data
# directory of a fresh member. LIST defaults to NAME=URL (a one-member cluster); for three members run
# it on each member with the same snapshot and the full list.
vws_etcd_restore() {
  local usage="usage: vws restore --etcd SNAPSHOT --data-dir DIR --name NAME --peer-url URL [--initial-cluster LIST]"
  local snap='' dir='' name='' peer='' cluster='' status err
  while (($#)); do
    case $1 in
      --data-dir | --name | --peer-url | --initial-cluster)
        (($# >= 2)) || vws_usage "$1 needs a value"
        case $1 in
          --data-dir) dir=$2 ;;
          --name) name=$2 ;;
          --peer-url) peer=$2 ;;
          --initial-cluster) cluster=$2 ;;
        esac
        shift 2
        ;;
      -*) vws_usage "$usage" ;;
      *) [[ -z $snap ]] || vws_usage "$usage"; snap=$1; shift ;;
    esac
  done
  [[ -n $snap && -n $dir && -n $name && -n $peer ]] || vws_usage "$usage"
  [[ -f $snap ]] || vws_usage "${snap} is not a file"
  [[ $peer =~ ^https:// ]] || vws_usage "--peer-url must be https"
  [[ ! -e $dir ]] || vws_usage "${dir} exists; restore writes only a fresh member"
  vws_need etcdutl jq
  if ! status=$(etcdutl snapshot status "$snap" -w json 2>/dev/null); then
    vws_emit FAIL etcd-restore "${snap} is not a valid etcd snapshot; nothing was written"
    return 0
  fi
  umask 077
  # A private staging directory: a failed or interrupted restore removes only its own files.
  vws_etcd_staging=$(mktemp -d "${dir%/}.XXXXXX") || vws_die_env "cannot write next to ${dir}"
  # shellcheck disable=SC2154 # vws_results is set by lib/common.sh
  trap 'rm -rf "$vws_etcd_staging" "$vws_results"' EXIT
  if ! err=$(etcdutl snapshot restore "$snap" --data-dir "${vws_etcd_staging}/member" --name "$name" \
    --initial-advertise-peer-urls "$peer" --initial-cluster "${cluster:-${name}=${peer}}" \
    --initial-cluster-token vws-etcd 2>&1 >/dev/null); then
    vws_emit FAIL etcd-restore "etcdutl snapshot restore failed: $(tail -n 1 <<<"$err")"
    return 0
  fi
  if ! mv -T "${vws_etcd_staging}/member" "$dir" 2>/dev/null; then
    vws_emit FAIL etcd-restore "${dir} appeared during the restore; nothing was replaced"
    return 0
  fi
  vws_emit OK etcd-restore "restored revision $(jq -r .revision <<<"$status") into ${dir}; start member ${name} on it"
}
