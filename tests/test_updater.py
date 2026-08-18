import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from deployment.gate_controller_updater import (
    LOGGER,
    ReleaseEntry,
    UpdateError,
    UpdateConfig,
    UpdateDecision,
    _atomic_symlink,
    _atomic_write,
    _command_environment,
    _legacy_command_state,
    _systemctl_is,
    activate_release,
    decide_update,
    discover_current_sha,
    has_successful_ci_run,
    read_main_sha,
    reconcile_pending_activation,
    releases_to_prune,
    run_once,
    stage_release,
    verify_release,
)


TARGET_SHA = "a" * 40
OTHER_SHA = "b" * 40


class SimulatedPowerLoss(BaseException):
    pass


def workflow_run(
    *,
    sha=TARGET_SHA,
    status="completed",
    conclusion="success",
    event="push",
    branch="master",
    path=".github/workflows/ci.yml",
):
    return {
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "head_branch": branch,
        "path": path,
    }


class MainCommitTests(unittest.TestCase):
    def test_reads_exact_lowercase_commit_sha(self):
        self.assertEqual(TARGET_SHA, read_main_sha({"sha": TARGET_SHA}))

    def test_rejects_malformed_commit_payload(self):
        for payload in ({}, {"sha": "abc"}, {"sha": "G" * 40}, []):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    read_main_sha(payload)


class WorkflowDecisionTests(unittest.TestCase):
    def test_accepts_only_completed_successful_protected_branch_push_for_exact_sha(self):
        payload = {
            "workflow_runs": [
                workflow_run(sha=OTHER_SHA),
                workflow_run(status="in_progress", conclusion=None),
                workflow_run(conclusion="failure"),
                workflow_run(event="pull_request"),
                workflow_run(branch="feature"),
                workflow_run(path=".github/workflows/other.yml"),
                workflow_run(),
            ]
        }

        self.assertTrue(has_successful_ci_run(payload, TARGET_SHA))

    def test_rejects_success_for_wrong_sha_when_target_failed(self):
        payload = {
            "workflow_runs": [
                workflow_run(sha=OTHER_SHA),
                workflow_run(conclusion="failure"),
            ]
        }

        self.assertFalse(has_successful_ci_run(payload, TARGET_SHA))

    def test_accepts_github_workflow_path_with_branch_suffix(self):
        payload = {
            "workflow_runs": [
                workflow_run(path=".github/workflows/ci.yml@master"),
            ]
        }

        self.assertTrue(has_successful_ci_run(payload, TARGET_SHA))

    def test_rejects_malformed_workflow_payload(self):
        for payload in ({}, {"workflow_runs": {}}, []):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    has_successful_ci_run(payload, TARGET_SHA)

    def test_no_change_does_not_require_workflow_payload(self):
        decision = decide_update(TARGET_SHA, {"sha": TARGET_SHA}, None)

        self.assertEqual(UpdateDecision.NO_CHANGE, decision)

    def test_defers_candidate_without_successful_exact_ci(self):
        decision = decide_update(
            OTHER_SHA,
            {"sha": TARGET_SHA},
            {"workflow_runs": [workflow_run(conclusion="failure")]},
        )

        self.assertEqual(UpdateDecision.DEFER, decision)

    def test_installs_candidate_with_successful_exact_ci(self):
        decision = decide_update(
            OTHER_SHA,
            {"sha": TARGET_SHA},
            {"workflow_runs": [workflow_run()]},
        )

        self.assertEqual(UpdateDecision.INSTALL, decision)


