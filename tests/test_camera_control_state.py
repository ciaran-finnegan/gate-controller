import configparser
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import gate_controller.__main__ as gate_main
from gate_camera_control.state import default_state, state_document, write_state
from gate_controller.camera_control_state import (
    read_camera_control_state,
    unavailable,
    validated_camera_control,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read_unit(relative_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
    return parser


def ready_document(now=None, **overrides):
    snapshot = {
        "state": "Off",
        "default": "Off",
        "effective_until": None,
        "lease_seconds_remaining": None,
        "revert_failed": False,
        "last_error": None,
    }
    snapshot.update(overrides)
    return state_document(snapshot, now=time.time() if now is None else now)


class CameraControlStateFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def test_the_published_file_is_world_readable_and_never_secret(self):
        write_state(self.path, ready_document())

        self.assertEqual(0o644, self.path.stat().st_mode & 0o777)
        self.assertEqual(
            {"observed_at", "camera_control"},
            set(json.loads(self.path.read_text(encoding="utf-8"))),
        )

    def test_a_fresh_ready_file_round_trips_into_the_heartbeat_block(self):
        write_state(self.path, ready_document(
            state="Auto", effective_until="2026-09-07T21:10:00+00:00",
        ))

        block = read_camera_control_state(self.path)

        self.assertEqual({
            "available": True,
            "reason": "ready",
            "ir": {
                "state": "Auto",
                "default": "Off",
                "effective_until": "2026-09-07T21:10:00+00:00",
                "revert_failed": False,
            },
        }, block)

    def test_a_missing_file_reads_as_not_configured_rather_than_off(self):
        block = read_camera_control_state(self.path)

        self.assertEqual(unavailable("not_configured"), block)
        self.assertEqual("unknown", block["ir"]["state"])

    def test_a_stale_or_future_file_reads_as_service_unhealthy(self):
        for observed_at in (time.time() - 31, time.time() + 31):
            with self.subTest(observed_at=observed_at):
                write_state(self.path, ready_document(now=observed_at))

                self.assertEqual(
                    unavailable("service_unhealthy"),
                    read_camera_control_state(self.path),
                )

    def test_malformed_oversized_and_symlinked_files_fail_closed(self):
        self.path.write_text("not-json", encoding="utf-8")
        self.assertEqual(
            unavailable("service_unhealthy"), read_camera_control_state(self.path)
        )

        self.path.write_text("x" * (9 * 1024), encoding="utf-8")
        self.assertEqual(
            unavailable("service_unhealthy"), read_camera_control_state(self.path)
        )

        self.path.unlink()
        target = Path(self.directory.name) / "real.json"
        write_state(target, ready_document())
        os.symlink(target, self.path)
        self.assertEqual(
            unavailable("service_unhealthy"), read_camera_control_state(self.path)
        )

    def test_a_fifo_never_blocks_the_heartbeat(self):
        fifo = Path(self.directory.name) / "fifo.json"
        os.mkfifo(fifo)

        self.assertEqual(
            unavailable("service_unhealthy"), read_camera_control_state(fifo)
        )

    def test_incoherent_blocks_are_rejected_so_unknown_never_reads_as_ready(self):
        incoherent = [
            {"available": True, "reason": "ready", "ir": {
                "state": "unknown", "default": "Off",
                "effective_until": None, "revert_failed": False}},
            {"available": False, "reason": "ready", "ir": {
                "state": "Off", "default": "Off",
                "effective_until": None, "revert_failed": False}},
            {"available": True, "reason": "camera_busy", "ir": {
                "state": "Off", "default": "Off",
                "effective_until": None, "revert_failed": False}},
            {"available": True, "reason": "ready", "ir": {
                "state": "On", "default": "Off",
                "effective_until": None, "revert_failed": False}},
            {"available": True, "reason": "ready", "ir": {
                "state": "Off", "default": "Sometimes",
                "effective_until": None, "revert_failed": False}},
            {"available": True, "reason": "not_configured", "ir": {
                "state": "Off", "default": "Off",
                "effective_until": None, "revert_failed": False}},
            {"available": True, "reason": "ready", "ir": {
                "state": "Off", "default": "Off",
                "effective_until": "x" * 64, "revert_failed": False}},
            {"available": True, "reason": "ready", "ir": {
                "state": "Off", "default": "Off", "revert_failed": False}},
            {"available": "yes", "reason": "ready", "ir": {
                "state": "Off", "default": "Off",
                "effective_until": None, "revert_failed": False}},
        ]

        for block in incoherent:
            with self.subTest(block=block):
                self.assertEqual(
                    unavailable("service_unhealthy"), validated_camera_control(block)
                )

    def test_the_default_document_claims_nothing(self):
        block = default_state()["camera_control"]

        self.assertFalse(block["available"])
        self.assertEqual("unknown", block["ir"]["state"])


class ControllerHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def status(self):
        store = type("Store", (), {"pending_outbox_count": staticmethod(lambda: 0)})()
        return gate_main._controller_status(
            store, type("Prompt", (), {"available": False})(), {},
            camera_control_state_path=self.path,
        )

    def test_the_heartbeat_carries_the_camera_control_block(self):
        write_state(self.path, ready_document(state="Auto"))

        status = self.status()

        self.assertEqual("Auto", status["camera_control"]["ir"]["state"])
        self.assertTrue(status["camera_control"]["available"])

    def test_an_un_observed_camera_reaches_the_heartbeat_as_not_observed(self):
        """A running service that has not called the camera yet is not a fault.

        The app hides the control on `camera_unreachable`, so a restart with no
        lease used to hide it until somebody opened the page and forced a read.
        """
        write_state(self.path, state_document({
            "state": "unknown", "default": "Off", "effective_until": None,
            "lease_seconds_remaining": None, "revert_failed": False,
            "last_error": None,
        }, now=time.time()))

        block = self.status()["camera_control"]

        self.assertEqual("not_observed", block["reason"])
        self.assertFalse(block["available"])

    def test_a_broken_state_file_cannot_break_the_heartbeat(self):
        self.path.write_text("not-json", encoding="utf-8")

        status = self.status()

        self.assertEqual("service_unhealthy", status["camera_control"]["reason"])
        self.assertEqual("unknown", status["camera_control"]["ir"]["state"])
        self.assertEqual(0, status["queue_depth"])

    def test_the_heartbeat_never_carries_a_camera_address_or_credential(self):
        write_state(self.path, ready_document(state="Auto"))

        text = json.dumps(self.status())

        self.assertNotIn("192.168", text)
        self.assertNotIn("password", text.lower())
        self.assertNotIn("token", text.lower())


class CameraControlDeploymentTests(unittest.TestCase):
    def test_the_service_runs_as_its_own_user_with_its_own_root_only_environment(self):
        unit = read_unit("deployment/systemd/gate-camera-control.service")["Service"]

        self.assertEqual("gate-camera-control", unit["User"])
        self.assertEqual("gate-camera-control", unit["Group"])
        self.assertEqual("/etc/gate-camera-control.env", unit["EnvironmentFile"])
        self.assertEqual("/usr/bin/python3 -m gate_camera_control", unit["ExecStart"])
        self.assertEqual("/run/gate-camera", unit["ReadWritePaths"])

    def test_the_service_cannot_reach_anything_but_loopback_by_default(self):
        unit = read_unit("deployment/systemd/gate-camera-control.service")["Service"]

        self.assertEqual("any", unit["IPAddressDeny"])
        self.assertEqual("localhost", unit["IPAddressAllow"])

    def test_the_service_is_locked_out_of_gpio_controller_and_media_secrets(self):
        unit = read_unit("deployment/systemd/gate-camera-control.service")["Service"]
        inaccessible = unit["InaccessiblePaths"]

        for path in (
            "/dev/gpiomem", "/dev/gpiochip0", "/var/lib/gate-controller",
            "/opt/gate-controller", "/etc/gate-media-gateway.env",
            "/etc/gate-media-auth.env", "/etc/gate-media-turn.env",
        ):
            with self.subTest(path=path):
                self.assertIn(path, inaccessible)
        self.assertEqual("true", unit["NoNewPrivileges"])
        self.assertEqual("strict", unit["ProtectSystem"])
        self.assertEqual("", unit["CapabilityBoundingSet"])

    def test_the_media_units_are_not_given_camera_api_credentials(self):
        for relative in (
            "deployment/systemd/gate-media-auth.service",
            "deployment/systemd/gate-media-gateway.service",
            "deployment/systemd/gate-media-transcoder.service",
        ):
            with self.subTest(unit=relative):
                unit = read_unit(relative)["Service"]

                self.assertNotIn(
                    "gate-camera-control.env", unit.get("EnvironmentFile", "")
                )
                self.assertNotEqual("gate-camera-control", unit.get("User"))

    def test_the_tunnel_routes_camera_control_through_its_own_hostname(self):
        ingress = (
            REPOSITORY_ROOT / "deployment/cloudflared/gate-controller-tunnel.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("hostname: gate-camera.example.com", ingress)
        self.assertIn("service: http://127.0.0.1:8767", ingress)
        # The command hostname keeps its own service token and its own port.
        self.assertIn("service: http://127.0.0.1:8765", ingress)

    def test_the_installer_never_touches_the_media_gateway_environment(self):
        installer = (
            REPOSITORY_ROOT / "deployment/install-camera-control.sh"
        ).read_text(encoding="utf-8")

        self.assertNotIn("gate-media-gateway.env", installer)
        self.assertIn("/etc/gate-camera-control.env", installer)
        self.assertIn("d $CAMERA_RUNTIME_ROOT 0755", installer)
        self.assertIn("IPAddressAllow=$host/32", installer)
        self.assertIn("reject_gpio_membership", installer)

    def test_the_release_verifier_syntax_checks_the_camera_installer(self):
        """The check must be presence-guarded, like every other file check.

        An unconditional `bash -n` on a release that predates the installer
        exits 127, and a verifier that fails defers every future update
        forever -- the auto-update freeze this list exists to avoid.
        """
        from deployment.gate_controller_updater import OPTIONAL_SHELL_SYNTAX_CHECKS

        self.assertIn(
            ("/bin/bash", "deployment/install-camera-control.sh"),
            OPTIONAL_SHELL_SYNTAX_CHECKS,
        )
        updater = (
            REPOSITORY_ROOT / "deployment/gate_controller_updater.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            '["/bin/bash", "-n", "deployment/install-camera-control.sh"]', updater
        )

    def test_the_environment_template_documents_the_isolated_credential_file(self):
        template = (
            REPOSITORY_ROOT / "deployment/gate-camera-control.env.example"
        ).read_text(encoding="utf-8")

        self.assertIn("GATE_CAMERA_HOST=", template)
        self.assertIn("GATE_CAMERA_USERNAME=", template)
        self.assertIn("GATE_CAMERA_PASSWORD=", template)
        self.assertIn("GATE_CAMERA_IR_DEFAULT=Off", template)
        self.assertNotIn("MTX_", template)

    def test_the_controller_environment_example_gains_no_camera_credentials(self):
        example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertNotIn("GATE_CAMERA_USERNAME", example)
        self.assertNotIn("GATE_CAMERA_PASSWORD", example)
        self.assertNotIn("GATE_CAMERA_HOST", example)


if __name__ == "__main__":
    unittest.main()
