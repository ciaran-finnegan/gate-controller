import csv
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from gate_controller.models import GateEvent
from gate_controller.store import LocalStore
import gate_controller.telemetry_export as telemetry_export_module
from gate_controller.telemetry import EventTelemetry, FrameTelemetry, StageDurations
from gate_controller.telemetry_export import export_telemetry


class TelemetryExportTests(unittest.TestCase):
    def _store_with_telemetry(self, root: Path):
        store = LocalStore(root / "gate.db")
        event_id = store.record_event(GateEvent(
            source="ocr", reason="no_match", opened=False,
            idempotency_key="export-one",
            received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
        ))
        store.attach_event_telemetry(event_id, EventTelemetry(
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
            stage_durations=StageDurations(end_to_end_ms=125),
            frames=(FrameTelemetry(
                sequence=0, digest="a" * 64, width=1, height=1,
                sharpness=0, brightness=0, darkness=0,
                highlight_clipping=0, status="quality_unavailable",
            ),), ocr_attempts=(),
            decision_outcome="denied", decision_reason="no_match",
            actuation_claim="not_requested", actuation_attempted=False,
            relay_outcome="not_attempted", outbox_attempt=0,
            delivery_state="pending",
        ))
        return store, event_id

    def test_json_export_is_bounded_by_since_and_redacts_unsafe_stored_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, event_id = self._store_with_telemetry(root)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                payload = store.event_telemetry(event_id)
                payload.update({
                    "api_token": "do-not-export",
                    "image_path": "/private/camera.jpg",
                    "image": {"data_base64": "do-not-export"},
                })
                connection.execute(
                    "UPDATE event_telemetry SET payload = ? WHERE event_id = ?",
                    (json.dumps(payload), event_id),
                )
            output = root / "telemetry.json"

            count = export_telemetry(
                store,
                format="json",
                since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                output=output,
            )

            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            self.assertEqual(exported[0]["event_id"], event_id)
            self.assertEqual(exported[0]["telemetry"]["trace_id"], payload["trace_id"])
            self.assertEqual(
                exported[0]["telemetry"]["frames"][0]["status"],
                "quality_unavailable",
            )
            encoded = json.dumps(exported)
            for forbidden in ("do-not-export", "image_path", "data_base64", "api_token"):
                self.assertNotIn(forbidden, encoded)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

            later = root / "later.json"
            self.assertEqual(export_telemetry(
                store,
                format="json",
                since=datetime.now(timezone.utc) + timedelta(days=1),
                output=later,
            ), 0)
            self.assertEqual(json.loads(later.read_text(encoding="utf-8")), [])

    def test_csv_export_flattens_only_the_safe_diagnostic_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, event_id = self._store_with_telemetry(root)
            output = root / "telemetry.csv"

            self.assertEqual(export_telemetry(
                store, format="csv",
                since=datetime(2026, 8, 1, tzinfo=timezone.utc), output=output,
            ), 1)

            with output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["event_id"], str(event_id))
            self.assertEqual(rows[0]["trace_id"], "ae2398aa-7107-44f4-a723-290de0f8c7b2")
            self.assertEqual(rows[0]["end_to_end_ms"], "125")
            headings = set(rows[0])
            self.assertFalse(any(
                forbidden in heading
                for heading in headings
                for forbidden in ("image", "path", "token", "secret", "credential")
            ))

    def test_atomic_failure_preserves_existing_output_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _ = self._store_with_telemetry(root)
            output = root / "telemetry.json"
            output.write_text("previous", encoding="utf-8")

            with mock.patch(
                "gate_controller.telemetry_export.os.replace",
                side_effect=OSError("replace failed"),
            ), self.assertRaises(OSError):
                export_telemetry(
                    store, format="json",
                    since=datetime(2026, 8, 1, tzinfo=timezone.utc), output=output,
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(root.glob(".telemetry-*.tmp")), [])

    def test_export_rejects_database_sidecars_and_same_file_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, event_id = self._store_with_telemetry(root)
            symlink = root / "database-symlink"
            symlink.symlink_to(store.path)
            hardlink = root / "database-hardlink"
            os.link(store.path, hardlink)
            alias_directory = root / "database-parent-alias"
            alias_directory.symlink_to(root, target_is_directory=True)
            outputs = (
                store.path,
                Path(f"{store.path}-wal"),
                Path(f"{store.path}-shm"),
                Path(f"{store.path}-journal"),
                symlink,
                hardlink,
                alias_directory / store.path.name,
            )

            for output in outputs:
                with self.subTest(output=output), mock.patch(
                    "gate_controller.telemetry_export.os.replace",
                    side_effect=AssertionError("unsafe database replacement reached"),
                ), self.assertRaisesRegex(ValueError, "database"):
                    export_telemetry(
                        store, format="json",
                        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                        output=output,
                    )

            self.assertIsNotNone(store.event_telemetry(event_id))

    def test_output_is_rechecked_for_database_aliases_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _ = self._store_with_telemetry(root)
            output = root / "telemetry.json"
            original_write = telemetry_export_module._write

            def retarget_output(destination, format, rows):
                result = original_write(destination, format, rows)
                output.symlink_to(store.path)
                return result

            with mock.patch.object(
                telemetry_export_module, "_write", side_effect=retarget_output
            ), mock.patch(
                "gate_controller.telemetry_export.os.replace",
                side_effect=AssertionError("unsafe database replacement reached"),
            ), self.assertRaisesRegex(ValueError, "database"):
                export_telemetry(
                    store, format="json",
                    since=datetime(2026, 8, 1, tzinfo=timezone.utc), output=output,
                )

    def test_concurrent_snapshot_export_does_not_delay_an_actuation_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _ = self._store_with_telemetry(root)
            output = root / "telemetry.json"
            export_paused = threading.Event()
            release_export = threading.Event()
            errors = []
            original_safe_row = telemetry_export_module._safe_row

            def pause_snapshot_row(row):
                export_paused.set()
                release_export.wait(timeout=2)
                return original_safe_row(row)

            def run_export():
                try:
                    export_telemetry(
                        store, format="json",
                        since=datetime(2026, 8, 1, tzinfo=timezone.utc), output=output,
                    )
                except Exception as error:
                    errors.append(error)

            with mock.patch.object(
                store, "event_telemetry_rows",
                side_effect=AssertionError("export read the live database"),
            ), mock.patch.object(
                store, "event_telemetry_page",
                side_effect=AssertionError("export paged the live database"),
            ), mock.patch.object(
                telemetry_export_module, "_safe_row", side_effect=pause_snapshot_row
            ):
                export_thread = threading.Thread(target=run_export)
                export_thread.start()
                self.assertTrue(export_paused.wait(timeout=1))
                release_timer = threading.Timer(0.5, release_export.set)
                release_timer.start()
                started = time.monotonic()
                claim = LocalStore(store.path).claim_actuation(
                    "claim-during-export", datetime.now(timezone.utc)
                )
                elapsed = time.monotonic() - started
                release_export.set()
                release_timer.cancel()
                export_thread.join(timeout=2)

            self.assertEqual(errors, [])
            self.assertFalse(export_thread.is_alive())
            self.assertEqual(claim.status, "claimed")
            self.assertLess(elapsed, 0.25)

    def test_snapshot_busy_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _ = self._store_with_telemetry(root)
            blocker = sqlite3.connect(store.path)
            blocker.execute("BEGIN EXCLUSIVE")

            def short_live_connection():
                return sqlite3.connect(store.path, timeout=0.05)

            started = time.monotonic()
            try:
                with mock.patch.object(
                    store, "_connect", side_effect=short_live_connection
                ), self.assertRaisesRegex(TimeoutError, "snapshot"):
                    export_telemetry(
                        store, format="json",
                        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                        output=root / "telemetry.json",
                    )
            finally:
                blocker.rollback()
                blocker.close()

            self.assertLess(time.monotonic() - started, 1.0)

    def test_export_requires_an_explicit_output_and_supported_format(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _ = self._store_with_telemetry(Path(directory))

            with self.assertRaises(ValueError):
                export_telemetry(
                    store, format="json",
                    since=datetime(2026, 8, 1, tzinfo=timezone.utc), output=None,
                )
            with self.assertRaises(ValueError):
                export_telemetry(
                    store, format="xml",
                    since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    output=Path(directory) / "telemetry.xml",
                )


if __name__ == "__main__":
    unittest.main()
