#!/usr/bin/env bash

set -Eeuo pipefail

MEDIA_ENV=/etc/gate-media.env
MEDIA_CONFIG_ROOT=/etc/gate-media
MEDIA_CONFIG=$MEDIA_CONFIG_ROOT/mediamtx.yml
MEDIA_RUNTIME=/run/gate-media
MEDIA_TMPFILES=/etc/tmpfiles.d/gate-media.conf
MEDIA_LIBRARY=/usr/local/lib/gate-media
MEDIA_BINARY=/usr/local/bin/mediamtx
SYSTEMD_ROOT=/etc/systemd/system
SOURCE=
MEDIAMTX_ARCHIVE=
MEDIAMTX_VERSION=
CHECKSUM_MAP=

usage() {
  cat <<'EOF'
Usage: sudo deployment/install-media.sh --source PATH --mediamtx-archive PATH \
  --mediamtx-version VERSION --checksum-map PATH

The archive and checksum map are operator-supplied, pre-approved local files.
This installer never downloads release assets or invents a checksum.
EOF
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
  local map_version map_architecture checksum extra matched=

  [[ -f $map && ! -L $map ]] || fail "checksum map must be a regular file"
  while read -r map_version map_architecture checksum extra || [[ -n ${map_version:-} ]]; do
    [[ -n ${map_version:-} && ${map_version:0:1} != '#' ]] || continue
    [[ -z ${extra:-} ]] || fail "checksum map has an invalid row"
    [[ $checksum =~ ^[A-Fa-f0-9]{64}$ ]] || fail "checksum map has an invalid SHA-256"
    if [[ $map_version == "$version" && $map_architecture == "$architecture" ]]; then
      [[ -z $matched ]] || fail "checksum map has duplicate version and architecture"
      matched=$checksum
    fi
  done <"$map"
  [[ -n $matched ]] || fail "no approved checksum for MediaMTX $version $architecture"
  printf '%s\n' "$matched"
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
  grep -Eq '^GATE_MEDIA_HMAC_SECRET=.+$' "$MEDIA_ENV" \
    && grep -Eq '^GATE_MEDIA_RTSP_SOURCE=.+$' "$MEDIA_ENV"
}

install_mediamtx_binary() {
  local archive=$1
  local version=$2
  local checksum_map=$3
  local architecture checksum extracted
  architecture=$(normalize_architecture)
  checksum=$(lookup_mediamtx_checksum "$version" "$architecture" "$checksum_map")
  [[ -f $archive && ! -L $archive ]] || fail "MediaMTX archive must be a regular file"
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status - \
    || fail "MediaMTX archive SHA-256 does not match approved map"
  extracted=$(mktemp -d)
  trap 'rm -rf -- "$extracted"' RETURN
  tar -xzf "$archive" -C "$extracted" mediamtx
  [[ -x $extracted/mediamtx ]] || fail "MediaMTX archive has no executable mediamtx"
  install -o root -g root -m 0755 "$extracted/mediamtx" "$MEDIA_BINARY"
  "$MEDIA_BINARY" --version | grep -F -- "$version" >/dev/null \
    || fail "installed MediaMTX version does not match $version"
  rm -rf -- "$extracted"
  trap - RETURN
}

install_fixed_media_files() {
  local source=$1
  local source_auth=$source/gate_media_auth

  [[ -f $source/deployment/media/mediamtx.yml ]] || fail "MediaMTX config is missing"
  [[ -f $source/deployment/systemd/gate-media-auth.service ]] || fail "media auth unit is missing"
  [[ -f $source/deployment/systemd/gate-media-gateway.service ]] || fail "media gateway unit is missing"
  [[ -d $source_auth ]] || fail "media auth package is missing"
  install -d -o root -g root -m 0755 "$MEDIA_CONFIG_ROOT" "$MEDIA_LIBRARY"
  install -d -o root -g root -m 0755 "$MEDIA_LIBRARY/gate_media_auth"
  install -o root -g gate-media -m 0640 \
    "$source/deployment/media/mediamtx.yml" "$MEDIA_CONFIG"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-auth.service" "$SYSTEMD_ROOT/gate-media-auth.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-gateway.service" "$SYSTEMD_ROOT/gate-media-gateway.service"
  install -o root -g root -m 0644 "$source_auth/__init__.py" "$MEDIA_LIBRARY/gate_media_auth/__init__.py"
  install -o root -g root -m 0644 "$source_auth/__main__.py" "$MEDIA_LIBRARY/gate_media_auth/__main__.py"
  install -o root -g root -m 0644 "$source_auth/token.py" "$MEDIA_LIBRARY/gate_media_auth/token.py"
  install -o root -g root -m 0644 "$source_auth/capabilities.py" "$MEDIA_LIBRARY/gate_media_auth/capabilities.py"
  install -o root -g root -m 0644 /dev/stdin "$MEDIA_TMPFILES" <<'EOF'
d /run/gate-media 0775 root gate-media-auth -
EOF
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source) SOURCE=$2; shift 2 ;;
      --mediamtx-archive) MEDIAMTX_ARCHIVE=$2; shift 2 ;;
      --mediamtx-version) MEDIAMTX_VERSION=$2; shift 2 ;;
      --checksum-map) CHECKSUM_MAP=$2; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown argument: $1" ;;
    esac
  done
  [[ $EUID -eq 0 ]] || fail "run this installer with sudo"
  [[ -n $SOURCE && -n $MEDIAMTX_ARCHIVE && -n $MEDIAMTX_VERSION && -n $CHECKSUM_MAP ]] \
    || fail "source, archive, version, and checksum map are required"
  [[ $MEDIAMTX_VERSION =~ ^[0-9][0-9A-Za-z._-]{0,63}$ ]] || fail "invalid MediaMTX version"
  SOURCE=$(readlink -f "$SOURCE")
  [[ -d $SOURCE ]] || fail "source checkout does not exist"

  for command in grep install mktemp readlink rm sha256sum systemctl tar uname useradd; do
    command -v "$command" >/dev/null 2>&1 || fail "required command is missing: $command"
  done
  for account in gate-media gate-media-auth; do
    if ! id "$account" >/dev/null 2>&1; then
      useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin "$account"
    fi
  done
  if [[ ! -e $MEDIA_ENV ]]; then
    install -o root -g root -m 0600 /dev/null "$MEDIA_ENV"
  fi
  validate_root_file "$MEDIA_ENV" 600
  install_fixed_media_files "$SOURCE"
  install_mediamtx_binary "$MEDIAMTX_ARCHIVE" "$MEDIAMTX_VERSION" "$CHECKSUM_MAP"
  systemd-tmpfiles --create "$MEDIA_TMPFILES"
  systemctl daemon-reload
  if media_environment_complete; then
    systemctl enable --now gate-media-auth.service gate-media-gateway.service
  else
    systemctl disable --now gate-media-gateway.service gate-media-auth.service || true
    printf 'Media services remain disabled until /etc/gate-media.env has source and HMAC values.\n'
  fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
