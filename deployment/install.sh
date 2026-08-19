#!/usr/bin/env bash

set -Eeuo pipefail

INSTALL_ROOT=/opt/gate-controller-deploy
RELEASES_ROOT="$INSTALL_ROOT/releases"
CURRENT_LINK="$INSTALL_ROOT/current"
APP_SERVICE=file-monitor.service
LEGACY_COMMAND_UNIT=gate-command-server.service
UPDATER_SERVICE=gate-controller-updater.service
UPDATER_TIMER=gate-controller-updater.timer
SYSTEMD_ROOT=/etc/systemd/system
CLOUDFLARED_SERVICE=cloudflared.service
CLOUDFLARED_DROP_IN=cloudflared.service.d/20-http2.conf
UPDATER_HELPER=/usr/local/libexec/gate-controller/gate-controller-updater.py
MEDIA_BOOTSTRAP_ROOT=/usr/local/libexec/gate-media-bootstrap
STATE_ROOT=/var/lib/gate-controller
UPLOAD_ROOT=$STATE_ROOT/uploads
FTP_USER=ftp-user
ENV_FILE=/etc/gate-controller.env
UPDATE_LOCK=/run/gate-controller-updater/update.lock
UPDATE_BRANCH=${GATE_UPDATE_BRANCH:-master}
SOURCE=/opt/gate-controller
ENABLE_UPDATES=false
STAGING=
BACKUP_DIR=
PREVIOUS_CURRENT=
ACTIVATION_STARTED=false
INSTALL_SUCCEEDED=false
APP_WAS_ENABLED=false
APP_WAS_ACTIVE=false
LEGACY_COMMAND_WAS_ENABLED=false
LEGACY_COMMAND_WAS_ACTIVE=false
UPDATER_WAS_ENABLED=false
UPDATER_WAS_ACTIVE=false
CLOUDFLARED_WAS_PRESENT=false
CLOUDFLARED_WAS_ENABLED=false
CLOUDFLARED_WAS_ACTIVE=false
CLOUDFLARED_DROP_IN_CHANGED=false
CLOUDFLARED_CURRENT_JOB=
CLOUDFLARED_CURRENT_ACTIVE_STATE=
CLOUDFLARED_JOB_TIMEOUT_SECONDS=30
CLOUDFLARED_QUIESCE_TIMEOUT_SECONDS=10
CLOUDFLARED_POLL_SECONDS=1
ROLLBACK_FAILED=false
ROLLBACK_DIAGNOSTICS=
ROLLBACK_OWNER_SUBSHELL=$BASH_SUBSHELL
ROLLBACK_STARTED=false
ROLLBACK_STEP_SUCCEEDED=true
ROLLBACK_PREREQUISITES_SUCCEEDED=true
TRUST_ANCHOR_HANDOFF=
FTP_PREVIOUS_HOME=

usage() {
  cat <<'EOF'
Usage: sudo deployment/install.sh [--source PATH] --enable-updates

Stages one clean Git commit into /opt/gate-controller-deploy, configures the
shared FTP upload path, migrates legacy state only when the persistent
destination is absent, refreshes the fixed helper and systemd units, and
explicitly enables the outbound updater timer.
EOF
}

fail() {
  printf 'gate-controller install: %s\n' "$*" >&2
  return 1
}

acquire_install_lock() {
  local lock_file=$1
  exec 9>"$lock_file"
  flock -n 9 || fail "another install or update is already running"
}

