#!/usr/bin/env bash
# vws backup dispatches only to the vws-etcd snapshot implementation.
# Real member, TLS, restore and failed-save coverage lives in etcd.test.sh.
set -euo pipefail
# shellcheck source=tests/vws/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

for args in '' '--out old-backup' '--etcd-snapshot old.db' '--nosuch'; do
  read -r -a argv <<<"$args"
  run_vws_real backup "${argv[@]}"
  expect_rc "backup $args without --etcd exits 2" 2
  expect_text "backup $args names the supported mode" "$ERR" "usage: vws backup --etcd [--out FILE]"
done

# Stub only the tool boundary; the dispatcher and etcd library are real.
mkdir "${WORK}/bin"
cat >"${WORK}/bin/etcdctl" <<'SH'
#!/usr/bin/env bash
[[ $1 == snapshot && $2 == save ]] || exit 1
printf snapshot >"$3"
SH
cat >"${WORK}/bin/etcdutl" <<'SH'
#!/usr/bin/env bash
[[ $1 == snapshot && $2 == status && $4 == -w && $5 == json ]] || exit 1
printf '%s\n' '{"revision":7,"totalKey":3}'
SH
chmod +x "${WORK}/bin/etcdctl" "${WORK}/bin/etcdutl"
export PATH="${WORK}/bin:$PATH"
export ETCDCTL_ENDPOINTS=https://127.0.0.1:2379
export ETCDCTL_CACERT="${WORK}/ca.crt" ETCDCTL_CERT="${WORK}/client.crt" ETCDCTL_KEY="${WORK}/client.key"
touch "$ETCDCTL_CACERT" "$ETCDCTL_CERT" "$ETCDCTL_KEY"
run_vws_real backup --etcd --out "${WORK}/snapshot.db"
expect_rc "--etcd dispatches to snapshot save" 0
expect_text "the output names the verified snapshot" "$OUT" "wrote ${WORK}/snapshot.db (0600, revision 7, 3 keys)"
expect_text "--out reaches the snapshot writer" "$(<"${WORK}/snapshot.db")" snapshot
expect_text "the snapshot is private" "$(stat -c %a "${WORK}/snapshot.db")" 600
if [[ ! -s $VWS_FAKE_LOG ]]; then
  ok "etcd backup invokes no cluster wrapper"
else
  bad "etcd backup invokes no cluster wrapper" "$(<"$VWS_FAKE_LOG")"
fi
run_vws_real backup --etcd --out "${WORK}/snapshot.db"
expect_rc "--etcd preserves overwrite refusal" 2
run_vws_real backup --etcd --nosuch
expect_rc "--etcd rejects unknown arguments" 2
expect_text "etcd usage names the supported mode" "$ERR" "usage: vws backup --etcd [--out FILE]"
finish_tests
