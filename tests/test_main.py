import argparse
import unittest
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import gate_controller.__main__ as gate_main
from gate_controller.__main__ import (
    _quiet_window, build_background_workers, default_runtime_paths,
)
from gate_controller.authorisation import AuthorisationRefreshWorker, AuthorisedPlateCache
from gate_controller.control_plane import HeartbeatWorker
from gate_controller.command_server import CommandServerWorker
from gate_controller.outbox import OutboxWorker
from gate_controller.store import LocalStore


class MainConfigurationTests(unittest.TestCase):
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
            def shutdown(self):
                calls.append("relay_shutdown")

        class Store:
            path = Path("gate.db")

            def recover_interrupted_actuations(self):
                calls.append("recover")

        class Authorised:
            def get(self):
                return ()

        relay = Relay()
        store = Store()

        def create_relay(_adapter):
            calls.append("relay")
            return relay

        def create_store(_path):
            calls.append("store")
            return store

        with patch.dict(
            os.environ, {"PLATE_RECOGNIZER_API_TOKEN": "token"}, clear=True
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
            gate_main, "GateProcessor", return_value=object()
        ), patch.object(gate_main, "run_worker"):
            gate_main.main()

        self.assertLess(calls.index("relay"), calls.index("store"))
        self.assertLess(calls.index("store"), calls.index("recover"))

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
        }, authorised=authorised)

        self.assertEqual(
            [type(worker).__name__ for worker in workers],
            ["OutboxWorker", "AuthorisationRefreshWorker", "HeartbeatWorker"],
        )
        self.assertEqual(0, status()["queue_depth"])

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

        self.assertEqual([type(worker) for worker in workers], [OutboxWorker])

    def test_runtime_path_defaults_use_the_writable_state_directory(self):
        authorised, database = default_runtime_paths({})

        self.assertEqual(
            authorised, Path("/var/lib/gate-controller/authorised_licence_plates.csv")
        )
        self.assertEqual(database, Path("/var/lib/gate-controller/gate-controller.db"))

    def test_example_authorisation_snapshot_uses_the_writable_state_directory(self):
        example = Path(".env.example").read_text(encoding="utf-8")

        self.assertIn(
            "GATE_AUTHORISED_PLATES=/var/lib/gate-controller/authorised_licence_plates.csv",
            example,
        )

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


if __name__ == "__main__":
    unittest.main()