validate_env_file() {
  local env_file=$1
  local expected_uid=${2:-0}
  local expected_gid=${3:-0}
  local owner group mode line value
  local has_supabase_url=false
  local has_service_key=false
  local has_cloudflare_api_url=false
  local has_cloudflare_access_client_id=false
  local has_cloudflare_access_client_secret=false

  [[ -f $env_file && ! -L $env_file ]] \
    || fail "$env_file must be a regular file"
  read -r owner group mode < <(
    python3 - "$env_file" <<'PY'
import os
import stat
import sys

metadata = os.stat(sys.argv[1], follow_symlinks=False)
print(metadata.st_uid, metadata.st_gid, format(stat.S_IMODE(metadata.st_mode), "o"))
PY
  )
  [[ $owner == "$expected_uid" && $group == "$expected_gid" ]] \
    || fail "$env_file must be owned by root:root"
  [[ $mode == 600 ]] || fail "$env_file must have mode 0600"

  while IFS= read -r line || [[ -n $line ]]; do
    line=${line#"${line%%[![:space:]]*}"}
    [[ -n $line && $line != \#* ]] || continue
    line=${line#export }
    case "$line" in
      SUPABASE_URL=*)
        value=${line#*=}
        [[ -n $value && $value != "''" && $value != '""' ]] && has_supabase_url=true
        ;;
      SUPABASE_SERVICE_ROLE_KEY=*)
        value=${line#*=}
        [[ -n $value && $value != "''" && $value != '""' ]] && has_service_key=true
        ;;
      GATE_CLOUDFLARE_API_URL=*)
        value=${line#*=}
        [[ -n $value && $value != "''" && $value != '""' ]] && has_cloudflare_api_url=true
        ;;
      GATE_CLOUDFLARE_ACCESS_CLIENT_ID=*)
        value=${line#*=}
        [[ -n $value && $value != "''" && $value != '""' ]] && has_cloudflare_access_client_id=true
        ;;
      GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET=*)
        value=${line#*=}
        [[ -n $value && $value != "''" && $value != '""' ]] && has_cloudflare_access_client_secret=true
        ;;
    esac
  done <"$env_file"
  [[ $has_supabase_url == false && $has_service_key == false ]] \
    || fail "legacy Supabase credentials must not be present in the active controller environment"
  [[ $has_cloudflare_api_url == "$has_cloudflare_access_client_id" \
    && $has_cloudflare_api_url == "$has_cloudflare_access_client_secret" ]] \
    || fail "GATE_CLOUDFLARE_API_URL, GATE_CLOUDFLARE_ACCESS_CLIENT_ID, and GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET must be configured together"
}

create_fixed_trust_anchor_handoff() {
  local source=$1
  local handoff=$2

  if [[ -e $handoff || -L $handoff ]]; then
    fail "trust-anchor handoff already exists: $handoff" || true
    return 1
  fi
  install -d -o root -g root -m 0700 \
    "$handoff/deployment/media" \
    "$handoff/deployment/systemd/cloudflared.service.d" \
    "$handoff/gate_media_auth" \
    "$handoff/gate_media_gateway" \
    "$handoff/gate_media_transcoder"
  copy_fixed_trust_anchor_file \
    "$source/deployment/gate_controller_updater.py" \
    "$handoff/deployment/gate_controller_updater.py" || return 1
  copy_fixed_trust_anchor_file \
    "$source/file-monitor.service" "$handoff/file-monitor.service" || return 1
  copy_fixed_trust_anchor_file \
    "$source/deployment/systemd/$UPDATER_SERVICE" \
    "$handoff/deployment/systemd/$UPDATER_SERVICE" || return 1
  copy_fixed_trust_anchor_file \
    "$source/deployment/systemd/$UPDATER_TIMER" \
    "$handoff/deployment/systemd/$UPDATER_TIMER" || return 1
  copy_fixed_trust_anchor_file \
    "$source/deployment/systemd/$CLOUDFLARED_DROP_IN" \
    "$handoff/deployment/systemd/$CLOUDFLARED_DROP_IN" || return 1
  copy_fixed_trust_anchor_file \
    "$source/deployment/install-media.sh" "$handoff/deployment/install-media.sh" \
    || return 1
  copy_fixed_trust_anchor_file \
    "$source/deployment/media/mediamtx.yml" \
    "$handoff/deployment/media/mediamtx.yml" || return 1
  copy_fixed_trust_anchor_file \
    "$source/deployment/media/nginx-whep-locations.conf.template" \
    "$handoff/deployment/media/nginx-whep-locations.conf.template" || return 1
  for name in \
    gate-media-auth.service \
    gate-media-gateway.service \
    gate-media-transcoder.service \
    gate-media-turn-refresh.service \
    gate-media-turn-refresh.timer; do
    copy_fixed_trust_anchor_file \
      "$source/deployment/systemd/$name" "$handoff/deployment/systemd/$name" \
      || return 1
  done
  copy_fixed_trust_anchor_file \
    "$source/deployment/gate_media_turn_refresh.py" \
    "$handoff/deployment/gate_media_turn_refresh.py" || return 1
  copy_fixed_trust_anchor_file \
    "$source/gate_media_config.py" "$handoff/gate_media_config.py" || return 1
  for name in __init__.py __main__.py token.py capabilities.py; do
    copy_fixed_trust_anchor_file \
      "$source/gate_media_auth/$name" "$handoff/gate_media_auth/$name" \
      || return 1
  done
  for name in __init__.py __main__.py; do
    copy_fixed_trust_anchor_file \
      "$source/gate_media_gateway/$name" "$handoff/gate_media_gateway/$name" \
      || return 1
    copy_fixed_trust_anchor_file \
      "$source/gate_media_transcoder/$name" "$handoff/gate_media_transcoder/$name" \
      || return 1
  done
  find "$handoff" -type f -exec chmod 0444 {} +
  find "$handoff" -type d -exec chmod 0555 {} +
}

copy_fixed_trust_anchor_file() {
  local source=$1
  local destination=$2

  if [[ ! -f $source || -L $source ]]; then
    fail "$source must be a regular file" || true
    return 1
  fi
  install -o root -g root -m 0444 "$source" "$destination"
}

publish_bootstrap_release() {
  local staging=$1
  local release=$2

  if [[ -e $release || -L $release ]]; then
    [[ -d $release && ! -L $release ]] \
      || fail "existing release is not a managed directory: $release"
    rm -rf -- "$staging"
  else
    mv "$staging" "$release"
  fi
}

install_fixed_trust_anchors() {
  local release=$1
  local systemd_root=$2
  local updater_helper=$3
  local helper_root=${updater_helper%/*}

  install -d -o root -g root -m 0755 "$helper_root"
  install -o root -g root -m 0755 \
    "$release/deployment/gate_controller_updater.py" "$updater_helper"
  install -o root -g root -m 0644 \
    "$release/file-monitor.service" "$systemd_root/$APP_SERVICE"
  install -o root -g root -m 0644 \
    "$release/deployment/systemd/$UPDATER_SERVICE" \
    "$systemd_root/$UPDATER_SERVICE"
  install -o root -g root -m 0644 \
    "$release/deployment/systemd/$UPDATER_TIMER" \
    "$systemd_root/$UPDATER_TIMER"
}

run_systemctl_bounded() {
  timeout --signal=TERM --kill-after=5s 30s systemctl "$@"
}

inspect_cloudflared_runtime_state() {
  local line output property value

  output=$(
    run_systemctl_bounded show "$CLOUDFLARED_SERVICE" \
      --property=Job --property=ActiveState --all
  ) || {
    fail "could not inspect the runtime state of $CLOUDFLARED_SERVICE" || true
    return 1
  }
  CLOUDFLARED_CURRENT_JOB=
  CLOUDFLARED_CURRENT_ACTIVE_STATE=
  while IFS= read -r line; do
    property=${line%%=*}
    value=${line#*=}
    case "$property" in
      Job) CLOUDFLARED_CURRENT_JOB=$value ;;
      ActiveState) CLOUDFLARED_CURRENT_ACTIVE_STATE=$value ;;
    esac
  done <<<"$output"
  [[ -n $CLOUDFLARED_CURRENT_ACTIVE_STATE ]] || {
    fail "$CLOUDFLARED_SERVICE returned no ActiveState" || true
    return 1
  }
}

cloudflared_runtime_is_quiescent() {
  [[ -z $CLOUDFLARED_CURRENT_JOB ]] || return 1
  case "$CLOUDFLARED_CURRENT_ACTIVE_STATE" in
    active|inactive|failed) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_cloudflared_quiescence() {
  local timeout_seconds=${1:-$CLOUDFLARED_QUIESCE_TIMEOUT_SECONDS}
  local deadline=$((SECONDS + timeout_seconds))

  while true; do
    inspect_cloudflared_runtime_state || return 1
    cloudflared_runtime_is_quiescent && return 0
    (( SECONDS < deadline )) || return 1
    sleep "$CLOUDFLARED_POLL_SECONDS"
  done
}

cancel_cloudflared_job_and_wait() {
  local job_path=$1
  local job_id=${job_path##*/}
  local cancel_failed=false

  if [[ ! $job_id =~ ^[1-9][0-9]*$ ]]; then
    fail "$CLOUDFLARED_SERVICE returned an invalid systemd job path: $job_path" \
      || true
    return 1
  fi
  if ! run_systemctl_bounded cancel "$job_id"; then
    fail "could not cancel systemd job $job_id for $CLOUDFLARED_SERVICE" \
      || true
    cancel_failed=true
  fi
  if ! wait_for_cloudflared_quiescence; then
    fail "$CLOUDFLARED_SERVICE did not become quiescent after cancelling job $job_id" \
      || true
    return 1
  fi
  [[ $cancel_failed == false ]]
}

quiesce_cloudflared_before_rollback() {
  inspect_cloudflared_runtime_state || return 1
  if [[ -n $CLOUDFLARED_CURRENT_JOB ]]; then
    cancel_cloudflared_job_and_wait "$CLOUDFLARED_CURRENT_JOB"
  elif ! cloudflared_runtime_is_quiescent; then
    wait_for_cloudflared_quiescence
  fi
}

restart_cloudflared_bounded() {
  local deadline=$((SECONDS + CLOUDFLARED_JOB_TIMEOUT_SECONDS))
  local job_path

  if ! run_systemctl_bounded --no-block restart "$CLOUDFLARED_SERVICE"; then
    fail "could not enqueue restart for $CLOUDFLARED_SERVICE" || true
    return 1
  fi
  while true; do
    inspect_cloudflared_runtime_state || return 1
    if [[ -z $CLOUDFLARED_CURRENT_JOB ]]; then
      if [[ $CLOUDFLARED_CURRENT_ACTIVE_STATE == active ]]; then
        break
      fi
      case "$CLOUDFLARED_CURRENT_ACTIVE_STATE" in
        inactive|failed)
          fail "$CLOUDFLARED_SERVICE became $CLOUDFLARED_CURRENT_ACTIVE_STATE during restart" \
            || true
          return 1
          ;;
      esac
    fi
    if (( SECONDS >= deadline )); then
      job_path=$CLOUDFLARED_CURRENT_JOB
      if [[ -n $job_path ]]; then
        cancel_cloudflared_job_and_wait "$job_path" || {
          fail "$CLOUDFLARED_SERVICE restart timed out and its systemd job could not be quiesced" \
            || true
          return 1
        }
      elif ! wait_for_cloudflared_quiescence; then
        fail "$CLOUDFLARED_SERVICE restart timed out in a transitional state" \
          || true
        return 1
      fi
      fail "$CLOUDFLARED_SERVICE restart timed out; its systemd job was cancelled and quiesced" \
        || true
      return 1
    fi
    sleep "$CLOUDFLARED_POLL_SECONDS"
  done
  run_systemctl_bounded is-active --quiet "$CLOUDFLARED_SERVICE" || {
    fail "$CLOUDFLARED_SERVICE is not active after restart" || true
    return 1
  }
}

capture_cloudflared_service_state() {
  local load_state status

  CLOUDFLARED_WAS_PRESENT=false
  CLOUDFLARED_WAS_ENABLED=false
  CLOUDFLARED_WAS_ACTIVE=false
  load_state=$(
    run_systemctl_bounded show "$CLOUDFLARED_SERVICE" \
      --property=LoadState --value
  ) || fail "could not inspect $CLOUDFLARED_SERVICE"
  if [[ $load_state == not-found ]]; then
    return
  fi
  [[ $load_state == loaded ]] \
    || fail "$CLOUDFLARED_SERVICE has unexpected load state: $load_state"
  CLOUDFLARED_WAS_PRESENT=true

  if run_systemctl_bounded is-enabled --quiet "$CLOUDFLARED_SERVICE"; then
    CLOUDFLARED_WAS_ENABLED=true
  else
    status=$?
    [[ $status -eq 1 ]] \
      || fail "could not inspect whether $CLOUDFLARED_SERVICE is enabled"
  fi
  if run_systemctl_bounded is-active --quiet "$CLOUDFLARED_SERVICE"; then
    CLOUDFLARED_WAS_ACTIVE=true
  else
    status=$?
    [[ $status -eq 3 ]] \
      || fail "could not inspect whether $CLOUDFLARED_SERVICE is active"
  fi
}

backup_cloudflared_transport_drop_in() {
  local systemd_root=$1
  local backup_dir=$2
  local destination=$systemd_root/$CLOUDFLARED_DROP_IN
  local backup=$backup_dir/cloudflared-transport-drop-in
  local absent=$backup_dir/cloudflared-transport-drop-in.absent

  [[ ! -e $backup && ! -L $backup && ! -e $absent ]] \
    || fail "cloudflared transport backup already exists"
  if [[ -e $destination || -L $destination ]]; then
    cp -a -- "$destination" "$backup"
  else
    : >"$absent"
  fi
}

install_cloudflared_transport_drop_in() {
  local source=$1
  local systemd_root=$2
  local source_path=$source/deployment/systemd/$CLOUDFLARED_DROP_IN
  local destination_root=$systemd_root/cloudflared.service.d
  local destination=$systemd_root/$CLOUDFLARED_DROP_IN
  local temporary

  [[ -f $source_path && ! -L $source_path ]] \
    || fail "trusted cloudflared transport drop-in is missing"
  [[ ! -L $destination_root ]] \
    || fail "$destination_root must not be a symbolic link"
  [[ ! -d $destination ]] \
    || fail "$destination must not be a directory"
  if [[ ! -f $destination || -L $destination ]] \
    || ! cmp -s -- "$source_path" "$destination"; then
    CLOUDFLARED_DROP_IN_CHANGED=true
  else
    CLOUDFLARED_DROP_IN_CHANGED=false
  fi
  install -d -o root -g root -m 0755 "$destination_root"
  temporary=$(mktemp "$destination_root/.20-http2.conf.XXXXXX")
  if ! install -o root -g root -m 0644 "$source_path" "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  mv -f -- "$temporary" "$destination"
}

apply_cloudflared_transport_if_active() {
  [[ $CLOUDFLARED_DROP_IN_CHANGED == true ]] || return 0
  [[ $CLOUDFLARED_WAS_PRESENT == true ]] || return 0
  [[ $CLOUDFLARED_WAS_ACTIVE == true ]] || return 0

  restart_cloudflared_bounded || {
    fail "could not restart $CLOUDFLARED_SERVICE" || true
    return 1
  }
  verify_cloudflared_enabled_state
}

verify_cloudflared_enabled_state() {
  local currently_enabled=false
  local status

  if run_systemctl_bounded is-enabled --quiet "$CLOUDFLARED_SERVICE"; then
    currently_enabled=true
  else
    status=$?
    if [[ $status -ne 1 ]]; then
      fail "could not verify whether $CLOUDFLARED_SERVICE is enabled" || true
      return 1
    fi
  fi
  if [[ $currently_enabled != "$CLOUDFLARED_WAS_ENABLED" ]]; then
    fail "$CLOUDFLARED_SERVICE enabled state changed during bootstrap" || true
    return 1
  fi
}

restore_cloudflared_transport_drop_in() {
  local systemd_root=$1
  local backup_dir=$2
  local destination_root=$systemd_root/cloudflared.service.d
  local destination=$systemd_root/$CLOUDFLARED_DROP_IN
  local backup=$backup_dir/cloudflared-transport-drop-in
  local absent=$backup_dir/cloudflared-transport-drop-in.absent
  local temporary

  if [[ -e $backup || -L $backup ]]; then
    if [[ -L $destination_root ]]; then
      fail "$destination_root must not be a symbolic link" || true
      return 1
    fi
    if [[ -d $destination ]]; then
      fail "$destination must not be a directory" || true
      return 1
    fi
    if ! install -d -o root -g root -m 0755 "$destination_root"; then
      fail "could not prepare cloudflared transport restore directory" || true
      return 1
    fi
    temporary=$destination_root/.20-http2.conf.restore.$$
    if [[ -e $temporary || -L $temporary ]]; then
      fail "cloudflared transport restore path already exists" || true
      return 1
    fi
    if ! cp -a -- "$backup" "$temporary"; then
      rm -f -- "$temporary"
      fail "could not copy the exact cloudflared transport backup" || true
      return 1
    fi
    if ! mv -f -- "$temporary" "$destination"; then
      rm -f -- "$temporary"
      fail "could not atomically restore the cloudflared transport drop-in" \
        || true
      return 1
    fi
  elif [[ -f $absent && ! -L $absent ]]; then
    if ! rm -f -- "$destination"; then
      fail "could not restore absence of the cloudflared transport drop-in" \
        || true
      return 1
    fi
  else
    fail "cloudflared transport backup is missing" || true
    return 1
  fi
}

restore_cloudflared_transport_after_rollback() {
  local systemd_root=$1
  local backup_dir=$2

  if ! restore_cloudflared_transport_drop_in "$systemd_root" "$backup_dir"; then
    fail "could not restore exact cloudflared transport drop-in during rollback" \
      || true
    return 1
  fi
  if [[ $CLOUDFLARED_DROP_IN_CHANGED == true \
        && $CLOUDFLARED_WAS_PRESENT == true \
        && $CLOUDFLARED_WAS_ACTIVE == true ]]; then
    if ! quiesce_cloudflared_before_rollback; then
      fail "$CLOUDFLARED_SERVICE could not be quiesced before rollback" \
        || true
      return 1
    fi
  fi
  if ! run_systemctl_bounded daemon-reload; then
    fail "could not reload systemd after cloudflared transport rollback" \
      || true
    return 1
  fi
  if [[ $CLOUDFLARED_DROP_IN_CHANGED == true \
        && $CLOUDFLARED_WAS_PRESENT == true \
        && $CLOUDFLARED_WAS_ACTIVE == true ]]; then
    restart_cloudflared_bounded || {
      fail "could not restart $CLOUDFLARED_SERVICE during rollback" \
        || true
      return 1
    }
    verify_cloudflared_enabled_state
  fi
}

restore_cloudflared_transport_with_diagnostics() {
  local systemd_root=$1
  local backup_dir=$2
  local diagnostics=$backup_dir/cloudflared-rollback-error.log
  local status

  if ! (umask 077 && : >"$diagnostics"); then
    fail "could not create cloudflared rollback diagnostics at $diagnostics" \
      || true
    return 1
  fi
  if restore_cloudflared_transport_after_rollback \
    "$systemd_root" "$backup_dir" 2>"$diagnostics"; then
    status=0
  else
    status=$?
  fi
  if [[ -s $diagnostics ]]; then
    cat -- "$diagnostics" >&2 || true
  fi
  if [[ $status -eq 0 ]]; then
    rm -f -- "$diagnostics"
  fi
  return "$status"
}

install_fixed_media_bootstrap() {
  local source=$1
  local auth_source=$source/gate_media_auth
  local gateway_source=$source/gate_media_gateway
  local transcoder_source=$source/gate_media_transcoder

  install -d -o root -g root -m 0755 \
    "$MEDIA_BOOTSTRAP_ROOT/gate_media_auth" \
    "$MEDIA_BOOTSTRAP_ROOT/gate_media_gateway" \
    "$MEDIA_BOOTSTRAP_ROOT/gate_media_transcoder"
  install -o root -g root -m 0755 \
    "$source/deployment/install-media.sh" "$MEDIA_BOOTSTRAP_ROOT/install-media.sh"
  install -o root -g root -m 0644 \
    "$source/deployment/media/mediamtx.yml" "$MEDIA_BOOTSTRAP_ROOT/mediamtx.yml"
  install -o root -g root -m 0644 \
    "$source/deployment/media/nginx-whep-locations.conf.template" \
    "$MEDIA_BOOTSTRAP_ROOT/nginx-whep-locations.conf.template"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-auth.service" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-auth.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-gateway.service" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-gateway.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-transcoder.service" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-transcoder.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-turn-refresh.service" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-turn-refresh.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-turn-refresh.timer" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-turn-refresh.timer"
  install -o root -g root -m 0700 \
    "$source/deployment/gate_media_turn_refresh.py" \
    "$MEDIA_BOOTSTRAP_ROOT/gate_media_turn_refresh.py"
  install -o root -g root -m 0644 \
    "$source/gate_media_config.py" "$MEDIA_BOOTSTRAP_ROOT/gate_media_config.py"
  for name in __init__.py __main__.py token.py capabilities.py; do
    install -o root -g root -m 0644 \
      "$auth_source/$name" "$MEDIA_BOOTSTRAP_ROOT/gate_media_auth/$name"
  done
  for name in __init__.py __main__.py; do
    install -o root -g root -m 0644 \
      "$gateway_source/$name" "$MEDIA_BOOTSTRAP_ROOT/gate_media_gateway/$name"
    install -o root -g root -m 0644 \
      "$transcoder_source/$name" "$MEDIA_BOOTSTRAP_ROOT/gate_media_transcoder/$name"
  done
}

pin_trusted_directory() {
  local directory=$1
  local descriptor=$2

  if [[ $directory != /* ]]; then
    fail "trusted directory path must be absolute: $directory" || true
    return 1
  fi
  case "$descriptor" in
    17)
      if ! exec 17<"$directory"; then
        fail "could not pin trusted directory: $directory" || true
        return 1
      fi
      ;;
    18)
      if ! exec 18<"$directory"; then
        fail "could not pin trusted directory: $directory" || true
        return 1
      fi
      ;;
    19)
      if ! exec 19<"$directory"; then
        fail "could not pin trusted directory: $directory" || true
        return 1
      fi
      ;;
    *)
      fail "unsupported trusted directory descriptor: $descriptor" || true
      return 1
      ;;
  esac

  if python3 - "$directory" "$descriptor" <<'PY'
import os
import stat
import sys

directory = os.path.normpath(sys.argv[1])
pinned_descriptor = int(sys.argv[2])
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
walk_descriptor = os.open(os.sep, flags)
try:
    for component in directory.split(os.sep)[1:]:
        if not component:
            continue
        next_descriptor = os.open(
            component,
            flags,
            dir_fd=walk_descriptor,
        )
        os.close(walk_descriptor)
        walk_descriptor = next_descriptor
    pinned = os.fstat(pinned_descriptor)
    walked = os.fstat(walk_descriptor)
    if (pinned.st_dev, pinned.st_ino) != (walked.st_dev, walked.st_ino):
        raise OSError("directory changed while its trusted chain was validated")
    if pinned.st_uid != os.geteuid():
        raise PermissionError("trusted directory is not owned by the executing uid")
    if pinned.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError("trusted directory is group or world writable")
finally:
    os.close(walk_descriptor)
PY
  then
    return 0
  fi

  case "$descriptor" in
    17) exec 17<&- ;;
    18) exec 18<&- ;;
    19) exec 19<&- ;;
  esac
  fail "trusted directory chain is unsafe: $directory" || true
  return 1
}

create_trusted_fixed_media_directory() {
  local parent_descriptor=$1
  local name=$2

  python3 - "$parent_descriptor" "$name" <<'PY'
import os
import stat
import sys

parent_descriptor = int(sys.argv[1])
name = sys.argv[2]
if not name or name in (".", "..") or os.sep in name:
    raise SystemExit("fixed media directory name is unsafe")
os.mkdir(name, 0o755, dir_fd=parent_descriptor)
descriptor = os.open(
    name,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    dir_fd=parent_descriptor,
)
try:
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.geteuid():
        raise PermissionError("created directory has an unexpected owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError("created directory is group or world writable")
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.fsync(parent_descriptor)
PY
}

pin_or_create_trusted_directory() {
  local directory=$1
  local descriptor=$2
  local parent name kind

  if [[ $directory == / ]]; then
    pin_trusted_directory "$directory" "$descriptor"
    return
  fi
  if [[ $directory != /* ]]; then
    fail "trusted directory path must be absolute: $directory" || true
    return 1
  fi
  parent=${directory%/*}
  name=${directory##*/}
  [[ -n $parent ]] || parent=/
  if [[ -z $name ]]; then
    fail "trusted directory path must name a directory: $directory" || true
    return 1
  fi

  pin_trusted_directory "$parent" 17 || return 1
  kind=$(fixed_media_path_kind 17 "$name") || return 1
  case "$kind" in
    absent)
      if ! create_trusted_fixed_media_directory 17 "$name"; then
        fail "could not securely create trusted directory: $directory" || true
        return 1
      fi
      ;;
    directory)
      ;;
    symlink)
      fail "$directory must not be a symbolic link" || true
      return 1
      ;;
    *)
      fail "$directory must be a directory" || true
      return 1
      ;;
  esac
  pin_trusted_directory "$directory" "$descriptor"
}

