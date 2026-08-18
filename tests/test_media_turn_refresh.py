import os
import configparser
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.gate_media_turn_refresh import (
    TurnRefreshError,
    fetch_ice_servers,
    refresh_turn_credentials,
    select_turn_credentials,
)
from gate_media_config import MediaConfigError, validate_turn_environment


class FakeGatewayService:
    def __init__(self, restart_results=(True,)):
        self.active = True
        self.restart_results = iter(restart_results)
        self.calls = []

    def is_active(self):
        self.calls.append("is-active")
        return self.active

    def restart(self):
        self.calls.append("restart")
        succeeded = next(self.restart_results)
        self.active = succeeded
        return succeeded

    def stop(self):
        self.calls.append("stop")
        self.active = False
        return True


class MediaTurnRefreshTests(unittest.TestCase):
    def test_credential_request_uses_cloudflare_ice_endpoint_and_24_hour_ttl(self):
        class Response:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, _type, _value, _traceback):
                return False

            def read(self, _limit):
                return b'{"iceServers": []}'

        class Opener:
            def open(self, request, *, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        with patch("deployment.gate_media_turn_refresh.build_opener", return_value=opener):
            self.assertEqual({"iceServers": []}, fetch_ice_servers("key-id", "api-token"))

        request = opener.request
        self.assertEqual(
            "https://rtc.live.cloudflare.com/v1/turn/keys/key-id/credentials/"
            "generate-ice-servers",
            request.full_url,
        )
        self.assertEqual(
            {"ttl": 86400, "customIdentifier": "gate-mate-pi"},
            __import__("json").loads(request.data.decode("ascii")),
        )
        self.assertEqual("Bearer api-token", request.get_header("Authorization"))

    def test_rotation_units_keep_long_term_secret_outside_gateway_and_refresh_on_boot(self):
        service = self._read_unit("deployment/systemd/gate-media-turn-refresh.service")
        timer = self._read_unit("deployment/systemd/gate-media-turn-refresh.timer")

        self.assertEqual("oneshot", service["Service"].get("Type"))
        self.assertEqual("root", service["Service"].get("User"))
        self.assertNotIn("EnvironmentFile", service["Service"])
        self.assertNotIn("TURN_KEY", (Path(__file__).parents[1] /
                                        "deployment/systemd/gate-media-turn-refresh.service").read_text())
        self.assertIn("/usr/bin/flock --nonblock", service["Service"].get("ExecStart"))
        self.assertEqual(
            {"/etc", "/run/gate-media-turn-refresh"},
            set(service["Service"].get("ReadWritePaths").split()),
        )
        self.assertEqual("", service["Service"].get("CapabilityBoundingSet"))
        self.assertIn("ProtectSystem=strict", (Path(__file__).parents[1] /
                                                  "deployment/systemd/gate-media-turn-refresh.service").read_text())
        self.assertEqual("2min", timer["Timer"].get("OnBootSec"))
        self.assertEqual("12h", timer["Timer"].get("OnUnitInactiveSec"))
        self.assertEqual("15min", timer["Timer"].get("RandomizedDelaySec"))
        self.assertEqual("true", timer["Timer"].get("Persistent"))
        self.assertEqual("gate-media-turn-refresh.service", timer["Timer"].get("Unit"))

    def test_installer_enables_timer_only_for_a_valid_root_turn_secret(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            turn = root / "gate-media-turn.env"
            turn.write_text(
                "TURN_KEY_ID=turn-key-id\nTURN_KEY_API_TOKEN=long-term-api-token\n",
                encoding="utf-8",
            )
            turn.chmod(0o600)
            log = root / "systemctl.log"
            command = f"""
source deployment/install-media.sh
MEDIA_TURN_ENV={shlex.quote(str(turn))}
MEDIA_LIBRARY={shlex.quote(str(Path(__file__).parents[1]))}
MEDIA_TURN_REFRESH_HELPER={shlex.quote(str(Path(__file__).parents[1] / 'deployment/gate_media_turn_refresh.py'))}
systemctl() {{ printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}; }}
configure_turn_refresh_timer
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                ["enable --now gate-media-turn-refresh.timer"],
                log.read_text(encoding="utf-8").splitlines(),
            )

    def test_installer_keeps_manual_static_turn_install_supported_without_refresh_secret(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            log = root / "systemctl.log"
            command = f"""
source deployment/install-media.sh
MEDIA_TURN_ENV={shlex.quote(str(root / 'missing-turn.env'))}
MEDIA_LIBRARY={shlex.quote(str(root / 'library'))}
systemctl() {{ printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}; }}
configure_turn_refresh_timer
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                ["disable --now gate-media-turn-refresh.timer"],
                log.read_text(encoding="utf-8").splitlines(),
            )

    def test_turn_secret_environment_allows_only_the_two_root_credentials(self):
        expected = {
            "TURN_KEY_ID": "turn-key-id",
            "TURN_KEY_API_TOKEN": "long-term-api-token",
        }

        self.assertEqual(expected, validate_turn_environment(expected))
        for invalid in (
            {"TURN_KEY_ID": "turn-key-id"},
            {**expected, "MTX_WEBRTCICESERVERS2_0_PASSWORD": "forbidden"},
            {"TURN_KEY_ID": "turn key", "TURN_KEY_API_TOKEN": "token"},
            {"TURN_KEY_ID": "turn/key", "TURN_KEY_API_TOKEN": "token"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(MediaConfigError):
                    validate_turn_environment(invalid)

    def test_selects_tls_5349_before_other_authenticated_turn_urls(self):
        selected = select_turn_credentials({
            "iceServers": [
                {"urls": ["stun:stun.cloudflare.com:3478"]},
                {
                    "urls": [
                        "turn:turn.cloudflare.com:3478?transport=udp",
                        "turn:turn.cloudflare.com:53?transport=udp",
                        "turns:turn.cloudflare.com:5349?transport=tcp",
                    ],
                    "username": "short-lived-user",
                    "credential": "short-lived-password",
                },
            ],
        })

        self.assertEqual(
            (
                "turns:turn.cloudflare.com:5349?transport=tcp",
                "short-lived-user",
                "short-lived-password",
            ),
            selected,
        )

    def test_rejects_malformed_or_port_53_only_turn_responses(self):
        payloads = (
            {},
            {"iceServers": {}},
            {"iceServers": [{"urls": ["turn:turn.cloudflare.com:53?transport=udp"],
                             "username": "user", "credential": "password"}]},
            {"iceServers": [{"urls": ["turn:turn.cloudflare.com:3478?transport=udp"]}]},
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(TurnRefreshError):
                    select_turn_credentials(payload)

    def test_refresh_replaces_only_short_lived_values_after_full_validation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway, turn = self._write_environments(root)
            before = gateway.read_bytes()
            service = FakeGatewayService()

            refresh_turn_credentials(
                turn_environment=turn,
                auth_environment=auth,
                gateway_environment=gateway,
                fetch_ice_servers=lambda _key_id, _api_token: {
                    "iceServers": [{
                        "urls": ["turns:turn.cloudflare.com:5349?transport=tcp"],
                        "username": "new-short-lived-user",
                        "credential": "new-short-lived-password",
                    }],
                },
                service=service,
            )

            after = gateway.read_bytes()
            self.assertNotEqual(before, after)
            self.assertIn(
                b"MTX_WEBRTCICESERVERS2_0_URL=turns:turn.cloudflare.com:5349?transport=tcp\n",
                after,
            )
            self.assertIn(b"MTX_WEBRTCICESERVERS2_0_USERNAME=new-short-lived-user\n", after)
            self.assertIn(b"MTX_WEBRTCICESERVERS2_0_PASSWORD=new-short-lived-password\n", after)
            self.assertIn(b"MTX_PATHS_GATE_SOURCE=rtsp://camera:password@10.0.0.10:554/live\n", after)
            self.assertNotIn(b"long-term-api-token", after)
            self.assertEqual(0o600, gateway.stat().st_mode & 0o777)
            self.assertEqual(["is-active", "restart", "is-active"], service.calls)

    def test_parse_failure_leaves_gateway_and_running_service_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway, turn = self._write_environments(root)
            before = gateway.read_bytes()
            service = FakeGatewayService()

            with self.assertRaises(TurnRefreshError):
                refresh_turn_credentials(
                    turn_environment=turn,
                    auth_environment=auth,
                    gateway_environment=gateway,
                    fetch_ice_servers=lambda _key_id, _api_token: {"iceServers": []},
                    service=service,
                )

            self.assertEqual(before, gateway.read_bytes())
            self.assertEqual([], service.calls)

    def test_failed_new_restart_restores_old_gateway_environment_and_restarts_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway, turn = self._write_environments(root)
            before = gateway.read_bytes()
            service = FakeGatewayService(restart_results=(False, True))

            with self.assertRaises(TurnRefreshError):
                refresh_turn_credentials(
                    turn_environment=turn,
                    auth_environment=auth,
                    gateway_environment=gateway,
                    fetch_ice_servers=lambda _key_id, _api_token: {
                        "iceServers": [{
                            "urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
                            "username": "replacement-user",
                            "credential": "replacement-password",
                        }],
                    },
                    service=service,
                )

            self.assertEqual(before, gateway.read_bytes())
            self.assertEqual(
                ["is-active", "restart", "is-active", "restart", "is-active"],
                service.calls,
            )

    def _write_environments(self, root):
        auth = root / "gate-media-auth.env"
        gateway = root / "gate-media-gateway.env"
        turn = root / "gate-media-turn.env"
        auth.write_text(
            "GATE_MEDIA_HMAC_SECRET=" + "x" * 32 + "\n"
            "GATE_MEDIA_VIDEO_CONFIGURED=false\n"
            "GATE_MEDIA_VIDEO_VERIFIED=false\n"
            "GATE_MEDIA_LISTEN_CONFIGURED=false\n"
            "GATE_MEDIA_LISTEN_VERIFIED=false\n"
            "GATE_MEDIA_TALKBACK_CONFIGURED=false\n",
            encoding="utf-8",
        )
        gateway.write_text(
            "MTX_PATHS_GATE_SOURCE=rtsp://camera:password@10.0.0.10:554/live\n"
            "MTX_WEBRTCLOCALUDPADDRESS=10.0.0.5:8189\n"
            "MTX_WEBRTCADDITIONALHOSTS=10.0.0.5\n"
            "MTX_WEBRTCICESERVERS2_0_URL=turn:old.example:3478?transport=udp\n"
            "MTX_WEBRTCLOCALTCPADDRESS=10.0.0.5:8189\n"
            "MTX_WEBRTCICESERVERS2_0_USERNAME=old-user\n"
            "MTX_WEBRTCICESERVERS2_0_PASSWORD=old-password\n"
            "MTX_WEBRTCICESERVERS2_0_CLIENTONLY=false\n",
            encoding="utf-8",
        )
        turn.write_text(
            "TURN_KEY_ID=turn-key-id\n"
            "TURN_KEY_API_TOKEN=long-term-api-token\n",
            encoding="utf-8",
        )
        for path in (auth, gateway, turn):
            path.chmod(0o600)
        return auth, gateway, turn

    @staticmethod
    def _read_unit(relative_path):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(Path(__file__).parents[1] / relative_path, encoding="utf-8")
        return parser


if __name__ == "__main__":
    unittest.main()
