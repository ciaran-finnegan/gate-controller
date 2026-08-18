#!/usr/bin/env bash

set -Eeuo pipefail

MEDIA_AUTH_ENV=/etc/gate-media-auth.env
MEDIA_GATEWAY_ENV=/etc/gate-media-gateway.env
MEDIA_TURN_ENV=/etc/gate-media-turn.env
MEDIA_CONFIG_ROOT=/etc/gate-media
MEDIA_CONFIG=$MEDIA_CONFIG_ROOT/mediamtx.yml
MEDIA_PROXY_TEMPLATE=$MEDIA_CONFIG_ROOT/nginx-whep-locations.conf.template
MEDIA_PROXY_CONFIG=$MEDIA_CONFIG_ROOT/nginx-whep-locations.conf
MEDIA_RUNTIME=/run/gate-media
MEDIA_TMPFILES=/etc/tmpfiles.d/gate-media.conf
MEDIA_LIBRARY=/usr/local/lib/gate-media
MEDIA_TURN_REFRESH_HELPER=$MEDIA_LIBRARY/gate_media_turn_refresh.py
MEDIA_BINARY=/usr/local/bin/mediamtx
MEDIA_ARCHIVE_ROOT=/var/lib/gate-media/archives
NGINX_BINARY=/usr/sbin/nginx
NGINX_PROXY_CONFIG=/etc/nginx/conf.d/gate-media-whep.conf
SYSTEMD_ROOT=/etc/systemd/system
MEDIA_TURN_REFRESH_TIMER=gate-media-turn-refresh.timer
PINNED_MEDIAMTX_VERSION=1.19.3
SOURCE=
MEDIAMTX_ARCHIVE=
MEDIAMTX_VERSION=
CHECKSUM_MAP=
ALLOWED_ORIGIN=
EXTRACTED_MEDIA_DIR=
STAGED_MEDIA_BINARY=
STAGED_MEDIA_ARCHIVE=
STAGED_MEDIA_PROXY_CONFIG=

usage() {
  cat <<'EOF'
Usage: sudo deployment/install-media.sh --source PATH --mediamtx-archive PATH \
  --mediamtx-version VERSION --checksum-map PATH --allowed-origin HTTPS_ORIGIN

The archive and checksum map are operator-supplied, pre-approved local files for
MediaMTX 1.19.3. This installer never downloads assets or invents a checksum.
EOF
}

require_option_value() {
  local option=$1
  [[ $# -ge 2 ]] || fail "$option requires a value"
}

fail() {
  printf 'gate media install: %s\n' "$*" >&2
  return 1
}

normalize_architecture() {
  case "$(uname -m)" in
    aarch64|arm64) printf 'arm64\n' ;;
    armv7l|armv7*) printf 'armv7\n' ;;
    *) fail "unsupported MediaMTX architecture: $(uname -m)" ;;
  esac
}

