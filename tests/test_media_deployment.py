import configparser
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from gate_controller.media_capabilities import read_media_capabilities
import gate_media_auth.capabilities as media_health
from gate_media_auth.capabilities import (
    _gateway_is_ready, capability_snapshot, default_capabilities, write_capabilities,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read_unit(relative_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
    return parser["Service"]


class MediaGatewayDeploymentTests(unittest.TestCase):
    def test_mediamtx_plain_http_and_transports_default_to_loopback(self):
        config = (REPOSITORY_ROOT / "deployment/media/mediamtx.yml").read_text(encoding="utf-8")

        self.assertIn("authMethod: http", config)
        self.assertIn("authHTTPAddress: http://127.0.0.1:9189/auth", config)
        self.assertIn("rtspAddress: 127.0.0.1:8554", config)
        self.assertIn("apiAddress: 127.0.0.1:9997", config)
        self.assertIn("metricsAddress: 127.0.0.1:9998", config)
        self.assertIn("webrtcAddress: 127.0.0.1:8889", config)
        self.assertIn("webrtcLocalUDPAddress: 127.0.0.1:8189", config)
        self.assertIn("webrtcLocalTCPAddress: 127.0.0.1:8189", config)
        self.assertIn("gate:", config)
        self.assertNotIn("${", config)
        self.assertNotIn("    source:", config)
        self.assertNotRegex(config, r"rtsp://[^\s]*@")
        self.assertIn("hls: false", config)
        self.assertIn("rtmp: false", config)
        self.assertIn("srt: false", config)

    def test_proxy_template_allows_only_whep_create_and_exact_teardown_routes(self):
        path = REPOSITORY_ROOT / "deployment/media/nginx-whep-locations.conf.template"
        self.assertTrue(path.is_file())
        proxy = path.read_text(encoding="utf-8")

        self.assertIn("location = /gate/whep", proxy)
        self.assertRegex(proxy, r"location ~ \^/gate/whep/\[A-Za-z0-9_-\]")
        self.assertIn("__GATE_MEDIA_ALLOWED_ORIGIN__", proxy)
        self.assertIn("proxy_pass http://127.0.0.1:8889", proxy)
        self.assertNotRegex(proxy, r"location\s+/\s*\{[^}]*proxy_pass")
        self.assertIn("limit_except POST OPTIONS", proxy)
        self.assertIn("limit_except DELETE OPTIONS", proxy)
        self.assertEqual(2, proxy.count("proxy_set_header Host $http_host;"))

    def test_services_receive_disjoint_root_owned_environment_files(self):
        auth = read_unit("deployment/systemd/gate-media-auth.service")
        gateway = read_unit("deployment/systemd/gate-media-gateway.service")

        self.assertEqual("/etc/gate-media-auth.env", auth.get("EnvironmentFile"))
        self.assertEqual("/etc/gate-media-gateway.env", gateway.get("EnvironmentFile"))
        self.assertNotEqual(auth.get("EnvironmentFile"), gateway.get("EnvironmentFile"))

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
                self.assertNotIn("SupplementaryGroups=gpio", contents)
                self.assertNotIn("Group=gpio", contents)
                inaccessible = set(shlex.split(service.get("InaccessiblePaths", "")))
                self.assertTrue(any("/var/lib/gate-controller" in path for path in inaccessible))
                self.assertTrue(any("/opt/gate-controller-deploy" in path for path in inaccessible))
                self.assertTrue(any("gpio" in path for path in inaccessible))
                self.assertEqual("/", service.get("NoExecPaths"))
                self.assertTrue(service.get("ExecPaths"))

    def test_media_installer_uses_an_operator_approved_version_architecture_checksum_map(self):
        installer = (REPOSITORY_ROOT / "deployment/install-media.sh").read_text(encoding="utf-8")
        controller_installer = (REPOSITORY_ROOT / "deployment/install.sh").read_text(encoding="utf-8")

        self.assertIn("lookup_mediamtx_checksum", installer)
        self.assertIn("sha256sum --check", installer)
        self.assertIn("--mediamtx-version", installer)
        self.assertIn("--checksum-map", installer)
        self.assertIn("--mediamtx-archive", installer)
        self.assertIn("--allowed-origin", installer)
        self.assertIn("--version", installer)
        self.assertNotIn("curl ", installer)
        self.assertNotIn("wget ", installer)
        self.assertIn("install_fixed_media_bootstrap", controller_installer)
        self.assertIn("nginx-whep-locations.conf.template", controller_installer)
        self.assertNotIn("install_mediamtx_binary", controller_installer)
        self.assertNotIn("/usr/local/bin/mediamtx", controller_installer)

    def test_proxy_renderer_substitutes_one_validated_exact_https_origin(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "whep.conf"
            template = (
                REPOSITORY_ROOT / "deployment/media/nginx-whep-locations.conf.template"
            )
            command = (
                "source deployment/install-media.sh; "
                f"render_proxy_config {shlex.quote(str(template))} "
                f"{shlex.quote(str(output))} https://gate.example"
            )
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("__GATE_MEDIA_ALLOWED_ORIGIN__", rendered)
            self.assertIn('Access-Control-Allow-Origin "https://gate.example"', rendered)

            rejected = subprocess.run(
                ["bash", "-c", command.replace(
                    "https://gate.example", "https://gate.example/unsafe"
                )], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, rejected.returncode)

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

    def test_media_environment_files_reject_cross_contaminated_secrets(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth = root / "auth.env"
            gateway = root / "gateway.env"
            auth.write_text(
                "GATE_MEDIA_HMAC_SECRET=0123456789abcdef0123456789abcdef\n"
                "GATE_MEDIA_VIDEO_CONFIGURED=true\n",
                encoding="utf-8",
            )
            gateway.write_text(
                "MTX_PATHS_GATE_SOURCE=rtsp://camera.example/stream\n",
                encoding="utf-8",
            )
            base = (
                "source deployment/install-media.sh; "
                f"MEDIA_AUTH_ENV={shlex.quote(str(auth))}; "
                f"MEDIA_GATEWAY_ENV={shlex.quote(str(gateway))}; "
            )

            complete = subprocess.run(
                ["bash", "-c", base + "media_environment_complete"], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertEqual(0, complete.returncode, complete.stderr)

            auth.write_text(auth.read_text() + "MTX_PATHS_GATE_SOURCE=forbidden\n")
            contaminated = subprocess.run(
                ["bash", "-c", base + "media_environment_complete"], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, contaminated.returncode)

    def test_installer_rejects_a_media_user_in_the_gpio_group(self):
        command = """
source deployment/install-media.sh
id() {
  if [[ $1 == -nG ]]; then printf 'gate-media gpio\n'; else return 0; fi
}
reject_gpio_membership gate-media
"""
        completed = subprocess.run(
            ["bash", "-c", command], cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("gpio", completed.stderr)

    def test_installer_rejects_a_symlinked_private_archive_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "operator.tar.gz"
            archive.write_bytes(b"approved archive")
            real_private = root / "real-private"
            real_private.mkdir()
            linked_private = root / "private"
            linked_private.symlink_to(real_private, target_is_directory=True)
            command = f"""
source deployment/install-media.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac; done
  command install "${{forwarded[@]}}"
}}
MEDIA_ARCHIVE_ROOT={shlex.quote(str(linked_private))}
stage_mediamtx_archive {shlex.quote(str(archive))} 1.2.3 arm64
"""
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("archive directory", completed.stderr)
            self.assertEqual([], list(real_private.iterdir()))

    def test_installer_hashes_and_extracts_the_same_private_staged_archive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive, checksum_map = self._make_mediamtx_archive(root, "1.2.3")
            private = root / "private"
            binary = root / "bin/mediamtx"
            fake_bin = self._make_sha256sum(root, archive)
            command = f"""
source deployment/install-media.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac; done
  command install "${{forwarded[@]}}"
}}
normalize_architecture() {{ printf 'arm64\n'; }}
MEDIA_ARCHIVE_ROOT={shlex.quote(str(private))}
MEDIA_BINARY={shlex.quote(str(binary))}
install_mediamtx_binary {shlex.quote(str(archive))} 1.2.3 {shlex.quote(str(checksum_map))}
"""
            environment = dict(os.environ)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["ORIGINAL_ARCHIVE"] = str(archive)
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            stable = private / "mediamtx-1.2.3-arm64.tar.gz"
            self.assertTrue(stable.is_file())
            self.assertEqual(0o600, stable.stat().st_mode & 0o777)
            self.assertIn("1.2.3", subprocess.check_output(
                [str(binary), "--version"], text=True
            ))
            self.assertFalse(binary.with_suffix(".new").exists())

    def test_version_failure_preserves_existing_binary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive, checksum_map = self._make_mediamtx_archive(root, "wrong-version")
            private = root / "private"
            binary = root / "bin/mediamtx"
            binary.parent.mkdir()
            binary.write_text("existing-binary\n", encoding="utf-8")
            fake_bin = self._make_sha256sum(root, archive, mutate_original=False)
            command = f"""
source deployment/install-media.sh
install() {{
  local -a forwarded=()
  while [[ $# -gt 0 ]]; do case "$1" in -o|-g) shift 2 ;; *) forwarded+=("$1"); shift ;; esac; done
  command install "${{forwarded[@]}}"
}}
normalize_architecture() {{ printf 'arm64\n'; }}
MEDIA_ARCHIVE_ROOT={shlex.quote(str(private))}
MEDIA_BINARY={shlex.quote(str(binary))}
install_mediamtx_binary {shlex.quote(str(archive))} 1.2.3 {shlex.quote(str(checksum_map))}
"""
            environment = dict(os.environ)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["ORIGINAL_ARCHIVE"] = str(archive)
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertEqual("existing-binary\n", binary.read_text(encoding="utf-8"))

    def test_every_installer_failure_disables_media_services(self):
        for arguments in ("--unknown-option", "--source"):
            with (self.subTest(arguments=arguments),
                  tempfile.TemporaryDirectory() as temporary_directory):
                log = Path(temporary_directory) / "systemctl.log"
                command = f"""
source deployment/install-media.sh
systemctl() {{ printf '%s\n' "$*" >> {shlex.quote(str(log))}; }}
main {arguments}
"""
                completed = subprocess.run(
                    ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
                )

                self.assertNotEqual(0, completed.returncode)
                self.assertTrue(log.is_file())
                self.assertIn(
                    "disable --now gate-media-gateway.service gate-media-auth.service",
                    log.read_text(encoding="utf-8"),
                )

    def _make_mediamtx_archive(self, root, reported_version):
        executable = root / "mediamtx"
        executable.write_text(
            "#!/bin/sh\nprintf 'mediamtx %s\\n' " + shlex.quote(reported_version) + "\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        archive = root / "operator.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(executable, arcname="mediamtx")
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum_map = root / "checksums.txt"
        checksum_map.write_text(f"1.2.3 arm64 {checksum}\n", encoding="utf-8")
        return archive, checksum_map

    def _make_sha256sum(self, root, original_archive, mutate_original=True):
        fake_bin = root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        sha256sum = fake_bin / "sha256sum"
        mutation = "printf 'corrupted' > \"$ORIGINAL_ARCHIVE\"" if mutate_original else ":"
        sha256sum.write_text(
            "#!/bin/sh\n"
            "read expected archive\n"
            "actual=$(shasum -a 256 \"$archive\" | awk '{print $1}')\n"
            "[ \"$actual\" = \"$expected\" ] || exit 1\n"
            f"{mutation}\n",
            encoding="utf-8",
        )
        sha256sum.chmod(0o755)
        return fake_bin


class MediaCapabilityTests(unittest.TestCase):
    def test_atomic_capabilities_default_all_media_features_to_false(self):
        expected_media = {
            "video": {"configured": False, "ready": False, "verified": False,
                      "reason": "not_configured"},
            "listen": {"configured": False, "ready": False, "verified": False,
                       "reason": "not_configured"},
            "talkback": {"configured": False, "ready": False, "verified": False,
                         "reason": "hardware_unverified"},
        }
        before = int(__import__("time").time())
        expected = default_capabilities()
        after = int(__import__("time").time())
        self.assertEqual(expected_media, expected["media"])
        self.assertLessEqual(before, expected["observed_at"])
        self.assertLessEqual(expected["observed_at"], after)

        expected_shape = {
            "observed_at": expected["observed_at"],
            "media": {
                "video": {"configured": False, "ready": False, "verified": False,
                          "reason": "not_configured"},
                "listen": {"configured": False, "ready": False, "verified": False,
                           "reason": "not_configured"},
                "talkback": {"configured": False, "ready": False, "verified": False,
                             "reason": "hardware_unverified"},
            }
        }
        self.assertEqual(expected_shape, expected)

        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "capabilities.json"
            write_capabilities(target, expected)
            self.assertEqual(expected, json.loads(target.read_text(encoding="utf-8")))
            self.assertFalse(list(target.parent.glob(".capabilities.*")))

    def test_heartbeat_capabilities_are_best_effort_and_malformed_or_stale_data_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "capabilities.json"
            healthy = {
                "observed_at": 100,
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
            self.assertEqual(
                healthy["media"],
                read_media_capabilities(target, max_age_seconds=60, now=101),
            )

            target.write_text("not-json", encoding="utf-8")
            unavailable = read_media_capabilities(target, max_age_seconds=60)
            self.assertFalse(unavailable["video"]["ready"])
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])

            healthy["observed_at"] = 1
            write_capabilities(target, healthy)
            unavailable = read_media_capabilities(target, max_age_seconds=60, now=100)
            self.assertFalse(unavailable["listen"]["ready"])
            self.assertEqual("gateway_unhealthy", unavailable["listen"]["reason"])

    def test_capability_reader_fails_closed_for_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.json"
            target = root / "capabilities.json"
            snapshot = default_capabilities()
            snapshot["observed_at"] = 100
            write_capabilities(source, snapshot)
            target.symlink_to(source)

            unavailable = read_media_capabilities(target, max_age_seconds=60, now=101)

            self.assertFalse(unavailable["video"]["ready"])
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])

    def test_capability_reader_rejects_fifo_oversize_future_and_incoherent_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "capabilities.json"

            os.mkfifo(target)
            script = (
                "from gate_controller.media_capabilities import read_media_capabilities; "
                "import pathlib, sys; "
                "print(read_media_capabilities(pathlib.Path(sys.argv[1]), now=100)"
                "['video']['reason'])"
            )
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(target)], cwd=REPOSITORY_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    check=False, timeout=1,
                )
            except subprocess.TimeoutExpired:
                self.fail("capability reader blocked on a FIFO")
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("gateway_unhealthy", completed.stdout.strip())
            target.unlink()

            target.write_bytes(b"{" + b"x" * 16_384 + b"}")
            unavailable = read_media_capabilities(target, max_age_seconds=60, now=100)
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])

            future = default_capabilities()
            future["observed_at"] = 101
            write_capabilities(target, future)
            unavailable = read_media_capabilities(target, max_age_seconds=60, now=100)
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])

            incoherent = default_capabilities()
            incoherent["observed_at"] = 100
            incoherent["media"]["video"] = {
                "configured": True, "ready": False, "verified": True, "reason": "ready",
            }
            write_capabilities(target, incoherent)
            unavailable = read_media_capabilities(target, max_age_seconds=60, now=100)
            self.assertEqual("gateway_unhealthy", unavailable["video"]["reason"])

    def test_reader_forces_talkback_to_hardware_unverified(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "capabilities.json"
            snapshot = default_capabilities()
            snapshot["observed_at"] = 100
            snapshot["media"]["talkback"] = {
                "configured": True, "ready": True, "verified": True, "reason": "ready",
            }
            write_capabilities(target, snapshot)

            media = read_media_capabilities(target, max_age_seconds=60, now=100)

            self.assertEqual(media["talkback"], {
                "configured": True,
                "ready": False,
                "verified": False,
                "reason": "hardware_unverified",
            })

    def test_publisher_uses_nonsecret_configuration_flags(self):
        snapshot = capability_snapshot({
            "GATE_MEDIA_VIDEO_CONFIGURED": "true",
            "GATE_MEDIA_VIDEO_VERIFIED": "true",
            "GATE_MEDIA_LISTEN_CONFIGURED": "false",
        }, True)

        self.assertTrue(snapshot["media"]["video"]["configured"])
        self.assertTrue(snapshot["media"]["video"]["verified"])
        self.assertFalse(snapshot["media"]["listen"]["configured"])
        self.assertNotIn("MTX_PATHS_GATE_SOURCE", snapshot)


class MediaGatewayHealthTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(media_health, "gateway_status_ready"))

    def test_gate_path_requires_available_or_ready_and_nonempty_tracks(self):
        for flag in ("ready", "available"):
            payload = json.dumps({
                "itemCount": 1,
                "pageCount": 1,
                "items": [{"name": "gate", flag: True, "tracks": ["H264"]}],
            }).encode("utf-8")
            with self.subTest(flag=flag):
                self.assertTrue(media_health.gateway_status_ready(payload))

        unavailable = (
            {"name": "gate", "ready": False, "tracks": ["H264"]},
            {"name": "gate", "ready": True, "tracks": []},
            {"name": "other", "ready": True, "tracks": ["H264"]},
        )
        for item in unavailable:
            payload = json.dumps({"itemCount": 1, "pageCount": 1, "items": [item]}).encode()
            with self.subTest(item=item):
                self.assertFalse(media_health.gateway_status_ready(payload))

    def test_gateway_health_rejects_malformed_duplicate_and_oversized_responses(self):
        payloads = (
            b"not-json",
            b'{"items":[],"items":[]}',
            b"{" + b"x" * 65_536 + b"}",
        )
        for payload in payloads:
            with self.subTest(size=len(payload)):
                self.assertFalse(media_health.gateway_status_ready(payload))

    def test_gateway_health_reads_only_a_bounded_response(self):
        body = json.dumps({
            "itemCount": 1,
            "pageCount": 1,
            "items": [{"name": "gate", "ready": True, "tracks": ["H264"]}],
        }).encode()

        class Response:
            status = 200
            headers = {"Content-Length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                self.limit = limit
                return body

        response = Response()

        self.assertTrue(_gateway_is_ready(opener=lambda *_args, **_kwargs: response))
        self.assertEqual(65_537, response.limit)


if __name__ == "__main__":
    unittest.main()
