import os
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment import gate_controller_updater as updater
from deployment.gate_controller_updater import (
    ManagedComponent, UpdateConfig, UpdateError, component_digest, reconcile_components,
)


def config() -> UpdateConfig:
    return UpdateConfig.from_mapping({})


def make_release(root: Path, *, version: str = "1") -> Path:
    release = root / "releases" / ("a" * 40)
    (release / "gate_camera_control").mkdir(parents=True)
    (release / "gate_camera_control" / "__init__.py").write_text("")
    (release / "gate_camera_control" / "clock.py").write_text(f"VERSION = {version}\n")
    (release / "gate_camera_control" / "__pycache__").mkdir()
    (release / "gate_camera_control" / "__pycache__" / "x.pyc").write_bytes(b"ignored")
    (release / "gate_media_config.py").write_text("CONFIG = 1\n")
    (release / "deployment").mkdir()
    (release / "deployment" / "install-camera-control.sh").write_text("#!/bin/bash\n")
    return release


class ComponentDigestTests(unittest.TestCase):
    def test_the_digest_follows_source_content_and_ignores_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = ("gate_camera_control", "gate_media_config.py", "deployment/install-camera-control.sh")
            release = make_release(root)
            first = component_digest(release, sources)
            self.assertEqual(first, component_digest(release, sources), "deterministic")
            (release / "gate_camera_control" / "__pycache__" / "x.pyc").write_bytes(b"changed")
            self.assertEqual(first, component_digest(release, sources), "bytecode never counts")
            (release / "gate_camera_control" / "clock.py").write_text("VERSION = 2\n")
            self.assertNotEqual(first, component_digest(release, sources))
            (release / "gate_media_config.py").unlink()
            missing = component_digest(release, sources)
            self.assertNotEqual(first, missing, "a dropped file changes the digest")


class ReconcileComponentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.release = make_release(self.root)
        self.marker = self.root / "installed" / ".gate-release-digest"
        (self.root / "installed").mkdir()
        self.refreshes = []

    def tearDown(self):
        self.temporary.cleanup()

    def component(self, *, configured=True, refresh=None):
        def default_refresh(release, config):
            self.refreshes.append(release)

        return ManagedComponent(
            name="camera-control",
            sources=("gate_camera_control", "gate_media_config.py"),
            marker=self.marker,
            configured=lambda: configured,
            refresh=refresh or default_refresh,
        )

    def test_a_stale_component_is_refreshed_once_and_then_left_alone(self):
        component = self.component()
        self.assertEqual(reconcile_components(self.release, config(), [component]), [])
        self.assertEqual(self.refreshes, [self.release])
        digest = self.marker.read_text().strip()
        self.assertEqual(digest, component_digest(self.release, component.sources))
        self.assertEqual(reconcile_components(self.release, config(), [component]), [])
        self.assertEqual(len(self.refreshes), 1, "a matching marker skips the installer")

    def test_a_changed_release_refreshes_again(self):
        component = self.component()
        reconcile_components(self.release, config(), [component])
        (self.release / "gate_camera_control" / "clock.py").write_text("VERSION = 9\n")
        reconcile_components(self.release, config(), [component])
        self.assertEqual(len(self.refreshes), 2)

    def test_an_unbootstrapped_component_is_skipped_without_a_marker(self):
        with self.assertLogs(updater.LOGGER, level="INFO") as logs:
            self.assertEqual(reconcile_components(self.release, config(), [self.component(configured=False)]), [])
        self.assertEqual(self.refreshes, [])
        self.assertFalse(self.marker.exists())
        self.assertIn("not bootstrapped", "\n".join(logs.output))

    def test_a_failed_refresh_is_reported_and_retried_next_time(self):
        calls = []

        def failing(release, config):
            calls.append(release)
            raise UpdateError("installer exited 1")

        component = self.component(refresh=failing)
        with self.assertLogs(updater.LOGGER, level="ERROR") as logs:
            self.assertEqual(reconcile_components(self.release, config(), [component]), ["camera-control"])
        self.assertFalse(self.marker.exists(), "no marker until the refresh succeeds")
        self.assertIn("was not refreshed: installer exited 1", logs.output[0])
        reconcile_components(self.release, config(), [component])
        self.assertEqual(len(calls), 2)

    def test_a_corrupt_marker_is_treated_as_stale(self):
        self.marker.write_text("not a digest\n")
        component = self.component()
        reconcile_components(self.release, config(), [component])
        self.assertEqual(len(self.refreshes), 1)


class WritablePathTests(unittest.TestCase):
    def test_a_read_only_component_root_is_reported_and_never_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = make_release(root)
            blocked = root / "blocked"
            blocked.mkdir(mode=0o555)
            refreshes = []
            component = ManagedComponent(
                name="camera-control",
                sources=("gate_camera_control",),
                marker=root / "marker",
                configured=lambda: True,
                refresh=lambda release, config: refreshes.append(release),
                writable=(blocked,),
            )
            if os.geteuid() == 0:
                blocked.chmod(0o755)
                self.skipTest("root ignores the permission bits this probes")
            try:
                with self.assertLogs(updater.LOGGER, level="WARNING") as logs:
                    self.assertEqual(reconcile_components(release, config(), [component]), [])
                self.assertEqual(refreshes, [], "nothing is published into a read-only root")
                self.assertFalse((root / "marker").exists(), "and the marker stays stale")
                self.assertIn("cannot follow releases yet", logs.output[0])
                self.assertIn("deployment/install.sh", logs.output[0])
            finally:
                # Restored here, not in addCleanup: the temporary directory is
                # already gone by the time cleanups run.
                blocked.chmod(0o755)

    def test_a_writable_root_probes_clean_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(updater.unwritable_path([root]))
            self.assertEqual(list(root.iterdir()), [], "the probe file is removed")

    def test_a_missing_root_is_not_treated_as_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(updater.unwritable_path([Path(directory) / "absent"]))


