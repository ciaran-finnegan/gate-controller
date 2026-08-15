import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from unittest import mock
from threading import Barrier, Thread
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.models import GateEvent
from gate_controller.store import LocalStore


class LocalStoreTests(unittest.TestCase):
    def test_migrates_legacy_truthy_cooldown_values(self):
        for legacy_value in ("True", "Yes", 1):
            with self.subTest(legacy_value=legacy_value), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "gate.db"
                connection = sqlite3.connect(database)
                connection.execute(
                    "CREATE TABLE log (timestamp TEXT, gate_opened TEXT)"
                )
                connection.execute(
                    "INSERT INTO log VALUES (?, ?)",
                    ("2026-08-13T10:00:00+00:00", legacy_value),
                )
                connection.commit()
                connection.close()

                store = LocalStore(database)

                self.assertTrue(
                    store.was_opened_since(datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc))
                )

    def test_records_timed_event_and_queues_an_outbox_item(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            event = GateEvent(
                source="ocr",
                reason="exact_match",
                opened=True,
                idempotency_key="image:one",
                received_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
                relay_activated_at=datetime(2026, 8, 13, 10, 0, 1, tzinfo=timezone.utc),
            )

            event_id = store.record_event(event)
            store.queue_outbox(event_id, {"event_id": event_id})

            self.assertTrue(
                store.was_opened_since(datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc))
            )
            self.assertTrue(store.event_exists("image:one"))
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_cooldown_uses_actual_relay_activation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            store.record_event(GateEvent(
                source="ocr", reason="exact_match", opened=True, idempotency_key="one",
                received_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
                relay_activated_at=datetime(2026, 8, 13, 10, 0, 30, tzinfo=timezone.utc),
            ))

            self.assertTrue(store.was_opened_since(
                datetime(2026, 8, 13, 10, 0, 11, tzinfo=timezone.utc)
            ))

    def test_unfinalized_actuation_claim_is_retained_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            first_store = LocalStore(database)
            claim = first_store.claim_actuation(
                "upload-1", datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
            )

            retry_claim = LocalStore(database).claim_actuation(
                "upload-1", datetime(2026, 8, 13, 10, 1, tzinfo=timezone.utc)
            )

            self.assertEqual(claim.status, "claimed")
            self.assertEqual(retry_claim.status, "indeterminate_claim")

    def test_concurrent_duplicate_claims_have_one_durable_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            barrier = Barrier(2)
            results = []

            def claim_once():
                store = LocalStore(database)
                barrier.wait()
                results.append(store.claim_actuation(
                    "upload-1", datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
                ).status)

            threads = [Thread(target=claim_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertEqual(sorted(results), ["claimed", "indeterminate_claim"])

    def test_finalization_and_outbox_are_one_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
            claim = store.claim_actuation("upload-1", now)

            store.finalize_actuation(claim, GateEvent(
                source="ocr", reason="exact_match", opened=True, idempotency_key="upload-1",
                received_at=now, relay_activated_at=now,
            ), outbox_payload={"event_id": None})

            self.assertEqual(store.pending_outbox_count(), 1)

    def test_failed_outbox_insert_rolls_back_actuation_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
            claim = store.claim_actuation("upload-1", now)

            with mock.patch.object(store, "_ensure_outbox", side_effect=sqlite3.OperationalError("disk full")):
                with self.assertRaises(sqlite3.OperationalError):
                    store.finalize_actuation(claim, GateEvent(
                        source="ocr", reason="exact_match", opened=True, idempotency_key="upload-1",
                        received_at=now, relay_activated_at=now,
                    ), outbox_payload={"event_id": None})

            self.assertEqual(store.actuation_claim_status("upload-1"), "indeterminate_claim")
            self.assertEqual(store.pending_outbox_count(), 0)

    def test_failed_command_ack_insert_rolls_back_actuation_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
            claim = store.claim_actuation("command:one", now)

            with mock.patch.object(
                store, "_ensure_command_ack", side_effect=sqlite3.OperationalError("disk full")
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    store.finalize_actuation(claim, GateEvent(
                        source="remote_command", reason="remote_command", opened=True,
                        idempotency_key="command:one", received_at=now,
                        relay_activated_at=now,
                    ), command_ack=("one", now))

            self.assertEqual(store.actuation_claim_status("command:one"), "indeterminate_claim")
            self.assertEqual(store.pending_command_acks(), [])

    def test_migration_enriches_legacy_pending_outbox_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            store = LocalStore(database)
            event_id = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="legacy-event", received_at=datetime.now(timezone.utc),
            ))
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO outbox (event_id, payload, created_at) VALUES (?, ?, ?)",
                    (event_id, json.dumps({"event_id": event_id}), datetime.now(timezone.utc).isoformat()),
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE name = 'outbox_payload_v1'"
                )

            migrated = LocalStore(database)
            _, payload = migrated.pending_outbox_items()[0]

            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["reason"], "no_match")

    def test_migration_removes_legacy_mutable_evidence_paths_without_reopening_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "gate.db"
            mutable_image = root / "camera.jpg"
            mutable_image.write_bytes(b"a different vehicle now")
            store = LocalStore(database)
            event_id = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="legacy-image-event", received_at=datetime.now(timezone.utc),
            ))
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO outbox (event_id, payload, created_at) VALUES (?, ?, ?)",
                    (event_id, json.dumps({
                        "event_id": event_id,
                        "_local_image_path": str(mutable_image),
                    }), datetime.now(timezone.utc).isoformat()),
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE name = 'outbox_evidence_v2'"
                )

            migrated = LocalStore(database)
            _, payload = migrated.pending_outbox_items()[0]

            self.assertNotIn("_local_image_path", payload)
            self.assertNotIn(str(mutable_image), json.dumps(payload))
            self.assertEqual(payload["image_status"], "legacy_evidence_unavailable")
            self.assertFalse((root / "event-evidence").exists())


if __name__ == "__main__":
    unittest.main()
