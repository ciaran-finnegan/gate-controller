import configparser
import os
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


def create_trusted_test_root(temporary_directory):
    trusted_root = Path(temporary_directory).resolve() / "trusted"
    trusted_root.mkdir(mode=0o700)
    trusted_root.chmod(0o700)
    return trusted_root


def read_unit(relative_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
    return parser


class CloudflareDocumentationTests(unittest.TestCase):
    def test_reolink_docs_cover_authenticated_trigger_provenance_without_a_proxy(self):
        camera = (REPOSITORY_ROOT / "docs/reolink-rlc-810a.md").read_text(
            encoding="utf-8"
        )
        environment = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("GATE_REOLINK_WEBHOOK_SECRET", environment)
        self.assertIn("GATE_REOLINK_WEBHOOK_HOST", environment)
        self.assertIn("GATE_REOLINK_WEBHOOK_PORT", environment)
        self.assertIn("http://PI_PRIVATE_ADDRESS:8766/reolink/events", camera)
        self.assertIn('"secret"', camera)
        self.assertIn("camera_ftp/unverified", camera)
        self.assertIn("sensitivity 80", camera)
        self.assertIn("does not wait", camera)
        self.assertIn("No nginx or ONVIF listener", camera)

    def test_camera_docs_distinguish_installed_810a_from_new_811a_plate_camera(self):
        installed = (REPOSITORY_ROOT / "docs/reolink-rlc-810a.md").read_text(
            encoding="utf-8"
        )
        plate_camera = (REPOSITORY_ROOT / "docs/reolink-rlc-811a.md").read_text(
            encoding="utf-8"
        )
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("The installed gate camera is an RLC-810A", installed)
        self.assertIn("reolink-rlc-811a.md", installed)
        self.assertIn("The installed camera is an RLC-810A", readme)
        self.assertIn("docs/reolink-rlc-811a.md", readme)
        self.assertIn("RLC-810A (installed)", plate_camera)
        self.assertIn("RLC-811A (new)", plate_camera)
        self.assertIn("150-250 pixels wide", plate_camera)
        self.assertIn("1/500 second", plate_camera)
        self.assertIn("distinct rule name", plate_camera)
        self.assertIn("Camera events never actuate the relay", plate_camera)
        self.assertIn("The controller assumes one camera", plate_camera)
        self.assertIn("one camera at a time", plate_camera)
        self.assertIn("MTX_PATHS_CAMERA_SOURCE", plate_camera)
        self.assertNotIn("already tolerate frames from two cameras", plate_camera)
        self.assertNotIn("open_gate", plate_camera)

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

    def test_deployment_docs_cover_safe_cloudflared_transport_activation(self):
        deployment = (REPOSITORY_ROOT / "docs/deployment.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("30-second bound", deployment)
        self.assertRegex(
            deployment,
            r"An inactive or\s+absent service\s+is never started",
        )
        self.assertIn("exact previous drop-in, including its absence", deployment)
        self.assertIn("PID 1 job", deployment)
        self.assertIn("unchanged drop-in does not restart", deployment)
        self.assertIn("rollback diagnostics", deployment)

    def test_deployment_docs_require_rollback_before_decommission(self):
        deployment = (REPOSITORY_ROOT / "docs/deployment.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("restore the previous release before removing", deployment)
        self.assertIn("decommissioning the prior service", deployment)

    def test_camera_docs_run_pi_validation_with_a_safe_non_actuating_harness(self):
        camera = (REPOSITORY_ROOT / "docs/reolink-rlc-810a.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("local-network SSH or directly on the Pi", camera)
        self.assertIn("Tailscale availability is not a\ndeployment prerequisite", camera)
        self.assertIn("default non-actuating command", camera)
        self.assertIn("passive endpoint probes", camera)
        self.assertIn("--skip-network", camera)
        self.assertNotIn("--actuate", camera)
        self.assertNotIn("open_gate", camera)
        self.assertNotIn("skipped_until_tailscale_or_home_wifi", camera)

    def test_historical_plan_uses_the_current_performance_summary_schema(self):
        plan = (
            REPOSITORY_ROOT
            / "docs/superpowers/plans/2026-08-17-cloudflare-replatform.md"
        ).read_text(encoding="utf-8")

        self.assertIn('"run_mode": "host_metrics_only"', plan)
        self.assertNotIn("actuation_requested", plan)
        self.assertNotIn("pi_ssh_tests", plan)
        self.assertNotIn("skipped_until_tailscale_or_home_wifi", plan)

    def test_performance_docs_describe_only_non_actuating_modes(self):
        performance = (
            REPOSITORY_ROOT / "docs/pi-cloudflare-performance.md"
        ).read_text(encoding="utf-8")

        self.assertIn("only passive `GET` requests", performance)
        self.assertIn("never sends a command", performance)
        self.assertNotIn("--actuate", performance)
        self.assertNotIn("open_gate", performance)


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

    def test_application_service_uses_the_bounded_low_latency_quiet_window(self):
        service = read_unit("file-monitor.service")["Service"]
        command = shlex.split(service["ExecStart"])

        self.assertEqual(["--quiet-window", "0.2"], command[-2:])

    def test_application_service_leaves_webhook_network_overrides_to_environment_file(self):
        environment = read_unit("file-monitor.service")["Service"].get("Environment", "")

        self.assertNotIn("GATE_REOLINK_WEBHOOK_HOST", environment)
        self.assertNotIn("GATE_REOLINK_WEBHOOK_PORT", environment)

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
    def test_fixed_media_test_root_is_owner_only_intermediate_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            trusted_root = create_trusted_test_root(temporary_directory)

            self.assertEqual(temporary_root, trusted_root.parent)
            self.assertEqual(os.geteuid(), trusted_root.stat().st_uid)
            self.assertEqual(0o700, trusted_root.stat().st_mode & 0o777)

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
            for relative_path in (
                "deployment/install-media.sh",
                "deployment/media/mediamtx.yml",
                "deployment/media/nginx-whep-locations.conf.template",
                "deployment/systemd/gate-media-auth.service",
                "deployment/systemd/gate-media-gateway.service",
                "deployment/systemd/gate-media-transcoder.service",
                "deployment/systemd/gate-media-turn-refresh.service",
                "deployment/systemd/gate-media-turn-refresh.timer",
                "deployment/gate_media_turn_refresh.py",
                "gate_media_config.py",
                "gate_media_auth/__init__.py",
                "gate_media_auth/__main__.py",
                "gate_media_auth/token.py",
                "gate_media_auth/capabilities.py",
                "gate_media_gateway/__init__.py",
                "gate_media_gateway/__main__.py",
                "gate_media_transcoder/__init__.py",
                "gate_media_transcoder/__main__.py",
            ):
                path = source / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("trusted media artifact\n", encoding="utf-8")
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

    def test_fixed_media_bootstrap_includes_turn_refresh_and_transcoder_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bootstrap = Path(temporary_directory) / "gate-media-bootstrap"
            command = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
MEDIA_BOOTSTRAP_ROOT={shlex.quote(str(bootstrap))}
install_fixed_media_bootstrap {shlex.quote(str(REPOSITORY_ROOT))}
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                (REPOSITORY_ROOT / "deployment/gate_media_turn_refresh.py").read_bytes(),
                (bootstrap / "gate_media_turn_refresh.py").read_bytes(),
            )
            self.assertEqual(0o700, (bootstrap / "gate_media_turn_refresh.py").stat().st_mode & 0o777)
            for name in ("gate-media-turn-refresh.service", "gate-media-turn-refresh.timer"):
                self.assertTrue((bootstrap / name).is_file(), name)
            self.assertEqual(
                (REPOSITORY_ROOT / "deployment/systemd/gate-media-transcoder.service").read_bytes(),
                (bootstrap / "gate-media-transcoder.service").read_bytes(),
            )
            for name in ("__init__.py", "__main__.py"):
                self.assertEqual(
                    (REPOSITORY_ROOT / "gate_media_transcoder" / name).read_bytes(),
                    (bootstrap / "gate_media_transcoder" / name).read_bytes(),
                )
            updater = (
                REPOSITORY_ROOT / "deployment/gate_controller_updater.py"
            ).read_text(encoding="utf-8")
            self.assertNotIn("gate-media-transcoder", updater)
            self.assertNotIn("gate_media_transcoder", updater)

    def test_fixed_media_bootstrap_is_installed_from_immutable_handoff(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            handoff = root / "handoff"
            bootstrap = root / "gate-media-bootstrap"
            artifacts = {
                "deployment/gate_controller_updater.py": "updater\n",
                "file-monitor.service": "application\n",
                "deployment/systemd/gate-controller-updater.service": "updater service\n",
                "deployment/systemd/gate-controller-updater.timer": "updater timer\n",
                "deployment/systemd/cloudflared.service.d/20-http2.conf": "cloudflared\n",
                "deployment/install-media.sh": "#!/bin/sh\n",
                "deployment/media/mediamtx.yml": "paths: {}\n",
                "deployment/media/nginx-whep-locations.conf.template": "location / {}\n",
                "deployment/systemd/gate-media-auth.service": "auth service\n",
                "deployment/systemd/gate-media-gateway.service": "gateway service\n",
                "deployment/systemd/gate-media-transcoder.service": "transcoder service\n",
                "deployment/systemd/gate-media-turn-refresh.service": "refresh service\n",
                "deployment/systemd/gate-media-turn-refresh.timer": "refresh timer\n",
                "deployment/gate_media_turn_refresh.py": "refresh helper\n",
                "gate_media_config.py": "media config\n",
                "gate_media_auth/__init__.py": "auth init\n",
                "gate_media_auth/__main__.py": "auth main\n",
                "gate_media_auth/token.py": "auth token\n",
                "gate_media_auth/capabilities.py": "auth capabilities\n",
                "gate_media_gateway/__init__.py": "gateway init\n",
                "gate_media_gateway/__main__.py": "gateway main\n",
                "gate_media_transcoder/__init__.py": "transcoder init\n",
                "gate_media_transcoder/__main__.py": "transcoder main\n",
            }
            for relative_path, contents in artifacts.items():
                path = source / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")

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
find {shlex.quote(str(source))} -type f -exec sh -c 'printf tampered >"$1"' _ {{}} \\;
MEDIA_BOOTSTRAP_ROOT={shlex.quote(str(bootstrap))}
install_fixed_media_bootstrap {shlex.quote(str(handoff))}
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
            for relative_path, contents in artifacts.items():
                handed_off = handoff / relative_path
                self.assertEqual(contents, handed_off.read_text(encoding="utf-8"))
                self.assertEqual(0o444, handed_off.stat().st_mode & 0o777)
            self.assertEqual(0o555, handoff.stat().st_mode & 0o777)
            self.assertEqual(0o555, (handoff / "deployment").stat().st_mode & 0o777)
            self.assertEqual(
                "#!/bin/sh\n",
                (bootstrap / "install-media.sh").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "auth capabilities\n",
                (bootstrap / "gate_media_auth/capabilities.py").read_text(
                    encoding="utf-8"
                ),
            )

    def test_fixed_media_handoff_rejects_symbolic_link_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            handoff = root / "handoff"
            artifact_paths = (
                "deployment/gate_controller_updater.py",
                "file-monitor.service",
                "deployment/systemd/gate-controller-updater.service",
                "deployment/systemd/gate-controller-updater.timer",
                "deployment/systemd/cloudflared.service.d/20-http2.conf",
                "deployment/install-media.sh",
                "deployment/media/mediamtx.yml",
                "deployment/media/nginx-whep-locations.conf.template",
                "deployment/systemd/gate-media-auth.service",
                "deployment/systemd/gate-media-gateway.service",
                "deployment/systemd/gate-media-transcoder.service",
                "deployment/systemd/gate-media-turn-refresh.service",
                "deployment/systemd/gate-media-turn-refresh.timer",
                "deployment/gate_media_turn_refresh.py",
                "gate_media_config.py",
                "gate_media_auth/__init__.py",
                "gate_media_auth/__main__.py",
                "gate_media_auth/token.py",
                "gate_media_auth/capabilities.py",
                "gate_media_gateway/__init__.py",
                "gate_media_gateway/__main__.py",
                "gate_media_transcoder/__init__.py",
                "gate_media_transcoder/__main__.py",
            )
            for relative_path in artifact_paths:
                path = source / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("trusted\n", encoding="utf-8")
            (source / "gate_media_config.py").unlink()
            (source / "gate_media_config.py").symlink_to(root / "outside")

            command = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
set +e
create_fixed_trust_anchor_handoff {shlex.quote(str(source))} {shlex.quote(str(handoff))}
status=$?
set -e
printf 'status=%s\\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertIn("must be a regular file", completed.stderr)

    def test_fixed_media_backup_records_absence_when_parent_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap_parent = root / "libexec"
            bootstrap = bootstrap_parent / "gate-media-bootstrap"
            backup = root / "backup"
            backup.mkdir()

            command = f"""
source deployment/install.sh
backup_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
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
            self.assertTrue((backup / "fixed-media-bootstrap.absent").is_file())
            self.assertTrue(bootstrap_parent.is_dir())
            self.assertEqual(os.geteuid(), bootstrap_parent.stat().st_uid)
            self.assertEqual(0, bootstrap_parent.stat().st_mode & 0o022)

    def test_fixed_media_bootstrap_restore_reinstates_the_exact_prior_tree(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            backup.mkdir()
            (bootstrap / "gate_media_auth").mkdir(parents=True)
            (bootstrap / "install-media.sh").write_text("previous bootstrap\n")
            (bootstrap / "gate_media_auth/token.py").write_text("previous token\n")
            (bootstrap / "gate_media_auth/token.py").chmod(0o600)

            command = f"""
source deployment/install.sh
MEDIA_BOOTSTRAP_ROOT={shlex.quote(str(bootstrap))}
backup_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" {shlex.quote(str(backup))}
rm -f -- {shlex.quote(str(bootstrap / 'gate_media_auth/token.py'))}
printf 'candidate only\\n' > {shlex.quote(str(bootstrap / 'candidate-only'))}
printf 'candidate replacement\\n' > {shlex.quote(str(bootstrap / 'install-media.sh'))}
restore_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" {shlex.quote(str(backup))}
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
                "previous bootstrap\n",
                (bootstrap / "install-media.sh").read_text(encoding="utf-8"),
            )
            token = bootstrap / "gate_media_auth/token.py"
            self.assertEqual("previous token\n", token.read_text(encoding="utf-8"))
            self.assertEqual(0o600, token.stat().st_mode & 0o777)
            self.assertFalse((bootstrap / "candidate-only").exists())

    def test_fixed_media_restore_publication_failure_preserves_live_candidate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            backup.mkdir()
            bootstrap.mkdir()
            (bootstrap / "live-candidate").write_text(
                "candidate remains available\n", encoding="utf-8"
            )
            (backup / "fixed-media-bootstrap").mkdir()
            (backup / "fixed-media-bootstrap/restored").write_text(
                "previous bootstrap\n", encoding="utf-8"
            )
            command = f"""
source deployment/install.sh
publish_fixed_media_restore() {{ return 23; }}
set +e
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertEqual(
                "candidate remains available\n",
                (bootstrap / "live-candidate").read_text(encoding="utf-8"),
            )
            self.assertFalse((bootstrap / "restored").exists())
            generations = list(root.glob("gate-media-bootstrap.rollback.*"))
            self.assertEqual(1, len(generations))
            self.assertEqual(
                "previous bootstrap\n",
                (generations[0] / "restored").read_text(encoding="utf-8"),
            )

    def test_fixed_media_post_exchange_failure_preserves_both_generations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            backup.mkdir()
            bootstrap.mkdir()
            (bootstrap / "live-candidate").write_text(
                "old candidate survives\n", encoding="utf-8"
            )
            (backup / "fixed-media-bootstrap").mkdir()
            (backup / "fixed-media-bootstrap/restored").write_text(
                "new live restore\n", encoding="utf-8"
            )
            command = f"""
source deployment/install.sh
publish_fixed_media_restore() {{
  python3 - "$@" <<'PY'
import os
import errno
import sys

parent_descriptor = int(sys.argv[1])
live_name = sys.argv[2]
staged_name = sys.argv[3]
holder_name = f"{{live_name}}.test-exchange-holder"
os.rename(
    live_name,
    holder_name,
    src_dir_fd=parent_descriptor,
    dst_dir_fd=parent_descriptor,
)
os.rename(
    staged_name,
    live_name,
    src_dir_fd=parent_descriptor,
    dst_dir_fd=parent_descriptor,
)
os.rename(
    holder_name,
    staged_name,
    src_dir_fd=parent_descriptor,
    dst_dir_fd=parent_descriptor,
)
raise OSError(errno.EIO, "simulated parent fsync failure after exchange")
PY
  return 37
}}
set +e
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertEqual(
                "new live restore\n",
                (bootstrap / "restored").read_text(encoding="utf-8"),
            )
            generations = list(root.glob("gate-media-bootstrap.rollback.*"))
            self.assertEqual(1, len(generations))
            self.assertEqual(
                "old candidate survives\n",
                (generations[0] / "live-candidate").read_text(encoding="utf-8"),
            )

            discovery = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"""
source deployment/install.sh
pin_trusted_directory {shlex.quote(str(root))} 18
list_fixed_media_stale_generations 18 gate-media-bootstrap
""",
                ],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(0, discovery.returncode, discovery.stderr)
            self.assertEqual(f"{generations[0].name}\n", discovery.stdout)

    def test_fixed_media_absence_interruption_retains_renamed_candidate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            bootstrap.mkdir()
            backup.mkdir()
            (bootstrap / "live-candidate").write_text(
                "candidate retained in quarantine\n", encoding="utf-8"
            )
            (backup / "fixed-media-bootstrap.absent").touch()
            command = f"""
source deployment/install.sh
publish_fixed_media_absence() {{
  python3 - "$@" <<'PY'
import os
import sys

parent_descriptor = int(sys.argv[1])
os.rename(
    sys.argv[2],
    sys.argv[3],
    src_dir_fd=parent_descriptor,
    dst_dir_fd=parent_descriptor,
)
PY
  return 23
}}
set +e
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertFalse(bootstrap.exists())
            quarantines = list(root.glob("gate-media-bootstrap.quarantine.*"))
            self.assertEqual(1, len(quarantines))
            self.assertEqual(
                "candidate retained in quarantine\n",
                (quarantines[0] / "live-candidate").read_text(encoding="utf-8"),
            )

    def test_fixed_media_absence_removal_failure_retains_durable_quarantine(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            bootstrap.mkdir()
            backup.mkdir()
            (bootstrap / "live-candidate").write_text(
                "candidate retained in quarantine\n", encoding="utf-8"
            )
            (backup / "fixed-media-bootstrap.absent").touch()
            command = f"""
source deployment/install.sh
remove_fixed_media_quarantine() {{ return 29; }}
set +e
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertFalse(bootstrap.exists())
            quarantines = list(root.glob("gate-media-bootstrap.quarantine.*"))
            self.assertEqual(1, len(quarantines))
            self.assertEqual(
                "candidate retained in quarantine\n",
                (quarantines[0] / "live-candidate").read_text(encoding="utf-8"),
            )

    def test_fixed_media_restore_requires_staged_tree_fsync_before_publish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            actions = root / "actions"
            bootstrap.mkdir()
            (backup / "fixed-media-bootstrap/nested").mkdir(parents=True)
            (bootstrap / "live-candidate").write_text(
                "candidate remains available\n", encoding="utf-8"
            )
            (backup / "fixed-media-bootstrap/nested/restored").write_text(
                "previous bootstrap\n", encoding="utf-8"
            )
            command = f"""
source deployment/install.sh
fsync_fixed_media_tree() {{
  printf 'fsync %s\n' "$2" >>{shlex.quote(str(actions))}
  return 31
}}
publish_fixed_media_restore() {{
  printf 'publish\n' >>{shlex.quote(str(actions))}
}}
set +e
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertEqual(
                ["fsync gate-media-bootstrap.rollback."],
                [
                    re.sub(r"\d+$", "", line)
                    for line in actions.read_text().splitlines()
                ],
            )
            self.assertEqual(
                "candidate remains available\n",
                (bootstrap / "live-candidate").read_text(encoding="utf-8"),
            )

    def test_fixed_media_restore_cleans_stale_generations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            stale_rollback = root / "gate-media-bootstrap.rollback.111"
            stale_quarantine = root / "gate-media-bootstrap.quarantine.222"
            bootstrap.mkdir()
            (backup / "fixed-media-bootstrap").mkdir(parents=True)
            stale_rollback.mkdir()
            stale_quarantine.mkdir()
            (bootstrap / "candidate").write_text("candidate\n", encoding="utf-8")
            (backup / "fixed-media-bootstrap/previous").write_text(
                "previous bootstrap\n", encoding="utf-8"
            )
            (stale_rollback / "stale").write_text("stale\n", encoding="utf-8")
            (stale_quarantine / "stale").write_text("stale\n", encoding="utf-8")
            command = f"""
source deployment/install.sh
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
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
            self.assertFalse(stale_rollback.exists())
            self.assertFalse(stale_quarantine.exists())
            self.assertEqual(
                "previous bootstrap\n",
                (bootstrap / "previous").read_text(encoding="utf-8"),
            )

    def test_fixed_media_restore_rejects_unsafe_stale_generation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            sentinel = root / "sentinel"
            stale = root / "gate-media-bootstrap.rollback.111"
            bootstrap.mkdir()
            (backup / "fixed-media-bootstrap").mkdir(parents=True)
            sentinel.write_text("sentinel unchanged\n", encoding="utf-8")
            stale.symlink_to(sentinel)
            (bootstrap / "candidate").write_text("candidate\n", encoding="utf-8")
            command = f"""
source deployment/install.sh
set +e
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertTrue(stale.is_symlink())
            self.assertEqual("sentinel unchanged\n", sentinel.read_text())
            self.assertEqual("candidate\n", (bootstrap / "candidate").read_text())

    def test_fixed_media_backup_rejects_writable_parent_before_child_swap(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap_parent = root / "libexec"
            bootstrap = bootstrap_parent / "gate-media-bootstrap"
            original = bootstrap_parent / "trusted-original"
            attacker = bootstrap_parent / "attacker-bootstrap"
            backup = root / "backup"
            hook_called = root / "hook-called"
            bootstrap.mkdir(parents=True)
            attacker.mkdir()
            backup.mkdir()
            bootstrap_parent.chmod(0o777)
            (bootstrap / "identity").write_text("trusted tree\n", encoding="utf-8")
            (attacker / "identity").write_text("attacker tree\n", encoding="utf-8")
            command = f"""
source deployment/install.sh
copy_fixed_media_tree() {{
  : >{shlex.quote(str(hook_called))}
  command mv {shlex.quote(str(bootstrap))} {shlex.quote(str(original))}
  command mv {shlex.quote(str(attacker))} {shlex.quote(str(bootstrap))}
  copy_fixed_media_tree_real "$@"
}}
set +e
backup_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertFalse(hook_called.exists())
            self.assertEqual("trusted tree\n", (bootstrap / "identity").read_text())
            self.assertFalse((backup / "fixed-media-bootstrap").exists())

    def test_fixed_media_backup_rejects_writable_backup_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            bootstrap.mkdir()
            backup.mkdir()
            backup.chmod(0o777)
            (bootstrap / "identity").write_text("trusted tree\n", encoding="utf-8")
            command = f"""
source deployment/install.sh
set +e
backup_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertFalse((backup / "fixed-media-bootstrap").exists())

    def test_fixed_media_backup_and_restore_reject_symlinked_parent(self):
        for operation in ("backup", "restore"):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = create_trusted_test_root(temporary_directory)
                    actual_parent = root / "actual-parent"
                    linked_parent = root / "linked-parent"
                    bootstrap = linked_parent / "gate-media-bootstrap"
                    backup = root / "backup"
                    actual_bootstrap = actual_parent / "gate-media-bootstrap"
                    actual_bootstrap.mkdir(parents=True)
                    backup.mkdir()
                    linked_parent.symlink_to(actual_parent, target_is_directory=True)
                    (actual_bootstrap / "candidate").write_text(
                        "candidate unchanged\n", encoding="utf-8"
                    )
                    if operation == "restore":
                        (backup / "fixed-media-bootstrap").mkdir()
                        (backup / "fixed-media-bootstrap/previous").write_text(
                            "previous bootstrap\n", encoding="utf-8"
                        )
                        invocation = "restore_fixed_media_bootstrap"
                    else:
                        invocation = "backup_fixed_media_bootstrap"
                    command = f"""
source deployment/install.sh
set +e
{invocation} {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\\n' "$status"
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
                    self.assertEqual("status=1\n", completed.stdout)
                    self.assertEqual(
                        "candidate unchanged\n",
                        (actual_bootstrap / "candidate").read_text(
                            encoding="utf-8"
                        ),
                    )
                    if operation == "backup":
                        self.assertFalse(
                            (backup / "fixed-media-bootstrap").exists()
                        )

    def test_fixed_media_backup_pins_parent_across_path_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap_parent = root / "bootstrap-parent"
            relocated_parent = root / "relocated-parent"
            attacker_parent = root / "attacker-parent"
            bootstrap = bootstrap_parent / "gate-media-bootstrap"
            attacker_bootstrap = attacker_parent / "gate-media-bootstrap"
            backup = root / "backup"
            bootstrap.mkdir(parents=True)
            attacker_bootstrap.mkdir(parents=True)
            backup.mkdir()
            (bootstrap / "identity").write_text("trusted tree\n", encoding="utf-8")
            (attacker_bootstrap / "identity").write_text(
                "redirected tree\n", encoding="utf-8"
            )
            command = f"""
source deployment/install.sh
copy_fixed_media_tree() {{
  command mv \
    {shlex.quote(str(bootstrap_parent))} {shlex.quote(str(relocated_parent))}
  command ln -s \
    {shlex.quote(str(attacker_parent))} {shlex.quote(str(bootstrap_parent))}
  copy_fixed_media_tree_real "$@"
}}
backup_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
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
            self.assertTrue(bootstrap_parent.is_symlink())
            self.assertEqual(
                "trusted tree\n",
                (backup / "fixed-media-bootstrap/identity").read_text(
                    encoding="utf-8"
                ),
            )

    def test_fixed_media_restore_pins_parent_across_path_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap_parent = root / "bootstrap-parent"
            relocated_parent = root / "relocated-parent"
            attacker_parent = root / "attacker-parent"
            bootstrap = bootstrap_parent / "gate-media-bootstrap"
            attacker_bootstrap = attacker_parent / "gate-media-bootstrap"
            backup = root / "backup"
            bootstrap.mkdir(parents=True)
            attacker_bootstrap.mkdir(parents=True)
            (backup / "fixed-media-bootstrap").mkdir(parents=True)
            (bootstrap / "identity").write_text("live candidate\n", encoding="utf-8")
            (attacker_bootstrap / "identity").write_text(
                "attacker tree\n", encoding="utf-8"
            )
            (backup / "fixed-media-bootstrap/identity").write_text(
                "previous tree\n", encoding="utf-8"
            )
            command = f"""
source deployment/install.sh
copy_fixed_media_tree() {{
  command mv \
    {shlex.quote(str(bootstrap_parent))} {shlex.quote(str(relocated_parent))}
  command ln -s \
    {shlex.quote(str(attacker_parent))} {shlex.quote(str(bootstrap_parent))}
  copy_fixed_media_tree_real "$@"
}}
restore_fixed_media_bootstrap \
  {shlex.quote(str(bootstrap))} {shlex.quote(str(backup))}
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
                "previous tree\n",
                (relocated_parent / "gate-media-bootstrap/identity").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                "attacker tree\n",
                (attacker_bootstrap / "identity").read_text(encoding="utf-8"),
            )

    def test_fixed_media_bootstrap_restore_reinstates_prior_absence(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = create_trusted_test_root(temporary_directory)
            bootstrap = root / "gate-media-bootstrap"
            backup = root / "backup"
            backup.mkdir()

            command = f"""
source deployment/install.sh
MEDIA_BOOTSTRAP_ROOT={shlex.quote(str(bootstrap))}
backup_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" {shlex.quote(str(backup))}
mkdir -p {shlex.quote(str(bootstrap))}
printf 'candidate only\\n' > {shlex.quote(str(bootstrap / 'candidate-only'))}
restore_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" {shlex.quote(str(backup))}
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
            self.assertTrue((backup / "fixed-media-bootstrap.absent").is_file())
            self.assertFalse(bootstrap.exists())

    def test_fixed_media_bootstrap_restore_rejects_unsafe_paths(self):
        for unsafe_state in ("destination-symlink", "destination-file", "backup-symlink", "backup-file", "temporary-symlink"):
            with self.subTest(unsafe_state=unsafe_state):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = create_trusted_test_root(temporary_directory)
                    bootstrap = root / "gate-media-bootstrap"
                    backup = root / "backup"
                    sentinel = root / "sentinel"
                    backup.mkdir()
                    sentinel.write_text("sentinel unchanged\n", encoding="utf-8")
                    if unsafe_state == "destination-symlink":
                        bootstrap.symlink_to(sentinel)
                        (backup / "fixed-media-bootstrap").mkdir()
                    elif unsafe_state == "destination-file":
                        bootstrap.write_text("candidate file\n", encoding="utf-8")
                        (backup / "fixed-media-bootstrap").mkdir()
                    else:
                        bootstrap.mkdir()
                        (bootstrap / "candidate").write_text("candidate\n")
                        if unsafe_state == "backup-symlink":
                            (backup / "fixed-media-bootstrap").symlink_to(sentinel)
                        elif unsafe_state == "backup-file":
                            (backup / "fixed-media-bootstrap").write_text(
                                "not a directory\n", encoding="utf-8"
                            )
                        else:
                            (backup / "fixed-media-bootstrap").mkdir()

                    temporary_setup = ""
                    if unsafe_state == "temporary-symlink":
                        temporary_setup = (
                            f"ln -s {shlex.quote(str(sentinel))} "
                            '"$MEDIA_BOOTSTRAP_ROOT.rollback.$$"'
                        )
                    command = f"""
source deployment/install.sh
MEDIA_BOOTSTRAP_ROOT={shlex.quote(str(bootstrap))}
{temporary_setup}
set +e
restore_fixed_media_bootstrap "$MEDIA_BOOTSTRAP_ROOT" {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\\n' "$status"
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
                    self.assertEqual("status=1\n", completed.stdout)
                    self.assertEqual("sentinel unchanged\n", sentinel.read_text())
                    if unsafe_state == "temporary-symlink":
                        self.assertTrue(
                            any(
                                path.is_symlink()
                                for path in root.glob(
                                    "gate-media-bootstrap.rollback.*"
                                )
                            )
                        )

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

    def test_fixed_helper_restore_fails_when_backup_state_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helper = root / "gate-controller-updater.py"
            helper.write_text("candidate helper\n", encoding="utf-8")
            backup = root / "backup"
            backup.mkdir()
            command = f"""
source deployment/install.sh
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

            self.assertNotEqual(0, completed.returncode)
            self.assertEqual("candidate helper\n", helper.read_text(encoding="utf-8"))
            self.assertIn("updater helper backup is missing", completed.stderr)

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
            daily_uploads.chmod(0o755)
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
find() {{
  printf 'find %s\n' "$*" >> {shlex.quote(str(account_log))}
  local -a forwarded=("$@")
  if [[ $3 == d && $6 == g+rwx,g+s,o-rwx ]]; then
    forwarded[5]=g+rwx,o-rwx
  fi
  command find "${{forwarded[@]}}"
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
            self.assertEqual(0o770, daily_uploads.stat().st_mode & 0o7777)
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
            self.assertIn(
                f"find {uploads} -type d -exec chmod g+rwx,g+s,o-rwx {{}} +",
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

    def test_inherited_err_trap_leaves_rollback_to_the_owner_process(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            systemd_root = root / "systemd"
            action_log = root / "actions.log"
            rollback_systemctl_log = root / "rollback-systemctl.log"
            host_systemctl_log = root / "host-systemctl.log"
            fake_bin = root / "bin"
            backup.mkdir()
            systemd_root.mkdir()
            fake_bin.mkdir()
            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(host_systemctl_log))}\n"
                "exit 97\n",
                encoding="utf-8",
            )
            fake_systemctl.chmod(0o755)
            fake_timeout = fake_bin / "timeout"
            fake_timeout.write_text(
                "#!/bin/sh\n"
                "shift 3\n"
                "exec \"$@\"\n",
                encoding="utf-8",
            )
            fake_timeout.chmod(0o755)
            previous_unit = backup / "file-monitor.service"
            installed_unit = systemd_root / "file-monitor.service"
            previous_unit.write_text("previous unit\n", encoding="utf-8")
            installed_unit.write_text("candidate unit\n", encoding="utf-8")
            command = f"""
source deployment/install.sh
restore_current_release() {{ printf 'restore current release\n' >> {shlex.quote(str(action_log))}; }}
run_systemctl_bounded() {{
  printf '%s\n' "$*" >> {shlex.quote(str(rollback_systemctl_log))}
  [[ "$*" == daemon-reload ]]
}}
restore_fixed_updater_helper() {{ :; }}
restore_fixed_media_bootstrap() {{ :; }}
restore_cloudflared_transport_with_diagnostics() {{ :; }}
configure_ftp_home() {{ :; }}
restore_application_activity() {{ :; }}
restore_legacy_command_activity() {{ :; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
STAGING=
SYSTEMD_ROOT={shlex.quote(str(systemd_root))}
UPDATER_WAS_ENABLED=true
APP_WAS_ENABLED=true
ROLLBACK_OWNER_SUBSHELL=$BASH_SUBSHELL
trap rollback ERR
trigger_nested_failure() {{
  local output
  output=$(false)
}}
trigger_nested_failure
"""
            environment = dict(os.environ)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertTrue(installed_unit.is_file(), completed.stderr)
            self.assertEqual(
                "previous unit\n", installed_unit.read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["restore current release"],
                action_log.read_text(encoding="utf-8").splitlines(),
            )
            self.assertEqual(
                ["daemon-reload"],
                rollback_systemctl_log.read_text(encoding="utf-8").splitlines(),
            )
            self.assertFalse(host_systemctl_log.exists())

    def test_rollback_reloads_restored_units_before_cloud_or_service_actions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            backup.mkdir()
            action_log = root / "actions.log"
            command = f"""
source deployment/install.sh
record_action() {{ printf '%s\n' "$1" >> {shlex.quote(str(action_log))}; }}
restore_current_release() {{ record_action 'restore current release'; }}
restore_path() {{ record_action "restore $1"; }}
restore_fixed_updater_helper() {{ record_action 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{ record_action 'restore fixed media bootstrap'; }}
run_systemctl_bounded() {{ record_action "$*"; }}
restore_cloudflared_transport_with_diagnostics() {{
  record_action 'restore cloudflared transport before its reload'
  return 1
}}
configure_ftp_home() {{ record_action 'restore FTP home'; }}
restore_application_activity() {{ record_action 'restart application'; }}
restore_legacy_command_activity() {{ record_action 'restart legacy command'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
STAGING=
UPDATER_WAS_ENABLED=true
APP_WAS_ENABLED=true
set +e
false
rollback
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            actions = action_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("daemon-reload", actions)
            reload_index = actions.index("daemon-reload")
            self.assertLess(
                reload_index,
                actions.index("restore cloudflared transport before its reload"),
            )
            self.assertNotIn("restart application", actions)
            self.assertNotIn("restart legacy command", actions)

    def test_critical_restore_failure_blocks_all_later_service_actions(self):
        prerequisites = [
            "restore current release",
            "restore file-monitor.service",
            "restore gate-command-server.service",
            "restore gate-controller-updater.service",
            "restore gate-controller-updater.timer",
            "restore fixed updater helper",
            "restore fixed media bootstrap",
        ]
        independent_restores = [
            *prerequisites,
            "reload restored systemd units",
            "restore cloudflared files",
            "restore FTP home",
        ]
        unsafe_actions = [
            "unsafe cloudflared service action",
            "unsafe timer enablement action",
            "unsafe timer activity action",
            "unsafe application enablement action",
            "unsafe application activity",
            "unsafe legacy activity",
        ]

        for failed_prerequisite in prerequisites:
            with self.subTest(failed_prerequisite=failed_prerequisite):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    backup = root / "backup"
                    backup.mkdir()
                    action_log = root / "actions.log"
                    command = f"""
source deployment/install.sh
FAIL_STEP={shlex.quote(failed_prerequisite)}
record_restore() {{
  printf '%s\n' "$1" >> {shlex.quote(str(action_log))}
  [[ $1 != "$FAIL_STEP" ]]
}}
restore_current_release() {{ record_restore 'restore current release'; }}
restore_path() {{ record_restore "restore $1"; }}
run_systemctl_bounded() {{ record_restore 'reload restored systemd units'; }}
restore_fixed_updater_helper() {{ record_restore 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{ record_restore 'restore fixed media bootstrap'; }}
restore_cloudflared_transport_with_diagnostics() {{
  record_restore 'unsafe cloudflared service action'
}}
restore_cloudflared_transport_drop_in() {{ record_restore 'restore cloudflared files'; }}
configure_ftp_home() {{ record_restore 'restore FTP home'; }}
restore_unit_enablement() {{ record_restore 'unsafe timer enablement action'; }}
restore_unit_activity() {{ record_restore 'unsafe timer activity action'; }}
systemctl() {{ record_restore 'unsafe application enablement action'; }}
restore_application_activity() {{ record_restore 'unsafe application activity'; }}
restore_legacy_command_activity() {{ record_restore 'unsafe legacy activity'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
: >"$BACKUP_DIR/$UPDATER_TIMER"
STAGING=
APP_WAS_ENABLED=false
set +e
false
rollback
"""

                    completed = subprocess.run(
                        ["bash", "-c", command],
                        cwd=REPOSITORY_ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(1, completed.returncode, completed.stderr)
                    actions = action_log.read_text(encoding="utf-8").splitlines()
                    for expected_restore in independent_restores:
                        self.assertIn(expected_restore, actions)
                    for unsafe_action in unsafe_actions:
                        self.assertNotIn(unsafe_action, actions)
                    self.assertIn(
                        f"Rollback step failed: {failed_prerequisite}",
                        (backup / "rollback-error.log").read_text(encoding="utf-8"),
                    )

    def test_media_bootstrap_restore_failure_blocks_service_state_restoration(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            action_log = root / "actions.log"
            backup.mkdir()
            command = f"""
source deployment/install.sh
record_action() {{ printf '%s\\n' "$1" >> {shlex.quote(str(action_log))}; }}
restore_current_release() {{ record_action 'restore current release'; }}
restore_path() {{ record_action "restore $1"; }}
run_systemctl_bounded() {{ record_action 'reload restored systemd units'; }}
restore_fixed_updater_helper() {{ record_action 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{
  record_action 'restore fixed media bootstrap'
  return 1
}}
restore_cloudflared_transport_with_diagnostics() {{
  record_action 'unsafe cloudflared service action'
}}
restore_cloudflared_transport_drop_in() {{ record_action 'restore cloudflared files'; }}
configure_ftp_home() {{ record_action 'restore FTP home'; }}
restore_unit_enablement() {{ record_action 'unsafe timer enablement action'; }}
restore_unit_activity() {{ record_action 'unsafe timer activity action'; }}
systemctl() {{ record_action 'unsafe application enablement action'; }}
restore_application_activity() {{ record_action 'unsafe application activity'; }}
restore_legacy_command_activity() {{ record_action 'unsafe legacy activity'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
: >"$BACKUP_DIR/$UPDATER_TIMER"
STAGING=
APP_WAS_ENABLED=false
set +e
false
rollback
"""
            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            actions = action_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("restore fixed media bootstrap", actions)
            self.assertIn("restore cloudflared files", actions)
            self.assertIn("restore FTP home", actions)
            for unsafe_action in (
                "unsafe cloudflared service action",
                "unsafe timer enablement action",
                "unsafe timer activity action",
                "unsafe application enablement action",
                "unsafe application activity",
                "unsafe legacy activity",
            ):
                self.assertNotIn(unsafe_action, actions)
            self.assertIn(
                "Rollback step failed: restore fixed media bootstrap",
                (backup / "rollback-error.log").read_text(encoding="utf-8"),
            )

    def test_restore_path_rejects_preexisting_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            systemd_root = root / "systemd"
            backup.mkdir()
            systemd_root.mkdir()
            destination = systemd_root / "file-monitor.service"
            sentinel = root / "sentinel"
            action_log = root / "actions.log"
            destination.write_text("candidate unit\n", encoding="utf-8")
            sentinel.write_text("sentinel unchanged\n", encoding="utf-8")
            (backup / "file-monitor.service").write_text(
                "previous unit\n", encoding="utf-8"
            )
            for unit in (
                "gate-command-server.service",
                "gate-controller-updater.service",
                "gate-controller-updater.timer",
            ):
                (backup / f"{unit}.absent").touch()
            command = f"""
source deployment/install.sh
record_action() {{ printf '%s\n' "$1" >> {shlex.quote(str(action_log))}; }}
restore_current_release() {{ :; }}
run_systemctl_bounded() {{ record_action 'reload restored systemd units'; }}
restore_fixed_updater_helper() {{ :; }}
restore_fixed_media_bootstrap() {{ :; }}
restore_cloudflared_transport_with_diagnostics() {{
  record_action 'unsafe cloudflared service action'
}}
restore_cloudflared_transport_drop_in() {{ record_action 'restore cloudflared files'; }}
configure_ftp_home() {{ record_action 'restore FTP home'; }}
restore_unit_enablement() {{ record_action 'unsafe timer enablement action'; }}
restore_application_activity() {{ record_action 'unsafe application activity'; }}
restore_legacy_command_activity() {{ record_action 'unsafe legacy activity'; }}
SYSTEMD_ROOT={shlex.quote(str(systemd_root))}
BACKUP_DIR={shlex.quote(str(backup))}
STAGING=
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
APP_WAS_ENABLED=true
ln -s {shlex.quote(str(sentinel))} "$SYSTEMD_ROOT/.$APP_SERVICE.rollback.$$"
set +e
false
rollback
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertEqual(
                "sentinel unchanged\n", sentinel.read_text(encoding="utf-8")
            )
            self.assertTrue(destination.is_file())
            self.assertFalse(destination.is_symlink())
            self.assertEqual(
                "candidate unit\n", destination.read_text(encoding="utf-8")
            )
            actions = action_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("restore cloudflared files", actions)
            self.assertIn("restore FTP home", actions)
            for unsafe_action in (
                "unsafe cloudflared service action",
                "unsafe timer enablement action",
                "unsafe application activity",
                "unsafe legacy activity",
            ):
                self.assertNotIn(unsafe_action, actions)
            self.assertIn(
                "Rollback step failed: restore file-monitor.service",
                (backup / "rollback-error.log").read_text(encoding="utf-8"),
            )

    def test_failed_unit_reload_withholds_dependent_service_actions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            backup.mkdir()
            action_log = root / "actions.log"
            command = f"""
source deployment/install.sh
record_action() {{ printf '%s\n' "$1" >> {shlex.quote(str(action_log))}; }}
restore_current_release() {{ record_action 'restore current release'; }}
restore_path() {{ record_action "restore $1"; }}
run_systemctl_bounded() {{
  record_action 'reload restored systemd units'
  return 1
}}
restore_fixed_updater_helper() {{ record_action 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{ record_action 'restore fixed media bootstrap'; }}
restore_cloudflared_transport_with_diagnostics() {{
  record_action 'unsafe cloudflared service action'
}}
restore_cloudflared_transport_drop_in() {{ record_action 'restore cloudflared files'; }}
configure_ftp_home() {{ record_action 'restore FTP home'; }}
restore_unit_enablement() {{ record_action 'unsafe timer enablement action'; }}
restore_unit_activity() {{ record_action 'unsafe timer activity action'; }}
systemctl() {{ record_action 'unsafe application enablement action'; }}
restore_application_activity() {{ record_action 'unsafe application activity'; }}
restore_legacy_command_activity() {{ record_action 'unsafe legacy activity'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
: >"$BACKUP_DIR/$UPDATER_TIMER"
STAGING=
APP_WAS_ENABLED=false
set +e
false
rollback
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            actions = action_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("restore fixed updater helper", actions)
            self.assertIn("restore cloudflared files", actions)
            self.assertIn("restore FTP home", actions)
            for unsafe_action in (
                "unsafe cloudflared service action",
                "unsafe timer enablement action",
                "unsafe timer activity action",
                "unsafe application enablement action",
                "unsafe application activity",
                "unsafe legacy activity",
            ):
                self.assertNotIn(unsafe_action, actions)
            self.assertIn(
                "Rollback step failed: reload restored systemd units",
                (backup / "rollback-error.log").read_text(encoding="utf-8"),
            )

    def test_signal_rollback_uses_conventional_nonzero_exit_status(self):
        for signal_name, expected_status in (("INT", 130), ("TERM", 143)):
            with self.subTest(signal=signal_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    backup = Path(temporary_directory) / "backup"
                    backup.mkdir()
                    command = f"""
source deployment/install.sh
ACTIVATION_STARTED=false
BACKUP_DIR={shlex.quote(str(backup))}
STAGING=
ROLLBACK_OWNER_SUBSHELL=$BASH_SUBSHELL
trap 'rollback {expected_status}' {signal_name}
kill -s {signal_name} $$
"""
                    completed = subprocess.run(
                        ["bash", "-c", command],
                        cwd=REPOSITORY_ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(
                        expected_status, completed.returncode, completed.stderr
                    )

        installer = (REPOSITORY_ROOT / "deployment/install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("trap 'rollback 130' INT", installer)
        self.assertIn("trap 'rollback 143' TERM", installer)

    def test_complete_rollback_aggregates_failures_and_retains_the_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            backup.mkdir()
            (backup / "recovery-marker").write_text("retain\n", encoding="utf-8")
            action_log = root / "actions.log"
            expected_failures = [
                "restore current release",
                "restore file-monitor.service",
                "restore gate-command-server.service",
                "restore gate-controller-updater.service",
                "restore gate-controller-updater.timer",
                "restore fixed updater helper",
                "restore cloudflared transport files",
                "restore FTP home",
            ]
            unsafe_actions = [
                "restore cloudflared transport",
                "restore updater timer enablement",
                "restore updater timer activity",
                "restore application enablement",
                "restore application activity",
                "restore legacy command activity",
            ]
            command = f"""
source deployment/install.sh
record_failure() {{
  printf '%s\n' "$1" >> {shlex.quote(str(action_log))}
  return 1
}}
ln() {{ record_failure 'restore current release'; }}
mv() {{ record_failure 'unexpected current release move'; }}
restore_path() {{ record_failure "restore $1"; }}
run_systemctl_bounded() {{
  printf '%s\n' 'reload restored systemd units' >> {shlex.quote(str(action_log))}
}}
restore_fixed_updater_helper() {{ record_failure 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{ :; }}
restore_cloudflared_transport_with_diagnostics() {{ record_failure 'restore cloudflared transport'; }}
restore_cloudflared_transport_drop_in() {{ record_failure 'restore cloudflared transport files'; }}
configure_ftp_home() {{ record_failure 'restore FTP home'; }}
restore_unit_enablement() {{ record_failure 'restore updater timer enablement'; }}
restore_unit_activity() {{ record_failure 'restore updater timer activity'; }}
systemctl() {{
  case "$*" in
    'disable file-monitor.service')
      record_failure 'restore application enablement'
      ;;
    *) return 99 ;;
  esac
}}
restore_application_activity() {{ record_failure 'restore application activity'; }}
restore_legacy_command_activity() {{ record_failure 'restore legacy command activity'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
: >"$BACKUP_DIR/$UPDATER_TIMER"
STAGING=
CURRENT_LINK={shlex.quote(str(root / 'current'))}
PREVIOUS_CURRENT={shlex.quote(str(root / 'previous'))}
SYSTEMD_ROOT={shlex.quote(str(root / 'systemd'))}
UPDATER_HELPER={shlex.quote(str(root / 'updater-helper'))}
FTP_USER=ftp-user
FTP_PREVIOUS_HOME={shlex.quote(str(root / 'ftp-home'))}
UPDATER_WAS_ENABLED=false
UPDATER_WAS_ACTIVE=false
APP_WAS_ENABLED=false
APP_WAS_ACTIVE=true
LEGACY_COMMAND_WAS_ENABLED=true
LEGACY_COMMAND_WAS_ACTIVE=true
set +e
false
rollback
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertTrue(backup.is_dir())
            self.assertTrue((backup / "recovery-marker").is_file())
            diagnostics = backup / "rollback-error.log"
            self.assertTrue(diagnostics.is_file())
            diagnostic_text = diagnostics.read_text(encoding="utf-8")
            actions = action_log.read_text(encoding="utf-8").splitlines()
            for expected in expected_failures:
                with self.subTest(expected=expected):
                    self.assertIn(expected, actions)
                    self.assertIn(f"Rollback step failed: {expected}", diagnostic_text)
            self.assertIn("reload restored systemd units", actions)
            for unsafe_action in unsafe_actions:
                self.assertNotIn(unsafe_action, actions)
            self.assertNotIn("unexpected current release move", actions)
            self.assertIn("Rollback was incomplete", completed.stderr)
            self.assertIn(str(backup), completed.stderr)

    def test_each_rollback_failure_independently_retains_diagnostics(self):
        prerequisite_failure_classes = [
            "restore current release",
            "restore file-monitor.service",
            "restore gate-command-server.service",
            "restore gate-controller-updater.service",
            "restore gate-controller-updater.timer",
            "restore fixed updater helper",
            "restore fixed media bootstrap",
        ]
        cloudflared_prerequisite_failure_classes = [
            "restore cloudflared transport",
        ]
        service_failure_classes = [
            "restore FTP home",
            "restore updater timer enablement",
            "restore updater timer activity",
            "restore application enablement",
            "restore application activity",
            "restore legacy command activity",
        ]
        prerequisite_actions = [
            *prerequisite_failure_classes,
            "reload restored systemd units",
        ]
        service_actions = [*service_failure_classes]
        blocked_service_actions = [
            "restore updater timer enablement",
            "restore updater timer activity",
            "restore application enablement",
            "restore application activity",
            "restore legacy command activity",
        ]

        all_failure_classes = (
            prerequisite_failure_classes
            + cloudflared_prerequisite_failure_classes
            + service_failure_classes
        )
        for failure_class in all_failure_classes:
            with self.subTest(failure_class=failure_class):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    backup = root / "backup"
                    backup.mkdir()
                    action_log = root / "actions.log"
                    command = f"""
source deployment/install.sh
FAIL_STEP={shlex.quote(failure_class)}
record_step() {{
  printf '%s\n' "$1" >> {shlex.quote(str(action_log))}
  [[ $1 != "$FAIL_STEP" ]]
}}
ln() {{ record_step 'restore current release'; }}
mv() {{ printf '%s\n' 'complete current release switch' >> {shlex.quote(str(action_log))}; }}
restore_path() {{ record_step "restore $1"; }}
run_systemctl_bounded() {{
  printf '%s\n' 'reload restored systemd units' >> {shlex.quote(str(action_log))}
}}
restore_fixed_updater_helper() {{ record_step 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{ record_step 'restore fixed media bootstrap'; }}
restore_cloudflared_transport_with_diagnostics() {{ record_step 'restore cloudflared transport'; }}
restore_cloudflared_transport_drop_in() {{ record_step 'restore cloudflared transport files'; }}
configure_ftp_home() {{ record_step 'restore FTP home'; }}
restore_unit_enablement() {{ record_step 'restore updater timer enablement'; }}
restore_unit_activity() {{ record_step 'restore updater timer activity'; }}
systemctl() {{
  case "$*" in
    'disable file-monitor.service')
      record_step 'restore application enablement'
      ;;
    *) return 99 ;;
  esac
}}
restore_application_activity() {{ record_step 'restore application activity'; }}
restore_legacy_command_activity() {{ record_step 'restore legacy command activity'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
: >"$BACKUP_DIR/$UPDATER_TIMER"
STAGING=
CURRENT_LINK={shlex.quote(str(root / 'current'))}
PREVIOUS_CURRENT={shlex.quote(str(root / 'previous'))}
SYSTEMD_ROOT={shlex.quote(str(root / 'systemd'))}
UPDATER_HELPER={shlex.quote(str(root / 'updater-helper'))}
FTP_USER=ftp-user
FTP_PREVIOUS_HOME={shlex.quote(str(root / 'ftp-home'))}
UPDATER_WAS_ENABLED=false
UPDATER_WAS_ACTIVE=false
APP_WAS_ENABLED=false
APP_WAS_ACTIVE=true
LEGACY_COMMAND_WAS_ENABLED=true
LEGACY_COMMAND_WAS_ACTIVE=true
set +e
false
rollback
"""

                    completed = subprocess.run(
                        ["bash", "-c", command],
                        cwd=REPOSITORY_ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(1, completed.returncode, completed.stderr)
                    self.assertTrue(backup.is_dir())
                    diagnostics = backup / "rollback-error.log"
                    self.assertTrue(diagnostics.is_file())
                    self.assertIn(
                        f"Rollback step failed: {failure_class}",
                        diagnostics.read_text(encoding="utf-8"),
                    )
                    actions = action_log.read_text(encoding="utf-8").splitlines()
                    for expected in prerequisite_actions:
                        self.assertIn(expected, actions)
                    if failure_class in prerequisite_failure_classes:
                        self.assertIn("restore cloudflared transport files", actions)
                        self.assertIn("restore FTP home", actions)
                        self.assertNotIn("restore cloudflared transport", actions)
                        for unsafe_action in blocked_service_actions:
                            self.assertNotIn(unsafe_action, actions)
                    elif failure_class in cloudflared_prerequisite_failure_classes:
                        self.assertIn("restore cloudflared transport", actions)
                        self.assertIn("restore FTP home", actions)
                        self.assertNotIn("restore cloudflared transport files", actions)
                        for unsafe_action in blocked_service_actions:
                            self.assertNotIn(unsafe_action, actions)
                    else:
                        for expected in service_actions:
                            self.assertIn(expected, actions)
                        self.assertNotIn("restore cloudflared transport files", actions)
                    if failure_class == "restore current release":
                        self.assertNotIn("complete current release switch", actions)

    def test_unwritable_diagnostics_do_not_skip_rollback_steps(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            backup.mkdir()
            action_log = root / "actions.log"
            expected_actions = [
                "restore current release",
                "restore file-monitor.service",
                "restore gate-command-server.service",
                "restore gate-controller-updater.service",
                "restore gate-controller-updater.timer",
                "reload restored systemd units",
                "restore fixed updater helper",
                "restore fixed media bootstrap",
                "restore cloudflared transport",
                "restore FTP home",
                "restore updater timer enablement",
                "restore updater timer activity",
                "restore application enablement",
                "restore application activity",
                "restore legacy command activity",
            ]
            command = f"""
source deployment/install.sh
record_step() {{ printf '%s\n' "$1" >> {shlex.quote(str(action_log))}; }}
prepare_rollback_diagnostics() {{
  ROLLBACK_DIAGNOSTICS={shlex.quote(str(root / 'missing' / 'rollback-error.log'))}
}}
restore_current_release() {{ record_step 'restore current release'; }}
restore_path() {{ record_step "restore $1"; }}
run_systemctl_bounded() {{ record_step 'reload restored systemd units'; }}
restore_fixed_updater_helper() {{ record_step 'restore fixed updater helper'; }}
restore_fixed_media_bootstrap() {{ record_step 'restore fixed media bootstrap'; }}
restore_cloudflared_transport_with_diagnostics() {{ record_step 'restore cloudflared transport'; }}
configure_ftp_home() {{ record_step 'restore FTP home'; }}
restore_unit_enablement() {{ record_step 'restore updater timer enablement'; }}
restore_unit_activity() {{ record_step 'restore updater timer activity'; }}
systemctl() {{
  case "$*" in
    'disable file-monitor.service')
      record_step 'restore application enablement'
      ;;
    *) return 99 ;;
  esac
}}
restore_application_activity() {{ record_step 'restore application activity'; }}
restore_legacy_command_activity() {{ record_step 'restore legacy command activity'; }}
ACTIVATION_STARTED=true
INSTALL_SUCCEEDED=false
BACKUP_DIR={shlex.quote(str(backup))}
: >"$BACKUP_DIR/$UPDATER_TIMER"
STAGING=
SYSTEMD_ROOT={shlex.quote(str(root / 'systemd'))}
UPDATER_HELPER={shlex.quote(str(root / 'updater-helper'))}
FTP_USER=ftp-user
FTP_PREVIOUS_HOME={shlex.quote(str(root / 'ftp-home'))}
UPDATER_WAS_ENABLED=false
UPDATER_WAS_ACTIVE=false
APP_WAS_ENABLED=false
APP_WAS_ACTIVE=true
LEGACY_COMMAND_WAS_ENABLED=true
LEGACY_COMMAND_WAS_ACTIVE=true
set +e
false
rollback
"""

            completed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertEqual(
                expected_actions,
                action_log.read_text(encoding="utf-8").splitlines(),
            )
            self.assertNotIn("Rollback step failed", completed.stderr)

    def test_failed_diagnostic_append_retains_detailed_step_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            backup = root / "backup"
            backup.mkdir()
            command = f"""
source deployment/install.sh
BACKUP_DIR={shlex.quote(str(backup))}
ROLLBACK_DIAGNOSTICS={shlex.quote(str(root / 'missing' / 'rollback-error.log'))}
failing_restore() {{
  printf 'durable restore detail\n' >&2
  return 23
}}
set +e
run_rollback_step 'restore test state' failing_restore
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
            fallbacks = list(backup.glob(".rollback-step.*"))
            self.assertEqual(1, len(fallbacks), completed.stderr)
            fallback_text = fallbacks[0].read_text(encoding="utf-8")
            self.assertIn("durable restore detail", fallback_text)
            self.assertIn(
                "Rollback step failed: restore test state (status 23).",
                fallback_text,
            )
            self.assertIn(str(fallbacks[0]), completed.stderr)

    def test_fixed_unit_backup_and_restore_preserve_symlink_identity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            systemd_root = root / "systemd"
            backup = root / "backup"
            systemd_root.mkdir()
            backup.mkdir()
            masked_unit = systemd_root / "file-monitor.service"
            linked_unit = systemd_root / "gate-controller-updater.service"
            masked_unit.symlink_to("/dev/null")
            linked_unit.symlink_to("../shared/gate-controller.service")
            command = f"""
source deployment/install.sh
SYSTEMD_ROOT={shlex.quote(str(systemd_root))}
BACKUP_DIR={shlex.quote(str(backup))}
backup_path file-monitor.service
backup_path gate-controller-updater.service
rm -f -- {shlex.quote(str(masked_unit))} {shlex.quote(str(linked_unit))}
printf 'candidate unit\n' > {shlex.quote(str(masked_unit))}
printf 'candidate unit\n' > {shlex.quote(str(linked_unit))}
restore_path file-monitor.service
restore_path gate-controller-updater.service
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
            self.assertTrue(masked_unit.is_symlink())
            self.assertEqual(Path("/dev/null"), masked_unit.readlink())
            self.assertTrue(linked_unit.is_symlink())
            self.assertEqual(
                Path("../shared/gate-controller.service"), linked_unit.readlink()
            )

    def test_updater_timer_active_state_is_captured_and_restored_independently(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            action_log = Path(temporary_directory) / "actions.log"
            command = f"""
source deployment/install.sh
systemctl() {{
  printf '%s\n' "$*" >> {shlex.quote(str(action_log))}
  case "$*" in
    'is-enabled --quiet gate-controller-updater.timer') return 1 ;;
    'is-active --quiet gate-controller-updater.timer') return 0 ;;
    'disable gate-controller-updater.timer') return 0 ;;
    'restart gate-controller-updater.timer') return 0 ;;
    *) return 99 ;;
  esac
}}
capture_updater_timer_state
restore_unit_enablement "$UPDATER_TIMER" "$UPDATER_WAS_ENABLED"
restore_unit_activity "$UPDATER_TIMER" "$UPDATER_WAS_ACTIVE"
printf 'enabled=%s active=%s\n' "$UPDATER_WAS_ENABLED" "$UPDATER_WAS_ACTIVE"
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
            self.assertEqual("enabled=false active=true\n", completed.stdout)
            self.assertEqual(
                [
                    "is-enabled --quiet gate-controller-updater.timer",
                    "is-active --quiet gate-controller-updater.timer",
                    "disable gate-controller-updater.timer",
                    "restart gate-controller-updater.timer",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_legacy_activity_reports_enablement_failure_after_restoring_activity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            previous_unit = Path(temporary_directory) / "gate-command-server.service"
            previous_unit.touch()
            action_log = Path(temporary_directory) / "actions.log"
            command = f"""
source deployment/install.sh
systemctl() {{
  printf '%s\n' "$*" >> {shlex.quote(str(action_log))}
  [[ $1 != enable ]]
}}
set +e
restore_legacy_command_activity \
  {shlex.quote(str(previous_unit))} true true
status=$?
set -e
printf 'status=%s\n' "$status"
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
            self.assertEqual("status=1\n", completed.stdout)
            self.assertEqual(
                [
                    "enable gate-command-server.service",
                    "restart gate-command-server.service",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )


class CloudflaredTransportLifecycleTests(unittest.TestCase):
    def run_shell(self, script):
        return subprocess.run(
            ["bash", "-c", script],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def test_active_service_tracks_systemd_restart_job_and_checks_active_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            systemd_root = root / "systemd"
            action_log = root / "actions.log"
            (source / "deployment/systemd/cloudflared.service.d").mkdir(
                parents=True
            )
            systemd_root.mkdir()
            (
                source
                / "deployment/systemd/cloudflared.service.d/20-http2.conf"
            ).write_text(
                "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n"
            )
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) forwarded+=("$1"); shift ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{
  case "$*" in
    'show cloudflared.service --property=LoadState --value') printf 'loaded\n' ;;
    'is-enabled --quiet cloudflared.service') return 1 ;;
    'is-active --quiet cloudflared.service') return 0 ;;
    'daemon-reload') return 0 ;;
    '--no-block restart cloudflared.service')
      printf 'pending\n' > {shlex.quote(str(root / 'job-state'))}
      ;;
    'show cloudflared.service --property=Job --property=ActiveState --all')
      if [[ $(cat {shlex.quote(str(root / 'job-state'))}) == pending ]]; then
        printf 'ActiveState=activating\nJob=/org/freedesktop/systemd1/job/41\n'
        printf 'complete\n' > {shlex.quote(str(root / 'job-state'))}
      else
        printf 'ActiveState=active\nJob=\n'
      fi
      ;;
    *) return 99 ;;
  esac
}}
capture_cloudflared_service_state
install_cloudflared_transport_drop_in \
  {shlex.quote(str(source))} {shlex.quote(str(systemd_root))}
run_systemctl_bounded daemon-reload
apply_cloudflared_transport_if_active
printf '%s %s %s\n' \
  "$CLOUDFLARED_WAS_PRESENT" \
  "$CLOUDFLARED_WAS_ACTIVE" \
  "$CLOUDFLARED_WAS_ENABLED"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("true true false\n", completed.stdout)
            self.assertEqual(
                [
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=LoadState --value",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-enabled --quiet cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-active --quiet cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl daemon-reload",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl --no-block restart cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-active --quiet cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-enabled --quiet cloudflared.service",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_enabled_state_inspection_failure_is_not_masked_under_set_plus_e(self):
        script = """
source deployment/install.sh
run_systemctl_bounded() { return 124; }
CLOUDFLARED_WAS_ENABLED=false
set +e
verify_cloudflared_enabled_state
status=$?
set -e
printf 'status=%s\n' "$status"
"""

        completed = self.run_shell(script)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("status=1\n", completed.stdout)
        self.assertIn(
            "could not verify whether cloudflared.service is enabled",
            completed.stderr,
        )

    def test_inactive_service_keeps_enabled_state_without_starting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            action_log = Path(temporary_directory) / "actions.log"
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) forwarded+=("$1"); shift ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{
  case "$*" in
    'show cloudflared.service --property=LoadState --value') printf 'loaded\n' ;;
    'is-enabled --quiet cloudflared.service') return 0 ;;
    'is-active --quiet cloudflared.service') return 3 ;;
    *) return 99 ;;
  esac
}}
capture_cloudflared_service_state
CLOUDFLARED_DROP_IN_CHANGED=true
apply_cloudflared_transport_if_active
printf '%s %s %s\n' \
  "$CLOUDFLARED_WAS_PRESENT" \
  "$CLOUDFLARED_WAS_ACTIVE" \
  "$CLOUDFLARED_WAS_ENABLED"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("true false true\n", completed.stdout)
            self.assertEqual(
                [
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=LoadState --value",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-enabled --quiet cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-active --quiet cloudflared.service",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_absent_service_is_not_started(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            action_log = Path(temporary_directory) / "actions.log"
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) forwarded+=("$1"); shift ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{
  case "$*" in
    'show cloudflared.service --property=LoadState --value') printf 'not-found\n' ;;
    *) return 99 ;;
  esac
}}
capture_cloudflared_service_state
CLOUDFLARED_DROP_IN_CHANGED=true
apply_cloudflared_transport_if_active
printf '%s %s %s\n' \
  "$CLOUDFLARED_WAS_PRESENT" \
  "$CLOUDFLARED_WAS_ACTIVE" \
  "$CLOUDFLARED_WAS_ENABLED"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("false false false\n", completed.stdout)
            self.assertEqual(
                [
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=LoadState --value",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_rollback_restores_exact_existing_drop_in_and_active_service(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            systemd_root = root / "systemd"
            backup = root / "backup"
            action_log = root / "actions.log"
            destination = (
                systemd_root / "cloudflared.service.d/20-http2.conf"
            )
            (source / "deployment/systemd/cloudflared.service.d").mkdir(
                parents=True
            )
            destination.parent.mkdir(parents=True)
            backup.mkdir()
            destination.write_text("previous transport\n", encoding="utf-8")
            destination.chmod(0o600)
            (
                source
                / "deployment/systemd/cloudflared.service.d/20-http2.conf"
            ).write_text(
                "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n"
            )
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) forwarded+=("$1"); shift ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{
  case "$*" in
    'daemon-reload'|'is-active --quiet cloudflared.service') return 0 ;;
    'is-enabled --quiet cloudflared.service') return 1 ;;
    '--no-block restart cloudflared.service')
      printf 'pending\n' > {shlex.quote(str(root / 'job-state'))}
      ;;
    'show cloudflared.service --property=Job --property=ActiveState --all')
      if [[ $(cat {shlex.quote(str(root / 'job-state'))}) == pending ]]; then
        printf 'ActiveState=activating\nJob=/org/freedesktop/systemd1/job/42\n'
        printf 'complete\n' > {shlex.quote(str(root / 'job-state'))}
      else
        printf 'ActiveState=active\nJob=\n'
      fi
      ;;
    *) return 99 ;;
  esac
}}
CLOUDFLARED_WAS_PRESENT=true
CLOUDFLARED_WAS_ACTIVE=true
CLOUDFLARED_WAS_ENABLED=false
backup_cloudflared_transport_drop_in \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
install_cloudflared_transport_drop_in \
  {shlex.quote(str(source))} {shlex.quote(str(systemd_root))}
restore_cloudflared_transport_after_rollback \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("previous transport\n", destination.read_text())
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertEqual(
                [
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl daemon-reload",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl --no-block restart cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-active --quiet cloudflared.service",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl is-enabled --quiet cloudflared.service",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_rollback_restores_drop_in_absence_without_starting_service(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            systemd_root = root / "systemd"
            backup = root / "backup"
            action_log = root / "actions.log"
            destination = (
                systemd_root / "cloudflared.service.d/20-http2.conf"
            )
            (source / "deployment/systemd/cloudflared.service.d").mkdir(
                parents=True
            )
            systemd_root.mkdir()
            backup.mkdir()
            (
                source
                / "deployment/systemd/cloudflared.service.d/20-http2.conf"
            ).write_text(
                "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n"
            )
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) forwarded+=("$1"); shift ;;
    esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{
  case "$*" in
    'daemon-reload') return 0 ;;
    *) return 99 ;;
  esac
}}
CLOUDFLARED_WAS_PRESENT=true
CLOUDFLARED_WAS_ACTIVE=false
CLOUDFLARED_WAS_ENABLED=true
backup_cloudflared_transport_drop_in \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
install_cloudflared_transport_drop_in \
  {shlex.quote(str(source))} {shlex.quote(str(systemd_root))}
restore_cloudflared_transport_after_rollback \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(
                [
                    "timeout --signal=TERM --kill-after=5s 30s systemctl daemon-reload",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_timed_restart_is_cancelled_and_quiescent_before_rollback_restart(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            systemd_root = root / "systemd"
            backup = root / "backup"
            action_log = root / "actions.log"
            state = root / "systemd-state"
            destination = systemd_root / "cloudflared.service.d/20-http2.conf"
            (source / "deployment/systemd/cloudflared.service.d").mkdir(
                parents=True
            )
            destination.parent.mkdir(parents=True)
            backup.mkdir()
            destination.write_text("previous transport\n", encoding="utf-8")
            (
                source
                / "deployment/systemd/cloudflared.service.d/20-http2.conf"
            ).write_text(
                "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n"
            )
            state.write_text("0 none active\n", encoding="utf-8")
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
sleep() {{ :; }}
systemctl() {{
  local restarts job active
  read -r restarts job active < {shlex.quote(str(state))}
  case "$*" in
    '--no-block restart cloudflared.service')
      restarts=$((restarts + 1))
      if [[ $restarts -eq 1 ]]; then
        printf '%s 41 activating\n' "$restarts" > {shlex.quote(str(state))}
      else
        printf '%s 42 activating\n' "$restarts" > {shlex.quote(str(state))}
      fi
      ;;
    'show cloudflared.service --property=Job --property=ActiveState --all')
      if [[ $job == none ]]; then
        printf 'ActiveState=%s\nJob=\n' "$active"
        if [[ $active == deactivating ]]; then
          printf '%s none active\n' "$restarts" > {shlex.quote(str(state))}
        fi
      else
        printf 'ActiveState=%s\nJob=/org/freedesktop/systemd1/job/%s\n' "$active" "$job"
        if [[ $job == 42 ]]; then
          printf '%s none active\n' "$restarts" > {shlex.quote(str(state))}
        fi
      fi
      ;;
    'cancel 41') printf '%s none deactivating\n' "$restarts" > {shlex.quote(str(state))} ;;
    'daemon-reload'|'is-active --quiet cloudflared.service') return 0 ;;
    'is-enabled --quiet cloudflared.service') return 1 ;;
    *) return 99 ;;
  esac
}}
CLOUDFLARED_WAS_PRESENT=true
CLOUDFLARED_WAS_ACTIVE=true
CLOUDFLARED_WAS_ENABLED=false
CLOUDFLARED_JOB_TIMEOUT_SECONDS=0
backup_cloudflared_transport_drop_in \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
install_cloudflared_transport_drop_in \
  {shlex.quote(str(source))} {shlex.quote(str(systemd_root))}
set +e
apply_cloudflared_transport_if_active
apply_status=$?
set -e
CLOUDFLARED_JOB_TIMEOUT_SECONDS=30
restore_cloudflared_transport_after_rollback \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
printf 'apply_status=%s\n' "$apply_status"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("apply_status=1\n", completed.stdout)
            actions = action_log.read_text(encoding="utf-8").splitlines()
            first_restart = actions.index(
                "timeout --signal=TERM --kill-after=5s 30s systemctl --no-block restart cloudflared.service"
            )
            cancellation = actions.index(
                "timeout --signal=TERM --kill-after=5s 30s systemctl cancel 41"
            )
            second_restart = actions.index(
                "timeout --signal=TERM --kill-after=5s 30s systemctl --no-block restart cloudflared.service",
                first_restart + 1,
            )
            self.assertLess(first_restart, cancellation)
            self.assertLess(cancellation, second_restart)
            post_cancel_checks = actions[cancellation + 1 : second_restart].count(
                "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all"
            )
            self.assertGreaterEqual(post_cancel_checks, 2)
            self.assertIn("timed out", completed.stderr)

    def test_restore_failure_is_loud_under_set_plus_e_and_skips_reload(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            systemd_root = root / "systemd"
            backup = root / "backup"
            outside = root / "outside"
            action_log = root / "actions.log"
            backup.mkdir()
            outside.mkdir()
            systemd_root.mkdir()
            (systemd_root / "cloudflared.service.d").symlink_to(outside)
            (backup / "cloudflared-transport-drop-in").write_text(
                "previous transport\n", encoding="utf-8"
            )
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{ return 0; }}
CLOUDFLARED_WAS_PRESENT=true
CLOUDFLARED_WAS_ACTIVE=false
CLOUDFLARED_DROP_IN_CHANGED=true
set +e
restore_cloudflared_transport_with_diagnostics \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("status=1\n", completed.stdout)
            self.assertIn("must not be a symbolic link", completed.stderr)
            self.assertIn("could not restore exact cloudflared transport", completed.stderr)
            diagnostics = backup / "cloudflared-rollback-error.log"
            self.assertTrue(diagnostics.is_file())
            self.assertIn("must not be a symbolic link", diagnostics.read_text())
            self.assertFalse(action_log.exists())

    def test_daemon_reload_failure_is_loud_under_set_plus_e_and_skips_restart(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            systemd_root = root / "systemd"
            backup = root / "backup"
            action_log = root / "actions.log"
            destination = systemd_root / "cloudflared.service.d/20-http2.conf"
            destination.parent.mkdir(parents=True)
            backup.mkdir()
            destination.write_text("candidate transport\n", encoding="utf-8")
            (backup / "cloudflared-transport-drop-in").write_text(
                "previous transport\n", encoding="utf-8"
            )
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{
  case "$*" in
    'show cloudflared.service --property=Job --property=ActiveState --all')
      printf 'ActiveState=active\nJob=\n'
      ;;
    'daemon-reload')
      printf 'mock daemon-reload detail\n' >&2
      return 1
      ;;
    *) return 99 ;;
  esac
}}
CLOUDFLARED_WAS_PRESENT=true
CLOUDFLARED_WAS_ACTIVE=true
CLOUDFLARED_DROP_IN_CHANGED=true
set +e
restore_cloudflared_transport_with_diagnostics \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
status=$?
set -e
printf 'status=%s\n' "$status"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("status=1\n", completed.stdout)
            self.assertEqual("previous transport\n", destination.read_text())
            self.assertIn("mock daemon-reload detail", completed.stderr)
            self.assertIn("could not reload systemd after cloudflared transport rollback", completed.stderr)
            diagnostics = backup / "cloudflared-rollback-error.log"
            self.assertTrue(diagnostics.is_file())
            self.assertIn("mock daemon-reload detail", diagnostics.read_text())
            self.assertEqual(
                [
                    "timeout --signal=TERM --kill-after=5s 30s systemctl show cloudflared.service --property=Job --property=ActiveState --all",
                    "timeout --signal=TERM --kill-after=5s 30s systemctl daemon-reload",
                ],
                action_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_rollback_does_not_restart_active_service_when_drop_in_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            systemd_root = root / "systemd"
            backup = root / "backup"
            action_log = root / "actions.log"
            destination = systemd_root / "cloudflared.service.d/20-http2.conf"
            source_drop_in = (
                source
                / "deployment/systemd/cloudflared.service.d/20-http2.conf"
            )
            source_drop_in.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            backup.mkdir()
            content = "[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=auto\n"
            source_drop_in.write_text(content, encoding="utf-8")
            destination.write_text(content, encoding="utf-8")
            destination.chmod(0o600)
            script = f"""
source deployment/install.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac
  done
  command install "${{forwarded[@]}}"
}}
timeout() {{
  printf 'timeout %s\n' "$*" >> {shlex.quote(str(action_log))}
  shift 3
  "$@"
}}
systemctl() {{ [[ $* == daemon-reload ]]; }}
CLOUDFLARED_WAS_PRESENT=true
CLOUDFLARED_WAS_ACTIVE=true
backup_cloudflared_transport_drop_in \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
install_cloudflared_transport_drop_in \
  {shlex.quote(str(source))} {shlex.quote(str(systemd_root))}
restore_cloudflared_transport_after_rollback \
  {shlex.quote(str(systemd_root))} {shlex.quote(str(backup))}
printf 'changed=%s\n' "$CLOUDFLARED_DROP_IN_CHANGED"
"""

            completed = self.run_shell(script)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("changed=false\n", completed.stdout)
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertEqual(
                ["timeout --signal=TERM --kill-after=5s 30s systemctl daemon-reload"],
                action_log.read_text(encoding="utf-8").splitlines(),
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
