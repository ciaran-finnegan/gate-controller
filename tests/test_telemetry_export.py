import csv
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from gate_controller.models import GateEvent
from gate_controller.store import LocalStore
from gate_controller.telemetry import EventTelemetry, StageDurations
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
            frames=(), ocr_attempts=(),
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