lookup_mediamtx_checksum() {
  local version=$1
  local architecture=$2
  local map=$3
  local validator=${SOURCE:-${BASH_SOURCE[0]%/*}/..}/gate_media_config.py

  python3 "$validator" checksum --map "$map" --version "$version" \
    --architecture "$architecture"
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

media_environment_complete() {
  local validator=${SOURCE:-${BASH_SOURCE[0]%/*}/..}/gate_media_config.py

  python3 "$validator" environment --auth "$MEDIA_AUTH_ENV" --gateway "$MEDIA_GATEWAY_ENV" \
    >/dev/null
}

reject_gpio_membership() {
  local account=$1
  local group
  for group in $(id -nG "$account"); do
    [[ $group != gpio ]] || fail "$account must not belong to the gpio group"
  done
}

disable_media_services() {
  systemctl disable --now gate-media-gateway.service gate-media-auth.service \
    >/dev/null 2>&1 || true
}

disable_turn_refresh_timer() {
  systemctl disable --now "$MEDIA_TURN_REFRESH_TIMER" >/dev/null 2>&1 || true
}

turn_refresh_environment_configured() {
  local validator=$MEDIA_LIBRARY/gate_media_config.py

  [[ -f $MEDIA_TURN_ENV && ! -L $MEDIA_TURN_ENV ]] || return 1
  python3 "$validator" turn --env "$MEDIA_TURN_ENV" >/dev/null
}

configure_turn_refresh_timer() {
  if turn_refresh_environment_configured; then
    systemctl enable --now "$MEDIA_TURN_REFRESH_TIMER"
  else
    disable_turn_refresh_timer
    printf 'TURN refresh timer remains disabled until /etc/gate-media-turn.env is valid.\n'
  fi
}

cleanup_media_install() {
  if [[ -n $EXTRACTED_MEDIA_DIR \
      && $EXTRACTED_MEDIA_DIR == "$MEDIA_ARCHIVE_ROOT"/.extract.* ]]; then
    rm -rf -- "$EXTRACTED_MEDIA_DIR"
  fi
  if [[ -n $STAGED_MEDIA_BINARY \
      && $STAGED_MEDIA_BINARY == "$MEDIA_BINARY".new.* ]]; then
    rm -f -- "$STAGED_MEDIA_BINARY"
  fi
  if [[ -n $STAGED_MEDIA_ARCHIVE \
      && $STAGED_MEDIA_ARCHIVE == "$MEDIA_ARCHIVE_ROOT"/.archive.* ]]; then
    rm -f -- "$STAGED_MEDIA_ARCHIVE"
  fi
  if [[ -n $STAGED_MEDIA_PROXY_CONFIG \
      && $STAGED_MEDIA_PROXY_CONFIG == "$MEDIA_PROXY_CONFIG".new.* ]]; then
    rm -f -- "$STAGED_MEDIA_PROXY_CONFIG"
  fi
}

on_media_install_failure() {
  local status=$?
  [[ $status -ne 0 ]] || status=1
  trap - ERR INT TERM
  disable_media_services
  disable_turn_refresh_timer
  cleanup_media_install
  exit "$status"
}

render_proxy_config() {
  local template=$1
  local output=$2
  local allowed_origin=$3

  python3 - "$template" "$output" "$allowed_origin" <<'PY'
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

template = Path(sys.argv[1])
output = Path(sys.argv[2])
origin = sys.argv[3]
parsed = urlsplit(origin)
if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.path or parsed.query or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
        or parsed.port is not None and not 1 <= parsed.port <= 65535):
    raise SystemExit("invalid exact HTTPS media origin")
source = template.read_text(encoding="utf-8")
placeholder = "__GATE_MEDIA_ALLOWED_ORIGIN__"
if placeholder not in source:
    raise SystemExit("media proxy template placeholder is missing")
rendered = source.replace(placeholder, origin).encode("utf-8")
temporary = Path(f"{output}.new.{os.getpid()}")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
try:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
}

activate_proxy_config() {
  local config=$1
  local config_root=${NGINX_PROXY_CONFIG%/*}
  local prior_proxy_target=
  local had_prior_proxy_link=0
  local staged_proxy_link=$NGINX_PROXY_CONFIG.new.$$

  [[ -f $config && ! -L $config ]] || fail "rendered WHEP proxy config must be regular"
  [[ -x $NGINX_BINARY ]] || fail "nginx is not installed at $NGINX_BINARY"
  [[ -d $config_root && ! -L $config_root ]] || fail "nginx config directory is invalid"
  [[ ! -e $NGINX_PROXY_CONFIG || -L $NGINX_PROXY_CONFIG ]] \
    || fail "$NGINX_PROXY_CONFIG is not a managed symlink"
  if [[ -L $NGINX_PROXY_CONFIG ]]; then
    prior_proxy_target=$(readlink -- "$NGINX_PROXY_CONFIG")
    had_prior_proxy_link=1
  fi
  rm -f -- "$staged_proxy_link"
  ln -s -- "$config" "$staged_proxy_link"
  mv -f -- "$staged_proxy_link" "$NGINX_PROXY_CONFIG"
  [[ -L $NGINX_PROXY_CONFIG \
      && $(readlink -f "$NGINX_PROXY_CONFIG") == "$(readlink -f "$config")" ]] \
    || fail "nginx WHEP proxy symlink could not be verified"
  if ! "$NGINX_BINARY" -t; then
    if [[ $had_prior_proxy_link -eq 1 ]]; then
      ln -s -- "$prior_proxy_target" "$staged_proxy_link"
      mv -f -- "$staged_proxy_link" "$NGINX_PROXY_CONFIG"
    else
      rm -f -- "$NGINX_PROXY_CONFIG"
    fi
    rm -f -- "$config"
    return 1
  fi
  ln -s -- "$MEDIA_PROXY_CONFIG" "$staged_proxy_link"
  mv -f -- "$staged_proxy_link" "$NGINX_PROXY_CONFIG"
  mv -f -- "$config" "$MEDIA_PROXY_CONFIG"
  STAGED_MEDIA_PROXY_CONFIG=
  [[ -L $NGINX_PROXY_CONFIG \
      && $(readlink -f "$NGINX_PROXY_CONFIG") == "$(readlink -f "$MEDIA_PROXY_CONFIG")" ]] \
    || fail "nginx WHEP proxy symlink could not be activated"
  systemctl enable nginx.service
  systemctl reload-or-restart nginx.service
}

stage_mediamtx_archive() {
  local archive=$1
  local version=$2
  local architecture=$3
  local stable=$MEDIA_ARCHIVE_ROOT/mediamtx-$version-$architecture.tar.gz

  [[ ! -e $MEDIA_ARCHIVE_ROOT || -d $MEDIA_ARCHIVE_ROOT && ! -L $MEDIA_ARCHIVE_ROOT ]] \
    || fail "private MediaMTX archive directory must not be a symlink"
  install -d -o root -g root -m 0700 "$MEDIA_ARCHIVE_ROOT"
  [[ -d $MEDIA_ARCHIVE_ROOT && ! -L $MEDIA_ARCHIVE_ROOT ]] \
    || fail "private MediaMTX archive directory must be a directory"
  STAGED_MEDIA_ARCHIVE=$MEDIA_ARCHIVE_ROOT/.archive.$$
  install -o root -g root -m 0600 "$archive" "$STAGED_MEDIA_ARCHIVE"
  [[ -f $STAGED_MEDIA_ARCHIVE && ! -L $STAGED_MEDIA_ARCHIVE ]] \
    || fail "staged MediaMTX archive must be a regular file"
  mv -f -- "$STAGED_MEDIA_ARCHIVE" "$stable"
  STAGED_MEDIA_ARCHIVE=
  printf '%s\n' "$stable"
}

install_mediamtx_binary() {
  local archive=$1
  local version=$2
  local checksum_map=$3
  local architecture checksum stable candidate version_output
  architecture=$(normalize_architecture)
  checksum=$(lookup_mediamtx_checksum "$version" "$architecture" "$checksum_map")
  [[ -f $archive && ! -L $archive ]] || fail "MediaMTX archive must be a regular file"
  stable=$(stage_mediamtx_archive "$archive" "$version" "$architecture")
  [[ -f $stable && ! -L $stable ]] || fail "private MediaMTX archive must be regular"
  printf '%s  %s\n' "$checksum" "$stable" | sha256sum --check --status - \
    || fail "MediaMTX archive SHA-256 does not match approved map"
  EXTRACTED_MEDIA_DIR=$(mktemp -d "$MEDIA_ARCHIVE_ROOT/.extract.XXXXXX")
  tar --no-same-owner --no-same-permissions -xzf "$stable" -C "$EXTRACTED_MEDIA_DIR" mediamtx
  candidate=$EXTRACTED_MEDIA_DIR/mediamtx
  [[ -f $candidate && ! -L $candidate && -x $candidate ]] \
    || fail "MediaMTX archive has no regular executable mediamtx"
  install -d -o root -g root -m 0755 "${MEDIA_BINARY%/*}"
  STAGED_MEDIA_BINARY=$MEDIA_BINARY.new.$$
  install -o root -g root -m 0755 "$candidate" "$STAGED_MEDIA_BINARY"
  [[ -f $STAGED_MEDIA_BINARY && ! -L $STAGED_MEDIA_BINARY && -x $STAGED_MEDIA_BINARY ]] \
    || fail "staged MediaMTX binary must be a regular executable"
  version_output=$("$STAGED_MEDIA_BINARY" --version)
  [[ $version_output == "$version" || $version_output == "v$version" \
      || $version_output == *" $version" || $version_output == *" v$version" ]] \
    || fail "installed MediaMTX version does not match $version"
  mv -f -- "$STAGED_MEDIA_BINARY" "$MEDIA_BINARY"
  STAGED_MEDIA_BINARY=
  rm -rf -- "$EXTRACTED_MEDIA_DIR"
  EXTRACTED_MEDIA_DIR=
}

install_fixed_media_files() {
  local source=$1
  local source_auth=$source/gate_media_auth
  local source_gateway=$source/gate_media_gateway

  [[ -f $source/deployment/media/mediamtx.yml ]] || fail "MediaMTX config is missing"
  [[ -f $source/deployment/media/nginx-whep-locations.conf.template ]] \
    || fail "WHEP proxy template is missing"
  [[ -f $source/deployment/systemd/gate-media-auth.service ]] || fail "media auth unit is missing"
  [[ -f $source/deployment/systemd/gate-media-gateway.service ]] || fail "media gateway unit is missing"
  [[ -f $source/deployment/systemd/gate-media-turn-refresh.service ]] \
    || fail "media TURN refresh service is missing"
  [[ -f $source/deployment/systemd/gate-media-turn-refresh.timer ]] \
    || fail "media TURN refresh timer is missing"
  [[ -f $source/deployment/gate_media_turn_refresh.py ]] \
    || fail "media TURN refresh helper is missing"
  [[ -d $source_auth ]] || fail "media auth package is missing"
  [[ -f $source_gateway/__init__.py && -f $source_gateway/__main__.py ]] \
    || fail "media gateway launcher is missing"
  [[ -f $source/gate_media_config.py ]] || fail "media config validator is missing"
  install -d -o root -g root -m 0755 "$MEDIA_CONFIG_ROOT" "$MEDIA_LIBRARY"
  install -d -o root -g root -m 0755 \
    "$MEDIA_LIBRARY/gate_media_auth" "$MEDIA_LIBRARY/gate_media_gateway"
  install -o root -g gate-media -m 0640 \
    "$source/deployment/media/mediamtx.yml" "$MEDIA_CONFIG"
  install -o root -g root -m 0640 \
    "$source/deployment/media/nginx-whep-locations.conf.template" "$MEDIA_PROXY_TEMPLATE"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-auth.service" "$SYSTEMD_ROOT/gate-media-auth.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-gateway.service" "$SYSTEMD_ROOT/gate-media-gateway.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-turn-refresh.service" \
    "$SYSTEMD_ROOT/gate-media-turn-refresh.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-turn-refresh.timer" \
    "$SYSTEMD_ROOT/gate-media-turn-refresh.timer"
  install -o root -g root -m 0700 \
    "$source/deployment/gate_media_turn_refresh.py" "$MEDIA_TURN_REFRESH_HELPER"
  install -o root -g root -m 0644 "$source_auth/__init__.py" "$MEDIA_LIBRARY/gate_media_auth/__init__.py"
  install -o root -g root -m 0644 "$source_auth/__main__.py" "$MEDIA_LIBRARY/gate_media_auth/__main__.py"
  install -o root -g root -m 0644 "$source_auth/token.py" "$MEDIA_LIBRARY/gate_media_auth/token.py"
  install -o root -g root -m 0644 "$source_auth/capabilities.py" "$MEDIA_LIBRARY/gate_media_auth/capabilities.py"
  install -o root -g root -m 0644 "$source/gate_media_config.py" "$MEDIA_LIBRARY/gate_media_config.py"
  install -o root -g root -m 0644 \
    "$source_gateway/__init__.py" "$MEDIA_LIBRARY/gate_media_gateway/__init__.py"
  install -o root -g root -m 0644 \
    "$source_gateway/__main__.py" "$MEDIA_LIBRARY/gate_media_gateway/__main__.py"
  install -o root -g root -m 0644 /dev/stdin "$MEDIA_TMPFILES" <<'EOF'
d /run/gate-media 0775 root gate-media-auth -
EOF
}

main() {
  trap on_media_install_failure ERR INT TERM
  trap cleanup_media_install EXIT
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source) require_option_value "$@"; SOURCE=$2; shift 2 ;;
      --mediamtx-archive) require_option_value "$@"; MEDIAMTX_ARCHIVE=$2; shift 2 ;;
      --mediamtx-version) require_option_value "$@"; MEDIAMTX_VERSION=$2; shift 2 ;;
      --checksum-map) require_option_value "$@"; CHECKSUM_MAP=$2; shift 2 ;;
      --allowed-origin) require_option_value "$@"; ALLOWED_ORIGIN=$2; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown argument: $1" ;;
    esac
  done
  [[ $EUID -eq 0 ]] || fail "run this installer with sudo"
  [[ -n $SOURCE && -n $MEDIAMTX_ARCHIVE && -n $MEDIAMTX_VERSION \
      && -n $CHECKSUM_MAP && -n $ALLOWED_ORIGIN ]] \
    || fail "source, archive, version, checksum map, and allowed origin are required"
  [[ $MEDIAMTX_VERSION == "$PINNED_MEDIAMTX_VERSION" ]] \
    || fail "MediaMTX version must be $PINNED_MEDIAMTX_VERSION"
  SOURCE=$(readlink -f "$SOURCE")
  [[ -d $SOURCE ]] || fail "source checkout does not exist"

  for command in flock id install ln mktemp mv python3 readlink rm sha256sum \
    systemctl systemd-tmpfiles tar uname useradd; do
    command -v "$command" >/dev/null 2>&1 || fail "required command is missing: $command"
  done
  for account in gate-media gate-media-auth; do
    if ! id "$account" >/dev/null 2>&1; then
      useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin "$account"
    fi
    reject_gpio_membership "$account"
  done
  for environment_file in "$MEDIA_AUTH_ENV" "$MEDIA_GATEWAY_ENV"; do
    if [[ ! -e $environment_file ]]; then
      install -o root -g root -m 0600 /dev/null "$environment_file"
    fi
    validate_root_file "$environment_file" 600
  done
  install_fixed_media_files "$SOURCE"
  STAGED_MEDIA_PROXY_CONFIG=$MEDIA_PROXY_CONFIG.new.$$
  render_proxy_config "$MEDIA_PROXY_TEMPLATE" "$STAGED_MEDIA_PROXY_CONFIG" "$ALLOWED_ORIGIN"
  install_mediamtx_binary "$MEDIAMTX_ARCHIVE" "$MEDIAMTX_VERSION" "$CHECKSUM_MAP"
  systemd-tmpfiles --create "$MEDIA_TMPFILES"
  systemctl daemon-reload
  activate_proxy_config "$STAGED_MEDIA_PROXY_CONFIG"
  if media_environment_complete; then
    systemctl enable gate-media-auth.service gate-media-gateway.service
    systemctl restart gate-media-auth.service gate-media-gateway.service
  else
    disable_media_services
    printf 'Media services remain disabled until split source and HMAC environment values exist.\n'
  fi
  configure_turn_refresh_timer
  trap - ERR INT TERM
  cleanup_media_install
  trap - EXIT
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