publish_fixed_media_restore() {
  local parent_descriptor=$1
  local live_name=$2
  local staged_name=$3

  python3 - "$parent_descriptor" "$live_name" "$staged_name" <<'PY'
import ctypes
import errno
import os
import stat
import sys

parent_descriptor = int(sys.argv[1])
live_name = sys.argv[2]
staged_name = sys.argv[3]
for name in (live_name, staged_name):
    if not name or name in (".", "..") or os.sep in name:
        raise SystemExit("fixed media restore name is unsafe")

staged = os.stat(staged_name, dir_fd=parent_descriptor, follow_symlinks=False)
if not stat.S_ISDIR(staged.st_mode):
    raise SystemExit("staged fixed media restore must be a directory")

try:
    live = os.stat(live_name, dir_fd=parent_descriptor, follow_symlinks=False)
except FileNotFoundError:
    live = None
if live is not None and not stat.S_ISDIR(live.st_mode):
    raise SystemExit("live fixed media bootstrap must be a directory")

if live is None:
    os.rename(
        staged_name,
        live_name,
        src_dir_fd=parent_descriptor,
        dst_dir_fd=parent_descriptor,
    )
else:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename_exchange = libc.renameat2
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable") from error
        rename_exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exchange.restype = ctypes.c_int
        result = rename_exchange(
            parent_descriptor,
            os.fsencode(live_name),
            parent_descriptor,
            os.fsencode(staged_name),
            0x00000002,
        )
    elif sys.platform == "darwin":
        rename_exchange = libc.renameatx_np
        rename_exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exchange.restype = ctypes.c_int
        result = rename_exchange(
            parent_descriptor,
            os.fsencode(live_name),
            parent_descriptor,
            os.fsencode(staged_name),
            0x00000002,
        )
    else:
        raise OSError(errno.ENOTSUP, "atomic directory exchange is unsupported")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))

