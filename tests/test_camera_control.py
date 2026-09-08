import json
import shutil
import socket
import ssl
import stat
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import gate_camera_control.__main__ as camera_control_main
from gate_camera_control.__main__ import (
    IR_BURST,
    SNAPSHOT_MIN_INTERVAL_SECONDS,
    STATE_BURST,
    CameraControlHandler,
    CameraControlServer,
    CameraControlService,
    build_service,
    journal,
    validated_camera_control_environment,
)
from gate_camera_control.ir import IrController, RevertWorker
from gate_camera_control.reolink import (
    CameraBusy,
    CameraError,
    CameraUnreachable,
    ReolinkClient,
    TokenCache,
)
from gate_camera_control.state import StatePublisher, state_document
from gate_media_config import (
    MediaConfigError,
    validate_camera_control_environment,
    write_camera_address_dropin,
)


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def self_signed_certificate(directory):
    """One throwaway self-signed certificate, exactly what the camera presents."""
    path = Path(directory) / "camera.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(path), "-out", str(path), "-days", "1",
            "-subj", "/CN=camera.invalid",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return path


class FakeCamera:
    """A stand-in for the RLC-810A api.cgi surface, with its 502 login behaviour."""

    def __init__(self, *, certificate=None):
        self.ir_state = "Off"
        self.logins = 0
        self.commands = []
        self.tokens = set()
        self.login_status = 200
        self.command_status = 200
        # The firmware's own behaviour: past this many logins it stops answering
        # Login at all and returns 502 for about a minute.
        self.login_502_after = None
        self.expire_next_token = False
        self.snapshot_body = JPEG
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCameraHandler)
        self._server.camera = self
        self._server.daemon_threads = True
        if certificate is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # `PROTOCOL_TLS_SERVER` alone leaves TLS 1.0 and 1.1 reachable on
            # some builds, and the fake camera must not accept a handshake the
            # client is pinned against -- the same floor `net_probe.tls_context`
            # sets, for the same reason.
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certificate)
            self._server.socket = context.wrap_socket(
                self._server.socket, server_side=True
            )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host(self):
        return f"127.0.0.1:{self._server.server_address[1]}"

    def connection_factory(self, host, timeout):
        return HTTPConnection(host, timeout=timeout)

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def handle(self, command, payload, token):
        with self._lock:
            if command == "Login":
                self.logins += 1
                if (self.login_502_after is not None
                        and self.logins > self.login_502_after):
                    return 502, b""
                if self.login_status != 200:
                    return self.login_status, b""
                name = f"token-{self.logins}"
                self.tokens.add(name)
                return 200, _command_response("Login", {
                    "Token": {"name": name, "leaseTime": 3600},
                })
            if self.command_status != 200:
                return self.command_status, b""
            if token not in self.tokens or self.expire_next_token:
                self.expire_next_token = False
                self.tokens.discard(token)
                return 200, _authentication_failure(command)
            self.commands.append(command)
            if command == "GetIrLights":
                return 200, _command_response("GetIrLights", {
                    "IrLights": {"state": self.ir_state},
                })
            if command == "SetIrLights":
                self.ir_state = payload[0]["param"]["IrLights"]["state"]
                return 200, _command_response("SetIrLights", None)
            return 200, _authentication_failure(command)

    def snapshot(self, token):
        with self._lock:
            if token not in self.tokens:
                return 401, b""
            self.commands.append("Snap")
            return 200, self.snapshot_body


class _FakeCameraHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        command, token = self._query()
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        status, body = self.server.camera.handle(command, payload, token)
        self._respond(status, "application/json", body)

    def do_GET(self):  # noqa: N802
        command, token = self._query()
        if command != "Snap":
            self._respond(404, "application/json", b"{}")
            return
        status, body = self.server.camera.snapshot(token)
        self._respond(status, "image/jpeg", body)

    def log_message(self, *_arguments):
        return

    def _query(self):
        _path, _, query = self.path.partition("?")
        fields = dict(
            part.split("=", 1) for part in query.split("&") if "=" in part
        )
        return fields.get("cmd"), fields.get("token")

    def _respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _command_response(command, value):
    entry = {"cmd": command, "code": 0}
    if value is not None:
        entry["value"] = value
    return json.dumps([entry]).encode("utf-8")


def _authentication_failure(command):
    return json.dumps([
        {"cmd": command, "code": 1, "error": {"rspCode": -6, "detail": "login required"}}
    ]).encode("utf-8")


class _SlowCamera:
    """Wraps a client so one read blocks, standing in for a slow 5 s camera."""

    def __init__(self, camera, released):
        self._camera = camera
        self._released = released
        self.reads = 0
        self.first_call_started = threading.Event()
        self._lock = threading.Lock()

    def breaker_seconds_remaining(self):
        return 0

    def ir_state(self):
        with self._lock:
            self.reads += 1
        self.first_call_started.set()
        self._released.wait(timeout=10)
        return self._camera.ir_state

    def set_ir_state(self, state):
        self._camera.ir_state = state


