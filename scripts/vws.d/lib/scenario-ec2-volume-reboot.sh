#!/usr/bin/env bash
# scenario: EC2 instance run, EBS volume create and attach, instance reboot keeps the attachment, then teardown
# Sourced by lib/verify_scenario.sh. Every instance and volume is tagged Name=<run ID>, so it touches nothing else.
# The steps pass IDs through files in vws_sc_tmp, because each step runs in its own child process.
# The reboot check is API-level: after RebootInstances it watches the state for VWS_VERIFY_REBOOT_SETTLE seconds
# and records every state it saw, then the instance must be running and the volume still attached to it. Nothing
# logs into the guest, so a reboot that changes no state is not proven to have happened.
# shellcheck disable=SC2034,SC2154 # VWS_SCENARIO_ENV is read by, and vws_sc_tmp, vws_sc_deadline and VWS_SCENARIO_RUN_ID come from, lib/verify_scenario.sh
VWS_SCENARIO_ENV=(VWS_VERIFY_EC2_ENDPOINT VWS_VERIFY_ACCESS_KEY_ID VWS_VERIFY_SECRET_ACCESS_KEY
  VWS_VERIFY_IMAGE_ID VWS_VERIFY_INSTANCE_TYPE)

# Output goes to files, not $(...): a killed aws can leave a child that holds a pipe open past the signal grace.
vws_ec2() { # OUT ARGS...: one ec2 call, its JSON reply in OUT
  local out=$1
  shift
  vws_aws ec2 "$@" --output json >"$out"
}

vws_ec2_id() { # instance|volume: the ID a create step saved
  [[ -s ${vws_sc_tmp}/$1 ]] || { echo "no ${1} ID was saved" >&2; return 1; }
  cat "${vws_sc_tmp}/$1"
}

vws_ec2_tags() { printf 'ResourceType=%s,Tags=[{Key=Name,Value=%s}]' "$1" "$VWS_SCENARIO_RUN_ID"; }

vws_ec2_state() { # instance|volume ID: the current state, empty when the resource is not listed
  local f="${vws_sc_tmp}/state.json"
  case $1 in
    instance) vws_ec2 "$f" describe-instances --instance-ids "$2" || return 1
      jq -r '[.Reservations[]?.Instances[]? | .State.Name] | first // empty' "$f" ;;
    volume) vws_ec2 "$f" describe-volumes --volume-ids "$2" || return 1
      jq -r '[.Volumes[]? | .State] | first // empty' "$f" ;;
  esac
}

vws_ec2_nap() { # END: VWS_VERIFY_POLL seconds (default 5), never past END
  local poll=${VWS_VERIFY_POLL:-5} left=$(($1 - $(date +%s)))
  ((poll < left)) || poll=$((left > 0 ? left : 0))
  echo "$poll"
}

# vws_ec2_wait_id KIND ID WANT: poll every VWS_VERIFY_POLL seconds (default 5) until the state is WANT, for at
# most VWS_VERIFY_WAIT seconds (default 600) and never past the signal grace.
vws_ec2_wait_id() {
  local got='' now limit=${VWS_VERIFY_WAIT:-600} end
  end=$(($(date +%s) + limit))
  [[ -z ${vws_sc_deadline:-} ]] || ((end < vws_sc_deadline)) || end=$vws_sc_deadline
  while :; do
    got=$(vws_ec2_state "$1" "$2") || return 1
    [[ $got != "$3" ]] || { echo "$2 $got"; return 0; }
    now=$(date +%s)
    ((now < end)) || { echo "$2 is ${got:-not listed}, not $3, after waiting ${limit}s" >&2; return 1; }
    sleep "$(vws_ec2_nap "$end")"
  done
}

vws_ec2_wait() { # instance|volume WANT
  local id
  id=$(vws_ec2_id "$1") || return 1
  vws_ec2_wait_id "$1" "$id" "$2"
}

vws_ec2_run() {
  local f="${vws_sc_tmp}/run.json"
  local -a args=(run-instances --image-id "$VWS_VERIFY_IMAGE_ID" --instance-type "$VWS_VERIFY_INSTANCE_TYPE"
    --count 1 --client-token "$VWS_SCENARIO_RUN_ID" --tag-specifications "$(vws_ec2_tags instance)")
  [[ -z ${VWS_VERIFY_SUBNET_ID:-} ]] || args+=(--subnet-id "$VWS_VERIFY_SUBNET_ID")
  vws_ec2 "$f" "${args[@]}" || return 1
  jq -er '.Instances[0].InstanceId | strings' "$f" >"${vws_sc_tmp}/instance" ||
    { echo "no InstanceId in the reply: $(head -c 200 "$f")" >&2; return 1; }
  cat "${vws_sc_tmp}/instance"
}

vws_ec2_create_volume() { # in the instance's availability zone
  local id az f="${vws_sc_tmp}/vol.json"
  id=$(vws_ec2_id instance) || return 1
  vws_ec2 "$f" describe-instances --instance-ids "$id" || return 1
  az=$(jq -er '[.Reservations[]?.Instances[]? | .Placement.AvailabilityZone | strings] | first' "$f") ||
    { echo "instance ${id} has no availability zone" >&2; return 1; }
  vws_ec2 "$f" create-volume --availability-zone "$az" --size "${VWS_VERIFY_VOLUME_SIZE:-1}" \
    --tag-specifications "$(vws_ec2_tags volume)" || return 1
  jq -er '.VolumeId | strings' "$f" >"${vws_sc_tmp}/volume" ||
    { echo "no VolumeId in the reply: $(head -c 200 "$f")" >&2; return 1; }
  cat "${vws_sc_tmp}/volume"
}

