#!/usr/bin/env bash

# Installs gate-camera-control: the only process on the Pi that holds Reolink
# camera API credentials. It runs as its own user, reads its own root-only
# environment file, binds 127.0.0.1:8767, and can reach nothing on the network
# but loopback and the camera's exact address.

set -Eeuo pipefail

CAMERA_ENV=/etc/gate-camera-control.env
CAMERA_LIBRARY=/usr/local/lib/gate-camera-control
CAMERA_TMPFILES=/etc/tmpfiles.d/gate-camera.conf
CAMERA_RUNTIME_ROOT=/run/gate-camera
CAMERA_SERVICE=gate-camera-control.service
CAMERA_USER=gate-camera-control
SYSTEMD_ROOT=/etc/systemd/system
CAMERA_DROPIN_DIR=$SYSTEMD_ROOT/$CAMERA_SERVICE.d
CAMERA_DROPIN=$CAMERA_DROPIN_DIR/10-camera-address.conf
SOURCE=

usage() {
  cat <<'EOF'
Usage: sudo deployment/install-camera-control.sh --source PATH

Installs the isolated gate camera-control service. Camera credentials are never
read from, or written to, the media gateway environment: they live only in
/etc/gate-camera-control.env (root:root 0600), which the operator populates.
EOF
}

fail() {
  printf 'gate camera control install: %s\n' "$*" >&2
  return 1
}

require_option_value() {
  local option=$1
  [[ $# -ge 2 ]] || fail "$option requires a value"
}

validate_root_file() {
  local path=$1
  local mode=$2
  local owner group actual_mode
  [[ -f $path && ! -L $path ]] || fail "$path must be a regular file"
  read -r owner group actual_mode < <(
    python3 - "$path" <<'PY'
import os
import stat
import sys

metadata = os.stat(sys.argv[1], follow_symlinks=False)
print(metadata.st_uid, metadata.st_gid, format(stat.S_IMODE(metadata.st_mode), "o"))
PY
  )
  [[ $owner == 0 && $group == 0 && $actual_mode == "$mode" ]] \
    || fail "$path must be root:root mode $mode"
}

reject_gpio_membership() {
  local account=$1
  local group
  for group in $(id -nG "$account"); do
    [[ $group != gpio ]] || fail "$account must not belong to the gpio group"
  done
}

camera_environment_configured() {
  python3 "$CAMERA_LIBRARY/gate_media_config.py" camera-control --env "$CAMERA_ENV" \
    >/dev/null
}

camera_host() {
  python3 "$CAMERA_LIBRARY/gate_media_config.py" camera-control --env "$CAMERA_ENV" \
    --print-host
}

disable_camera_service() {
  systemctl disable --now "$CAMERA_SERVICE" >/dev/null 2>&1 || true
}

preflight() {
  local command
  [[ $EUID -eq 0 ]] || fail "this installer must run as root"
  for command in id install python3 systemctl systemd-tmpfiles useradd; do
    command -v "$command" >/dev/null || fail "required command is missing: $command"
  done
  [[ -n $SOURCE ]] || fail "--source is required"
  [[ -d $SOURCE && ! -L $SOURCE ]] || fail "--source must be a directory"
  [[ -f $SOURCE/deployment/systemd/$CAMERA_SERVICE ]] \
    || fail "the camera control unit is missing from the source tree"
  [[ -d $SOURCE/gate_camera_control ]] \
    || fail "the gate_camera_control package is missing from the source tree"
}

ensure_account() {
  if ! id -u "$CAMERA_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin \
      "$CAMERA_USER"
  fi
  reject_gpio_membership "$CAMERA_USER"
  # The camera credentials must not become readable through a media group.
  local group
  for group in $(id -nG "$CAMERA_USER"); do
    [[ $group != gate-media && $group != gate-media-auth && $group != gate-controller ]] \
      || fail "$CAMERA_USER must not share a group with the media or controller services"
  done
}

ensure_environment_file() {
  [[ ! -L $CAMERA_ENV ]] || fail "$CAMERA_ENV must not be a symlink"
  if [[ ! -e $CAMERA_ENV ]]; then
    install -o root -g root -m 0600 /dev/null "$CAMERA_ENV"
  fi
  validate_root_file "$CAMERA_ENV" 600
}

publish_library() {
  local module
  install -d -o root -g root -m 0755 "$CAMERA_LIBRARY" \
    "$CAMERA_LIBRARY/gate_camera_control"
  install -o root -g root -m 0644 "$SOURCE/gate_media_config.py" \
    "$CAMERA_LIBRARY/gate_media_config.py"
  for module in __init__ __main__ atomic ir reolink state; do
    install -o root -g root -m 0644 \
      "$SOURCE/gate_camera_control/$module.py" \
      "$CAMERA_LIBRARY/gate_camera_control/$module.py"
  done
}

publish_unit() {
  install -o root -g root -m 0644 "$SOURCE/deployment/systemd/$CAMERA_SERVICE" \
    "$SYSTEMD_ROOT/$CAMERA_SERVICE"
  install -o root -g root -m 0644 /dev/stdin "$CAMERA_TMPFILES" <<EOF
d $CAMERA_RUNTIME_ROOT 0755 $CAMERA_USER $CAMERA_USER -
EOF
  systemd-tmpfiles --create "$CAMERA_TMPFILES"
}

publish_camera_address_dropin() {
  local host=$1
  install -d -o root -g root -m 0755 "$CAMERA_DROPIN_DIR"
  install -o root -g root -m 0644 /dev/stdin "$CAMERA_DROPIN" <<EOF
[Service]
IPAddressAllow=$host/32
EOF
}

remove_camera_address_dropin() {
  rm -f "$CAMERA_DROPIN"
  rmdir "$CAMERA_DROPIN_DIR" 2>/dev/null || true
}

activate() {
  systemctl daemon-reload
  if ! systemctl enable "$CAMERA_SERVICE"; then
    disable_camera_service
    return 1
  fi
  if ! systemctl restart "$CAMERA_SERVICE"; then
    disable_camera_service
    return 1
  fi
  if ! systemctl is-active --quiet "$CAMERA_SERVICE"; then
    disable_camera_service
    return 1
  fi
}

main() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --source)
        require_option_value "$1" "${2-}"
        SOURCE=$2
        shift 2
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        usage >&2
        fail "unknown option: $1"
        ;;
    esac
  done

  preflight
  ensure_account
  ensure_environment_file
  publish_library
  publish_unit

  if [[ ! -s $CAMERA_ENV ]] || ! camera_environment_configured; then
    disable_camera_service
    remove_camera_address_dropin
    systemctl daemon-reload
    printf '%s\n' \
      "gate-camera-control remains disabled until $CAMERA_ENV is valid." \
      "Populate it as root:root 0600 with GATE_CAMERA_HOST, GATE_CAMERA_USERNAME," \
      "GATE_CAMERA_PASSWORD and optionally GATE_CAMERA_IR_DEFAULT," \
      "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES, GATE_CAMERA_IR_LEASE_MAX_MINUTES," \
      "then re-run this installer. See docs/camera-control.md."
    return 0
  fi

  publish_camera_address_dropin "$(camera_host)"
  activate || fail "the camera control service could not be activated"
  printf 'gate-camera-control is active on 127.0.0.1:8767.\n'
}

main "$@"
