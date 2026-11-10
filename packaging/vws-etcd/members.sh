# shellcheck shell=bash
# Sourced by vws-etcd-tls and vws-etcd-config.
# vws_etcd_members LIST: parses NAME=IPV4[:PEERPORT][,...] into the arrays vws_etcd_names, vws_etcd_ips and
# vws_etcd_peer_ports (empty when the entry has no port). Exit 2 on a bad entry, the name client (the
# client certificate directory of vws-etcd-tls), a repeated name or peer address (with the default port
# VWS_ETCD_PEER_PORT or 2380 filled in), or an even member count (an even count adds a member without
# adding fault tolerance).
vws_etcd_members() {
  local entry octet addr addrs=() entries=()
  vws_etcd_names=() vws_etcd_ips=() vws_etcd_peer_ports=()
  IFS=, read -r -a entries <<<"$1"
  for entry in "${entries[@]}"; do
    [[ $entry =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)=(([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3}))(:([1-9][0-9]{0,4}))?$ ]] ||
      vws_etcd_die "bad member '${entry}'; want NAME=IPV4[:PEERPORT] with NAME in [a-z0-9-]"
    for octet in "${BASH_REMATCH[@]:4:4}"; do
      ((10#$octet <= 255)) || vws_etcd_die "bad IPv4 address in '${entry}'"
    done
    [[ -z ${BASH_REMATCH[9]} ]] || ((BASH_REMATCH[9] <= 65535)) || vws_etcd_die "bad port in '${entry}'"
    [[ ${BASH_REMATCH[1]} != client ]] || vws_etcd_die "member name client is reserved for the client certificate"
    addr="${BASH_REMATCH[3]}:${BASH_REMATCH[9]:-${VWS_ETCD_PEER_PORT:-2380}}"
    [[ " ${vws_etcd_names[*]} " != *" ${BASH_REMATCH[1]} "* ]] || vws_etcd_die "member name ${BASH_REMATCH[1]} appears twice"
    [[ " ${addrs[*]} " != *" ${addr} "* ]] || vws_etcd_die "member address ${addr} appears twice"
    addrs+=("$addr")
    vws_etcd_names+=("${BASH_REMATCH[1]}")
    vws_etcd_ips+=("${BASH_REMATCH[3]}")
    vws_etcd_peer_ports+=("${BASH_REMATCH[9]}")
  done
  ((${#vws_etcd_names[@]} % 2 == 1)) || vws_etcd_die "an etcd cluster needs an odd member count (1 or 3), got ${#vws_etcd_names[@]}"
}

vws_etcd_die() {
  echo "vws-etcd: $*" >&2
  exit 2
}