class ReleaseRetentionTests(unittest.TestCase):
    def test_keeps_active_release_even_when_it_is_oldest(self):
        entries = [
            ReleaseEntry(TARGET_SHA, 1.0),
            ReleaseEntry("1" * 40, 2.0),
            ReleaseEntry("2" * 40, 3.0),
            ReleaseEntry("3" * 40, 4.0),
        ]

        self.assertEqual(
            ["1" * 40],
            releases_to_prune(entries, current_sha=TARGET_SHA, keep=3),
        )

    def test_rejects_retention_without_a_rollback_release(self):
        with self.assertRaises(ValueError):
            releases_to_prune([], current_sha=TARGET_SHA, keep=2)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_keep_active_release_and_two_rollback_releases(self):
        config = UpdateConfig.from_mapping({})

        self.assertEqual(3, config.keep_releases)
        self.assertEqual("ciaran-finnegan/gate-controller", config.repository)
        self.assertEqual("master", config.branch)

    def test_accepts_an_explicit_release_branch(self):
        self.assertEqual(
            "release/stable",
            UpdateConfig.from_mapping({"GATE_UPDATE_BRANCH": "release/stable"}).branch,
        )

    def test_rejects_an_unsafe_release_branch(self):
        for branch in ("", "/main", "../main", "main//candidate", "main/"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                UpdateConfig.from_mapping({"GATE_UPDATE_BRANCH": branch})

    def test_pending_activation_record_is_inside_fixed_install_root(self):
        config = UpdateConfig.from_mapping({})

        self.assertEqual(
            Path("/opt/gate-controller-deploy/pending-activation.json"),
            getattr(config, "pending_activation_path", None),
        )

    def test_rejects_relative_or_persistent_install_roots(self):
        for root in ("relative", "/", "/var/lib/gate-controller"):
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    UpdateConfig.from_mapping({"GATE_UPDATE_ROOT": root})

    def test_candidate_commands_do_not_inherit_github_credentials(self):
        config = UpdateConfig.from_mapping({"GITHUB_TOKEN": "private-token"})

        with patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "inherited-token",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "unsafe-helper",
            },
        ):
            environment = _command_environment(config, include_git_auth=False)

        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("GIT_CONFIG_COUNT", environment)
        self.assertNotIn("GIT_CONFIG_KEY_0", environment)
        self.assertNotIn("GIT_CONFIG_VALUE_0", environment)

    def test_only_git_fetch_environment_receives_scoped_authorization(self):
        config = UpdateConfig.from_mapping({"GITHUB_TOKEN": "private-token"})

        environment = _command_environment(config, include_git_auth=True)

        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertEqual("1", environment["GIT_CONFIG_COUNT"])
        self.assertEqual(
            "http.https://github.com/.extraheader",
            environment["GIT_CONFIG_KEY_0"],
        )
        self.assertTrue(environment["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
        self.assertNotIn("private-token", environment["GIT_CONFIG_VALUE_0"])


class ActiveReleaseTests(unittest.TestCase):
    def test_atomic_write_fsyncs_file_and_containing_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "pending-activation.json"

            with patch(
                "deployment.gate_controller_updater.os.fsync",
                wraps=os.fsync,
            ) as fsync:
                _atomic_write(destination, b"pending", 0o600)

            self.assertEqual(b"pending", destination.read_bytes())
            self.assertEqual(2, fsync.call_count)

    def test_atomic_symlink_fsyncs_containing_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / TARGET_SHA
            target.mkdir()
            link = root / "current"

            with patch(
                "deployment.gate_controller_updater.os.fsync",
                wraps=os.fsync,
            ) as fsync:
                _atomic_symlink(target, link)

            self.assertEqual(target.resolve(strict=True), link.resolve(strict=True))
            self.assertEqual(1, fsync.call_count)

    def test_reads_sha_only_from_symlink_inside_release_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            releases_root = install_root / "releases"
            release = releases_root / TARGET_SHA
            release.mkdir(parents=True)
            (install_root / "current").symlink_to(release)

            self.assertEqual(
                TARGET_SHA,
                discover_current_sha(install_root / "current", releases_root),
            )

    def test_rejects_current_symlink_outside_release_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            releases_root = install_root / "releases"
            releases_root.mkdir()
            outside = install_root / TARGET_SHA
            outside.mkdir()
            (install_root / "current").symlink_to(outside)

            with self.assertRaises(ValueError):
                discover_current_sha(install_root / "current", releases_root)


class ActivationRecoveryTests(unittest.TestCase):
    def setUp(self):
        previous_disabled = LOGGER.disabled
        LOGGER.disabled = True
        self.addCleanup(setattr, LOGGER, "disabled", previous_disabled)
        systemctl_probe = patch(
            "deployment.gate_controller_updater.subprocess.run",
            return_value=subprocess.CompletedProcess([], 4, stdout="not-found\n"),
        )
        systemctl_probe.start()
        self.addCleanup(systemctl_probe.stop)

    def test_legacy_probe_recognises_confirmed_inactive_and_not_found_states(self):
        responses = [
            subprocess.CompletedProcess([], 1, stdout="disabled\n"),
            subprocess.CompletedProcess([], 4, stdout="not-found\n"),
        ]
        calls = []

        def return_confirmed_state(arguments, **_options):
            calls.append(arguments)
            return responses.pop(0)

        with patch(
            "deployment.gate_controller_updater.subprocess.run",
            side_effect=return_confirmed_state,
        ):
            state = _legacy_command_state()

        self.assertFalse(state.enabled)
        self.assertFalse(state.active)
        self.assertEqual(
            [
                ["systemctl", "is-enabled", "gate-command-server.service"],
                ["systemctl", "is-active", "gate-command-server.service"],
            ],
            calls,
        )

    def test_legacy_probe_treats_missing_unit_file_as_not_enabled(self):
        responses = [
            subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr=(
                    "Failed to get unit file state for "
                    "gate-command-server.service: No such file or directory\n"
                ),
            ),
            subprocess.CompletedProcess([], 3, stdout="inactive\n", stderr=""),
        ]

        def return_missing_unit(arguments, **_options):
            return responses.pop(0)

        with patch(
            "deployment.gate_controller_updater.subprocess.run",
            side_effect=return_missing_unit,
        ):
            state = _legacy_command_state()

        self.assertFalse(state.enabled)
        self.assertFalse(state.active)

    def test_legacy_probe_rejects_unrelated_missing_file_diagnostics(self):
        with patch(
            "deployment.gate_controller_updater.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="systemctl: /run/dbus/system_bus_socket: No such file or directory\n",
            ),
        ):
            with self.assertRaises(UpdateError):
                _systemctl_is("is-enabled", "gate-command-server.service")

    def test_legacy_probe_timeout_raises_update_error(self):
        with patch(
            "deployment.gate_controller_updater.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["systemctl"], 10),
        ):
            with self.assertRaises(UpdateError):
                _legacy_command_state()

    def test_activation_aborts_before_retiring_legacy_service_on_partial_probe(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            commands = []

            def enabled_then_timeout(arguments, **_options):
                if arguments[1] == "is-enabled":
                    return subprocess.CompletedProcess(arguments, 0)
                raise subprocess.TimeoutExpired(arguments, 10)

            def record_command(arguments, **_options):
                commands.append(tuple(str(argument) for argument in arguments))

            with patch(
                "deployment.gate_controller_updater.subprocess.run",
                side_effect=enabled_then_timeout,
            ), patch(
                "deployment.gate_controller_updater._run_command",
                side_effect=record_command,
            ), patch(
                "deployment.gate_controller_updater._restart_and_confirm"
            ) as restart_and_confirm:
                error = None
                try:
                    activate_release(candidate, previous, config)
                except Exception as caught:
                    error = caught

            self.assertIsInstance(error, UpdateError)
            self.assertEqual(previous.resolve(), config.current_link.resolve())
            self.assertEqual([], commands)
            restart_and_confirm.assert_not_called()

    def test_activation_writes_only_inside_managed_install_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(
                UpdateConfig.from_mapping({}),
                install_root=install_root,
                health_seconds=1,
            )
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            (candidate / "file-monitor.service").write_text("candidate", encoding="utf-8")
            config.current_link.symlink_to(previous)
            real_atomic_write = _atomic_write

            def reject_write_outside_install_root(path, content, mode):
                if path.parent != install_root:
                    raise AssertionError(f"privileged write outside install root: {path}")
                real_atomic_write(path, content, mode)

            activation_succeeded = True
            try:
                with patch(
                    "deployment.gate_controller_updater._atomic_write",
                    side_effect=reject_write_outside_install_root,
                ), patch(
                    "deployment.gate_controller_updater._run_command"
                ), patch(
                    "deployment.gate_controller_updater._restart_and_confirm"
                ):
                    activate_release(candidate, previous, config)
            except UpdateError:
                activation_succeeded = False

            self.assertTrue(activation_succeeded)
            self.assertEqual(candidate.resolve(), config.current_link.resolve())

    def test_successful_activation_clears_pending_record_after_health(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm"
            ):
                activate_release(candidate, previous, config)

            self.assertEqual(candidate.resolve(), config.current_link.resolve())
            self.assertFalse(config.pending_activation_path.exists())

    def test_activation_retires_legacy_command_service_before_restarting_candidate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            commands = []

            def record_systemctl(arguments, **_options):
                commands.append(tuple(str(argument) for argument in arguments))
                return ""

            def legacy_service_running(arguments, **_options):
                commands.append(tuple(arguments))
                return subprocess.CompletedProcess(arguments, 0)

            def restart_candidate(_config):
                commands.append(("restart-candidate",))

            with patch(
                "deployment.gate_controller_updater.subprocess.run",
                side_effect=legacy_service_running,
            ), patch(
                "deployment.gate_controller_updater._run_command",
                side_effect=record_systemctl,
            ), patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=restart_candidate,
            ):
                activate_release(candidate, previous, config)

            self.assertEqual(
                [
                    ("systemctl", "is-enabled", "gate-command-server.service"),
                    ("systemctl", "is-active", "gate-command-server.service"),
                    ("systemctl", "disable", "--now", "gate-command-server.service"),
                    ("restart-candidate",),
                ],
                commands,
            )

    def test_activation_rollback_restores_enabled_and_active_legacy_command_service(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            (previous / "deployment/systemd").mkdir(parents=True)
            (previous / "deployment/systemd/gate-command-server.service").touch()
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            commands = []
            health_attempts = 0

            def record_systemctl(arguments, **_options):
                commands.append(tuple(str(argument) for argument in arguments))
                return ""

            def legacy_service_running(_arguments, **_options):
                return subprocess.CompletedProcess([], 0)

            def fail_candidate_then_confirm_rollback(_config):
                nonlocal health_attempts
                health_attempts += 1
                if health_attempts == 1:
                    commands.append(("candidate-health",))
                    raise UpdateError("candidate unhealthy")
                commands.append(("rollback-health",))

            with patch(
                "deployment.gate_controller_updater.subprocess.run",
                side_effect=legacy_service_running,
            ), patch(
                "deployment.gate_controller_updater._run_command",
                side_effect=record_systemctl,
            ), patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=fail_candidate_then_confirm_rollback,
            ), self.assertRaises(UpdateError):
                activate_release(candidate, previous, config)

            self.assertEqual(
                [
                    ("systemctl", "disable", "--now", "gate-command-server.service"),
                    ("candidate-health",),
                    ("rollback-health",),
                    ("systemctl", "enable", "gate-command-server.service"),
                    ("systemctl", "restart", "gate-command-server.service"),
                ],
                commands,
            )

    def test_activation_failure_clears_marker_only_after_confirmed_rollback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            health_attempts = 0
            rollback_confirmation = install_root / "rollback-confirmed"

            def confirm_health(_config):
                nonlocal health_attempts
                health_attempts += 1
                if health_attempts == 1:
                    raise UpdateError("candidate unhealthy")
                rollback_confirmation.touch()

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=confirm_health,
            ), self.assertRaises(UpdateError):
                activate_release(candidate, previous, config)

            self.assertEqual(previous.resolve(), config.current_link.resolve())
            self.assertTrue(rollback_confirmation.is_file())
            self.assertFalse(config.pending_activation_path.exists())

    def test_symlink_fsync_failure_rolls_back_actual_candidate_before_clearing_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            real_fsync_directory = __import__(
                "deployment.gate_controller_updater", fromlist=["_fsync_directory"]
            )._fsync_directory
            fsync_calls = 0
            confirmed_shas = []

            def fail_candidate_link_fsync(directory):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("simulated directory fsync failure")
                real_fsync_directory(directory)

            def confirm_current(_config):
                confirmed_shas.append(
                    discover_current_sha(config.current_link, config.releases_root)
                )

            with patch(
                "deployment.gate_controller_updater._fsync_directory",
                side_effect=fail_candidate_link_fsync,
            ), patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=confirm_current,
            ), self.assertRaises(UpdateError):
                activate_release(candidate, previous, config)

            self.assertEqual(OTHER_SHA, discover_current_sha(config.current_link, config.releases_root))
            self.assertEqual([OTHER_SHA], confirmed_shas)
            self.assertFalse(config.pending_activation_path.exists())

    def test_activation_persists_candidate_and_previous_before_switch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            (candidate / "file-monitor.service").write_text("candidate", encoding="utf-8")
            config.current_link.symlink_to(previous)
            real_atomic_write = _atomic_write

            def write_only_inside_install_root(path, content, mode):
                if path.parent == install_root:
                    real_atomic_write(path, content, mode)

            with patch(
                "deployment.gate_controller_updater._atomic_write",
                side_effect=write_only_inside_install_root,
            ), patch(
                "deployment.gate_controller_updater._run_command"
            ), patch(
                "deployment.gate_controller_updater._atomic_symlink",
                side_effect=SimulatedPowerLoss,
            ), self.assertRaises(SimulatedPowerLoss):
                activate_release(candidate, previous, config)

            self.assertTrue(config.pending_activation_path.is_file())
            self.assertEqual(
                {
                    "candidate": TARGET_SHA,
                    "legacy_command_active": False,
                    "legacy_command_enabled": False,
                    "previous": OTHER_SHA,
                    "version": 2,
                },
                json.loads(config.pending_activation_path.read_text(encoding="utf-8")),
            )

    def test_run_once_health_checks_pending_candidate_before_accepting_current(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(
                UpdateConfig.from_mapping({}),
                install_root=install_root,
                health_seconds=1,
            )
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(candidate)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            health_confirmation = install_root / "health-confirmed"

            def confirm_health(_config):
                health_confirmation.touch()

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=confirm_health,
            ), patch(
                "deployment.gate_controller_updater._main_commit_payload",
                return_value={"sha": TARGET_SHA},
            ):
                result = run_once(config)

            self.assertEqual(0, result)
            self.assertTrue(health_confirmation.is_file())
            self.assertFalse(config.pending_activation_path.exists())

    def test_pending_candidate_health_failure_rolls_back_and_confirms_previous(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(
                UpdateConfig.from_mapping({}),
                install_root=install_root,
                health_seconds=1,
            )
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(candidate)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            rollback_confirmation = install_root / "rollback-confirmed"
            health_attempts = 0

            def confirm_health(_config):
                nonlocal health_attempts
                health_attempts += 1
                if health_attempts == 1:
                    raise UpdateError("candidate unhealthy")
                rollback_confirmation.touch()

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=confirm_health,
            ), self.assertRaises(UpdateError):
                reconcile_pending_activation(config)

            self.assertEqual(previous.resolve(), config.current_link.resolve())
            self.assertTrue(rollback_confirmation.is_file())
            self.assertFalse(config.pending_activation_path.exists())

    def test_pending_rollback_restores_enabled_and_active_legacy_command_service(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            (previous / "deployment/systemd").mkdir(parents=True)
            (previous / "deployment/systemd/gate-command-server.service").touch()
            candidate.mkdir()
            config.current_link.symlink_to(candidate)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "legacy_command_active": True,
                        "legacy_command_enabled": True,
                        "version": 2,
                    }
                ),
                encoding="utf-8",
            )
            commands = []
            health_attempts = 0

            def record_systemctl(arguments, **_options):
                commands.append(tuple(str(argument) for argument in arguments))
                return ""

            def fail_candidate_then_confirm_rollback(_config):
                nonlocal health_attempts
                health_attempts += 1
                if health_attempts == 1:
                    raise UpdateError("candidate unhealthy")

            with patch(
                "deployment.gate_controller_updater._run_command",
                side_effect=record_systemctl,
            ), patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=fail_candidate_then_confirm_rollback,
            ), self.assertRaises(UpdateError):
                reconcile_pending_activation(config)

            self.assertEqual(
                [
                    ("systemctl", "enable", "gate-command-server.service"),
                    ("systemctl", "restart", "gate-command-server.service"),
                ],
                commands,
            )

    def test_pending_previous_is_confirmed_as_interrupted_activation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(
                UpdateConfig.from_mapping({}),
                install_root=install_root,
                health_seconds=1,
            )
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            previous_confirmation = install_root / "previous-confirmed"

            def confirm_health(_config):
                previous_confirmation.touch()

            try:
                with patch(
                    "deployment.gate_controller_updater._restart_and_confirm",
                    side_effect=confirm_health,
                ):
                    reconciled_sha = reconcile_pending_activation(config)
            except UpdateError:
                reconciled_sha = None

            self.assertEqual(OTHER_SHA, reconciled_sha)
            self.assertTrue(previous_confirmation.is_file())
            self.assertFalse(config.pending_activation_path.exists())

    def test_unhealthy_pending_previous_returns_failure_and_preserves_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )

            try:
                with patch(
                    "deployment.gate_controller_updater._restart_and_confirm",
                    side_effect=UpdateError("previous unhealthy"),
                ):
                    result = run_once(config)
            except UpdateError:
                result = None

            self.assertEqual(1, result)
            self.assertTrue(config.pending_activation_path.is_file())

    def test_confirmed_recovery_fsyncs_marker_removal(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(candidate)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm"
            ), patch(
                "deployment.gate_controller_updater.os.fsync",
                wraps=os.fsync,
            ) as fsync:
                reconcile_pending_activation(config)

            self.assertFalse(config.pending_activation_path.exists())
            self.assertEqual(3, fsync.call_count)

    def test_pending_release_symlink_outside_managed_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            outside = install_root / "outside"
            previous.mkdir(parents=True)
            outside.mkdir()
            (config.releases_root / TARGET_SHA).symlink_to(outside)
            config.current_link.symlink_to(previous)
            config.pending_activation_path.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            health_confirmation = install_root / "health-confirmed"

            def confirm_health(_config):
                health_confirmation.touch()

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm",
                side_effect=confirm_health,
            ), patch(
                "deployment.gate_controller_updater._main_commit_payload",
                return_value={"sha": OTHER_SHA},
            ):
                result = run_once(config)

            self.assertEqual(1, result)
            self.assertTrue(config.pending_activation_path.is_file())
            self.assertFalse(health_confirmation.exists())

    def test_malformed_pending_records_fail_closed_and_remain_present(self):
        invalid_records = (
            b"[]",
            json.dumps(
                {"candidate": TARGET_SHA, "previous": OTHER_SHA}
            ).encode("utf-8"),
            json.dumps(
                {
                    "candidate": TARGET_SHA,
                    "previous": OTHER_SHA,
                    "unexpected": True,
                    "version": 1,
                }
            ).encode("utf-8"),
            json.dumps(
                {
                    "candidate": TARGET_SHA,
                    "previous": OTHER_SHA,
                    "version": 2,
                }
            ).encode("utf-8"),
            json.dumps(
                {
                    "candidate": "not-a-sha",
                    "previous": OTHER_SHA,
                    "version": 1,
                }
            ).encode("utf-8"),
            json.dumps(
                {
                    "candidate": OTHER_SHA,
                    "previous": OTHER_SHA,
                    "version": 1,
                }
            ).encode("utf-8"),
        )

        for record in invalid_records:
            with self.subTest(record=record):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    install_root = Path(temporary_directory)
                    config = replace(
                        UpdateConfig.from_mapping({}), install_root=install_root
                    )
                    previous = config.releases_root / OTHER_SHA
                    candidate = config.releases_root / TARGET_SHA
                    previous.mkdir(parents=True)
                    candidate.mkdir()
                    config.current_link.symlink_to(previous)
                    config.pending_activation_path.write_bytes(record)

                    try:
                        with patch(
                            "deployment.gate_controller_updater._restart_and_confirm"
                        ), patch(
                            "deployment.gate_controller_updater._main_commit_payload",
                            return_value={"sha": OTHER_SHA},
                        ):
                            result = run_once(config)
                    except Exception:
                        result = None

                    self.assertEqual(1, result)
                    self.assertTrue(config.pending_activation_path.is_file())

    def test_symlinked_pending_record_fails_closed_without_unlinking_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            previous = config.releases_root / OTHER_SHA
            candidate = config.releases_root / TARGET_SHA
            previous.mkdir(parents=True)
            candidate.mkdir()
            config.current_link.symlink_to(previous)
            outside_record = install_root / "outside-pending.json"
            outside_record.write_text(
                json.dumps(
                    {
                        "candidate": TARGET_SHA,
                        "previous": OTHER_SHA,
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            config.pending_activation_path.symlink_to(outside_record)

            with patch(
                "deployment.gate_controller_updater._restart_and_confirm"
            ), patch(
                "deployment.gate_controller_updater._main_commit_payload",
                return_value={"sha": OTHER_SHA},
            ):
                result = run_once(config)

            self.assertEqual(1, result)
            self.assertTrue(config.pending_activation_path.is_symlink())
            self.assertTrue(outside_record.is_file())


class ReleaseStagingTests(unittest.TestCase):
    def test_every_candidate_verification_command_runs_as_build_user(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            python = release / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            (release / "file-monitor.service").write_text(
                "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
            )
            config = replace(
                UpdateConfig.from_mapping({}), install_root=release.parent
            )
            command_users = []

            def record_command(_arguments, **options):
                command_users.append(options.get("run_as_user"))
                return ""

            with patch(
                "deployment.gate_controller_updater._run_command",
                side_effect=record_command,
            ), patch(
                "deployment.gate_controller_updater.shutil.which",
                return_value="/usr/bin/systemd-analyze",
            ):
                verify_release(release, config)

            self.assertTrue(command_users)
            self.assertEqual(
                ["gate-controller-build"] * len(command_users),
                command_users,
            )

    def test_git_staging_commands_run_as_unprivileged_build_user(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            install_root = Path(temporary_directory)
            config = replace(UpdateConfig.from_mapping({}), install_root=install_root)
            git_users = []

            def emulate_command(arguments, **options):
                command = [str(argument) for argument in arguments]
                if command[0] == "git":
                    git_users.append(options.get("run_as_user"))
                    if "init" in command:
                        Path(command[-1], ".git").mkdir()
                    if "rev-parse" in command:
                        return TARGET_SHA
                return ""

            with patch(
                "deployment.gate_controller_updater._run_command",
                side_effect=emulate_command,
            ), patch("deployment.gate_controller_updater.verify_release"):
                release = stage_release(TARGET_SHA, config)

            self.assertEqual(config.releases_root / TARGET_SHA, release)
            self.assertTrue(git_users)
            self.assertEqual(
                ["gate-controller-build"] * len(git_users),
                git_users,
            )


if __name__ == "__main__":
    unittest.main()