vws_ec2_on_volume() { # OP [ARGS...]: attach-volume, detach-volume or delete-volume on the saved volume
  local op=$1 vol
  shift
  vol=$(vws_ec2_id volume) || return 1
  vws_ec2 "${vws_sc_tmp}/op.json" "$op" --volume-id "$vol" "$@" || return 1
  echo "$op $vol"
}

vws_ec2_attach() {
  local id
  id=$(vws_ec2_id instance) || return 1
  vws_ec2_on_volume attach-volume --instance-id "$id" --device "${VWS_VERIFY_VOLUME_DEVICE:-/dev/sdf}"
}

vws_ec2_on_instance() { # OP: reboot-instances or terminate-instances on the saved instance
  local id
  id=$(vws_ec2_id instance) || return 1
  vws_ec2 "${vws_sc_tmp}/op.json" "$1" --instance-ids "$id" || return 1
  echo "$1 $id"
}

vws_ec2_reboot_settle() { # the states the instance passes through for VWS_VERIFY_REBOOT_SETTLE seconds (default 60)
  local id got seen='' end
  id=$(vws_ec2_id instance) || return 1
  end=$(($(date +%s) + ${VWS_VERIFY_REBOOT_SETTLE:-60}))
  while :; do
    got=$(vws_ec2_state instance "$id") || return 1
    [[ " ${seen} " == *" ${got:-not-listed} "* ]] || seen+=" ${got:-not-listed}"
    (($(date +%s) < end)) || break
    sleep "$(vws_ec2_nap "$end")"
  done
  echo "${id} states seen:${seen}"
}

vws_ec2_still_attached() {
  local id vol f="${vws_sc_tmp}/att.json"
  id=$(vws_ec2_id instance) && vol=$(vws_ec2_id volume) || return 1
  vws_ec2 "$f" describe-volumes --volume-ids "$vol" || return 1
  jq -e --arg i "$id" '[.Volumes[]?.Attachments[]? | select(.InstanceId == $i and .State == "attached")] | length == 1' \
    "$f" >/dev/null || { echo "${vol} is not attached to ${id}: $(head -c 200 "$f")" >&2; return 1; }
  echo "${vol} attached to ${id}"
}

vws_ec2_tagged() { # instance|volume: IDs tagged with the run ID that still exist, one per line
  local f="${vws_sc_tmp}/tagged.json" filter="Name=tag:Name,Values=${VWS_SCENARIO_RUN_ID}"
  case $1 in
    instance) vws_ec2 "$f" describe-instances --filters "$filter" || return 1
      jq -r '.Reservations[]?.Instances[]? | select(.State.Name != "terminated") | .InstanceId' "$f" ;;
    volume) vws_ec2 "$f" describe-volumes --filters "$filter" || return 1
      jq -r '.Volumes[]? | .VolumeId' "$f" ;;
  esac
}

# The cleanups find their resources by tag, not by the saved ID, so a create whose reply was lost is still found.
# The instance cleanup runs first. After a signal it sends every terminate and does not wait, so a slow detach in
# the volume cleanup cannot spend the grace before the instance is terminated.
vws_ec2_clean_instances() {
  local ids id
  ids=$(vws_ec2_tagged instance) || return 1
  [[ -n $ids ]] || { echo "already gone"; return 0; }
  for id in $ids; do
    vws_ec2 "${vws_sc_tmp}/op.json" terminate-instances --instance-ids "$id" || return 1
  done
  if [[ -n ${vws_sc_deadline:-} ]]; then
    echo "terminating" $ids
    return 0
  fi
  for id in $ids; do
    vws_ec2_wait_id instance "$id" terminated || return 1
  done
}

vws_ec2_clean_volumes() {
  local ids id state
  ids=$(vws_ec2_tagged volume) || return 1
  [[ -n $ids ]] || { echo "already gone"; return 0; }
  for id in $ids; do
    state=$(vws_ec2_state volume "$id") || return 1
    [[ $state != deleting && -n $state ]] || { echo "${id} already deleting"; continue; }
    if [[ $state != available ]]; then
      [[ $state != in-use ]] || vws_ec2 "${vws_sc_tmp}/op.json" detach-volume --volume-id "$id" --force || return 1
      vws_ec2_wait_id volume "$id" available || return 1
    fi
    vws_ec2 "${vws_sc_tmp}/op.json" delete-volume --volume-id "$id" || return 1
    echo "deleted ${id}"
  done
}

vws_scenario_run() {
  # Both before the first create, the volume one first, so the instance cleanup runs first.
  vws_defer delete-volume vws_ec2_clean_volumes
  vws_defer terminate-instance vws_ec2_clean_instances
  vws_step run-instance vws_ec2_run
  vws_step instance-running vws_ec2_wait instance running
  vws_step create-volume vws_ec2_create_volume
  vws_step volume-available vws_ec2_wait volume available
  vws_step attach-volume vws_ec2_attach
  vws_step volume-in-use vws_ec2_wait volume in-use
  vws_step reboot-instance vws_ec2_on_instance reboot-instances
  vws_step reboot-settle vws_ec2_reboot_settle
  vws_step running-after-reboot vws_ec2_wait instance running
  vws_step attached-after-reboot vws_ec2_still_attached
  vws_step detach-volume vws_ec2_on_volume detach-volume
  vws_step volume-detached vws_ec2_wait volume available
  vws_step delete-volume vws_ec2_on_volume delete-volume
  vws_step terminate-instance vws_ec2_on_instance terminate-instances
  vws_step instance-terminated vws_ec2_wait instance terminated
}
