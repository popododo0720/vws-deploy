#!/usr/bin/env bash
# vws-etcd on real etcd processes: member config and TLS (packaging/vws-etcd), a three-member cluster
# that speaks only TLS and refuses a client without a certificate, vws backup --etcd / vws restore --etcd
# into a fresh member, and the snapshot retention the backup timer runs. Members listen on 127.0.0.1
# with their own ports (on loopback every connection comes from 127.0.0.1, which the peer IP SAN check
# needs). etcd, etcdctl and etcdutl come from scripts/install-tools.sh.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

for tool in etcd etcdctl etcdutl openssl curl; do
  command -v "$tool" >/dev/null || { bad "tools" "${tool} is not on PATH; run scripts/install-tools.sh"; finish_tests; }
done
PKG="${VWS_TEST_ROOT}/packaging/vws-etcd"
TLS="${WORK}/tls"
pids=()
stop_members() {
  local pid
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  rm -rf "$WORK"
}
trap stop_members EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Nine distinct free ports (PX stays closed), held open together while they are picked.
read -r PA PB PC PR CA CB CC CR PX < <(python3 -c '
import socket
socks = [socket.socket() for _ in range(9)]
for s in socks: s.bind(("127.0.0.1", 0))
print(*[s.getsockname()[1] for s in socks])')
LIST="a=127.0.0.1:${PA},b=127.0.0.1:${PB},c=127.0.0.1:${PC}"

# run_cmd CMD ARGS...: sets OUT, ERR and RC like run_vws.
run_cmd() {
  set +e
  OUT=$("$@" 2>"${WORK}/err")
  RC=$?
  set -e
  ERR=$(cat "${WORK}/err")
}

# expect_refused NAME: the last run_cmd failed.
expect_refused() {
  if ((RC != 0)); then ok "$1"; else bad "$1" "exit 0, want a refusal (output: ${OUT})"; fi
}

# start_member NAME LIST CLIENTPORT: renders the member config with vws-etcd-config and starts etcd on it.
start_member() {
  VWS_ETCD_TLS_DIR="${TLS}/$1" VWS_ETCD_DATA_DIR="${WORK}/data-$1" VWS_ETCD_CLIENT_PORT=$3 \
    "${PKG}/vws-etcd-config" "$1" "$2" >"${WORK}/$1.yml"
  etcd --config-file "${WORK}/$1.yml" >"${WORK}/$1.log" 2>&1 &
  pids+=("$!")
}

# with_client PORT CMD ARGS...: CMD with the vws-etcd client certificate in the ETCDCTL_* variables.
with_client() {
  local port=$1
  shift
  ETCDCTL_ENDPOINTS="https://127.0.0.1:${port}" ETCDCTL_CACERT="${TLS}/client/ca.crt" \
    ETCDCTL_CERT="${TLS}/client/client.crt" ETCDCTL_KEY="${TLS}/client/client.key" "$@"
}

# wait_healthy PORT: polls etcdctl endpoint health for at most 30 seconds; stops when a member exited.
wait_healthy() {
  local pid
  for _ in $(seq 60); do
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null || return 1; done
    with_client "$1" etcdctl --dial-timeout=1s --command-timeout=2s endpoint health >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

# Config: bad member lists are refused before anything is written.
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1,b=10.0.0.2;                 expect_rc "an even member count exits 2" 2
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1,b=10.0.0.256,c=10.0.0.3;    expect_rc "a bad IPv4 address exits 2" 2
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1,a=10.0.0.2,c=10.0.0.3;      expect_rc "a repeated name exits 2" 2
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1,b=10.0.0.1,c=10.0.0.3;      expect_rc "a repeated address exits 2" 2
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1:2379,b=10.0.0.2,c=10.0.0.3; expect_rc "a peer port equal to the client port exits 2" 2
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1,b=10.0.0.1:2380,c=10.0.0.3; expect_rc "a repeated address with the default port written out exits 2" 2
run_cmd "${PKG}/vws-etcd-config" a a=10.0.0.1,client=10.0.0.2,c=10.0.0.3; expect_rc "the reserved name client exits 2" 2
run_cmd "${PKG}/vws-etcd-config" d a=10.0.0.1,b=10.0.0.2,c=10.0.0.3;      expect_rc "a member outside the list exits 2" 2
run_cmd "${PKG}/vws-etcd-config" b a=10.0.0.1,b=10.0.0.2,c=10.0.0.3:2390; expect_rc "a three-member config renders" 0
expect_text "every member is in initial-cluster" "$OUT" "initial-cluster: a=https://10.0.0.1:2380,b=https://10.0.0.2:2380,c=https://10.0.0.3:2390"
expect_text "the member listens on its own address" "$OUT" "listen-client-urls: https://10.0.0.2:2379"
expect_text "no listener is plain http" "$(grep -c 'http://' <<<"$OUT" || true)" "0"
expect_text "both ports require a client certificate" "$(grep -c 'client-cert-auth: true' <<<"$OUT")" "2"
expect_text "TLS comes from the systemd credentials" "$OUT" "cert-file: /run/credentials/vws-etcd.service/member.crt"

# TLS: one CA, a directory per member and one for clients; a rerun keeps what exists.
run_cmd "${PKG}/vws-etcd-tls" "$TLS" "$LIST";                              expect_rc "vws-etcd-tls exits 0" 0
expect_text "the keys are 0600" "$(stat -c %a "${TLS}/ca.key" "${TLS}"/*/*.key | sort -u | paste -sd,)" "600"
expect_text "a member certificate is signed by the CA" "$(openssl verify -CAfile "${TLS}/ca.crt" "${TLS}/b/member.crt")" "OK"
expect_text "the member SAN is its IP and name" "$(openssl x509 -in "${TLS}/b/member.crt" -noout -ext subjectAltName)" "IP Address:127.0.0.1, DNS:b"
ca_sum=$(sha256sum <"${TLS}/ca.crt")
run_cmd "${PKG}/vws-etcd-tls" "$TLS" "$LIST";                              expect_rc "a rerun exits 0" 0
expect_text "a rerun writes nothing" "${OUT:-nothing}" "nothing"
expect_text "a rerun keeps the CA" "$(sha256sum <"${TLS}/ca.crt")" "$ca_sum"
run_cmd flock "${TLS}/.lock" "${PKG}/vws-etcd-tls" "$TLS" "${LIST},d=127.0.0.2,e=127.0.0.3"
expect_rc "a run while another holds the lock exits 1" 1
expect_text "a locked run writes nothing" "$([[ -e ${TLS}/d ]] && echo written || echo none)" "none"

# Three TLS-only members.
start_member a "$LIST" "$CA"
start_member b "$LIST" "$CB"
start_member c "$LIST" "$CC"
if wait_healthy "$CA"; then ok "three members are healthy"; else bad "three members are healthy" "$(tail -n 5 "${WORK}/a.log")"; fi
run_cmd with_client "$CA" etcdctl member list -w json;                     expect_rc "member list exits 0" 0
expect_text "the cluster has three members" "$(jq '.members | length' <<<"$OUT")" "3"
expect_text "every peer URL is https" "$(jq -r '[.members[].peerURLs[] | select(startswith("https://") | not)] | length' <<<"$OUT")" "0"
run_cmd curl -fsS -m 10 --cacert "${TLS}/ca.crt" "https://127.0.0.1:${CA}/version"
expect_refused "a client without a certificate is refused"
run_cmd curl -fsS -m 10 "http://127.0.0.1:${CA}/version"
expect_refused "plain http is refused"
run_cmd curl -fsS -m 10 --cacert "${TLS}/ca.crt" --cert "${TLS}/client/client.crt" --key "${TLS}/client/client.key" "https://127.0.0.1:${CA}/version"
expect_rc "a client with the certificate is served" 0
run_cmd curl -fsS -m 10 --http1.1 --cacert "${TLS}/ca.crt" --cert "${TLS}/client/client.crt" --key "${TLS}/client/client.key" "https://127.0.0.1:${PA}/members"
expect_refused "the client certificate is refused on the peer port"
run_cmd curl -fsS -m 10 --http1.1 --cacert "${TLS}/ca.crt" --cert "${TLS}/b/member.crt" --key "${TLS}/b/member.key" "https://127.0.0.1:${PA}/members"
expect_rc "a member certificate is served on the peer port" 0

# vws backup --etcd and vws restore --etcd into a fresh member.
with_client "$CA" etcdctl put /vws/iam/probe restored-value >/dev/null
export ETCDCTL_ENDPOINTS="https://127.0.0.1:${CA}" ETCDCTL_CACERT="${TLS}/client/ca.crt"
export ETCDCTL_CERT="${TLS}/client/client.crt" ETCDCTL_KEY="${TLS}/client/client.key"
run_vws_real backup --etcd --out "${WORK}/snap.db";                        expect_rc "vws backup --etcd exits 0" 0
expect_text "backup names the snapshot" "$OUT" "OK    etcd-snapshot  wrote ${WORK}/snap.db"
expect_text "the snapshot is 0600" "$(stat -c %a "${WORK}/snap.db")" "600"
run_vws_real backup --etcd --out "${WORK}/snap.db";                        expect_rc "an existing snapshot is never overwritten" 2
ETCDCTL_CERT='' run_vws_real backup --etcd --out "${WORK}/x.db";          expect_rc "a backup without a client certificate exits 2" 2
ETCDCTL_KEY="${WORK}/missing.key" run_vws_real backup --etcd --out "${WORK}/x.db"; expect_rc "a missing key file exits 2" 2
ETCDCTL_ENDPOINTS="http://127.0.0.1:${CA}" run_vws_real backup --etcd --out "${WORK}/x.db"; expect_rc "an http endpoint exits 2" 2
ETCDCTL_COMMAND_TIMEOUT=3s ETCDCTL_ENDPOINTS="https://127.0.0.1:${PX}" run_vws_real backup --etcd --out "${WORK}/x.db"; expect_rc "a failed save exits 1" 1
expect_text "a failed save leaves no file" "$(find "$WORK" -maxdepth 1 -name 'x.db*' | wc -l)" "0"

printf 'not a snapshot' >"${WORK}/bad.db"
run_vws_real restore --etcd "${WORK}/bad.db" --data-dir "${WORK}/data-r" --name r --peer-url "https://127.0.0.1:${PR}"
expect_rc "a corrupt snapshot exits 1" 1
expect_text "a corrupt snapshot writes nothing" "$([[ -e ${WORK}/data-r ]] && echo written || echo none)" "none"
run_vws_real restore --etcd "${WORK}/snap.db" --data-dir "${WORK}/data-r" --name r --peer-url "http://127.0.0.1:${PR}"
expect_rc "an http peer URL exits 2" 2
run_vws_real restore --etcd "${WORK}/snap.db" --data-dir "${WORK}/data-r" --name r --peer-url "https://127.0.0.1:${PR}"
expect_rc "vws restore --etcd exits 0" 0
run_vws_real restore --etcd "${WORK}/snap.db" --data-dir "${WORK}/data-r" --name r --peer-url "https://127.0.0.1:${PR}"
expect_rc "an existing data directory exits 2" 2
run_cmd "${PKG}/vws-etcd-tls" "$TLS" "${LIST},d=127.0.0.2,r=127.0.0.1:${PR}"
expect_text "adding members writes the new ones" "$OUT" "wrote ${TLS}/r"
expect_text "adding members keeps the old ones" "$(grep -c "wrote ${TLS}/[abc]$" <<<"$OUT" || true)" "0"
start_member r "r=127.0.0.1:${PR}" "$CR"
if wait_healthy "$CR"; then ok "the restored member is healthy"; else bad "the restored member is healthy" "$(tail -n 5 "${WORK}/r.log")"; fi
expect_text "the restored member has the data" "$(with_client "$CR" etcdctl get --print-value-only /vws/iam/probe)" "restored-value"
expect_text "the restored member is a cluster of one" "$(with_client "$CR" etcdctl member list | wc -l)" "1"

# Retention: what the backup timer runs keeps the newest KEEP snapshots and touches nothing else.
mkdir -p "${WORK}/snaps"
touch "${WORK}/snaps/vws-etcd-20200101T000000Z.db" "${WORK}/snaps/vws-etcd-20200102T000000Z.db" \
  "${WORK}/snaps/vws-etcd-20200103T000000Z.db" "${WORK}/snaps/notes.txt"
run_cmd "${PKG}/vws-etcd-snapshot" "${WORK}/snaps" 2;                      expect_rc "vws-etcd-snapshot exits 0" 0
expect_text "KEEP snapshots remain" "$(find "${WORK}/snaps" -name 'vws-etcd-*.db' | wc -l)" "2"
expect_text "the newest older snapshot is kept" "$(ls "${WORK}/snaps")" "vws-etcd-20200103T000000Z.db"
expect_text "the oldest are removed" "$OUT" "removed ${WORK}/snaps/vws-etcd-20200102T000000Z.db"
expect_text "other files are kept" "$(ls "${WORK}/snaps")" "notes.txt"
new=$(find "${WORK}/snaps" -name 'vws-etcd-*.db' ! -name 'vws-etcd-2020*')
expect_text "the new snapshot verifies" "$(etcdutl snapshot status "$new" -w json | jq -r '.totalKey > 0')" "true"
expect_text "the new snapshot is 0600" "$(stat -c %a "$new")" "600"
run_cmd "${PKG}/vws-etcd-snapshot" "${WORK}/snaps" 0;                      expect_rc "KEEP 0 exits 2" 2
before=$(ls "${WORK}/snaps")
ETCDCTL_COMMAND_TIMEOUT=3s ETCDCTL_ENDPOINTS="https://127.0.0.1:${PX}" run_cmd "${PKG}/vws-etcd-snapshot" "${WORK}/snaps" 1
expect_rc "a failed save exits 1" 1
expect_text "a failed save removes nothing" "$(ls "${WORK}/snaps")" "$before"

finish_tests
