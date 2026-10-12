#!/usr/bin/env bash
# Shared by the lab scripts. LAB_DRY_RUN=1 prints every state-changing command instead of running it.
set -euo pipefail

LAB_ROOT=$(CDPATH='' cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../versions.env
source "$LAB_ROOT/scripts/versions.env"

LAB_CACHE=${LAB_CACHE:-$LAB_ROOT/.cache/lab}
mkdir -p "$LAB_CACHE"

run() {
  if [[ ${LAB_DRY_RUN:-0} == 1 ]]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

kc() {
  run sudo k3s kubectl "$@"
}

fetch() {
  local url=$1 sha=$2 file=$3
  if [[ -f $file ]] && echo "${sha}  ${file}" | sha256sum -c --status -; then
    return 0
  fi
  run curl -fsSL -o "$file" "$url"
  [[ ${LAB_DRY_RUN:-0} == 1 ]] || echo "${sha}  ${file}" | sha256sum -c -
}