os.fsync(parent_descriptor)
PY
}

publish_fixed_media_absence() {
  local parent_descriptor=$1
  local live_name=$2
  local quarantine_name=$3

  python3 - "$parent_descriptor" "$live_name" "$quarantine_name" <<'PY'
import os
import stat
import sys

parent_descriptor = int(sys.argv[1])
live_name = sys.argv[2]
quarantine_name = sys.argv[3]
for name in (live_name, quarantine_name):
    if not name or name in (".", "..") or os.sep in name:
        raise SystemExit("fixed media absence name is unsafe")

live = os.stat(live_name, dir_fd=parent_descriptor, follow_symlinks=False)
if not stat.S_ISDIR(live.st_mode):
    raise SystemExit("live fixed media bootstrap must be a directory")
try:
    os.stat(quarantine_name, dir_fd=parent_descriptor, follow_symlinks=False)
except FileNotFoundError:
    pass
else:
    raise SystemExit("fixed media quarantine already exists")

os.rename(
    live_name,
    quarantine_name,
    src_dir_fd=parent_descriptor,
    dst_dir_fd=parent_descriptor,
)
os.fsync(parent_descriptor)
PY
}

fixed_media_path_kind() {
  local parent_descriptor=$1
  local name=$2

  python3 - "$parent_descriptor" "$name" <<'PY'
import os
import stat
import sys

parent_descriptor = int(sys.argv[1])
name = sys.argv[2]
if not name or name in (".", "..") or os.sep in name:
    raise SystemExit("fixed media path name is unsafe")
try:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
except FileNotFoundError:
    print("absent")
else:
    if stat.S_ISLNK(metadata.st_mode):
        print("symlink")
    elif stat.S_ISDIR(metadata.st_mode):
        print("directory")
    elif stat.S_ISREG(metadata.st_mode):
        print("file")
    else:
        print("other")
PY
}

create_fixed_media_absent_marker() {
  local parent_descriptor=$1
  local name=$2

  python3 - "$parent_descriptor" "$name" <<'PY'
import os
import sys

parent_descriptor = int(sys.argv[1])
name = sys.argv[2]
if not name or name in (".", "..") or os.sep in name:
    raise SystemExit("fixed media marker name is unsafe")
descriptor = os.open(
    name,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
    0o600,
    dir_fd=parent_descriptor,
)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.fsync(parent_descriptor)
PY
}

fsync_fixed_media_tree() {
  local parent_descriptor=$1
  local name=$2

  python3 - "$parent_descriptor" "$name" <<'PY'
import os
import stat
import sys

parent_descriptor = int(sys.argv[1])
name = sys.argv[2]
if not name or name in (".", "..") or os.sep in name:
    raise SystemExit("fixed media fsync name is unsafe")
tree_descriptor = os.open(
    name,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    dir_fd=parent_descriptor,
)
try:
    for _, _, files, directory_descriptor in os.fwalk(
        ".",
        topdown=False,
        follow_symlinks=False,
        dir_fd=tree_descriptor,
    ):
        for filename in files:
            metadata = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(metadata.st_mode):
                continue
            file_descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
            try:
                opened = os.fstat(file_descriptor)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise OSError("fixed media file changed while being synced")
                os.fsync(file_descriptor)
            finally:
                os.close(file_descriptor)
        os.fsync(directory_descriptor)
finally:
    os.close(tree_descriptor)
os.fsync(parent_descriptor)
PY
}

list_fixed_media_stale_generations() {
  local parent_descriptor=$1
  local bootstrap_name=$2

  python3 - "$parent_descriptor" "$bootstrap_name" <<'PY'
import os
import stat
import sys

parent_descriptor = int(sys.argv[1])
bootstrap_name = sys.argv[2]
if not bootstrap_name or bootstrap_name in (".", "..") or os.sep in bootstrap_name:
    raise SystemExit("fixed media bootstrap name is unsafe")
prefixes = (
    f"{bootstrap_name}.rollback.",
    f"{bootstrap_name}.quarantine.",
)
generations = []
with os.scandir(parent_descriptor) as entries:
    for entry in entries:
        for prefix in prefixes:
            if entry.name.startswith(prefix):
                suffix = entry.name[len(prefix):]
                if suffix and suffix.isascii() and suffix.isdigit():
                    generations.append(entry.name)
                break
for generation in generations:
    metadata = os.stat(
        generation,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"unsafe fixed media restore generation: {generation}")
for generation in sorted(generations):
    print(generation)
PY
}

cleanup_stale_fixed_media_generations() {
  local parent_descriptor=$1
  local bootstrap_name=$2
  local generations generation

  generations=$(
    list_fixed_media_stale_generations "$parent_descriptor" "$bootstrap_name"
  ) || return 1
  [[ -n $generations ]] || return 0
  while IFS= read -r generation; do
    if ! remove_fixed_media_tree "$parent_descriptor" "$generation"; then
      fail "could not remove stale fixed media generation: $generation" || true
      return 1
    fi
  done <<EOF
$generations
EOF
}

