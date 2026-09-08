import argparse
import builtins
import unittest
import os
import pathlib
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from unittest.mock import Mock, patch

import gate_controller.__main__ as gate_main
from gate_controller.__main__ import (
    _quiet_window, _shutdown_controller, build_background_workers,
    build_reolink_trigger_pipeline, default_runtime_paths,
)
from gate_controller.authorisation import AuthorisationRefreshWorker, AuthorisedPlateCache
from gate_controller.control_plane import HeartbeatWorker
from gate_controller.command_server import CommandServerWorker
from gate_controller.metrics import MetricsRing, MetricsRollupWorker, QuotaLedger
from gate_controller.net_probe import NetProbeWorker
from gate_controller.outbox import OutboxWorker
from gate_controller.relay import RelayController
from gate_controller.store import LocalStore


#: Absolute paths the running controller owns on the device. No unit test may
#: reach one, not even to stat it.
#:
#: The Pi's release verifier runs this suite as the unprivileged
#: ``gate-controller-build`` user, for whom ``/var/lib/gate-controller`` is not
#: traversable at all. ``Path.exists()`` there does not return ``False``: it
#: raises ``PermissionError``, because pathlib swallows ENOENT and never
#: EACCES. A GitHub runner has no such directory, so the same call returns
#: ``False`` and CI stays green while every release is blocked on the device.
#: That asymmetry is exactly why the guard below lives in the suite rather than
#: in the pipeline.
LIVE_CONTROLLER_PATHS = (
    "/var/lib/gate-controller",
    "/etc/gate-controller.env",
    "/opt/gate-controller-deploy",
    "/run/gate-controller-updater",
    "/run/gate-media",
    "/home/ftp-user",
)

#: The filesystem entry points startup can reach. ``pathlib`` and ``tempfile``
#: both look these up as module attributes at call time, so patching them here
#: also covers ``Path.exists``/``stat``/``mkdir``/``unlink`` and ``mkstemp``.
_GUARDED_FILESYSTEM_CALLS = (
    ("builtins", "open"),
    ("os", "listdir"),
    ("os", "lstat"),
    ("os", "makedirs"),
    ("os", "mkdir"),
    ("os", "open"),
    ("os", "remove"),
    ("os", "rename"),
    ("os", "replace"),
    ("os", "rmdir"),
    ("os", "scandir"),
    ("os", "stat"),
    ("os", "unlink"),
)


def _guarded_roots(paths):
    """``paths`` plus their symlink-resolved forms.

    ``main`` resolves the database path before deriving the state directory
    from it, and on a development Mac ``/var`` is a symlink to ``/private/var``
    - so the very path that blocks the Pi arrives here as
    ``/private/var/lib/gate-controller/...``. Matching only the declared spelling
    would make this guard a no-op on the machine the fix is written on.

    This is the one place the suite names these roots to the kernel, and it is
    safe: ``realpath`` is non-strict, so it resolves what it can and returns the
    rest unchanged rather than raising on the unprivileged build user's EACCES.
    """
    roots = []
    for path in paths:
        for spelling in (path, os.path.realpath(path)):
            # Report the declared root either way, so the failure message names
            # the path a reader will recognise from the deployment.
            if (spelling, path) not in roots:
                roots.append((spelling, path))
    return tuple(roots)


_GUARDED_ROOTS = _guarded_roots(LIVE_CONTROLLER_PATHS)


def live_controller_path(candidate):
    """Return the device root ``candidate`` falls under, or ``None``."""
    try:
        text = os.fsdecode(candidate)
    except TypeError:
        return None  # A file descriptor or a socket, not a path.
    for spelling, root in _GUARDED_ROOTS:
        if text == spelling or text.startswith(spelling + "/"):
            return root
    return None


@contextmanager
def no_live_state_access():
    """Fail loudly if the wrapped code touches the running controller's state.

    Production behaviour is deliberate and unchanged: the service owns
    ``/var/lib/gate-controller`` and is entitled to write there. This guard
    exists so a *test* that exercises the real startup wiring can never
    silently resolve the real state directory instead of a temporary one.

    The offending call raises immediately, so nothing is created or removed on
    a developer machine that happens to have the directory. The recorded list
    is re-raised on exit as well, so a swallowed ``AssertionError`` inside a
    broad ``except`` still fails the test rather than passing quietly.
    """
    touched = []

    def guarded(name, original):
        def wrapper(*args, **kwargs):
            # Every positional argument, not just the first: os.replace and
            # os.rename carry the interesting path second as often as first.
            target, root = None, None
            for argument in args:
                root = live_controller_path(argument)
                if root is not None:
                    target = argument
                    break
            if root is not None:
                touched.append(f"{name}({target!r})")
                raise AssertionError(
                    f"{name}() reached {target!r}, which belongs to the running "
                    f"gate controller under {root}. Point the test's state "
                    "directory at a temporary one - main() derives every state "
                    "path from GATE_DATABASE and GATE_AUTHORISED_PLATES, so "
                    "MainConfigurationTests.isolated_state_environment() is "
                    "enough. On the Pi this call raises PermissionError for the "
                    "unprivileged build user and blocks the release; on CI the "
                    "directory does not exist, so nothing is noticed."
                )
            return original(*args, **kwargs)

        return wrapper

    with ExitStack() as stack:
        for module, attribute in _GUARDED_FILESYSTEM_CALLS:
            namespace = builtins if module == "builtins" else os
            stack.enter_context(patch(
                f"{module}.{attribute}",
                guarded(f"{module}.{attribute}", getattr(namespace, attribute)),
            ))
        # Python 3.10 and earlier bind the os functions onto a module-level
        # pathlib accessor at import time (``_NormalAccessor.stat = os.stat``),
        # so ``Path.exists()`` never looks at ``os.stat`` again and patching it
        # above is a silent no-op. CI runs 3.10, which is exactly where the
        # release-blocking call lives, so patch the accessor too where it
        # exists. 3.11+ call ``os.stat`` directly and have no accessor.
        accessor = getattr(pathlib, "_normal_accessor", None)
        if accessor is not None:
            for _, attribute in _GUARDED_FILESYSTEM_CALLS:
                if not hasattr(accessor, attribute):
                    continue
                stack.enter_context(patch.object(
                    accessor,
                    attribute,
                    guarded(f"os.{attribute}", getattr(accessor, attribute)),
                ))
        yield touched
    if touched:
        raise AssertionError(
            "the running gate controller's state was touched during a unit "
            "test: " + ", ".join(touched)
        )


