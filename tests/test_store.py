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
from gate_controller.telemetry import EventTelemetry, FrameTelemetry, StageDurations


def _telemetry(trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2", *, reason="exact_match"):
    return EventTelemetry(
        trace_id=trace_id,
        stage_durations=StageDurations(end_to_end_ms=125),
        frames=(),
        ocr_attempts=(),
        decision_outcome="allowed" if reason == "exact_match" else "denied",
        decision_reason=reason,
        actuation_claim="claimed" if reason == "exact_match" else "not_requested",
        actuation_attempted=reason == "exact_match",
        relay_outcome="activated" if reason == "exact_match" else "not_attempted",
        outbox_attempt=0,
        delivery_state="pending",
    )


class LocalStoreTests(unittest.TestCase):
    def test_frame_quality_status_survives_persistence_and_outbox_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            event_id = store.record_event_with_outbox(
                GateEvent(
                    source="ocr", reason="no_match", opened=False,
                    idempotency_key="quality-status",
                    received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
                ),
                {"controller_id": "pi-front-gate"},
            )
            telemetry = EventTelemetry(
                trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
                stage_durations=StageDurations(end_to_end_ms=125),
                frames=(FrameTelemetry(
                    sequence=0, digest="a" * 64, width=1, height=1,
                    sharpness=0, brightness=0, darkness=0,
                    highlight_clipping=0, status="quality_unavailable",
                ),),
                ocr_attempts=(), decision_outcome="denied",
                decision_reason="no_match", actuation_claim="not_requested",
                actuation_attempted=False, relay_outcome="not_attempted",
                outbox_attempt=0, delivery_state="pending",
            )

            store.attach_event_telemetry(event_id, telemetry)
            item_id, queued = store.pending_outbox_items()[0]
            attempted = store.prepare_outbox_attempt(
                item_id, datetime(2026, 8, 15, 10, 0, 1, tzinfo=timezone.utc)
            )

            self.assertEqual(
                store.event_telemetry(event_id)["frames"][0]["status"],
                "quality_unavailable",
            )
            self.assertEqual(
                queued["telemetry"]["frames"][0]["status"],
                "quality_unavailable",
            )
            self.assertEqual(
                attempted["telemetry"]["frames"][0]["status"],
                "quality_unavailable",
            )

    def test_telemetry_pages_use_received_at_and_event_id_as_a_stable_keyset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            received_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
            event_ids = []
            for index in range(5):
                event_id = store.record_event(GateEvent(
                    source="ocr", reason="no_match", opened=False,
                    idempotency_key=f"page-{index}", received_at=received_at,
                ))
                store.attach_event_telemetry(
                    event_id,
                    _telemetry(
                        f"00000000-0000-4000-8000-00000000000{index}",
                        reason="no_match",
                    ),
                )
                event_ids.append(event_id)

            first = store.event_telemetry_page(received_at, limit=2)
            second = store.event_telemetry_page(
                received_at,
                after=(first[-1]["received_at"], first[-1]["event_id"]),
                limit=2,
            )
            third = store.event_telemetry_page(
                received_at,
                after=(second[-1]["received_at"], second[-1]["event_id"]),
                limit=2,
            )

            self.assertEqual(
                [row["event_id"] for row in first + second + third], event_ids
            )
            self.assertEqual([len(first), len(second), len(third)], [2, 2, 1])

    def test_migration_adds_the_bounded_event_telemetry_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")

            with closing(sqlite3.connect(store.path)) as connection:
                columns = connection.execute("PRAGMA table_info(event_telemetry)").fetchall()
                indexes = connection.execute("PRAGMA index_list(event_telemetry)").fetchall()

            self.assertEqual(
                [(row[1], row[2], row[5]) for row in columns],
                [
                    ("event_id", "INTEGER", 1),
                    ("trace_id", "TEXT", 0),
                    ("payload", "TEXT", 0),
                    ("created_at", "TEXT", 0),
                ],
            )
            self.assertTrue(any(row[2] for row in indexes), indexes)

    def test_attach_is_idempotent_and_promotes_only_the_pending_outbox_to_v3(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            event_id = store.record_event_with_outbox(
                GateEvent(
                    source="ocr", reason="exact_match", opened=True,
                    idempotency_key="telemetry-one",
                    received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
                ),
                {"event_id": None, "controller_id": "pi-front-gate"},
            )
            telemetry = _telemetry()

            self.assertTrue(store.attach_event_telemetry(event_id, telemetry))
            self.assertFalse(store.attach_event_telemetry(event_id, telemetry))

            saved = store.event_telemetry(event_id)
            _, queued = store.pending_outbox_items()[0]
            self.assertEqual(saved["trace_id"], telemetry.trace_id)
            self.assertNotIn("schema_version", saved)
            self.assertEqual(queued["schema_version"], 3)
            self.assertEqual(queued["telemetry"], saved)
            self.assertEqual(queued["controller_id"], "pi-front-gate")

    def test_attach_rejects_event_and_trace_identity_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            first = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="telemetry-first", received_at=datetime.now(timezone.utc),
            ))
            second = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="telemetry-second", received_at=datetime.now(timezone.utc),
            ))
            store.attach_event_telemetry(first, _telemetry(reason="no_match"))

            with self.assertRaises(ValueError):
                store.attach_event_telemetry(
                    first,
                    _telemetry("b92dcb71-dd3c-4a82-b522-093f75746295", reason="no_match"),
                )
            with self.assertRaises(ValueError):
                store.attach_event_telemetry(second, _telemetry(reason="no_match"))

    def test_attach_after_v2_completion_preserves_the_acknowledged_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            queued_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
            sent_at = queued_at + timedelta(seconds=1)
            acknowledged_at = sent_at + timedelta(milliseconds=125)
            event_id = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="already-sent", received_at=queued_at,
            ))
            item_id = store.queue_outbox(event_id, {
                "controller_id": "pi-front-gate",
                "image_sha256": "a" * 64,
            })
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE outbox SET created_at = ? WHERE id = ?",
                    (queued_at.isoformat(), item_id),
                )
            prepared = store.prepare_outbox_attempt(item_id, sent_at)
            store.complete_outbox_item(
                item_id, acknowledged_at, prepared_payload=prepared
            )
            with closing(sqlite3.connect(store.path)) as connection:
                completed_payload, completed_at = connection.execute(
                    "SELECT payload, completed_at FROM outbox WHERE id = ?", (item_id,)
                ).fetchone()
            self.assertEqual(json.loads(completed_payload), prepared)
            self.assertEqual(completed_at, acknowledged_at.isoformat())

            self.assertTrue(store.attach_event_telemetry(
                event_id, _telemetry(reason="no_match")
            ))

            with closing(sqlite3.connect(store.path)) as connection:
                promoted_payload, promoted_completed_at = connection.execute(
                    "SELECT payload, completed_at FROM outbox WHERE id = ?", (item_id,)
                ).fetchone()
            saved = store.event_telemetry(event_id)
            self.assertEqual(promoted_completed_at, acknowledged_at.isoformat())
            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(store.pending_outbox_items(), [])
            self.assertEqual(json.loads(promoted_payload), prepared)
            self.assertEqual(saved["delivery"], {
                "outbox_attempt": 0,
                "state": "pending",
            })
            self.assertEqual(saved["stage_timestamps"], {
                "cloud_enqueued_at": queued_at.isoformat(),
            })

    def test_interrupted_telemetry_wait_is_released_for_v2_startup_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            event_id = store.record_event_with_outbox(
                GateEvent(
                    source="ocr", reason="no_match", opened=False,
                    idempotency_key="interrupted-telemetry",
                    received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
                ),
                {
                    "controller_id": "pi-front-gate",
                    "_awaiting_telemetry": True,
                },
            )

            self.assertEqual(store.pending_outbox_count(), 1)
            self.assertEqual(store.pending_outbox_items(), [])

            self.assertEqual(store.recover_interrupted_actuations(), 0)

            queued = store.pending_outbox_items()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][1]["event_id"], event_id)
            self.assertEqual(queued[0][1]["schema_version"], 2)
            self.assertNotIn("_awaiting_telemetry", queued[0][1])

    def test_migration_quarantines_rejected_v3_follow_up_without_losing_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.db"
            store = LocalStore(path)
            event_id = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="legacy-invalid-follow-up",
                received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
            ))
            item_id = store.queue_outbox(event_id, {
                "controller_id": "pi-front-gate",
                "image_sha256": "a" * 64,
            })
            prepared = store.prepare_outbox_attempt(item_id)
            store.complete_outbox_item(item_id, prepared_payload=prepared)
            store.attach_event_telemetry(event_id, _telemetry(reason="no_match"))
            telemetry = store.event_telemetry(event_id)
            invalid_follow_up = dict(prepared)
            invalid_follow_up.pop("image_sha256")
            invalid_follow_up.update({
                "image_status": "delivered_before_telemetry",
                "schema_version": 3,
                "telemetry": telemetry,
            })
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    UPDATE outbox SET payload = ?, completed_at = NULL,
                        send_state = 'ready'
                    WHERE id = ?
                    """,
                    (json.dumps(invalid_follow_up, sort_keys=True), item_id),
                )
                connection.execute(
                    """
                    DELETE FROM schema_migrations
                    WHERE name IN (
                        'outbox_incompatible_followup_v1',
                        'outbox_incompatible_followup_v2',
                        'outbox_incompatible_followup_v3'
                    )
                    """
                )

            migrated = LocalStore(path)

            self.assertEqual(migrated.pending_outbox_count(), 0)
            self.assertEqual(migrated.pending_outbox_items(), [])
            self.assertEqual(
                migrated.event_telemetry(event_id)["trace_id"], telemetry["trace_id"]
            )
            with closing(sqlite3.connect(path)) as connection:
                payload_text, completed_at, send_state = connection.execute(
                    "SELECT payload, completed_at, send_state FROM outbox WHERE id = ?",
                    (item_id,),
                ).fetchone()
            self.assertEqual(json.loads(payload_text), invalid_follow_up)
            self.assertIsNone(completed_at)
            self.assertEqual(send_state, "local_only")

    def test_migration_quarantines_image_free_legacy_v3_follow_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.db"
            store = LocalStore(path)
            event_id = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="legacy-image-free-follow-up",
                received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
            ))
            item_id = store.queue_outbox(
                event_id, {"controller_id": "pi-front-gate"}
            )
            prepared = store.prepare_outbox_attempt(item_id)
            store.complete_outbox_item(item_id, prepared_payload=prepared)
            store.attach_event_telemetry(event_id, _telemetry(reason="no_match"))
            telemetry = store.event_telemetry(event_id)
            legacy_follow_up = {
                **prepared,
                "schema_version": 3,
                "telemetry": telemetry,
            }
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    UPDATE outbox SET payload = ?, completed_at = NULL,
                        send_state = 'ready'
                    WHERE id = ?
                    """,
                    (json.dumps(legacy_follow_up, sort_keys=True), item_id),
                )
                connection.execute(
                    """
                    DELETE FROM schema_migrations
                    WHERE name IN (
                        'outbox_incompatible_followup_v1',
                        'outbox_incompatible_followup_v2',
                        'outbox_incompatible_followup_v3'
                    )
                    """
                )

            migrated = LocalStore(path)

            self.assertEqual(migrated.pending_outbox_count(), 0)
            self.assertEqual(migrated.pending_outbox_items(), [])
            self.assertEqual(
                migrated.event_telemetry(event_id)["trace_id"], telemetry["trace_id"]
            )
            with closing(sqlite3.connect(path)) as connection:
                payload_text, completed_at, send_state = connection.execute(
                    "SELECT payload, completed_at, send_state FROM outbox WHERE id = ?",
                    (item_id,),
                ).fetchone()
            self.assertEqual(json.loads(payload_text), legacy_follow_up)
            self.assertIsNone(completed_at)
            self.assertEqual(send_state, "local_only")
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations (name)
                    VALUES ('outbox_incompatible_followup_v2')
                    """
                )
                connection.execute(
                    """
                    DELETE FROM schema_migrations
                    WHERE name = 'outbox_incompatible_followup_v3'
                    """
                )

            remigrated = LocalStore(path)

            self.assertEqual(remigrated.pending_outbox_count(), 0)
            self.assertEqual(remigrated.pending_outbox_items(), [])
            with closing(sqlite3.connect(path)) as connection:
                remigrated_payload, remigrated_state = connection.execute(
                    "SELECT payload, send_state FROM outbox WHERE id = ?",
                    (item_id,),
                ).fetchone()
            self.assertEqual(json.loads(remigrated_payload), legacy_follow_up)
            self.assertEqual(remigrated_state, "local_only")

    def test_migration_keeps_pre_provenance_prepared_image_free_v3_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.db"
            store = LocalStore(path)
            event_id = store.record_event(GateEvent(
                source="ocr", reason="no_match", opened=False,
                idempotency_key="pre-provenance-prepared-image-free-v3",
                received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
            ))
            store.attach_event_telemetry(
                event_id, _telemetry(reason="no_match")
            )
            item_id = store.queue_outbox(
                event_id, {"controller_id": "pi-front-gate"}
            )
            prepared = store.prepare_outbox_attempt(
                item_id,
                datetime(2026, 8, 15, 10, 0, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(prepared["schema_version"], 3)
            self.assertEqual(
                prepared["telemetry"]["delivery"],
                {"outbox_attempt": 1, "state": "sending"},
            )
            self.assertNotIn("image_sha256", prepared)
            self.assertNotIn("image_status", prepared)
            with closing(sqlite3.connect(path)) as connection, connection:
                payload_text, completed_at, send_state = connection.execute(
                    "SELECT payload, completed_at, send_state FROM outbox WHERE id = ?",
                    (item_id,),
                ).fetchone()
                self.assertEqual(json.loads(payload_text), prepared)
                self.assertIsNone(completed_at)
                self.assertEqual(send_state, "ready")
                connection.execute(
                    "UPDATE outbox SET send_state = 'local_only' WHERE id = ?",
                    (item_id,),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations (name)
                    VALUES ('outbox_incompatible_followup_v2')
                    """
                )
                connection.execute(
                    """
                    DELETE FROM schema_migrations
                    WHERE name = 'outbox_incompatible_followup_v3'
                    """
                )

            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(store.pending_outbox_items(), [])

            migrated = LocalStore(path)

            queued = migrated.pending_outbox_items()
            self.assertEqual(migrated.pending_outbox_count(), 1)
            self.assertEqual(queued, [(item_id, prepared)])
            self.assertEqual(migrated.event_telemetry(event_id), prepared["telemetry"])
            with closing(sqlite3.connect(path)) as connection:
                payload_text, completed_at, send_state = connection.execute(
                    "SELECT payload, completed_at, send_state FROM outbox WHERE id = ?",
                    (item_id,),
                ).fetchone()
            self.assertEqual(json.loads(payload_text), prepared)
            self.assertIsNone(completed_at)
            self.assertEqual(send_state, "ready")

    def test_migration_keeps_current_image_free_v3_event_sendable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.db"
            store = LocalStore(path)
            event_id = store.record_event_with_outbox(
                GateEvent(
                    source="ocr", reason="queue_coalesced", opened=False,
                    idempotency_key="current-image-free-v3",
                    received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
                ),
                {
                    "controller_id": "pi-front-gate",
                    "_awaiting_telemetry": True,
                },
            )
            store.attach_event_telemetry(
                event_id, _telemetry(reason="queue_coalesced")
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    DELETE FROM schema_migrations
                    WHERE name IN (
                        'outbox_incompatible_followup_v1',
                        'outbox_incompatible_followup_v2',
                        'outbox_incompatible_followup_v3'
                    )
                    """
                )

            migrated = LocalStore(path)

            queued = migrated.pending_outbox_items()
            self.assertEqual(migrated.pending_outbox_count(), 1)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][1]["schema_version"], 3)
            self.assertNotIn("image_sha256", queued[0][1])
            self.assertNotIn("image_status", queued[0][1])

    def test_migration_keeps_current_image_bearing_v3_event_sendable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.db"
            store = LocalStore(path)
            event_id = store.record_event_with_outbox(
                GateEvent(
                    source="ocr", reason="no_match", opened=False,
                    idempotency_key="current-image-bearing-v3",
                    received_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
                ),
                {
                    "controller_id": "pi-front-gate",
                    "image_sha256": "b" * 64,
                    "_awaiting_telemetry": True,
                },
            )
            store.attach_event_telemetry(event_id, _telemetry(reason="no_match"))
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE outbox SET send_state = 'ready' WHERE event_id = ?",
                    (event_id,),
                )
                connection.execute(
                    """
                    DELETE FROM schema_migrations
                    WHERE name IN (
                        'outbox_incompatible_followup_v1',
                        'outbox_incompatible_followup_v2',
                        'outbox_incompatible_followup_v3'
                    )
                    """
                )

            migrated = LocalStore(path)

            queued = migrated.pending_outbox_items()
            self.assertEqual(migrated.pending_outbox_count(), 1)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][1]["schema_version"], 3)
            self.assertEqual(queued[0][1]["image_sha256"], "b" * 64)

    def test_retention_removes_only_old_telemetry_with_completed_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            event_ids = []
            for index in range(3):
                event_id = store.record_event(GateEvent(
                    source="ocr", reason="no_match", opened=False,
                    idempotency_key=f"retention-{index}", received_at=now,
                ))
                item_id = store.queue_outbox(event_id, {
                    "controller_id": "pi-front-gate",
                    "image_sha256": f"{index + 1}" * 64,
                })
                store.attach_event_telemetry(
                    event_id,
                    _telemetry(
                        f"00000000-0000-4000-8000-00000000000{index}", reason="no_match"
                    ),
                )
                if index != 1:
                    store.complete_outbox_item(item_id, now)
                event_ids.append(event_id)
            with closing(sqlite3.connect(store.path)) as connection:
                before = {
                    event_id: json.loads(payload)
                    for event_id, payload in connection.execute(
                        "SELECT event_id, payload FROM outbox"
                    ).fetchall()
                }
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE event_telemetry SET created_at = ? WHERE event_id IN (?, ?)",
                    (
                        (now - timedelta(days=31)).isoformat(),
                        event_ids[0], event_ids[1],
                    ),
                )

            removed = store.purge_delivered_telemetry(now - timedelta(days=30))

            self.assertEqual(removed, 1)
            self.assertIsNone(store.event_telemetry(event_ids[0]))
            self.assertIsNotNone(store.event_telemetry(event_ids[1]))
            self.assertIsNotNone(store.event_telemetry(event_ids[2]))
            with closing(sqlite3.connect(store.path)) as connection:
                after = {
                    event_id: json.loads(payload)
                    for event_id, payload in connection.execute(
                        "SELECT event_id, payload FROM outbox"
                    ).fetchall()
                }
            stripped = dict(before[event_ids[0]])
            stripped.pop("telemetry")
            stripped["schema_version"] = 2
            self.assertEqual(after[event_ids[0]], stripped)
            self.assertEqual(after[event_ids[1]], before[event_ids[1]])
            self.assertEqual(after[event_ids[2]], before[event_ids[2]])

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
