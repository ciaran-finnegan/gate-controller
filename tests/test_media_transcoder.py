import configparser
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import gate_media_transcoder.__main__ as transcoder


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class MediaTranscoderCommandTests(unittest.TestCase):
    def test_executes_only_the_fixed_loopback_h264_copy_and_opus_command(self):
        expected = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", "rtsp://127.0.0.1:8554/camera",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "libopus",
            "-application", "lowdelay",
            "-frame_duration", "20",
            "-b:a", "24k",
            "-vbr", "constrained",
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            "rtsp://127.0.0.1:8554/gate",
        ]

        with mock.patch.object(transcoder.os, "execve", side_effect=RuntimeError("exec")) as execute:
            with self.assertRaisesRegex(RuntimeError, "exec"):
                transcoder.main()

        execute.assert_called_once_with(
            "/usr/bin/ffmpeg", expected, {"LANG": "C", "LC_ALL": "C"}
        )
        command_text = " ".join(expected)
        self.assertNotIn("@", command_text)
        self.assertNotIn("MTX_", command_text)
        self.assertNotIn("GATE_", command_text)

    def test_pinned_ffmpeg_has_rtsp_tcp_and_libopus_when_binary_is_supplied(self):
        binary = os.environ.get("FFMPEG_5_1_5_BINARY")
        if not binary:
            self.skipTest("FFMPEG_5_1_5_BINARY is not available")

        version = subprocess.run(
            [binary, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        self.assertEqual(0, version.returncode, version.stderr)
        self.assertIn("ffmpeg version 5.1.5", version.stdout.splitlines()[0])
        self.assertIn("--enable-libopus", version.stdout)
        encoder = subprocess.run(
            [binary, "-hide_banner", "-h", "encoder=libopus"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        self.assertEqual(0, encoder.returncode, encoder.stdout)
        self.assertIn("libopus", encoder.stdout)
        muxer = subprocess.run(
            [binary, "-hide_banner", "-h", "muxer=rtsp"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        self.assertEqual(0, muxer.returncode, muxer.stdout)
        self.assertIn("tcp", muxer.stdout.lower())


class MediaTranscoderUnitTests(unittest.TestCase):
    def setUp(self):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read(
            REPOSITORY_ROOT / "deployment/systemd/gate-media-transcoder.service",
            encoding="utf-8",
        )
        self.unit = parser

    def test_unit_is_coupled_to_the_gateway_without_controller_privileges(self):
        unit = self.unit["Unit"]
        service = self.unit["Service"]

        self.assertEqual("gate-media-gateway.service", unit["Requires"])
        self.assertIn("gate-media-gateway.service", unit["After"])
        self.assertEqual("gate-media-gateway.service", unit["PartOf"])
        self.assertEqual("true", service["DynamicUser"])
        self.assertNotIn("User", service)
        self.assertNotIn("Group", service)
        self.assertNotIn("EnvironmentFile", service)
        self.assertEqual(
            "/usr/bin/python3 -I /usr/local/lib/gate-media/gate_media_transcoder/__main__.py",
            service["ExecStart"],
        )
        self.assertEqual("any", service["IPAddressDeny"])
        self.assertEqual("localhost", service["IPAddressAllow"])
        self.assertNotIn("ReadWritePaths", service)
        forbidden = service["InaccessiblePaths"]
        for path in (
            "/var/lib/gate-controller", "/opt/gate-controller",
            "/opt/gate-controller-deploy", "/usr/local/libexec/gate-controller",
            "/dev/gpiomem", "/dev/gpiochip0", "/dev/gpiochip1",
            "/dev/gpiochip2", "/dev/gpiochip3",
        ):
            self.assertIn(path, forbidden)

    def test_unit_retries_after_unbounded_camera_outage_at_bounded_cadence(self):
        unit = self.unit["Unit"]
        service = self.unit["Service"]

        self.assertEqual("0", unit["StartLimitIntervalSec"])
        self.assertNotIn("StartLimitBurst", unit)
        self.assertEqual("always", service["Restart"])
        self.assertEqual("5s", service["RestartSec"])

    def test_unit_has_exact_resource_bounds(self):
        service = self.unit["Service"]

        self.assertEqual("96M", service["MemoryMax"])
        self.assertEqual("20%", service["CPUQuota"])
        self.assertEqual("10", service["Nice"])
        self.assertEqual("32", service["TasksMax"])
        for hardening in (
            "NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp",
            "PrivateDevices", "ProtectKernelTunables", "ProtectControlGroups",
            "ProtectClock", "ProtectKernelModules", "ProtectKernelLogs",
            "ProtectHostname", "LockPersonality", "RestrictRealtime",
            "RestrictSUIDSGID",
        ):
            self.assertIn(service[hardening], {"true", "strict"})


if __name__ == "__main__":
    unittest.main()