copy_fixed_media_tree_real() {
  local source_descriptor=$1
  local source_name=$2
  local destination_descriptor=$3
  local destination_name=$4

  python3 - \
    "$source_descriptor" "$source_name" \
    "$destination_descriptor" "$destination_name" <<'PY'
import os
import stat
import subprocess
import sys

source_descriptor = int(sys.argv[1])
source_name = sys.argv[2]
destination_descriptor = int(sys.argv[3])
destination_name = sys.argv[4]
for name in (source_name, destination_name):
    if not name or name in (".", "..") or os.sep in name:
        raise SystemExit("fixed media copy name is unsafe")

source = os.stat(
    source_name,
    dir_fd=source_descriptor,
    follow_symlinks=False,
)
if not stat.S_ISDIR(source.st_mode):
    raise SystemExit("fixed media copy source must be a directory")
try:
    os.stat(
        destination_name,
        dir_fd=destination_descriptor,
        follow_symlinks=False,
    )
except FileNotFoundError:
    pass
else:
    raise SystemExit("fixed media copy destination already exists")

if sys.platform.startswith("linux"):
    subprocess.run(
        [
            "cp",
            "-a",
            "--",
            f"/proc/self/fd/{source_descriptor}/{source_name}",
            f"/proc/self/fd/{destination_descriptor}/{destination_name}",
        ],
        check=True,
        pass_fds=(source_descriptor, destination_descriptor),
    )
else:
    os.mkdir(destination_name, 0o700, dir_fd=destination_descriptor)
    source_tree_descriptor = os.open(
        source_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=source_descriptor,
    )
    destination_tree_descriptor = os.open(
        destination_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=destination_descriptor,
    )
    original_directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    producer = None
    try:
        os.fchdir(source_tree_descriptor)
        producer = subprocess.Popen(
            ["tar", "-cpf", "-", "."],
            stdout=subprocess.PIPE,
        )
        os.fchdir(destination_tree_descriptor)
        consumer = subprocess.run(
            ["tar", "-xpf", "-"],
            stdin=producer.stdout,
            check=False,
        )
        if producer.stdout is not None:
            producer.stdout.close()
        producer_status = producer.wait()
        if producer_status != 0 or consumer.returncode != 0:
            raise OSError("could not copy fixed media tree with tar")
    finally:
        if producer is not None and producer.poll() is None:
            producer.kill()
            producer.wait()
        os.fchdir(original_directory)
        os.close(original_directory)
        os.close(destination_tree_descriptor)
        os.close(source_tree_descriptor)
PY
}

copy_fixed_media_tree() {
  copy_fixed_media_tree_real "$@"
}

remove_fixed_media_tree_real() {
  local parent_descriptor=$1
  local name=$2

  python3 - "$parent_descriptor" "$name" <<'PY'
import os
import stat
import subprocess
import sys

parent_descriptor = int(sys.argv[1])
name = sys.argv[2]
if not name or name in (".", "..") or os.sep in name:
    raise SystemExit("fixed media removal name is unsafe")
try:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit("fixed media removal target must be a directory")
original_directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fchdir(parent_descriptor)
    subprocess.run(["rm", "-rf", "--", name], check=True)
finally:
    os.fchdir(original_directory)
    os.close(original_directory)
os.fsync(parent_descriptor)
PY
}

remove_fixed_media_tree() {
  remove_fixed_media_tree_real "$@"
}

remove_fixed_media_quarantine() {
  remove_fixed_media_tree "$@"
}