class CameraControlRefreshTests(unittest.TestCase):
    def test_the_installer_runs_from_the_release_through_bash(self):
        with tempfile.TemporaryDirectory() as directory:
            release = make_release(Path(directory))
            recorded = []
            with patch("deployment.gate_controller_updater._run_command", side_effect=lambda arguments, **options: recorded.append(([str(a) for a in arguments], options)) or ""):
                updater._refresh_camera_control(release, config())
            command, options = recorded[0]
            self.assertEqual(command[:2], ["/bin/bash", str(release / "deployment" / "install-camera-control.sh")])
            self.assertEqual(command[2:], ["--source", str(release)])
            self.assertEqual(options["cwd"], release)
            self.assertEqual(options["timeout"], updater.COMPONENT_INSTALL_TIMEOUT_SECONDS)

    def test_a_release_without_the_installer_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            release = make_release(Path(directory))
            (release / "deployment" / "install-camera-control.sh").unlink()
            with self.assertRaises(UpdateError):
                updater._refresh_camera_control(release, config())


class MediaRefreshTests(unittest.TestCase):
    def test_every_published_media_file_comes_from_the_release_and_services_are_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            for relative, _published, _mode in updater.MEDIA_PUBLISHED_FILES:
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {relative}\n")
            (release / "deployment" / "media" / "mediamtx.yml").write_text("paths: {}\n")
            recorded = []
            installed = []

            def fake_install(source, destination, mode, group="root"):
                installed.append((str(source.relative_to(release)), str(destination), mode, group))

            with patch("deployment.gate_controller_updater._install_file", side_effect=fake_install), \
                    patch("deployment.gate_controller_updater._run_command", side_effect=lambda arguments, **options: recorded.append([str(a) for a in arguments]) or ""):
                updater._refresh_media(release, config())
            self.assertEqual(len(installed), len(updater.MEDIA_PUBLISHED_FILES) + 1)
            self.assertIn(("deployment/gate_media_turn_refresh.py", "/usr/local/lib/gate-media/gate_media_turn_refresh.py", 0o700, "root"), installed)
            self.assertIn(("deployment/media/mediamtx.yml", "/etc/gate-media/mediamtx.yml", 0o640, "gate-media"), installed)
            # The talk credential is minted before the units that read it are
            # republished and restarted, so a host that only ever follows
            # releases ends the run with a working push-to-talk rather than a
            # sidecar refusing the loopback pull.
            self.assertEqual(
                recorded[0][1:],
                [str(release / "gate_media_config.py"), "talk-credential",
                 "--env", str(updater.TALK_CREDENTIAL_ENVIRONMENT), "--ensure"],
            )
            self.assertEqual(recorded[1], ["systemctl", "daemon-reload"])
            self.assertEqual(recorded[2][:2], ["systemctl", "try-restart"])
            self.assertEqual(set(recorded[2][2:]), set(updater.MEDIA_SERVICES))
            self.assertEqual(
                Path("/etc/gate-media/talk.env"), updater.TALK_CREDENTIAL_ENVIRONMENT
            )

    def test_a_release_without_the_config_validator_cannot_mint_a_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            release.mkdir()

            with self.assertRaises(UpdateError):
                updater._ensure_talk_credential(release, config())


class ProxyTemplateTests(unittest.TestCase):
    def test_a_changed_whep_template_is_reported_because_the_render_cannot_follow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            template = release / "deployment" / "media" / "nginx-whep-locations.conf.template"
            template.parent.mkdir(parents=True)
            template.write_text("location /whep { origin ORIGIN; }\n")
            installed = root / "etc"
            installed.mkdir()
            with patch("deployment.gate_controller_updater.MEDIA_CONFIG_ROOT", installed):
                self.assertFalse(
                    updater._warn_if_proxy_template_changed(release),
                    "nothing installed yet is a bootstrap question, not a drift warning",
                )
                (installed / "nginx-whep-locations.conf.template").write_text(
                    "location /whep { origin ORIGIN; }\n"
                )
                self.assertFalse(updater._warn_if_proxy_template_changed(release))
                (installed / "nginx-whep-locations.conf.template").write_text("older\n")
                with self.assertLogs(updater.LOGGER, level="WARNING") as logs:
                    self.assertTrue(updater._warn_if_proxy_template_changed(release))
                self.assertIn("still rendered", logs.output[0])
                self.assertIn("--allowed-origin", logs.output[0])


class RunOnceIntegrationTests(unittest.TestCase):
    def test_an_already_active_release_still_reconciles_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = make_release(root)
            current = root / "current"
            current.symlink_to(release)
            cfg = config()
            with patch.object(updater.UpdateConfig, "current_link", new=property(lambda self: current)), \
                    patch("deployment.gate_controller_updater.reconcile_pending_activation", return_value=release.name), \
                    patch("deployment.gate_controller_updater._main_commit_payload", return_value={"sha": release.name}), \
                    patch("deployment.gate_controller_updater.read_main_sha", return_value=release.name), \
                    patch("deployment.gate_controller_updater.reconcile_components", return_value=["camera-control"]) as reconcile:
                self.assertEqual(updater.run_once(cfg), 1)
            reconcile.assert_called_once()
            self.assertEqual(reconcile.call_args.args[0], release.resolve())


if __name__ == "__main__":
    unittest.main()
