import configparser
import json
import os
import stat
import subprocess
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


def _controller_reasons():
    from gate_controller.camera_control_state import _REASONS

    return _REASONS


def ready_document(now=None, talk_block=None, **overrides):
    snapshot = {
        "state": "Off",
        "default": "Off",
        "effective_until": None,
        "lease_seconds_remaining": None,
        "revert_failed": False,
        "last_error": None,
    }
    snapshot.update(overrides)
    return state_document(
        snapshot, now=time.time() if now is None else now, talk_block=talk_block,
    )


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
            "talkback": {"available": False, "reason": "not_enabled", "active": False},
        }, block)

    def test_the_talkback_block_is_optional_and_held_to_its_own_coherence_rule(self):
        """A pre-talkback service publishes no block; that reads as not enabled.

        A block that is present must be exactly its three keys, a known reason,
        and `available` only when the reason is `ready`, so the heartbeat can
        never claim a talk path the camera-control service did not prove.
        """
        legacy = ready_document()["camera_control"]
        del legacy["talkback"]
        self.assertEqual(
            {"available": False, "reason": "not_enabled", "active": False},
            validated_camera_control(legacy)["talkback"],
        )
        ready = ready_document(talk_block={
            "available": True, "reason": "ready", "active": True,
        })["camera_control"]
        self.assertEqual(
            {"available": True, "reason": "ready", "active": True},
            validated_camera_control(ready)["talkback"],
        )
        for talkback in (
            {"available": True, "reason": "camera_unreachable", "active": False},
            {"available": False, "reason": "ready", "active": False},
            {"available": True, "reason": "ready"},
            {"available": True, "reason": "ready", "active": False, "extra": 1},
            {"available": "yes", "reason": "ready", "active": False},
            {"available": True, "reason": "wonderful", "active": False},
            "ready",
        ):
            block = ready_document()["camera_control"]
            block["talkback"] = talkback
            with self.subTest(talkback=talkback):
                self.assertEqual(
                    unavailable("service_unhealthy"), validated_camera_control(block)
                )

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

    def test_the_ir_lease_is_kept_on_storage_that_survives_a_power_cut(self):
        """`/run` is tmpfs, and a lease on tmpfs dies with the thing it guards.

        The camera has its own supply, so a power cut during a lease left it
        holding the leased state while the record that would have reverted it
        was erased by the boot that recreated `/run`.
        """
        from gate_camera_control.__main__ import LEASE_PATH, STATE_ROOT

        unit = read_unit("deployment/systemd/gate-camera-control.service")["Service"]
        installer = (
            REPOSITORY_ROOT / "deployment/install-camera-control.sh"
        ).read_text(encoding="utf-8")

        self.assertEqual("/var/lib/gate-camera", STATE_ROOT)
        self.assertEqual("/var/lib/gate-camera/lease.json", LEASE_PATH)
        self.assertFalse(LEASE_PATH.startswith("/run/"))
        self.assertEqual("gate-camera", unit["StateDirectory"])
        self.assertEqual("0700", unit["StateDirectoryMode"])
        self.assertIn("d $CAMERA_STATE_ROOT 0700", installer)
        self.assertIn("CAMERA_STATE_ROOT=/var/lib/gate-camera", installer)

    def test_the_installer_names_a_missing_source_value_instead_of_dying(self):
        """`set -u` turned the intended message into "$2: unbound variable".

        `require_option_value "$1" "${2-}"` always passed two arguments, so the
        count-based guard never fired and the assignment below it aborted the
        script with bash's own error.
        """
        result = subprocess.run(
            ["/bin/bash", str(REPOSITORY_ROOT / "deployment/install-camera-control.sh"),
             "--source"],
            capture_output=True, text=True, timeout=60,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--source requires a value", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

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
        self.assertIn("--write-address-dropin", installer)
        self.assertIn("reject_gpio_membership", installer)

    def test_the_installer_never_carries_the_camera_address_through_the_shell(self):
        """The validator writes the pin; the installer only moves the file.

        A `host=$(...)` step puts the camera's address into a shell variable,
        where `bash -x` prints it and a failed substitution silently yields
        `IPAddressAllow=/32` -- a pin that matches nothing.
        """
        installer = (
            REPOSITORY_ROOT / "deployment/install-camera-control.sh"
        ).read_text(encoding="utf-8")

        self.assertNotIn("--print-host", installer)
        self.assertNotIn("IPAddressAllow=$", installer)
        self.assertNotIn("GATE_CAMERA_HOST=", installer)

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

    def test_the_docs_send_the_installer_at_the_managed_release_tree(self):
        """`/opt/gate-controller/releases/<sha>` does not exist, and the
        release tree keeps git's file modes.

        The updater unpacks into `/opt/gate-controller-deploy/releases/<sha>`,
        so a command aimed at `/opt/gate-controller/releases/` fails with "must
        be a directory". Releases up to 1d98e6e also shipped this script mode
        0644, so `sudo "$RELEASE/deployment/install-camera-control.sh"` fails
        there with "command not found": the one step an operator has to run by
        hand after every release must be documented through `bash`.
        """
        for relative in ("docs/camera-control.md", "docs/deployment.md"):
            with self.subTest(document=relative):
                text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")

                self.assertIn("/opt/gate-controller-deploy/releases/", text)
                self.assertNotIn("--source /opt/gate-controller/releases/", text)
                self.assertIn(
                    'sudo bash "$RELEASE/deployment/install-camera-control.sh" '
                    '--source "$RELEASE"',
                    text,
                )
                self.assertNotIn("sudo deployment/install-camera-control.sh", text)
                self.assertNotIn(
                    'sudo "$RELEASE/deployment/install-camera-control.sh"', text
                )

    def test_the_installer_scripts_carry_their_execute_bit(self):
        """The release tree is a git checkout, so it publishes git's modes.

        `install-camera-control.sh` was committed 0644, which made the documented
        direct invocation fail on the Pi with "command not found". Keep the bit
        set on every operator-run installer.
        """
        for relative in (
            "deployment/install.sh",
            "deployment/install-camera-control.sh",
            "deployment/install-media.sh",
        ):
            with self.subTest(script=relative):
                mode = (REPOSITORY_ROOT / relative).stat().st_mode
                self.assertTrue(mode & stat.S_IXUSR, f"{relative} is not executable")

    def test_the_documented_rollback_does_not_assume_the_default_is_off(self):
        """`GATE_CAMERA_IR_DEFAULT` may be `Auto`.

        Posting a hard-coded `{"state":"Off"}` at a deployment whose default is
        `Auto` does not cancel the lease -- it creates one, in the opposite
        direction, immediately before the service that would revert it is
        stopped.
        """
        text = (REPOSITORY_ROOT / "docs/camera-control.md").read_text(encoding="utf-8")
        rollback = text.split("## Rollback", 1)[1]

        self.assertNotIn('-d \'{"state":"Off"}\'', rollback)
        self.assertIn('["ir"]["default"]', rollback)

    def test_the_documented_snapshot_methods_match_the_ones_the_code_serves(self):
        from gate_camera_control.__main__ import _SNAPSHOT_PATHS

        text = (REPOSITORY_ROOT / "docs/camera-control.md").read_text(encoding="utf-8")

        self.assertEqual({"/camera/snap", "/camera/snapshot"}, set(_SNAPSHOT_PATHS))
        self.assertIn(
            "### `GET /camera/snap` — also served at `POST /camera/snap`, "
            "and `GET`/`POST /camera/snapshot`",
            text,
        )

    def test_the_documented_reboot_behaviour_is_the_behaviour(self):
        text = (REPOSITORY_ROOT / "docs/camera-control.md").read_text(encoding="utf-8")

        self.assertIn("/var/lib/gate-camera/lease.json", text)
        self.assertIn("StateDirectory=gate-camera", text)
        self.assertNotIn("/run/gate-camera/lease.json", text)

    def test_the_heartbeat_block_key_set_and_reason_enum_are_frozen(self):
        """PR #36's Worker narrows on exactly these keys and these reasons.

        `narrowedCameraControlCapabilities` drops the whole block when a
        required key is missing or a reason is not in its list, so the control
        disappears from the app. Nothing in this service may widen or rename
        them without the Worker changing first.
        """
        from gate_camera_control.state import IR_STATES, REASONS

        document = ready_document(state="Auto", effective_until="2026-09-08T21:14:11+00:00")
        block = document["camera_control"]

        self.assertEqual({"observed_at", "camera_control"}, set(document))
        # `talkback` is the one addition since #36: the Worker keeps unknown
        # keys rather than dropping the block, and reads this one for talk.
        self.assertEqual({"available", "reason", "ir", "talkback"}, set(block))
        self.assertEqual({"available", "reason", "active"}, set(block["talkback"]))
        self.assertEqual(
            {"state", "default", "effective_until", "revert_failed"}, set(block["ir"])
        )
        self.assertEqual(
            ("ready", "not_observed", "camera_busy", "camera_unreachable",
             "camera_error"),
            REASONS,
        )
        self.assertEqual(("Auto", "Off"), IR_STATES)
        # The two the controller adds when the service is absent, and no others.
        self.assertEqual(
            {"ready", "not_configured", "service_unhealthy", "not_observed",
             "camera_busy", "camera_unreachable", "camera_error"},
            set(_controller_reasons()),
        )

    def test_the_controller_environment_example_gains_no_camera_credentials(self):
        example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertNotIn("GATE_CAMERA_USERNAME", example)
        self.assertNotIn("GATE_CAMERA_PASSWORD", example)
        self.assertNotIn("GATE_CAMERA_HOST", example)


if __name__ == "__main__":
    unittest.main()