backup_fixed_media_bootstrap() (
  local bootstrap_root=$1
  local backup_dir=$2
  local bootstrap_parent=${bootstrap_root%/*}
  local bootstrap_name=${bootstrap_root##*/}
  local bootstrap_kind backup_kind absent_kind

  if [[ $bootstrap_root != /* || $bootstrap_root == / || -z $bootstrap_name ]]; then
    fail "fixed media bootstrap path must be an absolute child path" || true
    return 1
  fi
  [[ -n $bootstrap_parent ]] || bootstrap_parent=/
  pin_trusted_directory "$backup_dir" 19 || return 1
  pin_or_create_trusted_directory "$bootstrap_parent" 18 || return 1
  backup_kind=$(fixed_media_path_kind 19 fixed-media-bootstrap) || return 1
  absent_kind=$(fixed_media_path_kind 19 fixed-media-bootstrap.absent) \
    || return 1
  if [[ $backup_kind != absent || $absent_kind != absent ]]; then
    fail "fixed media bootstrap backup already exists" || true
    return 1
  fi
  bootstrap_kind=$(fixed_media_path_kind 18 "$bootstrap_name") || return 1
  case "$bootstrap_kind" in
    directory)
      if ! copy_fixed_media_tree \
        18 "$bootstrap_name" 19 fixed-media-bootstrap; then
        remove_fixed_media_tree 19 fixed-media-bootstrap || true
        fail "could not back up the fixed media bootstrap" || true
        return 1
      fi
      ;;
    absent)
      if ! create_fixed_media_absent_marker \
        19 fixed-media-bootstrap.absent; then
        fail "could not record absence of the fixed media bootstrap" || true
        return 1
      fi
      ;;
    symlink)
      fail "$bootstrap_root must not be a symbolic link" || true
      return 1
      ;;
    *)
      fail "$bootstrap_root must be a directory" || true
      return 1
      ;;
  esac
)

restore_fixed_media_bootstrap() (
  local bootstrap_root=$1
  local backup_dir=$2
  local bootstrap_parent=${bootstrap_root%/*}
  local bootstrap_name=${bootstrap_root##*/}
  local temporary_name=$bootstrap_name.rollback.$$
  local quarantine_name=$bootstrap_name.quarantine.$$
  local bootstrap_kind backup_kind absent_kind temporary_kind

  if [[ $bootstrap_root != /* || $bootstrap_root == / || -z $bootstrap_name ]]; then
    fail "fixed media bootstrap path must be an absolute child path" || true
    return 1
  fi
  [[ -n $bootstrap_parent ]] || bootstrap_parent=/
  pin_trusted_directory "$backup_dir" 19 || return 1
  pin_trusted_directory "$bootstrap_parent" 18 || return 1
  bootstrap_kind=$(fixed_media_path_kind 18 "$bootstrap_name") || return 1
  if [[ $bootstrap_kind == symlink ]]; then
    fail "$bootstrap_root must not be a symbolic link" || true
    return 1
  fi
  if [[ $bootstrap_kind != absent && $bootstrap_kind != directory ]]; then
    fail "$bootstrap_root must be a directory" || true
    return 1
  fi
  backup_kind=$(fixed_media_path_kind 19 fixed-media-bootstrap) || return 1
  absent_kind=$(fixed_media_path_kind 19 fixed-media-bootstrap.absent) \
    || return 1
  if [[ $backup_kind != absent ]]; then
    if [[ $backup_kind != directory ]]; then
      fail "fixed media bootstrap backup must be a directory" || true
      return 1
    fi
    if [[ $absent_kind != absent ]]; then
      fail "fixed media bootstrap backup state is ambiguous" || true
      return 1
    fi
  elif [[ $absent_kind != file ]]; then
    fail "fixed media bootstrap backup is missing" || true
    return 1
  fi

  cleanup_stale_fixed_media_generations 18 "$bootstrap_name" || return 1
  bootstrap_kind=$(fixed_media_path_kind 18 "$bootstrap_name") || return 1
  if [[ $bootstrap_kind != absent && $bootstrap_kind != directory ]]; then
    fail "$bootstrap_root must be a directory" || true
    return 1
  fi

  if [[ $backup_kind != absent ]]; then
    temporary_kind=$(fixed_media_path_kind 18 "$temporary_name") || return 1
    if [[ $temporary_kind != absent ]]; then
      fail "fixed media bootstrap restore path already exists" || true
      return 1
    fi
    if ! copy_fixed_media_tree \
      19 fixed-media-bootstrap 18 "$temporary_name"; then
      remove_fixed_media_tree 18 "$temporary_name" || true
      fail "could not copy the fixed media bootstrap backup" || true
      return 1
    fi
    if ! fsync_fixed_media_tree 18 "$temporary_name"; then
      remove_fixed_media_tree 18 "$temporary_name" || true
      fail "could not durably stage the fixed media bootstrap restore" || true
      return 1
    fi
    if ! publish_fixed_media_restore 18 "$bootstrap_name" "$temporary_name"; then
      remove_fixed_media_tree 18 "$temporary_name" || true
      fail "could not atomically publish the fixed media bootstrap restore" \
        || true
      return 1
    fi
    temporary_kind=$(fixed_media_path_kind 18 "$temporary_name") || return 1
    if [[ $temporary_kind != absent ]] \
      && ! remove_fixed_media_tree 18 "$temporary_name"; then
      fail "could not remove the replaced fixed media bootstrap" || true
      return 1
    fi
  elif [[ $bootstrap_kind == directory ]]; then
    if ! publish_fixed_media_absence \
      18 "$bootstrap_name" "$quarantine_name"; then
      fail "could not atomically publish fixed media bootstrap absence" \
        || true
      return 1
    fi
    if ! remove_fixed_media_quarantine 18 "$quarantine_name"; then
      fail "could not remove the quarantined fixed media bootstrap" || true
      return 1
    fi
  fi
)

backup_fixed_updater_helper() {
  local updater_helper=$1
  local backup_dir=$2

  [[ ! -L $updater_helper ]] \
    || fail "$updater_helper must not be a symbolic link"
  if [[ -f $updater_helper ]]; then
    install -o root -g root -m 0600 \
      "$updater_helper" "$backup_dir/updater-helper"
  elif [[ -e $updater_helper ]]; then
    fail "$updater_helper must be a regular file"
  else
    : >"$backup_dir/updater-helper.absent"
  fi
}

restore_fixed_updater_helper() {
  local updater_helper=$1
  local backup_dir=$2

  if [[ -f $backup_dir/updater-helper ]]; then
    install -o root -g root -m 0755 \
      "$backup_dir/updater-helper" "$updater_helper"
  elif [[ -f $backup_dir/updater-helper.absent ]]; then
    rm -f -- "$updater_helper"
  else
    fail "updater helper backup is missing" || true
    return 1
  fi
}

validate_upload_paths() {
  local state_root=$1
  local upload_root=$2
  local path

  [[ $upload_root == "$state_root/uploads" ]] \
    || fail "upload directory must be $state_root/uploads"
  for path in "$state_root" "$upload_root"; do
    [[ ! -L $path ]] || fail "$path must not be a symbolic link"
    [[ ! -e $path || -d $path ]] || fail "$path must be a directory"
  done
}

configure_upload_directory() {
  local state_root=$1
  local upload_root=$2
  local ftp_user=$3
  local app_user=$4
  local app_group=$5

  usermod -aG "$app_group" "$ftp_user"
  install -d -o "$app_user" -g "$app_group" -m 0710 "$state_root"
  install -d -o "$ftp_user" -g "$app_group" -m 2770 "$upload_root"
  chown -R "$ftp_user:$app_group" "$upload_root"
  find "$upload_root" -type d -exec chmod g+rwx,g+s,o-rwx {} +
  find "$upload_root" -type f -exec chmod g+rw,o-rwx {} +
  runuser --user "$ftp_user" -- /usr/bin/test -w "$upload_root" \
    || fail "$ftp_user cannot write $upload_root"
  runuser --user "$ftp_user" -- /usr/bin/test -x "$state_root" \
    || fail "$ftp_user cannot traverse $state_root"
  runuser --user "$app_user" -- /usr/bin/test -r "$upload_root" \
    || fail "$app_user cannot read $upload_root"
  runuser --user "$app_user" -- /usr/bin/test -x "$upload_root" \
    || fail "$app_user cannot watch $upload_root"
}

configure_ftp_home() {
  local ftp_user=$1
  local upload_root=$2

  usermod --home "$upload_root" "$ftp_user"
}

restore_application_activity() {
  local previous_unit=$1
  local was_active=$2

  if [[ (-e $previous_unit || -L $previous_unit) && $was_active == true ]]; then
    systemctl restart "$APP_SERVICE"
  else
    systemctl stop "$APP_SERVICE"
  fi
}

restore_legacy_command_activity() {
  local previous_unit=$1
  local was_enabled=$2
  local was_active=$3
  local restoration_failed=false

  if [[ (-e $previous_unit || -L $previous_unit) && $was_enabled == true ]]; then
    if ! systemctl enable "$LEGACY_COMMAND_UNIT"; then
      restoration_failed=true
    fi
  else
    if ! systemctl disable "$LEGACY_COMMAND_UNIT"; then
      restoration_failed=true
    fi
  fi
  if [[ (-e $previous_unit || -L $previous_unit) && $was_active == true ]]; then
    if ! systemctl restart "$LEGACY_COMMAND_UNIT"; then
      restoration_failed=true
    fi
  else
    if ! systemctl stop "$LEGACY_COMMAND_UNIT"; then
      restoration_failed=true
    fi
  fi
  [[ $restoration_failed == false ]]
}

cleanup() {
  if [[ -n $STAGING && -d $STAGING ]]; then
    rm -rf -- "$STAGING"
  fi
  if [[ $ROLLBACK_FAILED == false \
        && -n $BACKUP_DIR && -d $BACKUP_DIR ]]; then
    rm -rf -- "$BACKUP_DIR"
  fi
}

backup_path() {
  local name=$1
  local destination=$SYSTEMD_ROOT/$name
  local backup=$BACKUP_DIR/$name
  local absent=$BACKUP_DIR/$name.absent

  [[ ! -e $backup && ! -L $backup && ! -e $absent ]] \
    || fail "backup already exists for $name"
  if [[ -e $destination || -L $destination ]]; then
    cp -a -- "$destination" "$backup"
  else
    : >"$absent"
  fi
}

restore_path() {
  local name=$1
  local destination=$SYSTEMD_ROOT/$name
  local backup=$BACKUP_DIR/$name
  local absent=$BACKUP_DIR/$name.absent
  local temporary=$SYSTEMD_ROOT/.$name.rollback.$$

  if [[ -e $backup || -L $backup ]]; then
    if [[ -d $destination ]]; then
      fail "$destination must not be a directory" || true
      return 1
    fi
    if [[ -e $temporary || -L $temporary ]]; then
      fail "unit restore path already exists: $temporary" || true
      return 1
    fi
    if ! cp -a -- "$backup" "$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
    if ! rm -f -- "$destination"; then
      rm -f -- "$temporary"
      return 1
    fi
    mv -f -- "$temporary" "$destination"
  elif [[ -f $absent && ! -L $absent ]]; then
    rm -f -- "$destination"
  else
    fail "unit backup is missing for $name" || true
    return 1
  fi
}

capture_updater_timer_state() {
  local status

  UPDATER_WAS_ENABLED=false
  UPDATER_WAS_ACTIVE=false
  if systemctl is-enabled --quiet "$UPDATER_TIMER"; then
    UPDATER_WAS_ENABLED=true
  else
    status=$?
    [[ $status -eq 1 ]] \
      || fail "could not inspect whether $UPDATER_TIMER is enabled"
  fi
  if systemctl is-active --quiet "$UPDATER_TIMER"; then
    UPDATER_WAS_ACTIVE=true
  else
    status=$?
    [[ $status -eq 3 || $status -eq 4 ]] \
      || fail "could not inspect whether $UPDATER_TIMER is active"
  fi
}

restore_unit_enablement() {
  local unit=$1
  local was_enabled=$2

  if [[ $was_enabled == true ]]; then
    systemctl enable "$unit"
  else
    systemctl disable "$unit"
  fi
}

restore_unit_activity() {
  local unit=$1
  local was_active=$2

  if [[ $was_active == true ]]; then
    systemctl restart "$unit"
  else
    systemctl stop "$unit"
  fi
}

restore_current_release() {
  if [[ -n $PREVIOUS_CURRENT ]]; then
    ln -sfn "$PREVIOUS_CURRENT" "$CURRENT_LINK.rollback" || return 1
    mv -Tf "$CURRENT_LINK.rollback" "$CURRENT_LINK" || return 1
  else
    rm -f -- "$CURRENT_LINK"
  fi
}

prepare_rollback_diagnostics() {
  ROLLBACK_DIAGNOSTICS=$BACKUP_DIR/rollback-error.log
  if ! (umask 077 && : >"$ROLLBACK_DIAGNOSTICS"); then
    ROLLBACK_FAILED=true
    ROLLBACK_DIAGNOSTICS=
    printf '%s\n' \
      "Rollback diagnostics could not be created; backup retained at $BACKUP_DIR." \
      >&2
  fi
  return 0
}

run_rollback_step() {
  local description=$1
  local status
  local message
  local step_diagnostics=
  local diagnostics_appended=true
  shift
  ROLLBACK_STEP_SUCCEEDED=true

  if [[ -n $ROLLBACK_DIAGNOSTICS ]]; then
    step_diagnostics=$(mktemp "$BACKUP_DIR/.rollback-step.XXXXXX") || true
  fi
  if [[ -n $step_diagnostics ]]; then
    if "$@" 2>"$step_diagnostics"; then
      status=0
    else
      status=$?
    fi
    if [[ -s $step_diagnostics ]]; then
      cat -- "$step_diagnostics" >&2 || true
    fi
  elif "$@"; then
    status=0
  else
    status=$?
  fi
  if [[ $status -eq 0 ]]; then
    [[ -z $step_diagnostics ]] || rm -f -- "$step_diagnostics"
    return 0
  fi

  ROLLBACK_FAILED=true
  ROLLBACK_STEP_SUCCEEDED=false
  message="Rollback step failed: $description (status $status)."
  printf '%s\n' "$message" >&2
  if [[ -n $step_diagnostics ]]; then
    if ! cat -- "$step_diagnostics" >>"$ROLLBACK_DIAGNOSTICS"; then
      diagnostics_appended=false
    fi
  else
    diagnostics_appended=false
  fi
  if [[ -n $ROLLBACK_DIAGNOSTICS ]] \
      && ! printf '%s\n' "$message" >>"$ROLLBACK_DIAGNOSTICS"; then
    diagnostics_appended=false
  fi
  if [[ $diagnostics_appended == false && -n $step_diagnostics ]]; then
    printf '%s\n' "$message" >>"$step_diagnostics" || true
    printf '%s\n' \
      "Rollback diagnostics could not be updated; detailed fallback retained at $step_diagnostics." \
      >&2
    step_diagnostics=
  fi
  [[ -z $step_diagnostics ]] || rm -f -- "$step_diagnostics"
  return 0
}

run_rollback_prerequisite() {
  run_rollback_step "$@"
  if [[ $ROLLBACK_STEP_SUCCEEDED == false ]]; then
    ROLLBACK_PREREQUISITES_SUCCEEDED=false
  fi
}

rollback() {
  local original_status=$?
  if [[ $# -gt 0 ]]; then
    original_status=$1
  fi
  if [[ $BASH_SUBSHELL -ne $ROLLBACK_OWNER_SUBSHELL ]]; then
    return "$original_status"
  fi
  if [[ $ROLLBACK_STARTED == true ]]; then
    [[ $original_status -ne 0 ]] || original_status=1
    exit "$original_status"
  fi
  ROLLBACK_STARTED=true
  trap - ERR INT TERM
  set +e
  if [[ $ACTIVATION_STARTED == true && $INSTALL_SUCCEEDED == false ]]; then
    printf 'Managed startup failed; restoring the previous installation.\n' >&2
    ROLLBACK_PREREQUISITES_SUCCEEDED=true
    prepare_rollback_diagnostics
    run_rollback_prerequisite "restore current release" restore_current_release
    run_rollback_prerequisite "restore $APP_SERVICE" restore_path "$APP_SERVICE"
    run_rollback_prerequisite \
      "restore $LEGACY_COMMAND_UNIT" restore_path "$LEGACY_COMMAND_UNIT"
    run_rollback_prerequisite \
      "restore $UPDATER_SERVICE" restore_path "$UPDATER_SERVICE"
    run_rollback_prerequisite \
      "restore $UPDATER_TIMER" restore_path "$UPDATER_TIMER"
    run_rollback_prerequisite \
      "reload restored systemd units" \
      run_systemctl_bounded daemon-reload
    run_rollback_prerequisite \
      "restore fixed updater helper" \
      restore_fixed_updater_helper "$UPDATER_HELPER" "$BACKUP_DIR"
    run_rollback_prerequisite \
      "restore fixed media bootstrap" \
      restore_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" "$BACKUP_DIR"
    if [[ $ROLLBACK_PREREQUISITES_SUCCEEDED == true ]]; then
      run_rollback_prerequisite \
        "restore cloudflared transport" \
        restore_cloudflared_transport_with_diagnostics "$SYSTEMD_ROOT" "$BACKUP_DIR"
    else
      run_rollback_step \
        "restore cloudflared transport files" \
        restore_cloudflared_transport_drop_in "$SYSTEMD_ROOT" "$BACKUP_DIR"
    fi
    run_rollback_step \
      "restore FTP home" \
      configure_ftp_home "$FTP_USER" "$FTP_PREVIOUS_HOME"
    if [[ $ROLLBACK_PREREQUISITES_SUCCEEDED == true ]]; then
      run_rollback_step \
        "restore updater timer enablement" \
        restore_unit_enablement "$UPDATER_TIMER" "$UPDATER_WAS_ENABLED"
      if [[ -e $BACKUP_DIR/$UPDATER_TIMER \
            || -L $BACKUP_DIR/$UPDATER_TIMER ]]; then
        run_rollback_step \
          "restore updater timer activity" \
          restore_unit_activity "$UPDATER_TIMER" "$UPDATER_WAS_ACTIVE"
      fi
      if [[ $APP_WAS_ENABLED == false ]]; then
        run_rollback_step \
          "restore application enablement" \
          systemctl disable "$APP_SERVICE"
      fi
      run_rollback_step \
        "restore application activity" \
        restore_application_activity "$BACKUP_DIR/$APP_SERVICE" "$APP_WAS_ACTIVE"
      run_rollback_step \
        "restore legacy command activity" \
        restore_legacy_command_activity \
          "$BACKUP_DIR/$LEGACY_COMMAND_UNIT" \
          "$LEGACY_COMMAND_WAS_ENABLED" "$LEGACY_COMMAND_WAS_ACTIVE"
    else
      printf '%s\n' \
        "Service-state restoration skipped because critical rollback prerequisites failed." \
        >&2
    fi
  fi
  if [[ $ROLLBACK_FAILED == true ]]; then
    printf '%s\n' \
      "Rollback was incomplete; backup and diagnostics retained at $BACKUP_DIR." \
      >&2
    [[ $original_status -ne 0 ]] || original_status=1
  fi
  cleanup
  exit "$original_status"
}

migrate_legacy_authorised_plates() {
  local legacy_file=$1
  local persistent_file=$2
  local owner=$3
  local group=$4

  if [[ -e $persistent_file || -L $persistent_file ]]; then
    [[ -f $persistent_file && ! -L $persistent_file ]] \
      || fail "$persistent_file must be a regular file"
    return
  fi
  if [[ ! -e $legacy_file && ! -L $legacy_file ]]; then
    return
  fi
  [[ -f $legacy_file && ! -L $legacy_file ]] \
    || fail "$legacy_file must be a regular file"
  install -m 0600 -o "$owner" -g "$group" \
    "$legacy_file" "$persistent_file"
  printf 'Migrated legacy authorised plates to %s\n' "$persistent_file"
}

verify_candidate_release() {
  local release=$1

  (
    cd "$release"
    run_candidate_command .venv/bin/python -m unittest discover -s tests -v
    run_candidate_command .venv/bin/python -m compileall -q gate_controller deployment tests scripts
    run_candidate_command sh -n file_monitor.sh
    run_candidate_command bash -n deployment/install.sh
  )
}

main() {
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 ]] || fail "--source requires a path"
      SOURCE=$2
      shift 2
      ;;
    --enable-updates)
      ENABLE_UPDATES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ $EUID -eq 0 ]] || fail "run this installer with sudo"
[[ $ENABLE_UPDATES == true ]] || fail "pass --enable-updates after configuring required branch protection"
[[ -d $SOURCE ]] || fail "source checkout does not exist: $SOURCE"
[[ $UPDATE_BRANCH =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$ \
   && $UPDATE_BRANCH != *..* && $UPDATE_BRANCH != */ && $UPDATE_BRANCH != *//* ]] \
  || fail "GATE_UPDATE_BRANCH is invalid"
validate_env_file "$ENV_FILE" 0 0
validate_upload_paths "$STATE_ROOT" "$UPLOAD_ROOT"

for command in \
  bash cat chmod chown cmp cp env find flock getent git id install ln mktemp mv python3 readlink \
  rm runuser sed sh sleep systemctl systemd-analyze tar timeout /usr/bin/test useradd usermod; do
  command -v "$command" >/dev/null 2>&1 || fail "required command is missing: $command"
done
install -d -o root -g root -m 0755 "${UPDATE_LOCK%/*}"
acquire_install_lock "$UPDATE_LOCK"
id "$FTP_USER" >/dev/null 2>&1 \
  || fail "the FTP upload account is required: $FTP_USER"
while IFS=: read -r account _ _ _ _ home _; do
  if [[ $account == "$FTP_USER" ]]; then
    FTP_PREVIOUS_HOME=$home
    break
  fi
done < <(getent passwd "$FTP_USER")
[[ -n $FTP_PREVIOUS_HOME ]] || fail "cannot determine the home for $FTP_USER"

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 or newer is required"

run_candidate_command() {
  local -a unset_arguments=(-u GITHUB_TOKEN -u GH_TOKEN)
  local name
  while IFS='=' read -r name _; do
    if [[ $name == GIT_CONFIG_* ]]; then
      unset_arguments+=(-u "$name")
    fi
  done < <(env)
  env "${unset_arguments[@]}" runuser --user gate-controller-build -- "$@"
}

SOURCE=$(readlink -f "$SOURCE")
SHA=$(git -C "$SOURCE" rev-parse --verify 'HEAD^{commit}') \
  || fail "source must be a Git checkout"
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || fail "source HEAD is not a full commit SHA"
[[ -z $(git -C "$SOURCE" status --porcelain --untracked-files=normal) ]] \
  || fail "source checkout must be clean"
REMOTE_BRANCH=
read -r REMOTE_BRANCH _ < <(
  git -C "$SOURCE" ls-remote --exit-code origin "refs/heads/$UPDATE_BRANCH"
) || fail "origin/$UPDATE_BRANCH is unavailable"
[[ $REMOTE_BRANCH == "$SHA" ]] \
  || fail "source HEAD must equal the current origin/$UPDATE_BRANCH commit"
[[ -f $SOURCE/deployment/gate_controller_updater.py ]] \
  || fail "source does not contain the deployment updater"
[[ -f $SOURCE/file-monitor.service ]] \
  || fail "source does not contain the application service"
[[ -f $SOURCE/deployment/systemd/$UPDATER_SERVICE ]] \
  || fail "source does not contain the updater service"
[[ -f $SOURCE/deployment/systemd/$UPDATER_TIMER ]] \
  || fail "source does not contain the updater timer"
[[ -f $SOURCE/deployment/systemd/$CLOUDFLARED_DROP_IN ]] \
  || fail "source does not contain the cloudflared transport drop-in"
[[ -f $SOURCE/deployment/install-media.sh ]] \
  || fail "source does not contain the media installer"
[[ -f $SOURCE/deployment/media/mediamtx.yml ]] \
  || fail "source does not contain the MediaMTX config"
[[ -f $SOURCE/deployment/media/nginx-whep-locations.conf.template ]] \
  || fail "source does not contain the WHEP proxy template"
[[ -f $SOURCE/deployment/systemd/gate-media-auth.service ]] \
  || fail "source does not contain the media auth service"
[[ -f $SOURCE/deployment/systemd/gate-media-gateway.service ]] \
  || fail "source does not contain the media gateway service"
[[ -f $SOURCE/deployment/systemd/gate-media-transcoder.service ]] \
  || fail "source does not contain the media transcoder service"
[[ -f $SOURCE/deployment/systemd/gate-media-turn-refresh.service ]] \
  || fail "source does not contain the media TURN refresh service"
[[ -f $SOURCE/deployment/systemd/gate-media-turn-refresh.timer ]] \
  || fail "source does not contain the media TURN refresh timer"
[[ -f $SOURCE/deployment/gate_media_turn_refresh.py ]] \
  || fail "source does not contain the media TURN refresh helper"
[[ -f $SOURCE/gate_media_config.py ]] \
  || fail "source does not contain the media config validator"
[[ -f $SOURCE/gate_media_gateway/__init__.py \
    && -f $SOURCE/gate_media_gateway/__main__.py ]] \
  || fail "source does not contain the media gateway launcher"
[[ -f $SOURCE/gate_media_transcoder/__init__.py \
    && -f $SOURCE/gate_media_transcoder/__main__.py ]] \
  || fail "source does not contain the media transcoder launcher"

if ! id gate-controller >/dev/null 2>&1; then
  useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin gate-controller
fi
if ! id gate-controller-build >/dev/null 2>&1; then
  useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin gate-controller-build
fi
getent group gate-controller >/dev/null 2>&1 \
  || fail "the gate-controller group is required"
getent group gate-controller-build >/dev/null 2>&1 \
  || fail "the gate-controller-build group is required"
getent group gpio >/dev/null 2>&1 || fail "the Raspberry Pi gpio group is required"
usermod -aG gpio gate-controller

configure_upload_directory \
  "$STATE_ROOT" "$UPLOAD_ROOT" "$FTP_USER" gate-controller gate-controller

migrate_legacy_authorised_plates \
  /opt/gate-controller/authorised_licence_plates.csv \
  "$STATE_ROOT/authorised_licence_plates.csv" \
  gate-controller gate-controller

LEGACY_DATABASE=/opt/gate-controller/data/gate-controller-database.db
PERSISTENT_DATABASE=$STATE_ROOT/gate-controller.db
if [[ -f $LEGACY_DATABASE && ! -e $PERSISTENT_DATABASE ]]; then
  install -m 0600 -o gate-controller -g gate-controller \
    "$LEGACY_DATABASE" "$PERSISTENT_DATABASE"
  printf 'Migrated legacy SQLite database to %s\n' "$PERSISTENT_DATABASE"
fi

install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$RELEASES_ROOT"
RELEASE=$RELEASES_ROOT/$SHA
STAGING=$(mktemp -d "$RELEASES_ROOT/.bootstrap-$SHA.XXXXXX")
BACKUP_DIR=$(mktemp -d /run/gate-controller-install.XXXXXX)

ROLLBACK_OWNER_SUBSHELL=$BASH_SUBSHELL
ROLLBACK_STARTED=false
trap rollback ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM
trap cleanup EXIT

git -C "$SOURCE" archive --format=tar "$SHA" | tar -xf - -C "$STAGING"
TRUST_ANCHOR_HANDOFF=$BACKUP_DIR/trust-anchors
create_fixed_trust_anchor_handoff "$STAGING" "$TRUST_ANCHOR_HANDOFF"
chown -R gate-controller-build:gate-controller-build "$STAGING"
run_candidate_command python3 -m venv "$STAGING/.venv"
run_candidate_command "$STAGING/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-input -r "$STAGING/requirements.txt"
verify_candidate_release "$STAGING"

VERIFY_ROOT=$(mktemp -d "$BACKUP_DIR/verify.XXXXXX")
sed "s|/opt/gate-controller-deploy/current|$STAGING|g" \
  "$STAGING/file-monitor.service" >"$VERIFY_ROOT/$APP_SERVICE"
sed "s|/opt/gate-controller-deploy/current|$STAGING|g" \
  "$STAGING/deployment/systemd/$UPDATER_SERVICE" >"$VERIFY_ROOT/$UPDATER_SERVICE"
install -m 0644 "$STAGING/deployment/systemd/$UPDATER_TIMER" \
  "$VERIFY_ROOT/$UPDATER_TIMER"
systemd-analyze verify \
  "$VERIFY_ROOT/$APP_SERVICE" \
  "$VERIFY_ROOT/$UPDATER_SERVICE" \
  "$VERIFY_ROOT/$UPDATER_TIMER"

chown -R root:root "$STAGING"
chmod 0755 "$STAGING"
publish_bootstrap_release "$STAGING" "$RELEASE"
STAGING=

for name in "$APP_SERVICE" "$LEGACY_COMMAND_UNIT" "$UPDATER_SERVICE" "$UPDATER_TIMER"; do
  backup_path "$name"
done
backup_fixed_updater_helper "$UPDATER_HELPER" "$BACKUP_DIR"
backup_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" "$BACKUP_DIR"
backup_cloudflared_transport_drop_in "$SYSTEMD_ROOT" "$BACKUP_DIR"
if [[ -L $CURRENT_LINK ]]; then
  PREVIOUS_CURRENT=$(readlink -f "$CURRENT_LINK")
elif [[ -e $CURRENT_LINK ]]; then
  fail "$CURRENT_LINK exists but is not a symlink"
fi
if systemctl is-enabled --quiet "$APP_SERVICE"; then
  APP_WAS_ENABLED=true
fi
if systemctl is-active --quiet "$APP_SERVICE"; then
  APP_WAS_ACTIVE=true
fi
if systemctl is-enabled --quiet "$LEGACY_COMMAND_UNIT"; then
  LEGACY_COMMAND_WAS_ENABLED=true
fi
if systemctl is-active --quiet "$LEGACY_COMMAND_UNIT"; then
  LEGACY_COMMAND_WAS_ACTIVE=true
fi
capture_updater_timer_state
capture_cloudflared_service_state

ACTIVATION_STARTED=true
configure_ftp_home "$FTP_USER" "$UPLOAD_ROOT"
systemctl disable --now "$LEGACY_COMMAND_UNIT" >/dev/null 2>&1 || true
rm -f -- "$SYSTEMD_ROOT/$LEGACY_COMMAND_UNIT"
install_fixed_trust_anchors "$TRUST_ANCHOR_HANDOFF" "$SYSTEMD_ROOT" "$UPDATER_HELPER"
install_cloudflared_transport_drop_in "$TRUST_ANCHOR_HANDOFF" "$SYSTEMD_ROOT"
install_fixed_media_bootstrap "$TRUST_ANCHOR_HANDOFF"
rm -f -- "$CURRENT_LINK.new"
ln -s "$RELEASE" "$CURRENT_LINK.new"
mv -Tf "$CURRENT_LINK.new" "$CURRENT_LINK"
run_systemctl_bounded daemon-reload \
  || fail "could not reload systemd during bootstrap"
apply_cloudflared_transport_if_active
systemctl enable "$APP_SERVICE"
systemctl restart "$APP_SERVICE"

for ((health_check = 0; health_check < 15; health_check += 1)); do
  sleep 1
  systemctl is-active --quiet "$APP_SERVICE" \
    || fail "$APP_SERVICE did not remain active during bootstrap"
done

systemctl enable --now "$UPDATER_TIMER"
INSTALL_SUCCEEDED=true
trap - ERR INT TERM

printf 'Installed managed release %s.\n' "$SHA"
printf 'Active release: %s\n' "$(readlink -f "$CURRENT_LINK")"
printf 'Legacy checkout retained at %s.\n' "$SOURCE"
printf 'Updater timer: %s\n' "$(systemctl is-active "$UPDATER_TIMER")"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
