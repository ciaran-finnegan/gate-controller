import configparser
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read_unit(relative_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
    return parser


class CloudflareDocumentationTests(unittest.TestCase):
    def test_readme_describes_the_active_cloudflare_remote_control_path(self):
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare is the active remote-control path", readme)
        self.assertIn("GATE_CLOUDFLARE_API_URL", readme)
        self.assertIn("GATE_CLOUDFLARE_ACCESS_CLIENT_ID", readme)
        self.assertIn("GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET", readme)
        self.assertNotIn("SUPABASE_URL", readme)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", readme)

    def test_deployment_docs_cover_the_command_tunnel_and_ingest_contract(self):
        deployment = (REPOSITORY_ROOT / "docs/deployment.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("POST /commands", deployment)
        self.assertIn("cloudflared tunnel ingress validate", deployment)
        self.assertIn("cloudflared tunnel ingress rule", deployment)
        self.assertIn("POST\n/api/controller/events", deployment)
        self.assertIn("private R2 bucket", deployment)

    def test_deployment_docs_require_rollback_before_decommission(self):
        deployment = (REPOSITORY_ROOT / "docs/deployment.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("restore the previous release before removing", deployment)
        self.assertIn("decommissioning the prior service", deployment)

    def test_camera_docs_defer_pi_validation_to_safe_non_actuating_harness(self):
        camera = (REPOSITORY_ROOT / "docs/reolink-rlc-811a.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "Real Pi SSH,\nrelay, and media load validation remains deferred",
            camera,
        )
        self.assertIn("default non-actuating command", camera)
        self.assertIn("--skip-network", camera)
        self.assertIn("--actuate", camera)


class SystemdTrustBoundaryTests(unittest.TestCase):
    def test_cloudflared_config_has_command_media_and_catch_all_rules(self):
        config = Path("deployment/cloudflared/gate-controller-tunnel.yml").read_text()
        self.assertIn("service: http://127.0.0.1:8765", config)
        self.assertIn("service: http://127.0.0.1:8891", config)
        self.assertNotIn("service: http://127.0.0.1:8889", config)
        self.assertRegex(config, r"- service: http_status:404\s*$")

    def test_cloudflared_auto_transport_drop_in_is_managed_by_bootstrap(self):
        drop_in = Path(
            "deployment/systemd/cloudflared.service.d/20-http2.conf"
        ).read_text(encoding="utf-8")
        installer = Path("deployment/install.sh").read_text(encoding="utf-8")

        self.assertEqual(
            "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n",
            drop_in,
        )
        self.assertIn("install_cloudflared_transport_drop_in", installer)
        self.assertIn("cloudflared.service.d/20-http2.conf", installer)

    def test_command_server_is_owned_by_the_time_synchronised_main_service(self):
        unit = Path("file-monitor.service").read_text()
        installer = Path("deployment/install.sh").read_text()

        self.assertFalse(Path("deployment/systemd/gate-command-server.service").exists())
        self.assertIn("Requires=systemd-time-wait-sync.service", unit)
        self.assertIn(
            "After=network-online.target systemd-time-wait-sync.service",
            unit,
        )
        self.assertIn("-m gate_controller", unit)
        self.assertNotIn("COMMAND_SERVER_SERVICE", installer)
        self.assertIn('systemctl disable --now "$LEGACY_COMMAND_UNIT"', installer)
        self.assertIn('rm -f -- "$SYSTEMD_ROOT/$LEGACY_COMMAND_UNIT"', installer)
        self.assertIn("restore_legacy_command_activity", installer)

    def test_fixed_application_service_stays_non_root_and_preserves_upload_traversal(self):
        unit = read_unit("file-monitor.service")
        service = unit["Service"]

        self.assertEqual("gate-controller", service.get("User"))
        self.assertEqual("gate-controller", service.get("Group"))
        self.assertEqual("0710", service.get("StateDirectoryMode"))
        self.assertEqual(
            "/var/lib/gate-controller",
            service.get("WorkingDirectory"),
        )
        self.assertIn(
            "PYTHONPATH=/opt/gate-controller-deploy/current",
            service.get("Environment", ""),
        )
        self.assertTrue(
            service.get("ExecStart", "").startswith(
                "/opt/gate-controller-deploy/current/"
            )
        )
        self.assertIn("-m gate_controller.relay_safe", service.get("ExecStartPre", ""))
        self.assertIn("-m gate_controller.relay_safe", service.get("ExecStopPost", ""))
        self.assertFalse(service.get("ExecStopPost", "").startswith("-"))
        self.assertEqual("always", service.get("Restart"))
        self.assertEqual("512M", service.get("MemoryMax"))

    def test_root_updater_executes_only_fixed_bootstrap_helper(self):
        unit = read_unit("deployment/systemd/gate-controller-updater.service")
        command = shlex.split(unit["Service"]["ExecStart"])

        self.assertIn(
            "/usr/local/libexec/gate-controller/gate-controller-updater.py",
            command,
        )
        self.assertFalse(any("/current/" in argument for argument in command))
        self.assertFalse(any("/releases/" in argument for argument in command))

    def test_updater_has_narrow_writes_and_capabilities(self):
        unit = read_unit("deployment/systemd/gate-controller-updater.service")
        service = unit["Service"]

        self.assertEqual("strict", service.get("ProtectSystem"))
        self.assertEqual("true", service.get("NoNewPrivileges"))
        self.assertEqual("true", service.get("PrivateTmp"))
        self.assertEqual("true", service.get("PrivateDevices"))
        self.assertEqual("gate-controller-updater", service.get("RuntimeDirectory"))
        self.assertEqual("yes", service.get("RuntimeDirectoryPreserve"))
        self.assertEqual(
            {
                "/opt/gate-controller-deploy",
                "/run/gate-controller-updater",
            },
            set(shlex.split(service.get("ReadWritePaths", ""))),
        )
        self.assertEqual(
            {
                "CAP_CHOWN",
                "CAP_DAC_OVERRIDE",
                "CAP_FOWNER",
                "CAP_SETGID",
                "CAP_SETUID",
            },
            set(shlex.split(service.get("CapabilityBoundingSet", ""))),
        )
        command = shlex.split(service["ExecStart"])
        self.assertIn("/run/gate-controller-updater/update.lock", command)


class BootstrapInstallerTests(unittest.TestCase):
    def test_installer_can_be_sourced_without_executing_bootstrap(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                "source deployment/install.sh; printf sourced",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("sourced", completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_installs_fixed_root_owned_helper_and_units_outside_releases(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            systemd_root = root / "systemd"
            systemd_root.mkdir()
            helper = root / "libexec/gate-controller/gate-controller-updater.py"
            install_log = root / "install.log"
            command = f"""
source deployment/install.sh
install() {{
  printf '%s ' "$@" >> {shlex.quote(str(install_log))}
  printf '\n' >> {shlex.quote(str(install_log))}
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g)
        shift 2
        ;;
      *)
        forwarded+=("$1")
        shift
        ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
install_fixed_trust_anchors \
  {shlex.quote(str(REPOSITORY_ROOT))} \
  {shlex.quote(str(systemd_root))} \
  {shlex.quote(str(helper))}
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                (REPOSITORY_ROOT / "deployment/gate_controller_updater.py").read_bytes(),
                helper.read_bytes(),
            )
            self.assertEqual(0o755, helper.stat().st_mode & 0o777)
            for unit_name in (
                "file-monitor.service",
                "gate-controller-updater.service",
                "gate-controller-updater.timer",
            ):
                installed = systemd_root / unit_name
                self.assertTrue(installed.is_file(), unit_name)
                self.assertEqual(0o644, installed.stat().st_mode & 0o777)
            install_calls = install_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(install_calls)
            self.assertTrue(
                all("-o root -g root" in call for call in install_calls),
                install_calls,
            )

    def test_fixed_anchors_are_installed_from_root_owned_handoff_not_mutable_build_tree(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            handoff = root / "handoff"
            systemd_root = root / "systemd"
            helper = root / "libexec/gate-controller-updater.py"
            (source / "deployment/systemd/cloudflared.service.d").mkdir(
                parents=True
            )
            systemd_root.mkdir()
            (source / "deployment/gate_controller_updater.py").write_text("trusted helper\n")
            (source / "file-monitor.service").write_text("trusted app\n")
            (source / "deployment/systemd/gate-controller-updater.service").write_text("trusted updater\n")
            (source / "deployment/systemd/gate-controller-updater.timer").write_text("trusted timer\n")
            (
                source
                / "deployment/systemd/cloudflared.service.d/20-http2.conf"
            ).write_text(
                "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n"
            )
            command = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
create_fixed_trust_anchor_handoff {shlex.quote(str(source))} {shlex.quote(str(handoff))}
printf 'build-account mutation\n' > {shlex.quote(str(source / 'deployment/gate_controller_updater.py'))}
install_fixed_trust_anchors {shlex.quote(str(handoff))} {shlex.quote(str(systemd_root))} {shlex.quote(str(helper))}
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("trusted helper\n", helper.read_text())
            self.assertEqual(0o555, handoff.stat().st_mode & 0o777)

    @unittest.skipUnless(shutil.which("flock"), "requires the Linux flock command")
    def test_bootstrap_uses_same_nonblocking_lock_as_updater(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock = Path(temporary_directory) / "update.lock"
            ready = Path(temporary_directory) / "lock-ready"
            holder_script = """
import fcntl
import pathlib
import sys
import time

with open(sys.argv[1], "w") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pathlib.Path(sys.argv[2]).touch()
    time.sleep(30)
"""
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script, str(lock), str(ready)],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                for _ in range(100):
                    if ready.exists():
                        break
                    if holder.poll() is not None:
                        self.fail(f"lock holder exited early: {holder.stderr.read()}")
                    time.sleep(0.01)
                else:
                    self.fail("lock holder did not become ready")

                command = (
                    "source deployment/install.sh; "
                    f"acquire_install_lock {shlex.quote(str(lock))}"
                )
                completed = subprocess.run(
                    ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
                )
            finally:
                holder.terminate()
                holder.wait(timeout=5)

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("another install or update is already running", completed.stderr)

    def test_existing_release_allows_fixed_anchor_refresh(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            existing = root / "releases" / ("a" * 40)
            staging = root / "staging"
            handoff = root / "handoff"
            systemd_root = root / "systemd"
            helper = root / "helper.py"
            existing.mkdir(parents=True)
            staging.mkdir()
            (existing / "keep").write_text("existing\n")
            (handoff / "deployment/systemd").mkdir(parents=True)
            systemd_root.mkdir()
            (handoff / "deployment/gate_controller_updater.py").write_text("refreshed\n")
            (handoff / "file-monitor.service").write_text("app\n")
            (handoff / "deployment/systemd/gate-controller-updater.service").write_text("updater\n")
            (handoff / "deployment/systemd/gate-controller-updater.timer").write_text("timer\n")
            command = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac; done
  command install "${{forwarded[@]}}"
}}
publish_bootstrap_release {shlex.quote(str(staging))} {shlex.quote(str(existing))}
install_fixed_trust_anchors {shlex.quote(str(handoff))} {shlex.quote(str(systemd_root))} {shlex.quote(str(helper))}
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("existing\n", (existing / "keep").read_text())
            self.assertEqual("refreshed\n", helper.read_text())

    def test_legacy_authorised_plates_are_migrated_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            legacy = root / "legacy.csv"
            persistent = root / "authorised.csv"
            legacy.write_text("plate,name\n131D2696,Felim\n")
            command = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
migrate_legacy_authorised_plates \
  {shlex.quote(str(legacy))} {shlex.quote(str(persistent))} app app
printf 'replacement\n' > {shlex.quote(str(legacy))}
migrate_legacy_authorised_plates \
  {shlex.quote(str(legacy))} {shlex.quote(str(persistent))} app app
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("plate,name\n131D2696,Felim\n", persistent.read_text())
            self.assertEqual(0o600, persistent.stat().st_mode & 0o777)

    def test_environment_file_rejects_insecure_mode_and_partial_supabase(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / "gate-controller.env"
            env_file.write_text("SUPABASE_URL=https://example.supabase.co\nSUPABASE_SERVICE_ROLE_KEY=\n")
            env_file.chmod(0o644)
            common = f"source deployment/install.sh; validate_env_file {shlex.quote(str(env_file))} $(id -u) $(id -g)"

            insecure = subprocess.run(
                ["bash", "-c", common], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, insecure.returncode)
            self.assertIn("mode 0600", insecure.stderr)

            env_file.chmod(0o600)
            partial = subprocess.run(
                ["bash", "-c", common], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, partial.returncode)
            self.assertIn("legacy Supabase", partial.stderr)

    def test_environment_file_rejects_partial_cloudflare_and_mixed_cloud_credentials(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / "gate-controller.env"
            common = f"source deployment/install.sh; validate_env_file {shlex.quote(str(env_file))} $(id -u) $(id -g)"

            env_file.write_text("GATE_CLOUDFLARE_API_URL=https://gate.example.com\n")
            env_file.chmod(0o600)
            partial = subprocess.run(
                ["bash", "-c", common], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, partial.returncode)
            self.assertIn("GATE_CLOUDFLARE", partial.stderr)

            env_file.write_text(
                "SUPABASE_URL=https://example.supabase.co\n"
                "SUPABASE_SERVICE_ROLE_KEY=service-key\n"
                "GATE_CLOUDFLARE_API_URL=https://gate.example.com\n"
                "GATE_CLOUDFLARE_ACCESS_CLIENT_ID=client-id\n"
                "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET=client-secret\n"
            )
            mixed = subprocess.run(
                ["bash", "-c", common], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, mixed.returncode)
            self.assertIn("legacy Supabase", mixed.stderr)

    def test_environment_file_rejects_complete_legacy_supabase_only_configuration(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / "gate-controller.env"
            env_file.write_text(
                "SUPABASE_URL=https://example.supabase.co\n"
                "SUPABASE_SERVICE_ROLE_KEY=service-key\n"
            )
            env_file.chmod(0o600)
            command = (
                "source deployment/install.sh; "
                f"validate_env_file {shlex.quote(str(env_file))} $(id -u) $(id -g)"
            )

            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("legacy Supabase", completed.stderr)

    def test_upload_preflight_rejects_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_root = root / "state"
            state_root.mkdir()
            outside = root / "outside"
            outside.mkdir()
            uploads = state_root / "uploads"
            uploads.symlink_to(outside)
            command = f"""
source deployment/install.sh
validate_upload_paths \
  {shlex.quote(str(state_root))} \
  {shlex.quote(str(uploads))}
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("must not be a symbolic link", completed.stderr)

    def test_failed_bootstrap_refresh_can_restore_previous_fixed_helper(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helper = root / "gate-controller-updater.py"
            helper.write_text("previous helper\n", encoding="utf-8")
            helper.chmod(0o755)
            backup = root / "backup"
            backup.mkdir()
            command = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g)
        shift 2
        ;;
      *)
        forwarded+=("$1")
        shift
        ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
backup_fixed_updater_helper \
  {shlex.quote(str(helper))} {shlex.quote(str(backup))}
printf 'candidate helper\n' > {shlex.quote(str(helper))}
restore_fixed_updater_helper \
  {shlex.quote(str(helper))} {shlex.quote(str(backup))}
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("previous helper\n", helper.read_text(encoding="utf-8"))
            self.assertEqual(0o755, helper.stat().st_mode & 0o777)

    def test_bootstrap_candidate_verification_uses_build_account_for_shells(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            command_log = release / "commands.log"
            command = f"""
source deployment/install.sh
run_candidate_command() {{
  printf '%s\n' "$*" >> {shlex.quote(str(command_log))}
}}
verify_candidate_release {shlex.quote(str(release))}
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(4, len(commands))
            self.assertIn("sh -n file_monitor.sh", commands)
            self.assertIn("bash -n deployment/install.sh", commands)
    def test_configures_shared_upload_directory_and_verifies_account_access(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_root = root / "state"
            uploads = state_root / "uploads"
            daily_uploads = uploads / "2026" / "08" / "18"
            daily_uploads.mkdir(parents=True)
            daily_uploads.chmod(0o2755)
            stale_upload = daily_uploads / "old.jpg"
            stale_upload.write_bytes(b"jpeg")
            stale_upload.chmod(0o600)
            account_log = root / "accounts.log"
            command = f"""
source deployment/install.sh
install() {{
  printf 'install %s\n' "$*" >> {shlex.quote(str(account_log))}
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g)
        shift 2
        ;;
      -m)
        if [[ $2 == 2770 ]]; then
          forwarded+=("-m" "0770")
        else
          forwarded+=("$1" "$2")
        fi
        shift 2
        ;;
      *)
        forwarded+=("$1")
        shift
        ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
usermod() {{
  printf 'usermod %s\n' "$*" >> {shlex.quote(str(account_log))}
}}
chown() {{
  printf 'chown %s\n' "$*" >> {shlex.quote(str(account_log))}
}}
runuser() {{
  local user=
  if [[ $1 == --user ]]; then
    user=$2
    shift 2
  fi
  [[ $1 == -- ]]
  shift
  printf 'runuser %s %s\n' "$user" "$*" >> {shlex.quote(str(account_log))}
  if [[ $1 == /usr/bin/test ]]; then
    shift
    set -- /bin/test "$@"
  fi
  "$@"
}}
configure_upload_directory \
  {shlex.quote(str(state_root))} \
  {shlex.quote(str(uploads))} \
  ftp-user gate-controller gate-controller
configure_ftp_home ftp-user {shlex.quote(str(uploads))}
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(uploads.is_dir())
            self.assertEqual(0o710, state_root.stat().st_mode & 0o7777)
            self.assertEqual(0o770, uploads.stat().st_mode & 0o777)
            self.assertEqual(0o2770, daily_uploads.stat().st_mode & 0o7777)
            self.assertEqual(0o660, stale_upload.stat().st_mode & 0o777)
            account_actions = account_log.read_text(encoding="utf-8")
            self.assertIn(
                "install -d -o ftp-user -g gate-controller -m 2770",
                account_actions,
            )
            self.assertIn(
                f"chown -R ftp-user:gate-controller {uploads}",
                account_actions,
            )
            self.assertIn("usermod -aG gate-controller ftp-user", account_actions)
            self.assertIn(
                f"usermod --home {uploads} ftp-user",
                account_actions,
            )
            self.assertIn("runuser ftp-user /usr/bin/test -w", account_actions)
            self.assertIn("runuser gate-controller /usr/bin/test -r", account_actions)

    def test_rollback_restores_the_previous_application_activity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            previous_unit = root / "file-monitor.service"
            previous_unit.touch()
            action_log = root / "actions.log"
            command = f"""
source deployment/install.sh
systemctl() {{ printf '%s\n' "$*" >> {shlex.quote(str(action_log))}; }}
restore_application_activity {shlex.quote(str(previous_unit))} false
restore_application_activity {shlex.quote(str(previous_unit))} true
rm {shlex.quote(str(previous_unit))}
restore_application_activity {shlex.quote(str(previous_unit))} true
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                [
                    "stop file-monitor.service",
                    "restart file-monitor.service",
                    "stop file-monitor.service",
                ],
                action_log.read_text().splitlines(),
            )

            installer = (REPOSITORY_ROOT / "deployment/install.sh").read_text()
            self.assertIn('APP_WAS_ACTIVE=true', installer)
            self.assertIn(
                'restore_application_activity "$BACKUP_DIR/$APP_SERVICE" "$APP_WAS_ACTIVE"',
                installer,
            )
            self.assertIn(
                'configure_ftp_home "$FTP_USER" "$FTP_PREVIOUS_HOME"',
                installer,
            )
            self.assertIn(
                'configure_ftp_home "$FTP_USER" "$UPLOAD_ROOT"',
                installer,
            )


class DependencyLockTests(unittest.TestCase):
    def test_runtime_and_transitive_dependencies_are_exactly_pinned(self):
        expected_versions = {
            "Pillow": "12.3.0",
            "requests": "2.34.2",
            "watchdog": "6.0.0",
            "certifi": "2026.7.22",
            "charset-normalizer": "3.5.0",
            "idna": "3.18",
            "urllib3": "2.7.0",
            "rpi-lgpio": "0.6",
        }
        pinned_versions = {}
        for line in (REPOSITORY_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines():
            requirement = line.strip()
            if not requirement or requirement.startswith("#"):
                continue
            match = re.fullmatch(
                r"([A-Za-z0-9_.-]+)==([^;\s]+)(?:\s*;\s*.+)?",
                requirement,
            )
            self.assertIsNotNone(match, requirement)
            pinned_versions[match.group(1)] = match.group(2)

        self.assertEqual(expected_versions, pinned_versions)


if __name__ == "__main__":
    unittest.main()
