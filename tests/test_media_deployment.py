import configparser
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from gate_controller.media_capabilities import read_media_capabilities
from gate_media_auth.capabilities import default_capabilities, write_capabilities


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read_unit(relative_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
    return parser["Service"]


class MediaGatewayDeploymentTests(unittest.TestCase):
    def test_mediamtx_exposes_only_loopback_services_except_webrtc_whep(self):
        config = (REPOSITORY_ROOT / "deployment/media/mediamtx.yml").read_text(encoding="utf-8")

        self.assertIn("authMethod: http", config)
        self.assertIn("authHTTPAddress: http://127.0.0.1:9189/auth", config)
        self.assertIn("rtspAddress: 127.0.0.1:8554", config)
        self.assertIn("apiAddress: 127.0.0.1:9997", config)
        self.assertIn("metricsAddress: 127.0.0.1:9998", config)
        self.assertIn("webrtcAddress: :8889", config)
        self.assertIn("gate:", config)
        self.assertIn("source: ${GATE_MEDIA_RTSP_SOURCE}", config)
        self.assertNotRegex(config, r"rtsp://[^\s]*@")
        self.assertIn("hls: false", config)
        self.assertIn("rtmp: false", config)
        self.assertIn("srt: false", config)

    def test_media_units_are_restricted_nonroot_and_cannot_access_gpio_or_controller_state(self):
        for relative_path, user in (
            ("deployment/systemd/gate-media-auth.service", "gate-media-auth"),
            ("deployment/systemd/gate-media-gateway.service", "gate-media"),
        ):
            with self.subTest(unit=relative_path):
                service = read_unit(relative_path)
                contents = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertEqual(user, service.get("User"))
                self.assertEqual(user, service.get("Group"))
                self.assertEqual("true", service.get("NoNewPrivileges"))
                self.assertEqual("strict", service.get("ProtectSystem"))
                self.assertEqual("true", service.get("ProtectHome"))
                self.assertEqual("true", service.get("PrivateTmp"))
                self.assertEqual("always", service.get("Restart"))
                self.assertIn("MemoryMax", service)
                self.assertIn("CPUQuota", service)
                self.assertIn("TasksMax", service)
                self.assertNotIn("gpio", contents.lower())
                self.assertNotIn("/var/lib/gate-controller", contents)
                self.assertNotIn("/opt/gate-controller-deploy", contents)

    def test_media_installer_uses_an_operator_approved_version_architecture_checksum_map(self):
        installer = (REPOSITORY_ROOT / "deployment/install-media.sh").read_text(encoding="utf-8")
        controller_installer = (REPOSITORY_ROOT / "deployment/install.sh").read_text(encoding="utf-8")

        self.assertIn("lookup_mediamtx_checksum", installer)
        self.assertIn("sha256sum --check", installer)
        self.assertIn("--mediamtx-version", installer)
        self.assertIn("--checksum-map", installer)
        self.assertIn("--mediamtx-archive", installer)
        self.assertIn("--version", installer)
        self.assertNotIn("curl ", installer)
        self.assertNotIn("wget ", installer)
        self.assertIn("install_fixed_media_bootstrap", controller_installer)
        self.assertNotIn("install_mediamtx_binary", controller_installer)
        self.assertNotIn("/usr/local/bin/mediamtx", controller_installer)

    def test_checksum_lookup_requires_an_exact_approved_version_and_architecture(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            checksum_map = Path(temporary_directory) / "checksums.txt"
            checksum = "a" * 64
            checksum_map.write_text(
                f"1.2.3 arm64 {checksum}\n1.2.3 armv7 {'b' * 64}\n", encoding="utf-8"
            )
            command = (
                "source deployment/install-media.sh; "
                f"lookup_mediamtx_checksum 1.2.3 arm64 {shlex.quote(str(checksum_map))}"
            )
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(checksum, completed.stdout.strip())


class MediaCapabilityTests(unittest.TestCase):
    def test_atomic_capabilities_default_all_media_features_to_false(self):
        expected = {
            "media": {
                "video": {"configured": False, "ready": False, "verified": False,
                          "reason": "not_configured"},
                "listen": {"configured": False, "ready": False, "verified": False,
                           "reason": "not_configured"},
                "talkback": {"configured": False, "ready": False, "verified": False,
                             "reason": "hardware_unverified"},
            }
        }
        self.assertEqual(expected, default_capabilities())

        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "capabilities.json"
            write_capabilities(target, expected)
            self.assertEqual(expected, json.loads(target.read_text(encoding="utf-8")))
            self.assertFalse(list(target.parent.glob(".capabilities.*")))

    def test_heartbeat_capabilities_are_best_effort_and_malformed_or_stale_data_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "capabilities.json"
            healthy = {
                "media": {
                    "video": {"configured": True, "ready": True, "verified": True,
                              "reason": "ready"},
                    "listen": {"configured": True, "ready": True, "verified": False,
                               "reason": "hardware_unverified"},
                    "talkback": {"configured": False, "ready": False, "verified": False,
                                 "reason": "hardware_unverified"},
                }
            }
            write_capabilities(target, healthy)
            self.assertEqual(healthy["media"], read_media_capabilities(target, max_age_seconds=60))

            target.write_text("not-json", encoding="utf-8")
            unavailable = read_media_capabilities(target, max_age_seconds=60)
            self.assertFalse(unavailable["video"]["ready"])
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])

            write_capabilities(target, healthy)
            old = 1
            os.utime(target, (old, old))
            unavailable = read_media_capabilities(target, max_age_seconds=60, now=100)
            self.assertFalse(unavailable["listen"]["ready"])
            self.assertEqual("gateway_unhealthy", unavailable["listen"]["reason"])

    def test_capability_reader_fails_closed_for_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.json"
            target = root / "capabilities.json"
            write_capabilities(source, default_capabilities())
            target.symlink_to(source)

            unavailable = read_media_capabilities(target, max_age_seconds=60)

            self.assertFalse(unavailable["video"]["ready"])
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])


if __name__ == "__main__":
    unittest.main()
