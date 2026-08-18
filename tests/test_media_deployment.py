import configparser
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from gate_controller.media_capabilities import read_media_capabilities
import gate_media_auth.capabilities as media_health
from gate_media_auth.capabilities import (
    _gateway_is_ready, capability_snapshot, default_capabilities, write_capabilities,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PINNED_MEDIAMTX_VERSION = "1.19.3"


def read_unit(relative_path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / relative_path, encoding="utf-8")
    return parser["Service"]


def read_flat_yaml(relative_path):
    values = {}
    for line in (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ")) or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        raw_value = raw_value.strip()
        if raw_value == "true":
            value = True
        elif raw_value == "false":
            value = False
        elif raw_value == "[]":
            value = []
        elif raw_value.startswith('"'):
            value = json.loads(raw_value)
        else:
            value = raw_value
        values[key] = value
    return values


class MediaGatewayDeploymentTests(unittest.TestCase):
    def test_mediamtx_plain_http_and_transports_default_to_loopback(self):
        config = (REPOSITORY_ROOT / "deployment/media/mediamtx.yml").read_text(encoding="utf-8")

        self.assertIn("authMethod: http", config)
        self.assertIn("authHTTPAddress: http://127.0.0.1:9189/auth", config)
        self.assertIn("rtspAddress: 127.0.0.1:8554", config)
        self.assertIn("apiAddress: 127.0.0.1:9997", config)
        self.assertIn("metricsAddress: 127.0.0.1:9998", config)
        self.assertIn("webrtcAddress: 127.0.0.1:8889", config)
        self.assertIn('webrtcLocalUDPAddress: ""', config)
        self.assertIn('webrtcLocalTCPAddress: ""', config)
        self.assertIn("gate:", config)
        self.assertNotIn("${", config)
        self.assertNotIn("    source:", config)
        self.assertNotRegex(config, r"rtsp://[^\s]*@")
        self.assertIn("rtsp: false", config)
        self.assertIn("rtspTransports: []", config)
        self.assertIn("hls: false", config)
        self.assertIn("rtmp: false", config)
        self.assertIn("srt: false", config)

    def test_mediamtx_1_19_3_effective_listener_surface_is_complete(self):
        config = read_flat_yaml("deployment/media/mediamtx.yml")
        expected = {
            "api": True,
            "apiAddress": "127.0.0.1:9997",
            "metrics": True,
            "metricsAddress": "127.0.0.1:9998",
            "pprof": False,
            "pprofAddress": "127.0.0.1:9999",
            "playback": False,
            "playbackAddress": "127.0.0.1:9996",
            "rtsp": False,
            "rtspTransports": [],
            "rtspAddress": "127.0.0.1:8554",
            "rtspsAddress": "127.0.0.1:8322",
            "rtpAddress": "127.0.0.1:8000",
            "rtcpAddress": "127.0.0.1:8001",
            "rtmp": False,
            "rtmpAddress": "127.0.0.1:1935",
            "rtmpsAddress": "127.0.0.1:1936",
            "hls": False,
            "hlsAddress": "127.0.0.1:8888",
            "webrtc": True,
            "webrtcAddress": "127.0.0.1:8889",
            "webrtcLocalUDPAddress": "",
            "webrtcLocalTCPAddress": "",
            "webrtcIPsFromInterfaces": False,
            "webrtcIPsFromInterfacesList": [],
            "webrtcAdditionalHosts": [],
            "webrtcICEServers2": [],
            "srt": False,
            "srtAddress": "127.0.0.1:8890",
            "moq": False,
            "moqHTTP2Address": "127.0.0.1:8892",
            "moqHTTP3Address": "127.0.0.1:8892",
        }

        self.assertEqual(expected, {key: config.get(key) for key in expected})
        self.assertNotIn("moqAddress", config)
        for key, value in expected.items():
            if key.endswith("Address") and value:
                self.assertNotRegex(value, r"^(?::|0\.0\.0\.0:|\[::]:)")

    def test_mediamtx_1_19_3_hermetic_schema_and_validation_probe(self):
        schema = json.loads(
            (REPOSITORY_ROOT / "tests/fixtures/mediamtx-v1.19.3-schema.json")
            .read_text(encoding="utf-8")
        )
        config = read_flat_yaml("deployment/media/mediamtx.yml")
        configured_globals = set(config) - {"pathDefaults", "paths"}
        self.assertEqual("1.19.3", schema["version"])
        self.assertEqual(set(), configured_globals - set(schema["globalKeys"]))

        with tempfile.TemporaryDirectory() as temporary_directory:
            _auth, gateway = self._write_valid_media_environments(
                Path(temporary_directory)
            )
            environment = dict(
                line.split("=", 1)
                for line in gateway.read_text(encoding="utf-8").splitlines()
            )
        udp_host = environment["MTX_WEBRTCLOCALUDPADDRESS"].rsplit(":", 1)[0]
        tcp_host = environment["MTX_WEBRTCLOCALTCPADDRESS"].rsplit(":", 1)[0]
        additional_hosts = environment["MTX_WEBRTCADDITIONALHOSTS"].split(",")

        self.assertFalse(config["webrtcIPsFromInterfaces"])
        self.assertEqual(udp_host, tcp_host)
        self.assertEqual([udp_host], additional_hosts)
        self.assertTrue(additional_hosts)

    def test_actual_mediamtx_1_19_3_loads_pinned_config_when_binary_is_supplied(self):
        binary = os.environ.get("MEDIAMTX_1_19_3_BINARY")
        if not binary:
            self.skipTest("MEDIAMTX_1_19_3_BINARY is not available")
        version = subprocess.run(
            [binary, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        self.assertEqual(0, version.returncode, version.stderr)
        self.assertIn(version.stdout.strip(), {"1.19.3", "v1.19.3"})

        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("MTX_", "RTSP_"))
        }
        environment.update({
            "MTX_APIADDRESS": "127.0.0.1:0",
            "MTX_METRICSADDRESS": "127.0.0.1:0",
            "MTX_WEBRTCADDRESS": "127.0.0.1:0",
            "MTX_WEBRTCLOCALUDPADDRESS": "127.0.0.1:0",
            "MTX_WEBRTCADDITIONALHOSTS": "127.0.0.1",
        })
        process = subprocess.Popen(
            [binary, str(REPOSITORY_ROOT / "deployment/media/mediamtx.yml")],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(1)
            returncode = process.poll()
            if returncode is not None:
                output, _ = process.communicate(timeout=5)
                self.fail(output)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

    def test_gateway_launcher_executes_only_with_complete_reachable_ice_and_turn(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway = self._write_valid_media_environments(root)
            del auth
            fake_binary = root / "mediamtx"
            marker = root / "executed"
            config = root / "mediamtx.yml"
            config.write_text("paths: {}\n", encoding="utf-8")
            fake_binary.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$GATEWAY_MARKER\"\n",
                encoding="utf-8",
            )
            fake_binary.chmod(0o755)
            valid_values = dict(
                line.split("=", 1)
                for line in gateway.read_text(encoding="utf-8").splitlines()
            )
            environment = dict(os.environ)
            environment.update(valid_values)
            environment["GATEWAY_MARKER"] = str(marker)
            command = [
                sys.executable, "-m", "gate_media_gateway",
                str(fake_binary), str(config),
            ]

            accepted = subprocess.run(
                command, cwd=REPOSITORY_ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(str(config), marker.read_text(encoding="utf-8").strip())

            for missing in (
                "MTX_WEBRTCLOCALUDPADDRESS",
                "MTX_WEBRTCLOCALTCPADDRESS",
                "MTX_WEBRTCADDITIONALHOSTS",
                "MTX_WEBRTCICESERVERS2_0_URL",
                "MTX_WEBRTCICESERVERS2_0_USERNAME",
                "MTX_WEBRTCICESERVERS2_0_PASSWORD",
                "MTX_WEBRTCICESERVERS2_0_CLIENTONLY",
            ):
                with self.subTest(missing=missing):
                    marker.unlink(missing_ok=True)
                    invalid_environment = dict(environment)
                    del invalid_environment[missing]
                    rejected = subprocess.run(
                        command, cwd=REPOSITORY_ROOT, env=invalid_environment,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, check=False,
                    )
                    self.assertNotEqual(0, rejected.returncode)
                    self.assertFalse(marker.exists())
                    self.assertNotIn("turn-password", rejected.stderr)

            marker.unlink(missing_ok=True)
            whitespace_environment = dict(environment)
            whitespace_environment["MTX_WEBRTCICESERVERS2_0_PASSWORD"] = "turn password"
            rejected_whitespace = subprocess.run(
                command, cwd=REPOSITORY_ROOT, env=whitespace_environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, check=False,
            )
            self.assertNotEqual(0, rejected_whitespace.returncode)
            self.assertFalse(marker.exists())

    def test_installer_rejects_every_unpinned_mediamtx_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            trusted = Path(temporary_directory) / "trusted"
            trusted.mkdir(mode=0o700)
            checksum_map = trusted / "checksums.txt"
            checksum_map.write_text(
                f"{PINNED_MEDIAMTX_VERSION} arm64 {'a' * 64}\n", encoding="utf-8"
            )
            checksum_map.chmod(0o600)
            command = (
                "source deployment/install-media.sh; "
                f"lookup_mediamtx_checksum 1.19.2 arm64 {shlex.quote(str(checksum_map))}"
            )
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertNotEqual(0, completed.returncode)

    def test_proxy_template_allows_only_whep_create_and_exact_teardown_routes(self):
        path = REPOSITORY_ROOT / "deployment/media/nginx-whep-locations.conf.template"
        self.assertTrue(path.is_file())
        proxy = path.read_text(encoding="utf-8")

        self.assertIn("location = /gate/whep", proxy)
        self.assertIn(
            'location ~ "^/gate/whep/[A-Za-z0-9_-]{1,128}$" {',
            proxy,
        )
        self.assertIn("__GATE_MEDIA_ALLOWED_ORIGIN__", proxy)
        self.assertIn("proxy_pass http://127.0.0.1:8889", proxy)
        self.assertNotRegex(proxy, r"location\s+/\s*\{[^}]*proxy_pass")
        self.assertIn("listen 127.0.0.1:8891", proxy)
        self.assertRegex(proxy, r"location\s+/\s*\{\s*return 404;")
        self.assertIn("limit_except POST OPTIONS", proxy)
        self.assertIn("limit_except DELETE OPTIONS", proxy)
        self.assertEqual(2, proxy.count("proxy_set_header Host $http_host;"))

    def test_services_receive_disjoint_root_owned_environment_files(self):
        auth = read_unit("deployment/systemd/gate-media-auth.service")
        gateway = read_unit("deployment/systemd/gate-media-gateway.service")

        self.assertEqual("/etc/gate-media-auth.env", auth.get("EnvironmentFile"))
        self.assertEqual("/etc/gate-media-gateway.env", gateway.get("EnvironmentFile"))
        self.assertNotEqual(auth.get("EnvironmentFile"), gateway.get("EnvironmentFile"))
        self.assertEqual(
            "/usr/bin/python3 -m gate_media_gateway /usr/local/bin/mediamtx "
            "/etc/gate-media/mediamtx.yml",
            gateway.get("ExecStart"),
        )
        self.assertEqual("PYTHONPATH=/usr/local/lib/gate-media", gateway.get("Environment"))

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
                self.assertIn("-/opt/gate-controller", inaccessible)
                self.assertTrue(any("gpio" in path for path in inaccessible))
                no_exec_paths = set(shlex.split(service.get("NoExecPaths", "")))
                self.assertEqual(
                    {"/run/gate-media", "/tmp", "/var/tmp", "/dev/shm"},
                    no_exec_paths,
                )
                self.assertNotIn("ExecPaths", service)

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
        self.assertIn("gate_media_config.py", controller_installer)
        self.assertIn("gate_media_gateway", controller_installer)
        self.assertIn("gate_media_config.py", installer)
        self.assertIn("gate_media_gateway", installer)
        self.assertNotIn("install_mediamtx_binary", controller_installer)
        self.assertNotIn("/usr/local/bin/mediamtx", controller_installer)
        self.assertIn(
            "systemctl enable gate-media-auth.service gate-media-gateway.service",
            installer,
        )
        self.assertIn(
            "systemctl restart gate-media-auth.service gate-media-gateway.service",
            installer,
        )
        self.assertNotIn(
            "systemctl enable --now gate-media-auth.service gate-media-gateway.service",
            installer,
        )

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

    def test_proxy_activation_owns_validates_enables_and_reloads_nginx(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            proxy = root / "gate-media.conf"
            proxy.write_text("server {}\n", encoding="utf-8")
            enabled = root / "nginx-enabled.conf"
            nginx_log = root / "nginx.log"
            nginx = root / "nginx"
            nginx.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$NGINX_LOG\"\n",
                encoding="utf-8",
            )
            nginx.chmod(0o755)
            systemctl_log = root / "systemctl.log"
            command = f"""
source deployment/install-media.sh
NGINX_BINARY={shlex.quote(str(nginx))}
NGINX_PROXY_CONFIG={shlex.quote(str(enabled))}
systemctl() {{ printf '%s\n' "$*" >> {shlex.quote(str(systemctl_log))}; }}
activate_proxy_config {shlex.quote(str(proxy))}
"""
            environment = dict(os.environ)
            environment["NGINX_LOG"] = str(nginx_log)
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(enabled.is_symlink())
            self.assertEqual(proxy.resolve(), enabled.resolve())
            self.assertEqual("-t\n", nginx_log.read_text(encoding="utf-8"))
            self.assertEqual(
                ["enable nginx.service", "reload-or-restart nginx.service"],
                systemctl_log.read_text(encoding="utf-8").splitlines(),
            )

    def test_checksum_lookup_requires_an_exact_approved_version_and_architecture(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            trusted = Path(temporary_directory) / "trusted"
            trusted.mkdir(mode=0o700)
            checksum_map = trusted / "checksums.txt"
            checksum = "a" * 64
            checksum_map.write_text(
                f"{PINNED_MEDIAMTX_VERSION} arm64 {checksum}\n"
                f"{PINNED_MEDIAMTX_VERSION} armv7 {'b' * 64}\n",
                encoding="utf-8",
            )
            checksum_map.chmod(0o600)
            command = (
                "source deployment/install-media.sh; "
                f"lookup_mediamtx_checksum {PINNED_MEDIAMTX_VERSION} arm64 "
                f"{shlex.quote(str(checksum_map))}"
            )
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(checksum, completed.stdout.strip())

    def test_checksum_map_requires_trusted_mode_owner_directory_and_nonsymlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            trusted = root / "trusted"
            trusted.mkdir(mode=0o700)
            checksum_map = trusted / "checksums.txt"
            checksum_map.write_text(
                f"{PINNED_MEDIAMTX_VERSION} arm64 {'a' * 64}\n", encoding="utf-8"
            )
            checksum_map.chmod(0o600)

            base = [
                sys.executable, "-m", "gate_media_config", "checksum",
                "--map", str(checksum_map), "--version", PINNED_MEDIAMTX_VERSION,
                "--architecture", "arm64",
            ]
            accepted = subprocess.run(
                base, cwd=REPOSITORY_ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertEqual(0, accepted.returncode, accepted.stderr)

            checksum_map.chmod(0o640)
            insecure_mode = subprocess.run(
                base, cwd=REPOSITORY_ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, insecure_mode.returncode)
            checksum_map.chmod(0o600)

            trusted.chmod(0o770)
            insecure_directory = subprocess.run(
                base, cwd=REPOSITORY_ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, insecure_directory.returncode)
            trusted.chmod(0o700)

            link = trusted / "linked-checksums.txt"
            link.symlink_to(checksum_map)
            linked = subprocess.run(
                [*base[:5], str(link), *base[6:]], cwd=REPOSITORY_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            self.assertNotEqual(0, linked.returncode)

    def test_checksum_parser_uses_the_already_opened_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original = root / "checksums.txt"
            replacement = root / "replacement.txt"
            original.write_text(
                f"{PINNED_MEDIAMTX_VERSION} arm64 {'a' * 64}\n", encoding="utf-8"
            )
            replacement.write_text(
                f"{PINNED_MEDIAMTX_VERSION} arm64 {'b' * 64}\n", encoding="utf-8"
            )
            script = """
import os
import sys
from gate_media_config import lookup_checksum_from_fd

descriptor = os.open(sys.argv[1], os.O_RDONLY)
os.replace(sys.argv[2], sys.argv[1])
try:
    print(lookup_checksum_from_fd(descriptor, sys.argv[3], sys.argv[4]))
finally:
    os.close(descriptor)
"""
            completed = subprocess.run(
                [sys.executable, "-c", script, str(original), str(replacement),
                 PINNED_MEDIAMTX_VERSION, "arm64"],
                cwd=REPOSITORY_ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("a" * 64, completed.stdout.strip())

    def test_environment_parser_accepts_only_unique_exact_assignments(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway = self._write_valid_media_environments(root)
            accepted = self._validate_media_environments(auth, gateway)
            self.assertEqual(0, accepted.returncode, accepted.stderr)

            malformed_values = (
                "GATE_MEDIA_VIDEO_CONFIGURED =false\n",
                "GATE_MEDIA_VIDEO_CONFIGURED=false \n",
                "GATE_MEDIA_VIDEO_CONFIGURED=false\nGATE_MEDIA_VIDEO_CONFIGURED=false\n",
                "UNKNOWN_MEDIA_SETTING=false\n",
            )
            original = auth.read_text(encoding="utf-8")
            for malformed in malformed_values:
                with self.subTest(malformed=malformed):
                    auth.write_text(original + malformed, encoding="utf-8")
                    auth.chmod(0o600)
                    rejected = self._validate_media_environments(auth, gateway)
                    self.assertNotEqual(0, rejected.returncode)

            auth.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))
            auth.chmod(0o600)
            rejected_crlf = self._validate_media_environments(auth, gateway)
            self.assertNotEqual(0, rejected_crlf.returncode)

    def test_environment_parser_rejects_cross_secrets_and_validates_hmac_bytes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway = self._write_valid_media_environments(root)
            auth_text = auth.read_text(encoding="utf-8")
            gateway_text = gateway.read_text(encoding="utf-8")

            auth.write_text(auth_text + "MTX_PATHS_GATE_SOURCE=rtsp://forbidden\n")
            auth.chmod(0o600)
            self.assertNotEqual(
                0, self._validate_media_environments(auth, gateway).returncode
            )

            auth.write_text(auth_text, encoding="utf-8")
            auth.chmod(0o600)
            gateway.write_text(gateway_text + "GATE_MEDIA_HMAC_SECRET=forbidden\n")
            gateway.chmod(0o600)
            self.assertNotEqual(
                0, self._validate_media_environments(auth, gateway).returncode
            )

            gateway.write_text(gateway_text, encoding="utf-8")
            gateway.chmod(0o600)
            auth.write_text(auth_text.replace("x" * 32, "x" * 31), encoding="utf-8")
            auth.chmod(0o600)
            self.assertNotEqual(
                0, self._validate_media_environments(auth, gateway).returncode
            )

            auth.write_text(auth_text.replace("x" * 32, "é" * 16), encoding="utf-8")
            auth.chmod(0o600)
            accepted_multibyte = self._validate_media_environments(auth, gateway)
            self.assertEqual(0, accepted_multibyte.returncode, accepted_multibyte.stderr)

    def test_environment_parser_validates_rtsp_ice_and_mediamtx_turn_values(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway = self._write_valid_media_environments(root)
            valid_gateway = gateway.read_text(encoding="utf-8")
            accepted = self._validate_media_environments(auth, gateway)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            additional_host = "MTX_WEBRTCADDITIONALHOSTS=10.0.0.5"
            replacements = (
                ("rtsp://", "https://"),
                ("10.0.0.5:8189", "0.0.0.0:8189"),
                ("10.0.0.5:8189", "127.0.0.1:8189"),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS=10.0.0.6"),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS=10.0.0.5,10.0.0.6"),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS=0.0.0.0"),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS=127.0.0.1"),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS="),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS=10.0.0.5 "),
                (additional_host, "MTX_WEBRTCADDITIONALHOSTS" + "_0=10.0.0.5"),
                ("turns:turn.example.com:5349?transport=tcp", "stun:turn.example.com:3478"),
                ("MTX_WEBRTCICESERVERS2_0_USERNAME=turn-user\n", ""),
                ("MTX_WEBRTCICESERVERS2_0_CLIENTONLY=false", "MTX_WEBRTCICESERVERS2_0_CLIENTONLY=true"),
            )
            for old, new in replacements:
                with self.subTest(replacement=(old, new)):
                    gateway.write_text(valid_gateway.replace(old, new, 1), encoding="utf-8")
                    gateway.chmod(0o600)
                    rejected = self._validate_media_environments(auth, gateway)
                    self.assertNotEqual(0, rejected.returncode)

    def _write_valid_media_environments(self, root):
        auth = root / "gate-media-auth.env"
        gateway = root / "gate-media-gateway.env"
        auth.write_text(
            "GATE_MEDIA_HMAC_SECRET=" + "x" * 32 + "\n"
            "GATE_MEDIA_VIDEO_CONFIGURED=true\n"
            "GATE_MEDIA_VIDEO_VERIFIED=true\n"
            "GATE_MEDIA_LISTEN_CONFIGURED=false\n"
            "GATE_MEDIA_LISTEN_VERIFIED=false\n"
            "GATE_MEDIA_TALKBACK_CONFIGURED=false\n",
            encoding="utf-8",
        )
        gateway.write_text(
            "MTX_PATHS_GATE_SOURCE=rtsp://camera-user:camera-pass@10.0.0.10:554/stream\n"
            "MTX_WEBRTCLOCALUDPADDRESS=10.0.0.5:8189\n"
            "MTX_WEBRTCLOCALTCPADDRESS=10.0.0.5:8189\n"
            "MTX_WEBRTCADDITIONALHOSTS=10.0.0.5\n"
            "MTX_WEBRTCICESERVERS2_0_URL=turns:turn.example.com:5349?transport=tcp\n"
            "MTX_WEBRTCICESERVERS2_0_USERNAME=turn-user\n"
            "MTX_WEBRTCICESERVERS2_0_PASSWORD=turn-password\n"
            "MTX_WEBRTCICESERVERS2_0_CLIENTONLY=false\n",
            encoding="utf-8",
        )
        auth.chmod(0o600)
        gateway.chmod(0o600)
        return auth, gateway

    def _validate_media_environments(self, auth, gateway):
        return subprocess.run(
            [sys.executable, "-m", "gate_media_config", "environment",
             "--auth", str(auth), "--gateway", str(gateway)],
            cwd=REPOSITORY_ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False,
        )

    def test_media_environment_files_reject_cross_contaminated_secrets(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth, gateway = self._write_valid_media_environments(root)
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
stage_mediamtx_archive {shlex.quote(str(archive))} {PINNED_MEDIAMTX_VERSION} arm64
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
            archive, checksum_map = self._make_mediamtx_archive(
                root, PINNED_MEDIAMTX_VERSION
            )
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
install_mediamtx_binary {shlex.quote(str(archive))} {PINNED_MEDIAMTX_VERSION} {shlex.quote(str(checksum_map))}
"""
            environment = dict(os.environ)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["ORIGINAL_ARCHIVE"] = str(archive)
            completed = subprocess.run(
                ["bash", "-c", command], cwd=REPOSITORY_ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            stable = private / f"mediamtx-{PINNED_MEDIAMTX_VERSION}-arm64.tar.gz"
            self.assertTrue(stable.is_file())
            self.assertEqual(0o600, stable.stat().st_mode & 0o777)
            self.assertIn(PINNED_MEDIAMTX_VERSION, subprocess.check_output(
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
install_mediamtx_binary {shlex.quote(str(archive))} {PINNED_MEDIAMTX_VERSION} {shlex.quote(str(checksum_map))}
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
        checksum_map.write_text(
            f"{PINNED_MEDIAMTX_VERSION} arm64 {checksum}\n", encoding="utf-8"
        )
        checksum_map.chmod(0o600)
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

            noncanonical = default_capabilities()
            noncanonical["observed_at"] = 100
            noncanonical["media"]["video"] = {
                "configured": True, "ready": False, "verified": False,
                "reason": "hardware_unverified",
            }
            write_capabilities(target, noncanonical)
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
        }, {"video": True, "listen": False})

        self.assertTrue(snapshot["media"]["video"]["configured"])
        self.assertTrue(snapshot["media"]["video"]["verified"])
        self.assertFalse(snapshot["media"]["listen"]["configured"])
        self.assertNotIn("MTX_PATHS_GATE_SOURCE", snapshot)


class MediaGatewayHealthTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(media_health, "gateway_status_readiness"))

    def test_gate_path_reports_video_and_listen_readiness_by_recognized_track_type(self):
        cases = (
            (["H264"], {"video": True, "listen": False}),
            (["MPEG-4 Audio"], {"video": False, "listen": True}),
            (["AC-3"], {"video": False, "listen": True}),
            (["H264", "Opus"], {"video": True, "listen": True}),
        )
        environment = {
            "GATE_MEDIA_VIDEO_CONFIGURED": "true",
            "GATE_MEDIA_VIDEO_VERIFIED": "true",
            "GATE_MEDIA_LISTEN_CONFIGURED": "true",
            "GATE_MEDIA_LISTEN_VERIFIED": "true",
        }
        for flag in ("ready", "available"):
            for tracks, expected in cases:
                payload = json.dumps({
                    "itemCount": 1,
                    "pageCount": 1,
                    "items": [{"name": "gate", flag: True, "tracks": tracks}],
                }).encode("utf-8")
                with self.subTest(flag=flag, tracks=tracks):
                    readiness = media_health.gateway_status_readiness(payload)
                    self.assertEqual(expected, readiness)
                    media = capability_snapshot(environment, readiness)["media"]
                    self.assertEqual(expected["video"], media["video"]["ready"])
                    self.assertEqual(expected["video"], media["video"]["verified"])
                    self.assertEqual(expected["listen"], media["listen"]["ready"])
                    self.assertEqual(expected["listen"], media["listen"]["verified"])

        unavailable = (
            {"name": "gate", "ready": False, "tracks": ["H264"]},
            {"name": "gate", "ready": True, "tracks": []},
            {"name": "other", "ready": True, "tracks": ["H264"]},
        )
        for item in unavailable:
            payload = json.dumps({"itemCount": 1, "pageCount": 1, "items": [item]}).encode()
            with self.subTest(item=item):
                self.assertEqual(
                    {"video": False, "listen": False},
                    media_health.gateway_status_readiness(payload),
                )

    def test_gateway_health_fails_closed_for_unknown_or_malformed_tracks(self):
        invalid_tracks = (
            ["Generic"],
            ["H264", "unknown"],
            ["H264", ""],
            ["H264", {"codec": "Opus"}],
            "H264",
        )
        for tracks in invalid_tracks:
            payload = json.dumps({
                "items": [{"name": "gate", "ready": True, "tracks": tracks}],
            }).encode()
            with self.subTest(tracks=tracks):
                self.assertEqual(
                    {"video": False, "listen": False},
                    media_health.gateway_status_readiness(payload),
                )

    def test_gateway_health_rejects_malformed_duplicate_and_oversized_responses(self):
        payloads = (
            b"not-json",
            b'{"items":[],"items":[]}',
            b"{" + b"x" * 65_536 + b"}",
        )
        for payload in payloads:
            with self.subTest(size=len(payload)):
                self.assertEqual(
                    {"video": False, "listen": False},
                    media_health.gateway_status_readiness(payload),
                )

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

        self.assertEqual(
            {"video": True, "listen": False},
            _gateway_is_ready(opener=lambda *_args, **_kwargs: response),
        )
        self.assertEqual(65_537, response.limit)


if __name__ == "__main__":
    unittest.main()
