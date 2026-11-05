#!/usr/bin/env bash
# scenario: S3 bucket create, object put, get, list and delete through aws-cli
# Sourced by lib/verify_scenario.sh. The bucket is named after the run ID, so it touches nothing else.
# shellcheck disable=SC2034,SC2154 # VWS_SCENARIO_ENV is read by, and vws_sc_tmp and VWS_SCENARIO_RUN_ID come from, lib/verify_scenario.sh
VWS_SCENARIO_ENV=(VWS_VERIFY_ENDPOINT VWS_VERIFY_ACCESS_KEY_ID VWS_VERIFY_SECRET_ACCESS_KEY)

# A file, not $(...): a killed aws can leave a child that holds a pipe open past the signal grace.
vws_s3_gone() { # CODE ARGS...: the aws call succeeds, or fails only because the resource is already gone
  local code=$1 err rc=0
  shift
  vws_aws "$@" >/dev/null 2>"${vws_sc_tmp}/gone.err" || rc=$?
  ((rc != 0)) || return 0
  err=$(cat "${vws_sc_tmp}/gone.err")
  if [[ $err == *NoSuchBucket* || $err == *"$code"* ]]; then
    echo "already gone"
    return 0
  fi
  printf '%s\n' "${err:-aws exited ${rc} with no message (124: timed out)}" >&2
  return 1
}

vws_s3_same() { # WANT GOT: the downloaded object equals the uploaded one
  cmp -s "$1" "$2" || { echo "downloaded object differs from the uploaded one" >&2; return 1; }
  sha256sum "$1" | cut -d' ' -f1
}

vws_s3_lists() { # BUCKET KEY
  local keys
  keys=$(vws_aws s3api list-objects-v2 --bucket "$1" --query 'Contents[].Key' --output text) || return 1
  [[ $'\t'"${keys//$'\n'/$'\t'}"$'\t' == *$'\t'"$2"$'\t'* ]] || { echo "key $2 not listed: ${keys}" >&2; return 1; }
  echo "$2"
}

vws_scenario_run() {
  local bucket=$VWS_SCENARIO_RUN_ID key=verify.txt want="${vws_sc_tmp}/want" got="${vws_sc_tmp}/got"
  printf '%s\n' "$VWS_SCENARIO_RUN_ID" >"$want"
  vws_defer delete-bucket vws_s3_gone NoSuchBucket s3api delete-bucket --bucket "$bucket"
  vws_step create-bucket vws_aws s3api create-bucket --bucket "$bucket"
  vws_defer delete-object vws_s3_gone NoSuchKey s3api delete-object --bucket "$bucket" --key "$key"
  vws_step put-object vws_aws s3api put-object --bucket "$bucket" --key "$key" --body "$want"
  vws_step get-object vws_aws s3api get-object --bucket "$bucket" --key "$key" "$got"
  vws_step object-matches vws_s3_same "$want" "$got"
  vws_step list-objects vws_s3_lists "$bucket" "$key"
  vws_step delete-object vws_aws s3api delete-object --bucket "$bucket" --key "$key"
  vws_step delete-bucket vws_aws s3api delete-bucket --bucket "$bucket"
}
