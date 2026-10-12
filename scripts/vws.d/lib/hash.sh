#!/usr/bin/env bash
# The applied hash of VWSCluster status.appliedValuesHash (vws-deploy#101). Source it, do not run it.
# It covers the full rendered values and every file of charts/vws (path and content), so a chart-only
# change (new flag, template, default) is not "unchanged" for vws apply and is drift for vws status.

# vws_applied_hash_of VALUES_SHA256: the applied hash for values with that sha256 and this chart.
vws_applied_hash_of() {
  {
    printf 'values %s\n' "$1"
    (cd "${VWS_ROOT}/charts/vws" && find . -type f ! -name '*.pyc' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)
  } | sha256sum | cut -d' ' -f1
}

# vws_applied_hash VALUES_FILE: the applied hash of a rendered values file and this chart.
vws_applied_hash() {
  vws_applied_hash_of "$(sha256sum "$1" | cut -d' ' -f1)"
}