class MainConfigurationTests(unittest.TestCase):
    def setUp(self):
        """Point the status heartbeat's two device paths at a temporary tree.

        ``_controller_status`` defaults ``media_capabilities_path`` to
        ``/run/gate-media/capabilities.json`` and ``managed_releases_root`` to
        ``/opt/gate-controller-deploy/releases``, and
        ``build_background_workers`` builds its ``status`` callable without
        overriding either. Both reads swallow ``OSError``, so this never failed
        the way the match-policy marker did - on the device it quietly read the
        *running* media gateway's capability file, and resolved the *running*
        release root, into the dict the test then asserted on. A nonexistent
        temporary path gives every machine the answer CI already gets.

        Explicit arguments still win, so the tests that supply their own
        capability file or release root are untouched.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.device_paths = Path(directory.name)
        original_status = gate_main._controller_status

        def controller_status(*arguments, **keywords):
            keywords.setdefault(
                "media_capabilities_path",
                self.device_paths / "gate-media" / "capabilities.json",
            )
            keywords.setdefault(
                "managed_releases_root", self.device_paths / "releases"
            )
            return original_status(*arguments, **keywords)

        patcher = patch.object(gate_main, "_controller_status", controller_status)
        patcher.start()
        self.addCleanup(patcher.stop)

    def isolated_state_environment(self, **overrides):
        """A startup environment whose every controller path is temporary.

        ``main`` resolves the match-policy cache, the trigger-capture output
        directory and the plate snapshot from the database path
        (:func:`default_runtime_paths`, then ``Path(database).resolve().parent``),
        so redirecting ``GATE_DATABASE`` and ``GATE_AUTHORISED_PLATES`` - the
        seam production itself reads - moves the whole state tree into a
        per-test directory. Nothing about the production defaults changes.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        environment = {
            "PLATE_RECOGNIZER_API_TOKEN": "token",
            "GATE_DATABASE": str(state / "gate-controller.db"),
            "GATE_AUTHORISED_PLATES": str(state / "authorised_licence_plates.csv"),
            "GATE_WATCH_DIRECTORY": str(state / "uploads"),
        }
        environment.update(overrides)
        return environment

    def test_hot_stream_close_failure_cannot_skip_controller_safety_shutdown(self):
        calls = []

        class HotStream:
            def close(self):
                calls.append("hot_close")
                raise OSError("child unavailable")

        class Processor:
            def close(self):
                calls.append("processor_close")

        class Relay:
            def begin_shutdown(self):
                calls.append("relay_begin_shutdown")
                return True

            def shutdown(self):
                calls.append("relay_shutdown")
                return True

        with self.assertLogs("gate_controller.__main__", level="WARNING"):
            safe = gate_main._shutdown_controller_with_hot_stream(
                HotStream(), Processor(), Relay(),
            )

        self.assertTrue(safe)
        self.assertEqual(calls, [
            "hot_close", "relay_begin_shutdown", "processor_close", "relay_shutdown",
        ])

    def test_main_starts_and_selects_from_one_shared_hot_stream_buffer(self):
        hot_buffer = object()
        hot_config = type("Config", (), {"enabled": True})()
        base_worker = object()
        with patch.dict(
            os.environ, self.isolated_state_environment(), clear=True
        ), patch("sys.argv", ["gate-controller"]), patch.object(
            gate_main, "require_python_version"
        ), patch.object(
            gate_main, "PiRelayAdapter", return_value=object()
        ), patch.object(
            gate_main, "RelayController"
        ), patch.object(
            gate_main, "LocalStore"
        ), patch.object(
            gate_main, "AuthorisedPlateCache"
        ), patch.object(
            gate_main, "load_hot_stream_config", return_value=hot_config,
        ), patch.object(
            gate_main, "HotStreamBuffer", return_value=hot_buffer,
        ), patch.object(
            gate_main, "build_background_workers",
            return_value=((base_worker,), object(), object()),
        ), patch.object(
            gate_main, "PlateRecognizerClient", return_value=object()
        ), patch.object(
            gate_main, "GateProcessor", return_value=object()
        ), patch.object(
            gate_main, "run_worker"
        ) as run_worker, no_live_state_access():
            gate_main.main()

        self.assertIn(hot_buffer, run_worker.call_args.kwargs["background_workers"])
        self.assertIs(hot_buffer, run_worker.call_args.kwargs["hot_frame_provider"])

    def test_status_exposes_only_nonsecret_effective_hot_stream_health(self):
        store = self.create_store()

        class HotStream:
            def status(self):
                return {
                    "enabled": True,
                    "ready": True,
                    "stream": "fluent",
                    "sample_fps": 5.0,
                    "source_profile": {
                        "codec": "h264", "width": 640, "height": 360, "fps": 10,
                    },
                    "latest_frame_age_ms": 92,
                    "buffered_frames": 8,
                    "restart_count": 0,
                }

        status = gate_main._controller_status(
            store, type("Prompt", (), {"available": False})(), {},
            hot_stream=HotStream(),
        )

        self.assertTrue(status["recognition"]["hot_stream"]["ready"])
        self.assertEqual(640, status["recognition"]["hot_stream"]["source_profile"]["width"])
        self.assertNotIn("source_url", str(status))

    def test_reolink_trigger_pipeline_has_no_relay_or_authorisation_dependency(self):
        correlator, workers = build_reolink_trigger_pipeline({
            "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
        })

        self.assertEqual(len(workers), 1)
        self.assertTrue(callable(correlator.correlate))
        self.assertFalse(hasattr(workers[0], "relay"))
        self.assertFalse(hasattr(workers[0], "authorised"))

    def test_main_wires_webhook_worker_and_correlation_into_the_existing_worker(self):
        webhook_worker = object()
        correlator = Mock()
        base_worker = object()
        with patch.dict(
            os.environ, self.isolated_state_environment(), clear=True
        ), patch("sys.argv", ["gate-controller"]), patch.object(
            gate_main, "require_python_version"
        ), patch.object(
            gate_main, "PiRelayAdapter", return_value=object()
        ), patch.object(
            gate_main, "RelayController"
        ), patch.object(
            gate_main, "LocalStore"
        ), patch.object(
            gate_main, "AuthorisedPlateCache"
        ), patch.object(
            gate_main, "build_reolink_trigger_pipeline",
            return_value=(correlator, (webhook_worker,)),
        ), patch.object(
            gate_main, "build_background_workers",
            return_value=((base_worker,), object(), object()),
        ), patch.object(
            gate_main, "PlateRecognizerClient", return_value=object()
        ), patch.object(
            gate_main, "GateProcessor", return_value=object()
        ), patch.object(
            gate_main, "run_worker"
        ) as run_worker, no_live_state_access():
            gate_main.main()

        self.assertEqual(
            run_worker.call_args.kwargs["background_workers"],
            (base_worker, webhook_worker),
        )
        self.assertIs(
            run_worker.call_args.kwargs["trigger_resolver"], correlator.correlate,
        )

    def create_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return LocalStore(Path(directory.name) / "gate.db")

    def test_quiet_window_accepts_the_bounded_production_value(self):
        self.assertEqual(0.2, _quiet_window("0.2"))

    def test_quiet_window_rejects_nonfinite_or_unsafe_values(self):
        for value in ("-1", "0", "0.05", "2.1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(value=value), self.assertRaisesRegex(
                argparse.ArgumentTypeError, "quiet window must be between 0.1 and 2 seconds"
            ):
                _quiet_window(value)

    def test_candidate_release_defaults_to_200ms_without_a_refreshed_service_argument(self):
        with patch.dict(
            os.environ, self.isolated_state_environment(), clear=True
        ), patch("sys.argv", ["gate-controller"]), patch.object(
            gate_main, "require_python_version"
        ), patch.object(
            gate_main, "PiRelayAdapter", return_value=object()
        ), patch.object(
            gate_main, "RelayController"
        ), patch.object(
            gate_main, "LocalStore"
        ), patch.object(
            gate_main, "AuthorisedPlateCache"
        ), patch.object(
            gate_main, "build_background_workers", return_value=((), object(), object())
        ), patch.object(
            gate_main, "PlateRecognizerClient", return_value=object()
        ), patch.object(
            gate_main, "GateProcessor", return_value=object()
        ), patch.object(
            gate_main, "run_worker"
        ) as run_worker, no_live_state_access():
            gate_main.main()

        self.assertEqual(0.2, run_worker.call_args.kwargs["quiet_window"])
        self.assertIs(
            run_worker.call_args.kwargs["on_timed_skipped"],
            run_worker.call_args.kwargs["on_skipped"],
        )

    def test_telemetry_export_does_not_require_ocr_token_or_touch_the_relay(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "sys.argv",
            [
                "gate-controller", "telemetry-export", "--database", "gate.db",
                "--format", "json", "--since", "2026-08-01T00:00:00Z",
                "--output", "telemetry.json",
            ],
        ), patch.object(gate_main, "require_python_version"), patch.object(
            gate_main, "LocalStore", return_value=object()
        ) as create_store, patch.object(
            gate_main, "export_telemetry", return_value=1, create=True
        ) as export, patch.object(gate_main, "PiRelayAdapter") as relay:
            gate_main.main()

        create_store.assert_called_once_with(Path("gate.db"))
        export.assert_called_once()
        relay.assert_not_called()

    def test_main_forces_relay_safe_before_store_and_recovers_before_workers(self):
        calls = []

        class Relay:
            def begin_shutdown(self):
                calls.append("relay_begin_shutdown")
                return True

            def shutdown(self):
                calls.append("relay_shutdown")
                return True

        class Store:
            path = Path("gate.db")

            def recover_interrupted_actuations(self):
                calls.append("recover")

        class Authorised:
            def get(self):
                return ()

        class Processor:
            def close(self):
                calls.append("processor_close")

        relay = Relay()
        store = Store()
        processor = Processor()

        def create_relay(_adapter):
            calls.append("relay")
            return relay

        def create_store(_path):
            calls.append("store")
            return store

        with patch.dict(
            os.environ, self.isolated_state_environment(), clear=True
        ), patch("sys.argv", ["gate-controller"]), patch.object(
            gate_main, "require_python_version"
        ), patch.object(
            gate_main, "PiRelayAdapter", return_value=object()
        ), patch.object(
            gate_main, "RelayController", side_effect=create_relay
        ), patch.object(
            gate_main, "LocalStore", side_effect=create_store
        ), patch.object(
            gate_main, "AuthorisedPlateCache", return_value=Authorised()
        ), patch.object(
            gate_main, "build_background_workers", return_value=((), object(), object())
        ), patch.object(
            gate_main, "PlateRecognizerClient", return_value=object()
        ), patch.object(
            gate_main, "GateProcessor", return_value=processor
        ), patch.object(
            gate_main, "run_worker", side_effect=lambda *args, **kwargs: kwargs["shutdown"]()
        ), no_live_state_access():
            gate_main.main()

        self.assertLess(calls.index("relay"), calls.index("store"))
        self.assertLess(calls.index("store"), calls.index("recover"))
        self.assertLess(calls.index("relay_begin_shutdown"), calls.index("processor_close"))
        self.assertLess(calls.index("processor_close"), calls.index("relay_shutdown"))

    def test_shutdown_reaches_processor_close_when_relay_shutdown_hangs(self):
        release = Event()
        calls = []

        class Processor:
            def close(self):
                calls.append("processor_close")

        class Relay:
            def begin_shutdown(self):
                calls.append("relay_begin_shutdown")
                return True

            def shutdown(self):
                calls.append("relay_shutdown")
                release.wait(2)
                return True

        try:
            started = monotonic()
            safe = _shutdown_controller(Processor(), Relay(), relay_timeout=0.05)
            elapsed = monotonic() - started

            self.assertFalse(safe)
            self.assertLess(elapsed, 0.2)
            self.assertEqual(
                calls,
                ["relay_begin_shutdown", "processor_close", "relay_shutdown"],
            )
        finally:
            release.set()

    def test_shutdown_requests_the_relay_latch_before_processor_cleanup(self):
        calls = []

        class Processor:
            def close(self):
                calls.append("processor_close")

        class Relay:
            def begin_shutdown(self):
                calls.append("relay_begin_shutdown")

            def shutdown(self):
                calls.append("relay_shutdown")
                return True

        safe = _shutdown_controller(Processor(), Relay())

        self.assertTrue(safe)
        self.assertEqual(
            calls,
            ["relay_begin_shutdown", "processor_close", "relay_shutdown"],
        )

    def test_shutdown_waits_for_an_inflight_gpio_boundary_before_processor_cleanup(self):
        boundary_checked = Event()
        release_gpio = Event()
        processor_started = Event()
        calls = []

        class BoundaryBackend:
            def off(self):
                calls.append("off")

            def on(self, *, pre_activation_inhibit=None):
                inhibition = pre_activation_inhibit()
                boundary_checked.set()
                release_gpio.wait(1)
                if inhibition is not None:
                    return inhibition
                calls.append("on")
                return None

        class Processor:
            def close(self):
                calls.append("processor_close")
                processor_started.set()

        relay = RelayController(BoundaryBackend(), pulse_seconds=10)
        trigger = Thread(target=lambda: relay.trigger(
            "remote_command", "command:shutdown-barrier"
        ))
        trigger.start()
        self.assertTrue(boundary_checked.wait(1))

        shutdown_results = []
        shutdown = Thread(target=lambda: shutdown_results.append(
            _shutdown_controller(Processor(), relay)
        ))
        shutdown.start()

        self.assertFalse(processor_started.wait(0.05))
        release_gpio.set()
        trigger.join(1)
        shutdown.join(1)

        self.assertFalse(trigger.is_alive())
        self.assertFalse(shutdown.is_alive())
        self.assertEqual(shutdown_results, [True])
        self.assertLess(calls.index("off", 1), calls.index("processor_close"))

    def test_partial_cloudflare_configuration_fails_closed(self):
        configurations = (
            {"GATE_CLOUDFLARE_API_URL": "https://gate.example.com"},
            {
                "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "client-id",
                "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "client-secret",
            },
        )
        for environment in configurations:
            with self.subTest(environment=environment):
                store = self.create_store()

                with self.assertRaisesRegex(ValueError, "GATE_CLOUDFLARE"):
                    build_background_workers(
                        store, relay=object(), environment=environment, latest_image={}
                    )

    def test_cloudflare_configuration_builds_authorisation_status_and_outbox_workers(self):
        store = self.create_store()
        plates = store.path.parent / "plates.csv"
        plates.write_text("plate\n", encoding="utf-8")
        authorised = AuthorisedPlateCache(plates)

        workers, _, status = build_background_workers(store, relay=object(), environment={
            "GATE_CLOUDFLARE_API_URL": "https://gate.example.com",
            "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "client-id",
            "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "client-secret",
            "GATE_CONTROLLER_ID": "primary",
            "GATE_TELEMETRY_RETENTION_DAYS": "14",
        }, authorised=authorised)

        self.assertEqual(
            [type(worker).__name__ for worker in workers],
            ["OutboxWorker", "AuthorisationRefreshWorker", "HeartbeatWorker",
             "NetProbeWorker"],
        )
        self.assertEqual(workers[0]._telemetry_retention_days, 14)
        self.assertEqual(0, status()["queue_depth"])

    def test_a_metrics_ring_adds_the_rollup_worker_and_counts_heartbeats(self):
        """The producer the app's `/api/controller/metrics` never had."""
        store = self.create_store()
        ring = MetricsRing(quota=QuotaLedger(None))

        workers, _, status = build_background_workers(store, relay=object(), environment={
            "GATE_CLOUDFLARE_API_URL": "https://gate.example.com",
            "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "client-id",
            "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "client-secret",
            "GATE_METRICS_ROLLUP_SECONDS": "600",
        }, metrics=ring)

        rollup = next(w for w in workers if isinstance(w, MetricsRollupWorker))
        self.assertEqual(rollup.poll_interval, 600.0)
        heartbeat = next(w for w in workers if isinstance(w, HeartbeatWorker))
        self.assertIs(heartbeat._metrics, ring)
        # The heartbeat's cloud block carries the burn-down as well, because
        # the app's heartbeat allow-list already accepts both keys.
        cloud = status()["cloud"]
        self.assertEqual(cloud["recognition_lookups_month_to_date"], 0)
        self.assertEqual(cloud["recognition_lookup_quota"], 2500)

    def test_without_a_metrics_ring_no_rollup_worker_is_registered(self):
        workers, _, status = build_background_workers(
            self.create_store(), relay=object(), environment={
                "GATE_CLOUDFLARE_API_URL": "https://gate.example.com",
                "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "client-id",
                "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "client-secret",
            },
        )

        self.assertFalse(any(isinstance(w, MetricsRollupWorker) for w in workers))
        self.assertNotIn("recognition_lookup_quota", status()["cloud"])

    def test_telemetry_retention_days_must_be_a_positive_integer(self):
        for configured in ("0", "not-a-number"):
            with self.subTest(configured=configured):
                with self.assertRaisesRegex(ValueError, "GATE_TELEMETRY_RETENTION_DAYS"):
                    build_background_workers(
                        self.create_store(), relay=object(), environment={
                            "GATE_TELEMETRY_RETENTION_DAYS": configured,
                        }, latest_image={},
                    )

    def test_local_only_configuration_still_builds_a_telemetry_retention_worker(self):
        workers, _, _ = build_background_workers(
            self.create_store(), relay=object(), environment={
                "GATE_TELEMETRY_RETENTION_DAYS": "9",
            },
        )

        retention_workers = [
            worker for worker in workers
            if type(worker).__name__ == "TelemetryRetentionWorker"
        ]
        self.assertEqual(len(retention_workers), 1)
        self.assertEqual(retention_workers[0].retention_days, 9)

    def test_command_server_worker_uses_the_main_process_coordinator(self):
        store = self.create_store()
        coordinator = object()

        workers, _, _ = build_background_workers(
            store, relay=object(), environment={}, latest_image={},
            coordinator=coordinator,
        )

        command_worker = next(worker for worker in workers if isinstance(worker, CommandServerWorker))
        self.assertIs(command_worker.executor.coordinator, coordinator)

    def test_active_legacy_supabase_configuration_fails_closed(self):
        store = self.create_store()

        with self.assertRaisesRegex(ValueError, "legacy Supabase"):
            build_background_workers(store, relay=object(), environment={
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            })

    def test_configured_camera_upload_receiver_is_ready_before_the_first_vehicle(self):
        store = self.create_store()
        camera_directory = store.path.parent / "uploads"
        camera_directory.mkdir()

        _, _, status = build_background_workers(
            store, relay=object(), environment={}, latest_image={},
            camera_directory=camera_directory,
        )

        snapshot = status()
        self.assertTrue(snapshot["camera_configured"])
        self.assertTrue(snapshot["camera_upload_ready"])
        self.assertIsNone(snapshot["last_camera_upload_at"])
        self.assertFalse(snapshot["camera_upload_recent"])
        self.assertFalse(snapshot["camera_connection_probed"])
        self.assertIsNone(snapshot["camera_connected"])
        self.assertNotIn("camera_available", snapshot)

    def test_camera_inactivity_does_not_make_the_upload_receiver_unready(self):
        store = self.create_store()
        now = datetime.now(timezone.utc)
        latest_image = {
            "path": "/var/lib/gate-controller/uploads/latest.jpg",
            "received_at": (now - timedelta(seconds=2)).isoformat(),
        }
        camera_directory = store.path.parent / "uploads"
        camera_directory.mkdir()

        _, _, status = build_background_workers(
            store, relay=object(), environment={"GATE_CAMERA_STALE_SECONDS": "1"},
            latest_image=latest_image, camera_directory=camera_directory,
        )

        snapshot = status()
        self.assertTrue(snapshot["camera_upload_ready"])
        self.assertFalse(snapshot["camera_upload_recent"])

    def test_recent_camera_upload_is_reported_as_activity(self):
        store = self.create_store()
        latest_image = {
            "path": "/var/lib/gate-controller/uploads/latest.jpg",
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        camera_directory = store.path.parent / "uploads"
        camera_directory.mkdir()

        _, _, status = build_background_workers(
            store, relay=object(), environment={"GATE_CAMERA_STALE_SECONDS": "60"},
            latest_image=latest_image, camera_directory=camera_directory,
        )

        self.assertTrue(status()["camera_upload_recent"])

    def test_status_includes_only_measured_relay_readiness_and_outcome(self):
        class MeasuredRelay:
            def status(self):
                return {
                    "ready": True,
                    "last_outcome": "activated",
                    "last_outcome_at": "2026-08-14T10:00:00+00:00",
                }

        store = self.create_store()

        _, _, status = build_background_workers(
            store, relay=MeasuredRelay(), environment={}, latest_image={}
        )

        self.assertEqual(status()["relay"], {
            "ready": True,
            "last_outcome": "activated",
            "last_outcome_at": "2026-08-14T10:00:00+00:00",
        })

    def test_media_capabilities_are_best_effort_and_cannot_break_the_status_heartbeat(self):
        store = self.create_store()
        malformed = store.path.parent / "capabilities.json"
        malformed.write_text("not-json", encoding="utf-8")

        status = gate_main._controller_status(
            store, type("Prompt", (), {"available": False})(), {}, relay=object(),
            media_capabilities_path=malformed,
        )

        self.assertFalse(status["media"]["video"]["ready"])
        self.assertEqual("gateway_unhealthy", status["media"]["video"]["reason"])
        self.assertEqual(0, status["queue_depth"])

    def test_managed_release_sha_resolves_the_module_path_to_its_release_ancestor(self):
        release_sha = "0123456789abcdef0123456789abcdef01234567"
        root = Path(self.create_store().path).parent
        module = root / "releases" / release_sha / "gate_controller" / "__main__.py"
        module.parent.mkdir(parents=True)
        module.touch()
        current = root / "current"
        current.symlink_to(module.parent.parent, target_is_directory=True)

        self.assertEqual(
            release_sha,
            gate_main._managed_release_sha(
                current / "gate_controller" / "__main__.py",
                releases_root=root / "releases",
            ),
        )

    def test_managed_release_sha_rejects_a_canonical_sha_outside_the_releases_root(self):
        release_sha = "0123456789abcdef0123456789abcdef01234567"
        root = Path(self.create_store().path).parent
        releases_root = root / "managed" / "releases"
        releases_root.mkdir(parents=True)
        module = root / "unmanaged" / release_sha / "gate_controller" / "__main__.py"
        module.parent.mkdir(parents=True)
        module.touch()

        self.assertIsNone(
            gate_main._managed_release_sha(module, releases_root=releases_root)
        )

    def test_managed_release_sha_rejects_noncanonical_or_unmanaged_paths(self):
        root = Path(self.create_store().path).parent
        invalid_ancestors = (
            "0123456789ABCDEF0123456789ABCDEF01234567",
            "0123456789abcdef0123456789abcdef0123456",
            "0123456789abcdef0123456789abcdef012345678",
            "g123456789abcdef0123456789abcdef01234567",
            "checkout",
        )

        for ancestor in invalid_ancestors:
            with self.subTest(ancestor=ancestor):
                module = root / ancestor / "gate_controller" / "__main__.py"
                module.parent.mkdir(parents=True)
                module.touch()
                self.assertIsNone(
                    gate_main._managed_release_sha(
                        module, releases_root=root / "releases"
                    )
                )

    def test_controller_status_includes_only_a_valid_nested_software_release(self):
        store = self.create_store()
        release_sha = "fedcba9876543210fedcba9876543210fedcba98"
        module = (
            store.path.parent / "releases" / release_sha
            / "gate_controller" / "__main__.py"
        )
        module.parent.mkdir(parents=True)
        module.touch()
        prompt = type("Prompt", (), {"available": False})()

        managed = gate_main._controller_status(
            store, prompt, {}, module_path=module,
            managed_releases_root=store.path.parent / "releases",
        )
        unmanaged = gate_main._controller_status(
            store, prompt, {}, module_path=store.path.parent / "checkout" / "__main__.py",
            managed_releases_root=store.path.parent / "releases",
        )

        self.assertEqual({"release_sha": release_sha}, managed["software"])
        self.assertNotIn("software", unmanaged)

    def test_outbox_url_requires_a_nonempty_bearer_token(self):
        store = self.create_store()

        with self.assertRaisesRegex(ValueError, "GATE_OUTBOX_BEARER_TOKEN"):
            build_background_workers(store, relay=object(), environment={
                "GATE_OUTBOX_URL": "https://sync.example/events",
                "GATE_OUTBOX_BEARER_TOKEN": "   ",
            })

    def test_outbox_rejects_plain_http_for_non_loopback_hosts(self):
        store = self.create_store()

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            build_background_workers(store, relay=object(), environment={
                "GATE_OUTBOX_URL": "http://sync.example/events",
                "GATE_OUTBOX_BEARER_TOKEN": "event-secret",
            })

    def test_outbox_allows_authenticated_plain_http_on_loopback_for_local_testing(self):
        store = self.create_store()

        workers, _, _ = build_background_workers(store, relay=object(), environment={
            "GATE_OUTBOX_URL": "http://127.0.0.1:54321/events",
            "GATE_OUTBOX_BEARER_TOKEN": "event-secret",
        })

        self.assertEqual([type(worker) for worker in workers], [OutboxWorker, NetProbeWorker])

    def test_network_probe_can_be_switched_off_without_disturbing_the_workers(self):
        store = self.create_store()

        workers, _, status = build_background_workers(store, relay=object(), environment={
            "GATE_OUTBOX_URL": "http://127.0.0.1:54321/events",
            "GATE_OUTBOX_BEARER_TOKEN": "event-secret",
            "GATE_NET_PROBE_ENABLED": "false",
        })

        self.assertEqual([type(worker) for worker in workers], [OutboxWorker])
        self.assertNotIn("network", status())

    def test_runtime_path_defaults_use_the_writable_state_directory(self):
        authorised, database = default_runtime_paths({})

        self.assertEqual(
            authorised, Path("/var/lib/gate-controller/authorised_licence_plates.csv")
        )
        self.assertEqual(database, Path("/var/lib/gate-controller/gate-controller.db"))

    def test_startup_wiring_keeps_every_state_file_in_a_temporary_directory(self):
        """The whole of ``main`` runs without touching the device's own state.

        The four ``main`` tests above patch away the collaborators they are
        each about to assert on; this one leaves the state-owning ones real -
        ``MatchPolicyCache`` in particular, whose rejection marker is what
        escaped onto the Pi - so the guard is exercised against the code that
        actually resolves paths. Everything must land under the temporary
        directory, and nothing at all may reach ``/var/lib/gate-controller``.
        """
        environment = self.isolated_state_environment()
        state_directory = Path(environment["GATE_DATABASE"]).parent
        real_cache, cache_paths = gate_main.MatchPolicyCache, []

        def record_cache_path(path, **keywords):
            cache_paths.append(Path(path))
            return real_cache(path, **keywords)

        with patch.dict(
            os.environ, environment, clear=True
        ), patch("sys.argv", ["gate-controller"]), patch.object(
            gate_main, "require_python_version"
        ), patch.object(
            gate_main, "PiRelayAdapter", return_value=object()
        ), patch.object(
            gate_main, "RelayController"
        ), patch.object(
            gate_main, "LocalStore"
        ), patch.object(
            gate_main, "AuthorisedPlateCache"
        ), patch.object(
            gate_main, "MatchPolicyCache", side_effect=record_cache_path
        ), patch.object(
            gate_main, "build_background_workers", return_value=((), object(), object())
        ), patch.object(
            gate_main, "PlateRecognizerClient", return_value=object()
        ), patch.object(
            gate_main, "GateProcessor", return_value=object()
        ), patch.object(gate_main, "run_worker"), no_live_state_access() as touched:
            gate_main.main()

        self.assertEqual(touched, [])
        # ``main`` resolves the database path, so compare resolved paths: on a
        # Mac the temporary directory is reached through a /var symlink.
        self.assertEqual(
            cache_paths,
            [state_directory.resolve() / "match-policy.json"],
        )

    def test_live_state_guard_fails_loudly_on_the_real_state_directory(self):
        """The guard is only worth having if it actually fires.

        This is the exact call that blocked every release: pathlib re-raises
        EACCES from ``exists()``, so on the Pi the unprivileged build user got
        ``PermissionError`` for the settings rejection marker while CI, where
        the directory is absent, saw a quiet ``False``.
        """
        marker = Path("/var/lib/gate-controller/match-policy.json.rejected")

        with self.assertRaisesRegex(
            AssertionError, r"match-policy\.json\.rejected"
        ), no_live_state_access():
            marker.exists()

        # Both spellings of every root, the declared one and the
        # symlink-resolved one - ``main`` resolves before deriving the state
        # directory, and on a Mac that turns /var into /private/var. Read from
        # the table computed at import so this assertion does not itself stat a
        # device path.
        for spelling, root in _GUARDED_ROOTS:
            with self.subTest(path=spelling):
                self.assertEqual(root, live_controller_path(spelling))
                self.assertEqual(root, live_controller_path(f"{spelling}/child"))
        self.assertEqual(
            set(LIVE_CONTROLLER_PATHS), {root for _, root in _GUARDED_ROOTS}
        )
        self.assertIsNone(live_controller_path("/var/lib/gate-controller-other"))
        self.assertIsNone(live_controller_path(0))

    def test_example_authorisation_snapshot_uses_the_writable_state_directory(self):
        example = Path(".env.example").read_text(encoding="utf-8")

        self.assertIn(
            "GATE_AUTHORISED_PLATES=/var/lib/gate-controller/authorised_licence_plates.csv",
            example,
        )
        self.assertIn("GATE_TELEMETRY_RETENTION_DAYS=30", example)

    def test_image_runtime_limits_are_configurable(self):
        self.assertEqual(gate_main.image_runtime_limits({
            "GATE_MAX_BURST_CANDIDATES": "5",
            "GATE_MAX_CANDIDATE_IMAGE_BYTES": "1048576",
        }), (5, 1048576))

    def test_image_runtime_limits_reject_nonpositive_values(self):
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            gate_main.image_runtime_limits({"GATE_MAX_BURST_CANDIDATES": "0"})

    def test_image_runtime_limits_reject_unsafe_upper_bounds(self):
        unsafe = (
            {"GATE_MAX_BURST_CANDIDATES": "17"},
            {"GATE_MAX_CANDIDATE_IMAGE_BYTES": str(16 * 1024 * 1024 + 1)},
        )
        for environment in unsafe:
            with self.subTest(environment=environment), self.assertRaisesRegex(
                ValueError, "safe maximum"
            ):
                gate_main.image_runtime_limits(environment)

    def test_example_environment_documents_candidate_limits(self):
        example = Path(".env.example").read_text(encoding="utf-8")

        self.assertIn("GATE_MAX_BURST_CANDIDATES=8", example)
        self.assertIn("GATE_MAX_CANDIDATE_IMAGE_BYTES=8388608", example)


class AuthorisationStalenessTests(unittest.TestCase):
    def test_default_keeps_the_last_snapshot_for_fourteen_days(self):
        from gate_controller.__main__ import authorisation_max_staleness

        self.assertEqual(authorisation_max_staleness({}), timedelta(days=14))
        self.assertEqual(authorisation_max_staleness(
            {"GATE_AUTHORISATION_MAX_STALENESS_SECONDS": "  "}
        ), timedelta(days=14))

    def test_explicit_bound_is_honoured_and_zero_disables_it(self):
        from gate_controller.__main__ import authorisation_max_staleness

        self.assertEqual(authorisation_max_staleness(
            {"GATE_AUTHORISATION_MAX_STALENESS_SECONDS": "300"}
        ), timedelta(seconds=300))
        self.assertIsNone(authorisation_max_staleness(
            {"GATE_AUTHORISATION_MAX_STALENESS_SECONDS": "0"}
        ))
        self.assertIsNone(authorisation_max_staleness(
            {"GATE_AUTHORISATION_MAX_STALENESS_SECONDS": "-1"}
        ))

    def test_nonfinite_bound_is_rejected(self):
        from gate_controller.__main__ import authorisation_max_staleness

        for value in ("nan", "inf", "abc"):
            with self.assertRaises(ValueError):
                authorisation_max_staleness({"GATE_AUTHORISATION_MAX_STALENESS_SECONDS": value})


if __name__ == "__main__":
    unittest.main()


class HeartbeatObservabilityTests(unittest.TestCase):
    """Phase 1 of the observability plan: host health, network, cloud, counters."""

    def isolated_state_environment(self, **overrides):
        """The same temporary state tree MainConfigurationTests uses.

        This class drives ``main()`` too, so it needs the identical seam:
        ``main`` derives the match-policy cache and its ``.rejected`` marker
        from ``GATE_DATABASE``, and a bare ``clear=True`` environment falls
        back to ``/var/lib/gate-controller``. On the Pi the unprivileged build
        user cannot traverse that directory, ``Path.exists()`` re-raises EACCES
        rather than answering False, and the release is deferred.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        environment = {
            "PLATE_RECOGNIZER_API_TOKEN": "token",
            "GATE_DATABASE": str(state / "gate-controller.db"),
            "GATE_AUTHORISED_PLATES": str(state / "authorised_licence_plates.csv"),
            "GATE_WATCH_DIRECTORY": str(state / "uploads"),
        }
        environment.update(overrides)
        return environment

    def create_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return LocalStore(Path(directory.name) / "gate.db")

    def prompt(self):
        return type("Prompt", (), {"available": False})()

    def test_the_presence_counters_reach_the_heartbeat_instead_of_only_the_journal(self):
        """Eleven vehicles were at the gate while it stayed shut and the only
        record was the journal. The counters already existed; nothing read them."""
        class TriggerCapture:
            @staticmethod
            def status():
                return {
                    "enabled": True,
                    "captures": 42,
                    "failures": 1,
                    "presence": {
                        "unresolved": 11, "dropped_frames": 4, "lost_verdicts": 2,
                        "retries": 3, "window_seconds": 12.0,
                        "spacing_seconds": 1.0, "max_frames": 6,
                    },
                    "skipped": {"empty_scene": 7, "clipped": 0,
                                "empty_scene_threshold": 0.03,
                                "max_highlight_clipping": 0},
                }

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {}, trigger_capture=TriggerCapture(),
        )

        presence = status["recognition"]["trigger_capture"]["presence"]
        self.assertEqual(11, presence["unresolved"])
        self.assertEqual(4, presence["dropped_frames"])
        self.assertEqual(2, presence["lost_verdicts"])
        self.assertEqual(7, status["recognition"]["trigger_capture"]["skipped"]["empty_scene"])

    def test_the_corpus_backlog_reaches_the_heartbeat(self):
        """A buffer filling because uploads fail is the failure to surface.

        `pending` climbing while `last_success_at` stands still is the shape
        of a corpus that is back to being one copy on one SD card.
        """
        class Corpus:
            @staticmethod
            def status():
                return {"bytes": 11 * 1024 * 1024, "records": 94, "discarded": 40}

        class Upload:
            @staticmethod
            def status():
                return {
                    "enabled": True, "pending": 54, "oldest_pending_age_s": 7200.0,
                    "last_success_at": None, "consecutive_failures": 6,
                    "last_blocked_by": "event_delivery",
                }

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {},
            corpus=Corpus(), corpus_upload=Upload(),
            activity=gate_main.ActivityGate(quiet_seconds=60.0),
        )

        self.assertEqual(54, status["corpus"]["upload"]["pending"])
        self.assertIsNone(status["corpus"]["upload"]["last_success_at"])
        self.assertEqual(7200.0, status["corpus"]["upload"]["oldest_pending_age_s"])
        self.assertEqual(94, status["corpus"]["local"]["records"])
        self.assertEqual(60.0, status["corpus"]["backpressure"]["quiet_window_seconds"])

    def test_a_controller_without_a_corpus_omits_the_block_entirely(self):
        status = gate_main._controller_status(self.create_store(), self.prompt(), {})

        self.assertNotIn("corpus", status)

    def test_a_corpus_uploader_that_raises_cannot_stop_the_heartbeat(self):
        class Exploding:
            @staticmethod
            def status():
                raise RuntimeError("the card is gone")

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {},
            corpus=Exploding(), corpus_upload=Exploding(),
        )

        self.assertEqual({"local": {}, "upload": {}}, status["corpus"])
        self.assertEqual(0, status["queue_depth"])

    def test_a_controller_without_trigger_capture_omits_the_block_entirely(self):
        status = gate_main._controller_status(self.create_store(), self.prompt(), {})

        self.assertNotIn("trigger_capture", status["recognition"])

    def test_a_trigger_capture_that_raises_cannot_stop_the_heartbeat(self):
        class Exploding:
            @staticmethod
            def status():
                raise RuntimeError("capture worker is wedged")

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {}, trigger_capture=Exploding(),
        )

        self.assertNotIn("trigger_capture", status["recognition"])
        self.assertEqual(0, status["queue_depth"])

    def test_main_builds_trigger_capture_before_the_status_closure_needs_it(self):
        """The ordering bug: build_background_workers ran before trigger_capture
        existed, so the status closure could never see it."""
        captured = {}
        trigger_capture = Mock(name="TriggerFrameCapture")

        def record(*args, **kwargs):
            captured.update(kwargs)
            return ((), object(), object())

        environment = self.isolated_state_environment(
            GATE_REOLINK_WEBHOOK_SECRET="correct-horse-battery-staple",
            GATE_TRIGGER_CAPTURE_ENABLED="true",
        )
        with patch.dict(
            os.environ, environment, clear=True
        ), patch("sys.argv", ["gate-controller"]), patch.object(
            gate_main, "require_python_version"
        ), patch.object(
            gate_main, "PiRelayAdapter", return_value=object()
        ), patch.object(
            gate_main, "RelayController"
        ), patch.object(
            gate_main, "LocalStore"
        ), patch.object(
            gate_main, "AuthorisedPlateCache"
        ), patch.object(
            gate_main, "_clear_stream_source", return_value=None,
        ), patch.object(
            gate_main, "TriggerFrameCapture", return_value=trigger_capture,
        ), patch.object(
            gate_main, "build_background_workers", side_effect=record,
        ), patch.object(
            gate_main, "PlateRecognizerClient", return_value=object()
        ), patch.object(
            gate_main, "GateProcessor", return_value=object()
        ), patch.object(gate_main, "run_worker"), no_live_state_access():
            gate_main.main()

        self.assertIs(trigger_capture, captured["trigger_capture"])

    def test_the_heartbeat_never_carries_an_absolute_filesystem_path(self):
        now = datetime(2026, 9, 7, 10, 20, tzinfo=timezone.utc)
        latest_image = {
            "path": "/var/lib/gate-controller/uploads/DRIVEWAY_00_20260907102000.jpg",
            "received_at": (now - timedelta(seconds=12)).isoformat(),
        }

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), latest_image, clock=lambda: now,
        )

        self.assertNotIn("latest_camera_image", status)
        self.assertTrue(status["latest_camera_image_available"])
        self.assertEqual(12.0, status["latest_camera_image_age_seconds"])
        self.assertNotIn("/var/lib", str(status))
        self.assertNotIn(".jpg", str(status))

    def test_an_absent_camera_frame_reports_unavailable_rather_than_a_stale_age(self):
        status = gate_main._controller_status(self.create_store(), self.prompt(), {})

        self.assertFalse(status["latest_camera_image_available"])
        self.assertIsNone(status["latest_camera_image_age_seconds"])

    def test_host_metrics_reach_the_heartbeat_with_the_probe_s_throttle_word(self):
        class Probe:
            @staticmethod
            def throttled_flags():
                return {"raw": "0xe0006", "arm_capped": True}

            @staticmethod
            def status():
                return {"probed": True, "mode": "full", "hops": {
                    "lan": {"state": "ok", "loss": 0.0, "p95_ms": 0.63},
                    "router": {"state": "ok", "loss": 0.04, "p95_ms": 228.44},
                }}

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {}, net_probe=Probe(),
            host_metrics=lambda **kwargs: {"soc_temp_c": 82.0, **(
                {"throttled": kwargs["throttled"]} if kwargs.get("throttled") else {}
            )},
        )

        self.assertEqual(82.0, status["host"]["soc_temp_c"])
        self.assertTrue(status["host"]["throttled"]["arm_capped"])
        # The distribution reaches the app, not a mean that hides the tail.
        self.assertEqual(0.63, status["network"]["hops"]["lan"]["p95_ms"])
        self.assertEqual(228.44, status["network"]["hops"]["router"]["p95_ms"])

    def test_a_failed_host_read_still_lets_the_heartbeat_go_out_without_the_block(self):
        def explode(**_):
            raise OSError("/proc is unavailable")

        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {}, host_metrics=explode,
        )

        self.assertNotIn("host", status)
        self.assertNotIn("network", status)
        self.assertEqual(0, status["queue_depth"])

    def test_an_empty_host_read_omits_the_block_rather_than_looking_healthy(self):
        status = gate_main._controller_status(
            self.create_store(), self.prompt(), {}, host_metrics=lambda **_: {},
        )

        self.assertNotIn("host", status)

    def test_the_cloud_block_carries_the_backlog_and_the_failure_counters(self):
        class Heartbeat:
            @staticmethod
            def metrics():
                return {"heartbeat_rtt_ms": 184.2, "heartbeat_consecutive_failures": 3}

        class Plates:
            consecutive_failures = 7

        store = self.create_store()
        store.record_event_with_outbox(
            gate_main_gate_event(datetime.now(timezone.utc)), {"schema_version": 3},
        )
        # The outbox row is stamped when it is queued, so the clock advances
        # rather than the event being backdated.
        now = datetime.now(timezone.utc) + timedelta(seconds=90)

        status = gate_main._controller_status(
            store, self.prompt(), {}, heartbeat=Heartbeat(), plates=Plates(),
            clock=lambda: now,
        )

        self.assertEqual(184.2, status["cloud"]["heartbeat_rtt_ms"])
        self.assertEqual(3, status["cloud"]["heartbeat_consecutive_failures"])
        self.assertEqual(7, status["cloud"]["plates_consecutive_failures"])
        self.assertGreater(status["cloud"]["oldest_pending_outbox_age_s"], 0)

    def test_an_empty_outbox_reports_no_backlog_rather_than_zero_age(self):
        status = gate_main._controller_status(self.create_store(), self.prompt(), {})

        self.assertIsNone(status["cloud"]["oldest_pending_outbox_age_s"])
        self.assertIsNone(status["cloud"]["heartbeat_rtt_ms"])
        self.assertIsNone(status["cloud"]["plates_consecutive_failures"])

    def test_a_store_that_cannot_be_read_does_not_break_the_cloud_block(self):
        class BrokenStore:
            @staticmethod
            def pending_outbox_count():
                return 0

            @staticmethod
            def oldest_pending_outbox_age_seconds(*, now=None):
                raise RuntimeError("database is locked")

        status = gate_main._controller_status(BrokenStore(), self.prompt(), {})

        self.assertIsNone(status["cloud"]["oldest_pending_outbox_age_s"])


def gate_main_gate_event(received_at):
    from gate_controller.models import GateEvent

    return GateEvent(
        source="ocr", reason="no_match", opened=False,
        idempotency_key=None, received_at=received_at,
    )
