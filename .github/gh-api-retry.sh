#!/usr/bin/env bash
# gh_api_retry: one `gh api` call with bounded backoff on a GitHub primary or secondary rate limit.
# Canonical copy. The repo-guard and verify-queue jobs inline the marked block below: verify-gate
# checks out the caller base, and verify-queue checks out nothing, so none of them can source this file.
# tests/workflows/test_gh_api_retry.py fails when an inlined copy drifts.
#
# A successful body is written to stdout. A failed attempt's body is discarded, so a later success is
# not prefixed by an error document. The token is not printed.
#
# Retried: "API rate limit exceeded" (including "for installation"), "secondary rate limit",
# "abuse detection mechanism", gh's "(HTTP 429)", and a GraphQL error whose type is RATE_LIMITED.
# Any other error, including a 403 that is not a rate limit, fails on the first response.
# After GH_API_RETRY_ATTEMPTS (default 5) the last rate-limit error is printed and the function
# returns non-zero (fail closed). GH_API_RETRY_DELAY (default 5 seconds) doubles each retry and
# stops growing at GH_API_RETRY_DELAY_CAP (default 40). sleep is the wait.

# BEGIN gh-api-retry
gh_api_rate_limited() {
  local file=$1
  grep -E -q 'API rate limit exceeded|secondary rate limit|abuse detection mechanism|\(HTTP 429\)' "$file" && return 0
  jq -e '[.errors[]? | select(.type == "RATE_LIMITED")] | length > 0' "$file" >/dev/null 2>&1
}

gh_api_retry() {
  local attempts=${GH_API_RETRY_ATTEMPTS:-5}
  local delay=${GH_API_RETRY_DELAY:-5}
  local cap=${GH_API_RETRY_DELAY_CAP:-40}
  local n=1 rc out err limited
  [[ $attempts =~ ^[1-9][0-9]*$ ]] || attempts=5
  [[ $delay =~ ^[0-9]+$ ]] || delay=5
  [[ $cap =~ ^[1-9][0-9]*$ ]] || cap=40
  err=$(mktemp)
  while true; do
    rc=0
    limited=0
    out=$(gh api "$@" 2>"$err") || rc=$?
    if [[ $rc -ne 0 ]]; then
      printf '%s\n' "$out" >>"$err"
    elif printf '%s' "$out" | jq -e '[.errors[]? | select(.type == "RATE_LIMITED")] | length > 0' >/dev/null 2>&1; then
      printf '%s\n' "$out" >>"$err"
      rc=1
      limited=1
    else
      # Command substitution dropped the trailing newlines. Put one back when there is a body,
      # and write nothing when the call succeeded with an empty body. A blank line is a row to
      # the verify-queue awk (an empty commit-status list would look pending).
      if [[ -n $out ]]; then
        printf '%s\n' "$out"
      fi
      rm -f "$err"
      return 0
    fi
    if [[ $limited -eq 0 ]] && gh_api_rate_limited "$err"; then
      limited=1
    fi
    if [[ $limited -eq 0 || $n -ge $attempts ]]; then
      cat "$err" >&2
      rm -f "$err"
      return "$rc"
    fi
    echo "gh api: rate limit, retry ${n}/$((attempts - 1)) in ${delay}s" >&2
    sleep "$delay"
    n=$((n + 1))
    if [[ $((delay * 2)) -gt $cap ]]; then
      delay=$cap
    else
      delay=$((delay * 2))
    fi
  done
}
# END gh-api-retry