class ManualClock:
    def __init__(self, now=1_757_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingJournal:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    def stages(self):
        return [
            line.split("stage=", 1)[1].split(" ", 1)[0]
            for line in self.lines if "stage=" in line
        ]


class CameraControlEnvironmentTests(unittest.TestCase):
    def test_credentials_are_a_closed_key_set_with_bounded_defaults(self):
        settings = validate_camera_control_environment({
            "GATE_CAMERA_HOST": "192.168.0.54",
            "GATE_CAMERA_USERNAME": "gate",
            "GATE_CAMERA_PASSWORD": "s3cret",
        })

        self.assertEqual("Off", settings["GATE_CAMERA_IR_DEFAULT"])
        self.assertEqual("10", settings["GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES"])
        self.assertEqual("60", settings["GATE_CAMERA_IR_LEASE_MAX_MINUTES"])

    def test_invalid_camera_control_environments_fail_closed(self):
        base = {
            "GATE_CAMERA_HOST": "192.168.0.54",
            "GATE_CAMERA_USERNAME": "gate",
            "GATE_CAMERA_PASSWORD": "s3cret",
        }
        rejected = [
            {**base, "MTX_PATHS_CAMERA_SOURCE": "rtsp://camera/h264Preview_01_sub"},
            {**base, "GATE_CAMERA_HOST": "camera.local"},
            {**base, "GATE_CAMERA_HOST": "127.0.0.1"},
            {**base, "GATE_CAMERA_HOST": "0.0.0.0"},
            {**base, "GATE_CAMERA_IR_DEFAULT": "auto"},
            {**base, "GATE_CAMERA_IR_DEFAULT": "On"},
            {**base, "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "0"},
            {**base, "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "61"},
            {**base, "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "30",
             "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "10"},
            {"GATE_CAMERA_HOST": "192.168.0.54", "GATE_CAMERA_USERNAME": "gate"},
        ]

        for values in rejected:
            with self.subTest(values=sorted(values)):
                with self.assertRaises(MediaConfigError):
                    validate_camera_control_environment(values)

    def test_service_environment_selection_ignores_controller_camera_settings(self):
        selected = validated_camera_control_environment({
            "GATE_CAMERA_HOST": "192.168.0.54",
            "GATE_CAMERA_USERNAME": "gate",
            "GATE_CAMERA_PASSWORD": "s3cret",
            "GATE_CAMERA_STALE_SECONDS": "60",
            "PLATE_RECOGNIZER_API_TOKEN": "unrelated",
        })

        self.assertNotIn("GATE_CAMERA_STALE_SECONDS", selected)
        self.assertNotIn("PLATE_RECOGNIZER_API_TOKEN", selected)

    def test_the_egress_pin_is_written_rather_than_printed(self):
        """No value out of the credential file is ever echoed to stdout.

        The installer used to read the address back from a `--print-host`, which
        put it into a shell variable. The pin is rendered here instead, from the
        address `ipaddress` parsed, so the file holds the canonical address the
        service will dial and nothing crosses a pipe.
        """
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "10-camera-address.conf"

        write_camera_address_dropin(target, {
            "GATE_CAMERA_HOST": "192.168.0.54",
            "GATE_CAMERA_USERNAME": "gate",
            "GATE_CAMERA_PASSWORD": "s3cret",
        })

        self.assertEqual(
            "[Service]\nIPAddressAllow=192.168.0.54/32\n",
            target.read_text(encoding="ascii"),
        )
        self.assertEqual(0o644, stat.S_IMODE(target.stat().st_mode))
        self.assertNotIn("s3cret", target.read_text(encoding="ascii"))

    def test_the_egress_pin_can_never_be_wider_than_one_address(self):
        """`/32` is an IPv4 host route, and an IPv6 lie.

        `fd00::54` used to render `IPAddressAllow=fd00::54/32`, which allows
        2**96 addresses -- the whole point of the pin, gone. IPv6 is refused
        outright instead, because `http.client` splits a bare IPv6 literal at
        its last colon and would never have reached the camera anyway.
        """
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = {"GATE_CAMERA_USERNAME": "gate", "GATE_CAMERA_PASSWORD": "s3cret"}
        ipv4 = Path(directory.name) / "ipv4.conf"
        ipv6 = Path(directory.name) / "ipv6.conf"

        write_camera_address_dropin(ipv4, {**base, "GATE_CAMERA_HOST": "192.168.0.54"})

        self.assertEqual(
            "[Service]\nIPAddressAllow=192.168.0.54/32\n",
            ipv4.read_text(encoding="ascii"),
        )

        for host in ("fd00::54", "2001:db8::1"):
            with self.subTest(host=host):
                with self.assertRaises(MediaConfigError):
                    write_camera_address_dropin(ipv6, {**base, "GATE_CAMERA_HOST": host})
                self.assertFalse(ipv6.exists())

    def test_an_invalid_environment_writes_no_pin_at_all(self):
        """A drop-in with an empty prefix would pin nothing; write none instead."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "10-camera-address.conf"

        with self.assertRaises(MediaConfigError):
            write_camera_address_dropin(target, {
                "GATE_CAMERA_HOST": "camera.local",
                "GATE_CAMERA_USERNAME": "gate",
                "GATE_CAMERA_PASSWORD": "s3cret",
            })

        self.assertFalse(target.exists())
        self.assertEqual([], sorted(Path(directory.name).iterdir()))


class ReolinkClientTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.addCleanup(self.camera.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = ManualClock()

    def client(self, **overrides):
        settings = {
            "token_path": Path(self.directory.name) / "token.json",
            "clock": self.clock,
            "connection_factory": self.camera.connection_factory,
        }
        settings.update(overrides)
        return ReolinkClient(self.camera.host, "gate", "s3cret", **settings)

    def test_one_token_is_cached_across_many_calls_and_reloaded_after_a_restart(self):
        client = self.client()

        for _ in range(5):
            self.assertEqual("Off", client.ir_state())
        client.set_ir_state("Auto")

        self.assertEqual(1, self.camera.logins)

        # A restart re-reads the persisted token instead of logging in again.
        restarted = self.client()
        self.assertEqual("Auto", restarted.ir_state())
        self.assertEqual(1, self.camera.logins)
        self.assertEqual(0, restarted.login_count)

    def test_token_file_is_owner_only_and_rejected_when_it_is_malformed(self):
        client = self.client()
        client.ir_state()
        token_path = Path(self.directory.name) / "token.json"

        self.assertEqual(0o600, token_path.stat().st_mode & 0o777)

        token_path.write_text("not-json", encoding="utf-8")
        token_path.chmod(0o600)
        self.assertIsNone(TokenCache(token_path).load())
        self.assertEqual("Off", self.client().ir_state())
        self.assertEqual(2, self.camera.logins)

    def test_a_502_opens_the_breaker_for_sixty_seconds_without_retrying(self):
        client = self.client()
        client.ir_state()
        self.camera.command_status = 502

        with self.assertRaises(CameraBusy) as busy:
            client.ir_state()
        self.assertEqual(60, busy.exception.retry_after)

        self.camera.command_status = 200
        before = len(self.camera.commands)
        for _ in range(3):
            with self.assertRaises(CameraBusy):
                client.ir_state()
        self.assertEqual(before, len(self.camera.commands))

        self.clock.advance(60)
        self.assertEqual("Off", client.ir_state())

    def test_repeated_logins_are_throttled_to_one_a_minute(self):
        client = self.client(min_login_interval=60.0)
        client.ir_state()
        self.assertEqual(1, self.camera.logins)

        self.camera.expire_next_token = True
        with self.assertRaises(CameraBusy) as busy:
            client.ir_state()

        self.assertEqual(1, self.camera.logins)
        self.assertLessEqual(busy.exception.retry_after, 60)

        self.clock.advance(60)
        self.assertEqual("Off", client.ir_state())
        self.assertEqual(2, self.camera.logins)

    def test_only_one_login_is_ever_in_flight(self):
        client = self.client(min_login_interval=0.0)
        results = []

        def read():
            try:
                results.append(client.ir_state())
            except CameraError as error:  # pragma: no cover - defensive
                results.append(error)

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(["Off"] * 8, results)
        self.assertEqual(1, self.camera.logins)

    def test_the_camera_stops_answering_logins_of_its_own_accord(self):
        """The 502 the firmware really produces: too many logins, not a toggle.

        Nothing in this test sets a status by hand. The camera simply refuses to
        log anyone in past the third attempt, exactly as the RLC-810A does, and
        the client has to stop asking rather than hammering it for a minute.
        """
        client = self.client(min_login_interval=0.0)
        self.camera.login_502_after = 2

        # A token the camera drops costs a re-login. Twice is all it tolerates.
        self.camera.expire_next_token = True
        self.assertEqual("Off", client.ir_state())
        self.assertEqual(2, self.camera.logins)

        self.camera.expire_next_token = True
        with self.assertRaises(CameraBusy) as busy:
            client.ir_state()

        self.assertEqual(3, self.camera.logins)
        self.assertEqual(60, busy.exception.retry_after)

        # The breaker, not politeness: further calls never reach the camera.
        for _ in range(5):
            with self.assertRaises(CameraBusy):
                client.ir_state()
        self.assertEqual(3, self.camera.logins)

    def test_the_re_login_floor_survives_a_restart_that_finds_no_token(self):
        client = self.client()
        client.ir_state()
        self.assertEqual(1, self.camera.logins)
        client._invalidate_token()

        # A crash loop is a restart with the token gone but the camera still
        # inside its post-login 502 window. The floor has to be remembered.
        restarted = self.client()
        with self.assertRaises(CameraBusy):
            restarted.ir_state()
        self.assertEqual(1, self.camera.logins)

        self.clock.advance(60)
        self.assertEqual("Off", self.client().ir_state())
        self.assertEqual(2, self.camera.logins)

    def test_an_unreachable_camera_is_distinguished_from_a_busy_one(self):
        client = self.client()
        self.camera.close()

        with self.assertRaises(CameraUnreachable):
            client.ir_state()

    def test_an_unreachable_camera_opens_the_breaker_exactly_as_a_502_does(self):
        client = self.client()
        client.ir_state()
        self.camera.close()

        with self.assertRaises(CameraUnreachable):
            client.ir_state()

        # Without this, every reader paid a fresh 5 s connect attempt and a due
        # revert queued behind all of them.
        self.assertEqual(60, client.breaker_seconds_remaining())
        with self.assertRaises(CameraBusy):
            client.ir_state()

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required")
    def test_the_real_https_path_accepts_the_cameras_self_signed_certificate(self):
        certificate = self_signed_certificate(self.directory.name)
        camera = FakeCamera(certificate=certificate)
        self.addCleanup(camera.close)
        # No connection_factory override: this goes through _https_connection,
        # whose CERT_NONE context is what lets the camera's own certificate work
        # while the unit pins the reachable peers to its address.
        client = ReolinkClient(
            camera.host, "gate", "s3cret",
            token_path=Path(self.directory.name) / "tls-token.json",
            clock=self.clock,
        )

        self.assertEqual("Off", client.ir_state())
        client.set_ir_state("Auto")

        self.assertEqual("Auto", camera.ir_state)
        self.assertEqual(1, camera.logins)

    def test_snapshot_returns_jpeg_bytes_and_rejects_anything_else(self):
        client = self.client()

        self.assertTrue(client.snapshot().startswith(b"\xff\xd8\xff"))

        self.camera.snapshot_body = b"<html>error</html>"
        with self.assertRaises(CameraError):
            client.snapshot()

    def test_camera_command_allowlist_is_closed(self):
        client = self.client()

        with self.assertRaises(ValueError):
            client._command("SetIsp", 0, {"Isp": {"drc": 1}})
        with self.assertRaises(ValueError):
            client.set_ir_state("On")


class IrLeaseTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.addCleanup(self.camera.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = ManualClock()
        self.journal = RecordingJournal()

    def build(self, **overrides):
        client = ReolinkClient(
            self.camera.host, "gate", "s3cret",
            token_path=Path(self.directory.name) / "token.json",
            clock=self.clock, connection_factory=self.camera.connection_factory,
        )
        settings = {
            "default_state": "Off",
            "lease_path": Path(self.directory.name) / "lease.json",
            "default_lease_minutes": 10,
            "max_lease_minutes": 60,
            "clock": self.clock,
            "journal": lambda stage, **fields: journal(self.journal, stage, **fields),
        }
        settings.update(overrides)
        return IrController(client, **settings)

    def test_a_lease_reverts_to_the_configured_default_when_it_expires(self):
        controller = self.build()

        controller.set_state("Auto", 10)
        self.assertEqual("Auto", self.camera.ir_state)

        self.clock.advance(599)
        self.assertFalse(controller.run_due_revert())
        self.assertEqual("Auto", self.camera.ir_state)

        self.clock.advance(2)
        self.assertTrue(controller.run_due_revert())
        self.assertEqual("Off", self.camera.ir_state)
        self.assertIsNone(controller.snapshot()["effective_until"])
        self.assertIn("ir_revert", self.journal.stages())

    def test_a_restart_during_a_lease_reverts_before_the_service_answers(self):
        controller = self.build()
        controller.set_state("Auto", 30)
        lease_path = Path(self.directory.name) / "lease.json"
        self.assertTrue(lease_path.exists())

        # The process dies mid-lease; a fresh controller reads the persisted lease.
        restarted = self.build()
        self.assertEqual("Auto", self.camera.ir_state)

        restarted.restore_default_on_start()

        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(lease_path.exists())
        self.assertIn("startup_revert", self.journal.stages())

    def test_a_restart_with_no_outstanding_lease_never_writes_to_the_camera(self):
        """It reads once, and changes nothing when the camera already agrees.

        The read is what makes a lost lease record recoverable. It costs one
        `GetIrLights` on the cached token, so a restart loop still cannot become
        a login storm, and no `SetIrLights` is issued at all.
        """
        controller = self.build()

        controller.restore_default_on_start()

        self.assertEqual(["GetIrLights"], self.camera.commands)
        self.assertEqual("Off", self.camera.ir_state)

    def test_a_lease_record_lost_with_the_reboot_still_ends_the_lease(self):
        """The failure the persisted lease was supposed to prevent.

        `/run` is tmpfs and is recreated empty at boot, so a power cut during a
        lease took the record with it. The camera is on its own supply, kept the
        leased state, and nothing on the Pi was left that knew to put it back --
        the heartbeat published `ready` with `Auto` and no expiry, for ever.
        """
        controller = self.build()
        controller.set_state("Auto", 60)
        lease_path = Path(self.directory.name) / "lease.json"
        lease_path.unlink()
        self.assertEqual("Auto", self.camera.ir_state)

        restarted = self.build()
        restarted.restore_default_on_start()

        self.assertEqual("Off", self.camera.ir_state)
        self.assertIsNone(restarted.snapshot()["effective_until"])
        self.assertFalse(lease_path.exists())
        self.assertIn("startup_reconcile", self.journal.stages())

    def test_a_camera_that_will_not_answer_on_start_is_never_written_to(self):
        """Nothing is written on the strength of a camera we could not read."""
        controller = self.build()
        controller.set_state("Auto", 60)
        (Path(self.directory.name) / "lease.json").unlink()
        writes = self.camera.commands.count("SetIrLights")
        self.camera.command_status = 500

        restarted = self.build()
        restarted.restore_default_on_start()

        self.assertEqual(writes, self.camera.commands.count("SetIrLights"))
        self.assertEqual("unknown", restarted.snapshot()["state"])

    def test_a_lease_timestamp_outside_any_sane_window_is_corrupt_not_a_lease(self):
        """`expires_at: 1e18` used to freeze the whole service.

        `_isoformat` raised `OSError` out of `snapshot()`, which the state
        publisher calls, so the heartbeat stuck at `service_unhealthy` and the
        revert never ran. An unusable record is not "no lease": the camera may
        still be holding whatever it described, so the default is restored.
        """
        lease_path = Path(self.directory.name) / "lease.json"
        lease_path.write_text(
            json.dumps({"state": "Auto", "expires_at": 1e18, "set_at": 1e18}),
            encoding="utf-8",
        )
        self.camera.ir_state = "Auto"

        controller = self.build()

        document = state_document(controller.snapshot())
        self.assertEqual("Off", document["camera_control"]["ir"]["default"])
        self.assertIn("lease_corrupt", self.journal.stages())

        controller.restore_default_on_start()

        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(lease_path.exists())

    def test_a_change_that_fails_outright_leaves_the_state_unknown_not_stale(self):
        """A plain `CameraError` says nothing about whether the change landed.

        Only `camera_busy` and `camera_unreachable` used to clear the
        observation, so a nonzero `rspCode` or an unexpected status left the
        pre-change reading fresh: the heartbeat published `ready` with `Off`
        while the camera may well have been switched to `Auto`.
        """
        controller = self.build()
        self.assertTrue(controller.refresh_observation())
        self.assertEqual("Off", controller.snapshot()["state"])
        self.camera.command_status = 500

        with self.assertRaises(CameraError):
            controller.set_state("Auto", 10)

        snapshot = controller.snapshot()
        self.assertEqual("unknown", snapshot["state"])
        self.assertEqual("camera_error", snapshot["last_error"])
        self.assertFalse(state_document(snapshot)["camera_control"]["available"])
        # The lease is still kept, so the revert still fires.
        self.assertIsNotNone(snapshot["effective_until"])
        self.camera.command_status = 200
        self.clock.advance(601)
        self.assertTrue(controller.run_due_revert())
        self.assertEqual("Off", self.camera.ir_state)

    def test_a_cancel_keeps_the_lease_on_disk_until_the_camera_confirms(self):
        """The record has to outlive the call that ends it.

        Removing it first opens a window -- a crash, a power cut, an OOM kill --
        in which the lease is gone and the camera is still holding the leased
        state. `_revert` already wrote the removal after the camera confirmed;
        the cancel path did it the other way round.
        """
        controller = self.build()
        controller.set_state("Auto", 10)
        lease_path = Path(self.directory.name) / "lease.json"
        seen = []
        original = controller._client.set_ir_state

        def watched(state):
            seen.append(lease_path.exists())
            return original(state)

        controller._client.set_ir_state = watched
        controller.set_state("Off", 10)

        self.assertEqual([True], seen)
        self.assertFalse(lease_path.exists())
        self.assertEqual("Off", self.camera.ir_state)

    def test_a_failed_revert_is_retried_with_backoff_and_stays_visible(self):
        controller = self.build()
        controller.set_state("Auto", 1)
        self.clock.advance(61)
        self.camera.command_status = 502

        self.assertFalse(controller.run_due_revert())
        snapshot = controller.snapshot()
        self.assertTrue(snapshot["revert_failed"])
        self.assertEqual("camera_busy", snapshot["last_error"])

        # The breaker and the retry backoff both have to clear before a retry.
        self.assertFalse(controller.run_due_revert())
        self.camera.command_status = 200
        self.clock.advance(61)

        self.assertTrue(controller.run_due_revert())
        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(controller.snapshot()["revert_failed"])

    def test_the_revert_worker_thread_fires_a_real_expiry(self):
        controller = self.build(clock=time.time, default_lease_minutes=1)
        controller._lease = {
            "state": "Auto", "expires_at": time.time() - 1, "set_at": time.time() - 61,
        }
        controller._client.set_ir_state("Auto")
        worker = RevertWorker(controller, interval_seconds=0.01)
        worker.start()
        self.addCleanup(worker.stop)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.camera.ir_state != "Off":
            time.sleep(0.01)

        self.assertEqual("Off", self.camera.ir_state)

    def test_lease_bounds_are_enforced_and_the_default_is_used_when_omitted(self):
        controller = self.build(max_lease_minutes=60)

        controller.set_state("Auto")
        self.assertEqual(600, controller.snapshot()["lease_seconds_remaining"])

        for invalid in (0, -1, 61, 1.5, True, "10"):
            with self.subTest(lease=invalid):
                with self.assertRaises(ValueError):
                    controller.set_state("Auto", invalid)

    def test_setting_the_default_state_clears_the_lease_immediately(self):
        controller = self.build()
        controller.set_state("Auto", 30)

        snapshot = controller.set_state("Off", 30)

        self.assertIsNone(snapshot["effective_until"])
        self.assertFalse((Path(self.directory.name) / "lease.json").exists())

    def test_an_indeterminate_set_still_leaves_a_lease_that_will_revert(self):
        controller = self.build()
        self.camera.command_status = 502

        with self.assertRaises(CameraError):
            controller.set_state("Auto", 5)

        # The camera may have applied the change before failing to answer, so a
        # restart has to find a lease and revert it.
        lease_path = Path(self.directory.name) / "lease.json"
        self.assertTrue(lease_path.exists())

        self.camera.command_status = 200
        self.camera.ir_state = "Auto"
        self.clock.advance(61)
        restarted = self.build()
        restarted.restore_default_on_start()

        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(lease_path.exists())

    def test_a_failed_revert_keeps_the_outstanding_lease_for_the_retry(self):
        controller = self.build()
        controller.set_state("Auto", 5)
        self.camera.command_status = 502

        with self.assertRaises(CameraError):
            controller.set_state("Off", 5)

        lease_path = Path(self.directory.name) / "lease.json"
        self.assertTrue(lease_path.exists())

        self.camera.command_status = 200
        self.clock.advance(361)
        self.assertTrue(controller.run_due_revert())
        self.assertEqual("Off", self.camera.ir_state)
        self.assertFalse(lease_path.exists())

    def test_an_unreachable_camera_reports_unknown_rather_than_off(self):
        controller = self.build()
        controller.set_state("Auto", 10)
        self.camera.close()
        self.clock.advance(120)

        snapshot = controller.state()

        self.assertEqual("unknown", snapshot["state"])
        self.assertEqual("camera_unreachable", snapshot["last_error"])
        self.assertEqual("Off", snapshot["default"])

    def test_a_due_revert_is_not_starved_by_concurrent_readers(self):
        """The measured failure: readers held an expired lease's revert off.

        Reads used to make a bounded camera call while holding the state lock,
        so a handful of them in flight kept the revert waiting and IR stayed on
        well past its expiry. The revert now takes priority and the reads that
        cannot get the camera answer from the last observation instead.
        """
        controller = self.build(clock=time.time, default_lease_minutes=1)
        controller.set_state("Auto", 1)
        released = threading.Event()
        slow = _SlowCamera(self.camera, released)
        controller._client = slow
        controller._lease["expires_at"] = time.time() - 1

        readers = [
            threading.Thread(target=controller.state, daemon=True) for _ in range(6)
        ]
        for reader in readers:
            reader.start()
        slow.first_call_started.wait(timeout=5)

        started = time.monotonic()
        reverted = controller.run_due_revert()
        elapsed = time.monotonic() - started
        released.set()
        for reader in readers:
            reader.join(timeout=5)

        self.assertTrue(reverted)
        self.assertEqual("Off", self.camera.ir_state)
        # One in-flight read at most, never six queued in front of the revert.
        self.assertLess(elapsed, 2.0)
        self.assertLessEqual(slow.reads, 1)

    def test_concurrent_operators_are_serialised_and_both_journaled(self):
        controller = self.build()

        def toggle(state):
            controller.set_state(state, 5)

        threads = [
            threading.Thread(target=toggle, args=(state,))
            for state in ("Auto", "Auto", "Auto", "Auto")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual("Auto", self.camera.ir_state)
        self.assertEqual(4, self.journal.stages().count("ir_set"))


class CameraControlHttpTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.addCleanup(self.camera.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = RecordingJournal()
        # The service reads time only through this clock, so every budget it
        # keeps -- the token buckets, the snapshot interval, the lease -- moves
        # when the test moves it and at no other moment. Requests still travel
        # over a real socket; only the deadlines are ours. Without this a slow
        # runner could let a bucket refill between two requests the test
        # intended to be adjacent, and the assertion would turn on the
        # machine's speed rather than on the limiter.
        self.clock = ManualClock()
        self.service = build_service(
            {
                "GATE_CAMERA_HOST": self.camera.host,
                "GATE_CAMERA_USERNAME": "gate",
                "GATE_CAMERA_PASSWORD": "s3cret",
                "GATE_CAMERA_IR_DEFAULT": "Off",
                "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "10",
                "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "60",
            },
            token_path=Path(self.directory.name) / "token.json",
            lease_path=Path(self.directory.name) / "lease.json",
            logger=self.journal,
            connection_factory=self.camera.connection_factory,
            clock=self.clock,
        )
        self.server = CameraControlServer(
            ("127.0.0.1", 0), self.service, logger=self.journal
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, method, path, body=None, content_type="application/json"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={} if data is None else {"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def json_request(self, method, path, body=None):
        status, headers, payload = self.request(method, path, body)
        return status, json.loads(payload.decode("utf-8")), headers

    def test_state_is_readable_on_both_documented_paths(self):
        for path in ("/camera/state", "/camera/ir"):
            with self.subTest(path=path):
                status, payload, _ = self.json_request("GET", path)

                self.assertEqual(200, status)
                self.assertEqual({"observed_at", "ir"}, set(payload))
                self.assertEqual("Off", payload["ir"]["state"])
                self.assertEqual("Off", payload["ir"]["default"])
                self.assertIsNone(payload["ir"]["effective_until"])
                self.assertFalse(payload["ir"]["revert_failed"])

    def test_an_ir_lease_is_applied_and_echoed_with_its_expiry(self):
        status, payload, _ = self.json_request(
            "POST", "/camera/ir", {"state": "Auto", "lease_minutes": 5}
        )

        self.assertEqual(200, status)
        self.assertEqual("completed", payload["status"])
        self.assertEqual("Auto", payload["ir"]["state"])
        self.assertEqual(300, payload["ir"]["lease_seconds_remaining"])
        self.assertIsNotNone(payload["ir"]["effective_until"])
        self.assertEqual("Auto", self.camera.ir_state)
        self.assertIn("ir_set", self.journal.stages())

    def test_ttl_seconds_is_accepted_as_the_worker_compatible_alias(self):
        status, payload, _ = self.json_request(
            "POST", "/camera/ir", {"state": "Auto", "ttl_seconds": 1800}
        )

        self.assertEqual(200, status)
        self.assertEqual(1800, payload["ir"]["lease_seconds_remaining"])

    def test_an_omitted_lease_uses_the_configured_default(self):
        _status, payload, _ = self.json_request("POST", "/camera/ir", {"state": "Auto"})

        self.assertEqual(600, payload["ir"]["lease_seconds_remaining"])

    def test_a_replayed_idempotency_key_answers_from_the_live_lease(self):
        body = {"state": "Auto", "lease_minutes": 5, "idempotency_key": "abc-123"}

        _status, first, _ = self.json_request("POST", "/camera/ir", body)
        commands = len(self.camera.commands)
        # Wind the lease back, as if the replay had arrived four minutes later.
        self.service.controller._lease["expires_at"] -= 240
        _status, second, _ = self.json_request("POST", "/camera/ir", body)

        self.assertEqual(commands, len(self.camera.commands))
        self.assertEqual("completed", second["status"])
        self.assertEqual("abc-123", second["idempotency_key"])
        # A frozen copy would still promise the original 300 s.
        self.assertEqual(300, first["ir"]["lease_seconds_remaining"])
        self.assertEqual(60, second["ir"]["lease_seconds_remaining"])

    def test_two_concurrent_posts_of_one_key_reach_the_camera_once(self):
        body = {"state": "Auto", "lease_minutes": 5, "idempotency_key": "same-key"}
        results = []
        lock = threading.Lock()

        def post():
            _status, payload, _ = self.json_request("POST", "/camera/ir", body)
            with lock:
                results.append(payload)

        threads = [threading.Thread(target=post) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(4, len(results))
        self.assertEqual(1, self.camera.commands.count("SetIrLights"))
        self.assertTrue(all(payload["status"] == "completed" for payload in results))
        self.assertTrue(all(
            payload["ir"]["effective_until"] == results[0]["ir"]["effective_until"]
            for payload in results
        ))

    def test_a_replayed_key_repeats_the_failure_rather_than_reporting_success(self):
        """A replay of a call that failed must not be answered `completed`.

        Two concurrent posts of one key: the owner's `SetIrLights` raises and it
        answers 502, while the duplicate used to be handed
        `200 {"status":"completed"}` built from a snapshot taken *before* the
        change -- the app recorded a landed IR change the camera had refused.
        """
        body = {"state": "Auto", "lease_minutes": 5, "idempotency_key": "poisoned"}
        self.camera.command_status = 500

        status, first, _ = self.json_request("POST", "/camera/ir", body)

        self.assertEqual(502, status)
        self.assertEqual("camera_error", first["error"])

        self.camera.command_status = 200
        status, second, _ = self.json_request("POST", "/camera/ir", body)

        self.assertEqual(502, status)
        self.assertEqual("camera_error", second["error"])
        # The replay is answered from the record, not from a second attempt.
        self.assertEqual(1, self.journal.stages().count("ir_set"))

    def test_a_replay_that_outlives_the_wait_is_never_called_completed(self):
        """The owner is still talking to the camera; nothing is known yet.

        `502 camera_indeterminate` is the answer PR #36's `setIrState` records
        as indeterminate: a 2xx of any shape is `completed` there, and a 4xx or
        a `503 camera_busy` is `failed`. Neither is true here.
        """
        key = "still-in-flight"
        call, owned = self.service._reserve(key)
        self.assertTrue(owned)
        self.assertFalse(call.event.is_set())

        with mock.patch.object(
            camera_control_main, "IDEMPOTENCY_WAIT_SECONDS", 0.05
        ):
            status, payload, _ = self.json_request("POST", "/camera/ir", {
                "state": "Auto", "lease_minutes": 5, "idempotency_key": key,
            })

        self.assertEqual(502, status)
        self.assertEqual("camera_indeterminate", payload["error"])
        self.assertNotIn("status", payload)
        self.assertEqual(0, self.camera.commands.count("SetIrLights"))

    def test_a_stalled_connection_is_closed_rather_than_parking_a_thread(self):
        """`TasksMax=64`: twenty half-open connections used to be an outage.

        `BaseHTTPRequestHandler.timeout` is `None` by default, so a connection
        that opened and then said nothing held its thread for ever.
        """
        self.assertEqual(10, CameraControlHandler.timeout)

        with mock.patch.object(CameraControlHandler, "timeout", 0.5):
            connection = socket.create_connection(
                ("127.0.0.1", self.server.server_address[1]), timeout=10
            )
            self.addCleanup(connection.close)
            # A request line that never ends: the server is left reading.
            connection.sendall(b"GET /camera/state")
            started = time.monotonic()

            self.assertEqual(b"", connection.recv(4096))

        self.assertLess(time.monotonic() - started, 5)
        # The service itself is untouched by the dropped connection.
        self.assertEqual(200, self.json_request("GET", "/camera/state")[0])

    def test_a_still_is_served_on_every_path_and_method_the_docs_name(self):
        for method, path in (
            ("GET", "/camera/snap"), ("POST", "/camera/snap"),
            ("GET", "/camera/snapshot"), ("POST", "/camera/snapshot"),
        ):
            with self.subTest(method=method, path=path):
                self.clock.advance(SNAPSHOT_MIN_INTERVAL_SECONDS)
                status, headers, payload = self.request(method, path)

                self.assertEqual(200, status)
                self.assertEqual("image/jpeg", headers["Content-Type"])
                self.assertTrue(payload.startswith(b"\xff\xd8\xff"))

    def test_head_returns_the_headers_of_the_get_and_no_body(self):
        connection = HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(connection.close)

        connection.request("HEAD", "/camera/state")
        response = connection.getresponse()
        body = response.read()

        self.assertEqual(200, response.status)
        self.assertEqual("application/json", response.getheader("Content-Type"))
        self.assertEqual(b"", body)
        self.assertNotEqual("0", response.getheader("Content-Length"))

        # The connection is still framed: the next request on it is answered.
        connection.request("GET", "/camera/state", headers={"Connection": "close"})
        self.assertEqual(200, connection.getresponse().status)

    def test_a_malformed_request_line_gets_a_status_line_and_a_close(self):
        raw = socket.create_connection(
            ("127.0.0.1", self.server.server_address[1]), timeout=10
        )
        self.addCleanup(raw.close)

        raw.sendall(b"NOT-A-REQUEST-LINE\r\n\r\n")
        # Read to the close, not to the header terminator: the frame this
        # response announces ends at the close, and stopping at the first blank
        # line asserts against whatever happened to arrive in one segment.
        answer = b""
        while True:
            chunk = raw.recv(4096)
            if not chunk:
                break
            answer += chunk

        # Previously the body was written with no status line at all, and the
        # connection was then left open for a reply nothing could parse.
        self.assertTrue(answer.startswith(b"HTTP/1.1 400"), answer[:64])
        self.assertIn(b'{"error":"invalid_request"}', answer)
        self.assertIn(b"Connection: close", answer)

    def test_reads_and_lease_changes_are_rate_limited(self):
        # The clock does not move unless this test moves it, so the eleventh
        # read is refused because ten preceded it and for no other reason. On a
        # slow runner the wall clock used to refill the bucket mid-loop and the
        # eleventh read was allowed, which failed the test for a reason that had
        # nothing to do with the limiter.
        for _ in range(STATE_BURST):
            self.assertEqual(200, self.json_request("GET", "/camera/state")[0])

        status, payload, headers = self.json_request("GET", "/camera/state")

        self.assertEqual(429, status)
        self.assertEqual("rate_limited", payload["error"])
        self.assertEqual(str(payload["retry_after"]), headers["Retry-After"])

        # A refused caller that waits exactly as long as it was told is served,
        # and is not left to guess: the budget refills, it does not latch.
        self.clock.advance(payload["retry_after"])
        self.assertEqual(200, self.json_request("GET", "/camera/state")[0])

        commands = len(self.camera.commands)
        for _ in range(IR_BURST):
            self.assertEqual(
                200, self.json_request("POST", "/camera/ir", {"state": "Auto"})[0]
            )
        status, payload, _ = self.json_request("POST", "/camera/ir", {"state": "Auto"})

        self.assertEqual(429, status)
        self.assertEqual("rate_limited", payload["error"])
        # The refused lease change never reached the camera.
        self.assertEqual(IR_BURST, len(self.camera.commands) - commands)

        self.clock.advance(payload["retry_after"])
        self.assertEqual(
            200, self.json_request("POST", "/camera/ir", {"state": "Auto"})[0]
        )
        self.assertEqual(IR_BURST + 1, len(self.camera.commands) - commands)

    def test_bad_input_is_rejected_without_reaching_the_camera(self):
        commands = len(self.camera.commands)
        rejected = [
            {"state": "On"},
            {"state": "auto"},
            {"state": "Auto", "lease_minutes": 0},
            {"state": "Auto", "lease_minutes": 61},
            {"state": "Auto", "lease_minutes": "10"},
            {"state": "Auto", "lease_minutes": 10, "ttl_seconds": 600},
            {"state": "Auto", "ttl_seconds": 30},
            {"state": "Auto", "cmd": "SetIsp"},
            {"cmd": "SetIsp", "param": {}},
            [],
            "Auto",
        ]

        for body in rejected:
            with self.subTest(body=body):
                status, payload, _ = self.json_request("POST", "/camera/ir", body)
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", payload["error"])

        self.assertEqual(commands, len(self.camera.commands))
        self.assertEqual("Off", self.camera.ir_state)

    def test_oversized_and_unroutable_requests_are_bounded(self):
        status, _headers, _body = self.request(
            "POST", "/camera/ir", {"state": "Auto", "idempotency_key": "x" * 8192}
        )
        self.assertEqual(413, status)

        status, payload, _ = self.json_request("GET", "/camera/../etc/passwd")
        self.assertEqual(404, status)

        status, payload, _ = self.json_request("GET", "/camera/state?cmd=Login")
        self.assertEqual(404, status)

        status, payload, _ = self.json_request("POST", "/camera/state", {})
        self.assertEqual(405, status)

    def test_snapshots_are_jpeg_and_rate_limited_to_one_every_two_seconds(self):
        status, headers, body = self.request("GET", "/camera/snap")

        self.assertEqual(200, status)
        self.assertEqual("image/jpeg", headers["Content-Type"])
        self.assertTrue(body.startswith(b"\xff\xd8\xff"))

        status, payload, headers = self.json_request("GET", "/camera/snap")

        self.assertEqual(429, status)
        self.assertEqual("rate_limited", payload["error"])
        self.assertEqual(2, payload["retry_after"])
        self.assertEqual("2", headers["Retry-After"])

        # Half a second short of the interval is still too soon, and the moment
        # it has fully elapsed the snapshot is allowed. Both edges are waited
        # out on the test's own clock, rather than by reaching in and
        # back-dating the service's private bookkeeping.
        self.clock.advance(SNAPSHOT_MIN_INTERVAL_SECONDS - 0.5)
        self.assertEqual(429, self.json_request("GET", "/camera/snap")[0])

        self.clock.advance(0.5)
        status, _headers, body = self.request("GET", "/camera/snapshot")
        self.assertEqual(200, status)

    def test_a_busy_camera_answers_503_with_a_retry_after_and_no_payload_echo(self):
        self.json_request("GET", "/camera/state")
        self.camera.command_status = 502

        status, payload, headers = self.json_request(
            "POST", "/camera/ir", {"state": "Auto"}
        )

        self.assertEqual(503, status)
        self.assertEqual({"error": "camera_busy", "retry_after": 60}, payload)
        self.assertEqual("60", headers["Retry-After"])
        self.assertIn("camera_busy", self.journal.stages())
        self.assertNotIn("s3cret", " ".join(self.journal.lines))

    def test_an_unreachable_camera_answers_503_camera_unreachable(self):
        self.camera.close()

        status, payload, _ = self.json_request("POST", "/camera/ir", {"state": "Auto"})

        self.assertEqual(503, status)
        self.assertEqual({"error": "camera_unreachable"}, payload)

    def test_no_journal_line_carries_a_credential_or_a_camera_payload(self):
        self.json_request("POST", "/camera/ir", {"state": "Auto", "lease_minutes": 5})
        self.request("GET", "/camera/snap")

        lines = "\n".join(self.journal.lines)

        self.assertNotIn("s3cret", lines)
        self.assertNotIn("gate:", lines)
        self.assertNotIn("token", lines.replace("token_", ""))
        self.assertTrue(all(
            line.startswith("gate_camera_control stage=") for line in self.journal.lines
        ))


class JournalFormatTests(unittest.TestCase):
    def test_every_line_is_one_prefixed_key_value_record(self):
        recorder = RecordingJournal()

        journal(recorder, "ir_set", state="Auto", lease_seconds=600, outcome="completed")
        journal(recorder, "ir_revert", state="Off", outcome=None)
        journal(recorder, "camera_busy", retry_after=60)

        self.assertEqual([
            "gate_camera_control stage=ir_set lease_seconds=600 outcome=completed"
            " state=Auto",
            "gate_camera_control stage=ir_revert state=Off",
            "gate_camera_control stage=camera_busy retry_after=60",
        ], recorder.lines)

    def test_journal_values_cannot_inject_extra_fields_or_newlines(self):
        recorder = RecordingJournal()

        journal(recorder, "ir_set", state="Off outcome=completed\nstage=forged")

        self.assertEqual(
            ["gate_camera_control stage=ir_set state=Offoutcomecompletedstageforged"],
            recorder.lines,
        )


class StateDocumentTests(unittest.TestCase):
    def test_a_known_state_is_available_and_ready(self):
        document = state_document({
            "state": "Auto", "default": "Off", "effective_until": "2026-09-07T21:00:00+00:00",
            "lease_seconds_remaining": 600, "revert_failed": False, "last_error": None,
        }, now=1_757_000_000)

        self.assertEqual({
            "observed_at": 1_757_000_000,
            "camera_control": {
                "available": True,
                "reason": "ready",
                "ir": {
                    "state": "Auto",
                    "default": "Off",
                    "effective_until": "2026-09-07T21:00:00+00:00",
                    "revert_failed": False,
                },
            },
        }, document)

    def test_an_unknown_state_carries_the_reason_and_never_claims_off(self):
        document = state_document({
            "state": "unknown", "default": "Off", "effective_until": None,
            "lease_seconds_remaining": None, "revert_failed": True,
            "last_error": "camera_busy",
        }, now=1_757_000_000)

        block = document["camera_control"]

        self.assertFalse(block["available"])
        self.assertEqual("camera_busy", block["reason"])
        self.assertEqual("unknown", block["ir"]["state"])
        self.assertTrue(block["ir"]["revert_failed"])

    def test_a_camera_nobody_has_looked_at_is_not_reported_unreachable(self):
        """`not_observed` and `camera_unreachable` are different claims.

        At rest, with no lease and no request since a restart, nothing has
        called the camera. Reporting that as unreachable made a healthy camera
        look broken and the app hid the control until someone opened the page.
        """
        document = state_document({
            "state": "unknown", "default": "Off", "effective_until": None,
            "lease_seconds_remaining": None, "revert_failed": False,
            "last_error": None,
        }, now=1_757_000_000)

        block = document["camera_control"]

        self.assertEqual("not_observed", block["reason"])
        self.assertFalse(block["available"])
        # A real failure still says so.
        self.assertEqual("camera_unreachable", state_document({
            "state": "unknown", "default": "Off", "effective_until": None,
            "lease_seconds_remaining": None, "revert_failed": False,
            "last_error": "camera_unreachable",
        })["camera_control"]["reason"])

    def test_the_publisher_observes_the_camera_on_its_own_slow_timer(self):
        clock = ManualClock()
        refreshes = []
        publisher = StatePublisher(
            "/dev/null", dict, refresher=lambda: refreshes.append(clock.now),
            refresh_interval_seconds=30.0, clock=clock,
        )

        self.assertTrue(publisher.refresh_if_due())
        clock.advance(29)
        self.assertFalse(publisher.refresh_if_due())
        clock.advance(2)
        self.assertTrue(publisher.refresh_if_due())

        self.assertEqual(2, len(refreshes))

    def test_the_published_document_names_no_camera_and_no_credential(self):
        document = state_document({
            "state": "Off", "default": "Off", "effective_until": None,
            "lease_seconds_remaining": None, "revert_failed": False, "last_error": None,
        })

        text = json.dumps(document)

        self.assertNotIn("192.168", text)
        self.assertNotIn("password", text.lower())
        self.assertNotIn("token", text.lower())


class ServiceFacadeTests(unittest.TestCase):
    def test_the_service_never_exposes_a_free_form_camera_command(self):
        source = Path(__file__).resolve().parents[1] / "gate_camera_control"
        text = "\n".join(
            (source / name).read_text(encoding="utf-8")
            for name in ("__main__.py", "ir.py", "reolink.py", "state.py")
        )

        self.assertNotIn("SetIsp", text)
        self.assertIn(
            'ALLOWED_COMMANDS = frozenset({"Login", "GetIrLights", "SetIrLights", "Snap"})',
            text,
        )

    def test_the_snapshot_facade_reports_the_remaining_wait_exactly_once(self):
        clock = ManualClock()
        client = type("Client", (), {"snapshot": staticmethod(lambda: JPEG)})()
        controller = type("Controller", (), {
            "max_lease_minutes": 60, "snapshot": staticmethod(dict),
        })()
        service = CameraControlService(
            controller, client, clock=clock, logger=RecordingJournal()
        )

        self.assertEqual(JPEG, service.snapshot())
        clock.advance(0.5)
        with self.assertRaises(Exception) as limited:
            service.snapshot()
        self.assertEqual(2, limited.exception.retry_after)
        clock.advance(2)
        self.assertEqual(JPEG, service.snapshot())


if __name__ == "__main__":
    unittest.main()
