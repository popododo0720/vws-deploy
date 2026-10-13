#!/usr/bin/env bash
# Validate every chart CRD against a real kube-apiserver (envtest) with
# kubectl apply --dry-run=server. Catches what kubeconform cannot: structural
# schema rules the apiserver enforces, such as a forbidden uniqueItems (#210).
# Usage: scripts/crd-check.sh [crd.yaml ...]   (default: charts/*/crds/*.yaml)
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/scripts/versions.env"

cache=${VWS_ENVTEST_DIR:-$ROOT/.cache/envtest-${ENVTEST_VERSION}}
bin=$cache/controller-tools/envtest
if [[ ! -x $bin/kube-apiserver ]]; then
  mkdir -p "$cache"
  tgz=$cache/envtest.tgz
  curl -fsSL -o "$tgz" "https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-${ENVTEST_VERSION}/envtest-${ENVTEST_VERSION}-linux-amd64.tar.gz"
  echo "${ENVTEST_SHA256}  ${tgz}" | sha256sum -c - >/dev/null
  tar -xzf "$tgz" -C "$cache"
  rm -f "$tgz"
fi

if (($#)); then files=("$@"); else files=("$ROOT"/charts/*/crds/*.yaml); fi

work=$(mktemp -d)
pids=()
cleanup() {
  local i
  # Stop the apiserver before etcd (it blocks on a dead etcd), then force.
  for ((i = ${#pids[@]} - 1; i >= 0; i--)); do
    kill "${pids[i]}" 2>/dev/null || true
    for _ in $(seq 20); do kill -0 "${pids[i]}" 2>/dev/null || break; sleep 0.5; done
    kill -9 "${pids[i]}" 2>/dev/null || true
    wait "${pids[i]}" 2>/dev/null || true
  done
  rm -rf "$work"
}
trap cleanup EXIT

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
etcd_port=$(free_port); peer_port=$(free_port); api_port=$(free_port)

"$bin/etcd" --data-dir "$work/etcd" --listen-client-urls "http://127.0.0.1:$etcd_port" \
  --advertise-client-urls "http://127.0.0.1:$etcd_port" --listen-peer-urls "http://127.0.0.1:$peer_port" \
  >"$work/etcd.log" 2>&1 &
pids+=($!)

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=crd-check \
  -keyout "$work/sa.key" -out "$work/sa.crt" >/dev/null 2>&1
token=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
echo "${token},crd-check,crd-check,system:masters" >"$work/tokens.csv"

"$bin/kube-apiserver" --etcd-servers "http://127.0.0.1:$etcd_port" \
  --bind-address 127.0.0.1 --secure-port "$api_port" --cert-dir "$work/certs" \
  --token-auth-file "$work/tokens.csv" --authorization-mode AlwaysAllow \
  --service-account-issuer https://crd-check.local --service-account-key-file "$work/sa.crt" \
  --service-account-signing-key-file "$work/sa.key" --service-cluster-ip-range 10.0.0.0/24 \
  >"$work/apiserver.log" 2>&1 &
pids+=($!)

kc=("$bin/kubectl" --server "https://127.0.0.1:$api_port" --token "$token" --insecure-skip-tls-verify)
for _ in $(seq 120); do
  "${kc[@]}" get --raw /readyz >/dev/null 2>&1 && break
  sleep 0.5
done
"${kc[@]}" get --raw /readyz >/dev/null || { echo "crd-check: kube-apiserver not ready" >&2; tail -20 "$work/apiserver.log" >&2; exit 1; }

rc=0
for f in "${files[@]}"; do
  if out=$("${kc[@]}" apply --dry-run=server -f "$f" 2>&1); then
    echo "crd-check: ok ${f#"$ROOT"/}"
  else
    echo "crd-check: FAIL ${f#"$ROOT"/}" >&2
    echo "$out" >&2
    rc=1
  fi
done
exit "$rc"
