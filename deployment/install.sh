#!/usr/bin/env bash

set -Eeuo pipefail

INSTALL_ROOT=/opt/gate-controller-deploy
RELEASES_ROOT="$INSTALL_ROOT/releases"
CURRENT_LINK="$INSTALL_ROOT/current"
APP_SERVICE=file-monitor.service
UPDATER_SERVICE=gate-controller-updater.service
UPDATER_TIMER=gate-controller-updater.timer
SYSTEMD_ROOT=/etc/systemd/system
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
UPDATER_WAS_ENABLED=false
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
    esac
  done <"$env_file"
  [[ $has_supabase_url == "$has_service_key" ]] \
    || fail "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured together"
}

create_fixed_trust_anchor_handoff() {
  local source=$1
  local handoff=$2

  [[ ! -e $handoff && ! -L $handoff ]] \
    || fail "trust-anchor handoff already exists: $handoff"
  install -d -o root -g root -m 0700 "$handoff/deployment/systemd"
  install -o root -g root -m 0444 \
    "$source/deployment/gate_controller_updater.py" \
    "$handoff/deployment/gate_controller_updater.py"
  install -o root -g root -m 0444 \
    "$source/file-monitor.service" "$handoff/file-monitor.service"
  install -o root -g root -m 0444 \
    "$source/deployment/systemd/$UPDATER_SERVICE" \
    "$handoff/deployment/systemd/$UPDATER_SERVICE"
  install -o root -g root -m 0444 \
    "$source/deployment/systemd/$UPDATER_TIMER" \
    "$handoff/deployment/systemd/$UPDATER_TIMER"
  chmod 0555 "$handoff" "$handoff/deployment" "$handoff/deployment/systemd"
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

install_fixed_media_bootstrap() {
  local source=$1
  local auth_source=$source/gate_media_auth

  install -d -o root -g root -m 0755 "$MEDIA_BOOTSTRAP_ROOT/gate_media_auth"
  install -o root -g root -m 0755 \
    "$source/deployment/install-media.sh" "$MEDIA_BOOTSTRAP_ROOT/install-media.sh"
  install -o root -g root -m 0644 \
    "$source/deployment/media/mediamtx.yml" "$MEDIA_BOOTSTRAP_ROOT/mediamtx.yml"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-auth.service" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-auth.service"
  install -o root -g root -m 0644 \
    "$source/deployment/systemd/gate-media-gateway.service" \
    "$MEDIA_BOOTSTRAP_ROOT/gate-media-gateway.service"
  for name in __init__.py __main__.py token.py capabilities.py; do
    install -o root -g root -m 0644 \
      "$auth_source/$name" "$MEDIA_BOOTSTRAP_ROOT/gate_media_auth/$name"
  done
}

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

  if [[ -f $previous_unit && $was_active == true ]]; then
    systemctl restart "$APP_SERVICE"
  else
    systemctl stop "$APP_SERVICE"
  fi
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
    run_candidate_command .venv/bin/python -m compileall -q gate_controller deployment
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
  bash cat chmod chown env flock getent git id install ln mktemp mv python3 readlink \
  rm runuser sed sh sleep systemctl systemd-analyze tar /usr/bin/test useradd usermod; do
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
[[ -f $SOURCE/deployment/install-media.sh ]] \
  || fail "source does not contain the media installer"
[[ -f $SOURCE/deployment/media/mediamtx.yml ]] \
  || fail "source does not contain the MediaMTX config"
[[ -f $SOURCE/deployment/systemd/gate-media-auth.service ]] \
  || fail "source does not contain the media auth service"
[[ -f $SOURCE/deployment/systemd/gate-media-gateway.service ]] \
  || fail "source does not contain the media gateway service"

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

cleanup() {
  if [[ -n $STAGING && -d $STAGING ]]; then
    rm -rf -- "$STAGING"
  fi
  if [[ -n $BACKUP_DIR && -d $BACKUP_DIR ]]; then
    rm -rf -- "$BACKUP_DIR"
  fi
}

restore_path() {
  local name=$1
  local destination=$SYSTEMD_ROOT/$name
  if [[ -f $BACKUP_DIR/$name ]]; then
    install -m 0644 "$BACKUP_DIR/$name" "$destination"
  else
    rm -f -- "$destination"
  fi
}

rollback() {
  local original_status=$?
  set +e
  if [[ $ACTIVATION_STARTED == true && $INSTALL_SUCCEEDED == false ]]; then
    printf 'Managed startup failed; restoring the previous installation.\n' >&2
    if [[ -n $PREVIOUS_CURRENT ]]; then
      ln -sfn "$PREVIOUS_CURRENT" "$CURRENT_LINK.rollback"
      mv -Tf "$CURRENT_LINK.rollback" "$CURRENT_LINK"
    else
      rm -f -- "$CURRENT_LINK"
    fi
    restore_path "$APP_SERVICE"
    restore_path "$UPDATER_SERVICE"
    restore_path "$UPDATER_TIMER"
    restore_fixed_updater_helper "$UPDATER_HELPER" "$BACKUP_DIR"
    configure_ftp_home "$FTP_USER" "$FTP_PREVIOUS_HOME"
    if [[ $UPDATER_WAS_ENABLED == false ]]; then
      systemctl disable --now "$UPDATER_TIMER"
    fi
    if [[ $APP_WAS_ENABLED == false ]]; then
      systemctl disable "$APP_SERVICE"
    fi
    systemctl daemon-reload
    restore_application_activity "$BACKUP_DIR/$APP_SERVICE" "$APP_WAS_ACTIVE"
  fi
  cleanup
  exit "$original_status"
}

trap rollback ERR INT TERM
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

for name in "$APP_SERVICE" "$UPDATER_SERVICE" "$UPDATER_TIMER"; do
  if [[ -f $SYSTEMD_ROOT/$name ]]; then
    install -m 0600 "$SYSTEMD_ROOT/$name" "$BACKUP_DIR/$name"
  fi
done
backup_fixed_updater_helper "$UPDATER_HELPER" "$BACKUP_DIR"
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
if systemctl is-enabled --quiet "$UPDATER_TIMER"; then
  UPDATER_WAS_ENABLED=true
fi

ACTIVATION_STARTED=true
configure_ftp_home "$FTP_USER" "$UPLOAD_ROOT"
install_fixed_trust_anchors "$TRUST_ANCHOR_HANDOFF" "$SYSTEMD_ROOT" "$UPDATER_HELPER"
install_fixed_media_bootstrap "$RELEASE"
rm -f -- "$CURRENT_LINK.new"
ln -s "$RELEASE" "$CURRENT_LINK.new"
mv -Tf "$CURRENT_LINK.new" "$CURRENT_LINK"
systemctl daemon-reload
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
