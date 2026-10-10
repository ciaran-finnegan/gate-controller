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
from gate_controller.store import (
    LocalStore, _decode_pending_event, _encode_pending_event,
)
from gate_controller.telemetry import (
    EventTelemetry, FrameTelemetry, StageDurations, TriggerTelemetry,
)

#: `events` as the live Pi's database created it, before `ocr_confidence`
#: could hold "no reader saw this frame".
_LEGACY_EVENTS_DDL = """
    CREATE TABLE events (
        id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
        decision_at TEXT, relay_activated_at TEXT,
        source TEXT NOT NULL, reason TEXT NOT NULL,
        opened INTEGER NOT NULL, idempotency_key TEXT UNIQUE,
        authorised_plate TEXT, observed_plate TEXT,
        ocr_confidence REAL NOT NULL DEFAULT 0
    )
"""


def _write_legacy_events_database(path):
    """One pre-migration database holding one opened row with a real score."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(_LEGACY_EVENTS_DDL)
        connection.execute(
            """
            INSERT INTO events (
                id, received_at, relay_activated_at, source, reason, opened,
                idempotency_key, observed_plate, ocr_confidence
            ) VALUES (
                7, '2026-08-13T10:00:00+00:00', '2026-08-13T10:00:01+00:00',
                'ocr', 'exact_match', 1, 'image:legacy', '10CE1990', 0.999
            )
            """
        )
        connection.commit()


def _events_notnull(connection):
    return {row[1]: row[3] for row in connection.execute("PRAGMA table_info(events)")}


class _RaiseAfter:
    """A connection that dies once a given statement has run.

    Stands in for the power cut, or the exception, that the rebuild has to
    survive: the statement itself lands, and everything after it does not.
    """

    def __init__(self, connection, statement):
        self._connection = connection
        self._statement = statement
        self._armed = False

    def execute(self, sql, *args):
        if self._armed:
            raise RuntimeError("power cut")
        result = self._connection.execute(sql, *args)
        if self._statement in sql:
            self._armed = True
        return result

    def __enter__(self):
        return self._connection.__enter__()

    def __exit__(self, *exc_info):
        return self._connection.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._connection, name)


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
    def test_trigger_summary_survives_sqlite_and_outbox_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            event_id = store.record_event_with_outbox(
                GateEvent(
                    source="ocr", reason="no_match", opened=False,
                    idempotency_key="trigger-summary",
                    received_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
                ),
                {"controller_id": "pi-front-gate"},
            )
            trigger = TriggerTelemetry(
                source="reolink_webhook", event_type="vehicle",
                rule_id="vehicle_alert", correlation="matched", delta_ms=50,
            )
            telemetry = EventTelemetry(
                trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
                stage_durations=StageDurations(end_to_end_ms=125),
                frames=(), ocr_attempts=(), decision_outcome="denied",
                decision_reason="no_match", actuation_claim="not_requested",
                actuation_attempted=False, relay_outcome="not_attempted",
                outbox_attempt=0, delivery_state="pending", trigger=trigger,
            )

            store.attach_event_telemetry(event_id, telemetry)
            persisted = store.event_telemetry(event_id)
            _, queued = store.pending_outbox_items()[0]

            self.assertEqual(persisted["trigger"], trigger.to_wire())
            self.assertEqual(queued["telemetry"]["trigger"], trigger.to_wire())

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

    def test_adds_the_actuation_outcome_column_to_an_existing_database(self):
        """The live Pi database predates the column; opening it must add it.

        Every row already there is left NULL, which is what those rows meant:
        none of them was a granted decision that skipped the relay.
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("""
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
                        decision_at TEXT, relay_activated_at TEXT,
                        source TEXT NOT NULL, reason TEXT NOT NULL,
                        opened INTEGER NOT NULL, idempotency_key TEXT UNIQUE,
                        authorised_plate TEXT, observed_plate TEXT,
                        ocr_confidence REAL NOT NULL DEFAULT 0
                    )
                """)
                connection.execute(
                    """
                    INSERT INTO events (received_at, source, reason, opened)
                    VALUES ('2026-08-13T10:00:00+00:00', 'ocr', 'exact_match', 1)
                    """
                )
                connection.commit()

            store = LocalStore(database)

            with closing(sqlite3.connect(database)) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(events)")
                }
                existing = connection.execute(
                    "SELECT actuation_outcome FROM events"
                ).fetchall()
            still_counted = store.was_opened_since(
                datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc)
            )

        self.assertIn("actuation_outcome", columns)
        self.assertEqual(existing, [(None,)])
        self.assertTrue(still_counted, "an existing opened row still holds the window")

    def test_relaxes_the_not_null_on_ocr_confidence_in_an_existing_database(self):
        """The live Pi database created the column `NOT NULL DEFAULT 0`.

        A frame no reader saw has no score to record, and since the app's
        ingest contract took `ocr_confidence` as optional the controller writes
        that absence as NULL. SQLite cannot relax the constraint in place, so
        the table is rebuilt -- and the rebuild has to carry every existing row
        and score across untouched, and leave the indexes the cooldown lookups
        depend on standing.
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("""
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
                        decision_at TEXT, relay_activated_at TEXT,
                        source TEXT NOT NULL, reason TEXT NOT NULL,
                        opened INTEGER NOT NULL, idempotency_key TEXT UNIQUE,
                        authorised_plate TEXT, observed_plate TEXT,
                        ocr_confidence REAL NOT NULL DEFAULT 0
                    )
                """)
                connection.execute(
                    """
                    INSERT INTO events (
                        id, received_at, relay_activated_at, source, reason, opened,
                        idempotency_key, observed_plate, ocr_confidence
                    ) VALUES (
                        7, '2026-08-13T10:00:00+00:00', '2026-08-13T10:00:01+00:00',
                        'ocr', 'exact_match', 1, 'image:legacy', '10CE1990', 0.999
                    )
                    """
                )
                connection.commit()

            store = LocalStore(database)
            skipped = store.record_event(GateEvent(
                source="ocr", reason="queue_coalesced", opened=False,
                idempotency_key="image:coalesced",
                received_at=datetime(2026, 8, 13, 10, 0, 2, tzinfo=timezone.utc),
            ))

            with closing(sqlite3.connect(database)) as connection:
                notnull = {
                    row[1]: row[3]
                    for row in connection.execute("PRAGMA table_info(events)")
                }
                preserved = connection.execute(
                    "SELECT id, observed_plate, ocr_confidence FROM events WHERE id = 7"
                ).fetchone()
                recorded = connection.execute(
                    "SELECT ocr_confidence FROM events WHERE id = ?", (skipped,)
                ).fetchone()[0]
                indexes = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                        " AND tbl_name = 'events'"
                    )
                }
            still_counted = store.was_opened_since(
                datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc)
            )

        self.assertFalse(notnull["ocr_confidence"], "the column now accepts NULL")
        self.assertIn("actuation_outcome", notnull, "the rebuild kept the later column")
        self.assertEqual(preserved, (7, "10CE1990", 0.999), "every score survived")
        self.assertIsNone(recorded, "a frame no reader saw records no score")
        self.assertLessEqual(
            {"events_relay_cooldown", "events_received_cooldown"}, indexes,
        )
        self.assertTrue(still_counted, "the existing pulse still holds the window")

    def test_the_nullable_rebuild_does_not_run_twice(self):
        """Opening a database already created nullable leaves it alone."""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            LocalStore(database)
            with closing(sqlite3.connect(database)) as connection:
                before = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'events'"
                ).fetchone()[0]

            LocalStore(database)

            with closing(sqlite3.connect(database)) as connection:
                after = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'events'"
                ).fetchone()[0]
                leftovers = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%nullable%'"
                ).fetchall()

        self.assertEqual(before, after)
        self.assertNotIn('"events"', before, "a fresh database is never rebuilt")
        self.assertEqual(leftovers, [], "the scratch table is renamed, never left")

    def test_a_scratch_table_left_by_an_interrupted_rebuild_does_not_brick_the_boot(self):
        """The scratch table is the wreckage of a rebuild that did not finish.

        `sqlite3` opens a transaction for DML, not DDL, so a `CREATE TABLE`
        that is not inside an explicit one commits on its own: an exception or
        a power cut after it leaves `events_nullable_confidence` behind with
        the data still whole. `CREATE TABLE events_nullable_confidence` on the
        next boot then raises out of `LocalStore.__init__`, before `main()`
        claims the relay or starts the webhook listener, and systemd restarts
        into the same wall until a human drops the table. The gate stays shut
        the whole time.
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            _write_legacy_events_database(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE events_nullable_confidence"
                    " (id INTEGER PRIMARY KEY, junk TEXT)"
                )
                connection.commit()

            store = LocalStore(database)
            skipped = store.record_event(GateEvent(
                source="ocr", reason="queue_coalesced", opened=False,
                idempotency_key="image:coalesced",
                received_at=datetime(2026, 8, 13, 10, 0, 2, tzinfo=timezone.utc),
            ))

            with closing(sqlite3.connect(database)) as connection:
                nullable = not _events_notnull(connection)["ocr_confidence"]
                preserved = connection.execute(
                    "SELECT id, observed_plate, ocr_confidence FROM events WHERE id = 7"
                ).fetchone()
                recorded = connection.execute(
                    "SELECT ocr_confidence FROM events WHERE id = ?", (skipped,)
                ).fetchone()[0]
                leftovers = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%nullable%'"
                ).fetchall()

        self.assertTrue(nullable, "the rebuild completed on the second boot")
        self.assertEqual(preserved, (7, "10CE1990", 0.999), "every score survived")
        self.assertIsNone(recorded, "a frame no reader saw records no score")
        self.assertEqual(leftovers, [], "and the wreckage is gone")

    def test_an_abort_mid_rebuild_leaves_one_whole_events_table_and_a_bootable_database(self):
        """Whichever statement dies, the next boot has to get past `__init__`.

        The swap runs inside one `BEGIN IMMEDIATE`, so an abort rolls back to
        the old `events` -- rows, scores and indexes all standing -- or, past
        the commit, lands on the new one. Never neither, and never a scratch
        table that stops the boot after it.
        """
        aborts = (
            "CREATE TABLE events_nullable_confidence",
            "INSERT INTO events_nullable_confidence",
            "DROP TABLE events",
            "ALTER TABLE events_nullable_confidence RENAME TO events",
            "CREATE INDEX IF NOT EXISTS events_received_cooldown",
        )
        for abort_after in aborts:
            with self.subTest(abort_after=abort_after):
                with tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / "gate.db"
                    _write_legacy_events_database(database)

                    original = LocalStore._connect

                    def failing_connect(store, _original=original):
                        return _RaiseAfter(_original(store), abort_after)

                    with mock.patch.object(LocalStore, "_connect", failing_connect):
                        with self.assertRaises(RuntimeError):
                            LocalStore(database)

                    with closing(sqlite3.connect(database)) as connection:
                        surviving = connection.execute(
                            "SELECT id, observed_plate, ocr_confidence FROM events"
                        ).fetchall()
                        leftovers = connection.execute(
                            "SELECT name FROM sqlite_master WHERE name LIKE '%nullable%'"
                        ).fetchall()

                    store = LocalStore(database)
                    with closing(sqlite3.connect(database)) as connection:
                        nullable = not _events_notnull(connection)["ocr_confidence"]
                        rows = connection.execute(
                            "SELECT id, observed_plate, ocr_confidence FROM events"
                        ).fetchall()
                        indexes = {
                            row[0] for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type = 'index'"
                                " AND tbl_name = 'events'"
                            )
                        }
                    still_counted = store.was_opened_since(
                        datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc)
                    )

                self.assertEqual(
                    surviving, [(7, "10CE1990", 0.999)],
                    "an abort keeps one whole events table, old or new",
                )
                self.assertEqual(leftovers, [], "and no scratch table to trip on")
                self.assertTrue(nullable, "the next boot completes the rebuild")
                self.assertEqual(rows, [(7, "10CE1990", 0.999)], "with the row intact")
                self.assertLessEqual(
                    {"events_relay_cooldown", "events_received_cooldown"}, indexes,
                    "and both cooldown indexes standing",
                )
                self.assertTrue(still_counted, "the existing pulse still holds the window")

    def test_the_rebuild_never_turns_foreign_keys_on(self):
        """Dropping `events` out from under three REFERENCES clauses.

        `outbox`, `actuation_claims` and `event_telemetry` all name
        `events(id)`. The rebuild drops and re-adds the table they name, which
        is only safe while `PRAGMA foreign_keys` is off -- SQLite's default,
        and nothing in the store may quietly start turning it on.
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            _write_legacy_events_database(database)
            observed = []

            original = LocalStore._connect

            def watching_connect(store, _original=original):
                connection = _original(store)
                observed.append(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                )
                return connection

            with mock.patch.object(LocalStore, "_connect", watching_connect):
                store = LocalStore(database)
                store.record_event_with_outbox(
                    GateEvent(
                        source="ocr", reason="queue_coalesced", opened=False,
                        idempotency_key="image:coalesced",
                        received_at=datetime(2026, 8, 13, 10, 0, 2, tzinfo=timezone.utc),
                    ),
                    {"event_id": None},
                )

        self.assertTrue(observed, "the store opened at least one connection")
        self.assertEqual(set(observed), {0}, "foreign keys stay off on every connection")

    def test_the_rebuild_says_in_the_journal_that_it_started_and_when_it_is_skipped(self):
        """A rebuild that dies mid-flight has to have said it was running.

        The success line alone cannot distinguish a boot that skipped the
        rebuild from one that hung inside it, and the row count is the only
        hint at how long a Pi will sit there.
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            _write_legacy_events_database(database)
            with self.assertLogs("gate_controller.store", level="INFO") as migrating:
                LocalStore(database)
            with self.assertLogs("gate_controller.store", level="INFO") as reopening:
                LocalStore(database)

        started = [line for line in migrating.output if "status=started" in line]
        applied = [line for line in migrating.output if "status=applied" in line]
        skipped = [line for line in reopening.output if "status=skipped" in line]
        self.assertEqual(len(started), 1, migrating.output)
        self.assertIn("rows=1", started[0])
        self.assertEqual(len(applied), 1, migrating.output)
        self.assertEqual(len(skipped), 1, reopening.output)
        self.assertEqual(
            [line for line in reopening.output if "status=started" in line], [],
            "an already-nullable database is not rebuilt again",
        )

    def test_a_pending_event_without_a_score_omits_the_key_the_old_build_reads(self):
        """A rollback leaves this row for the previous release to decode.

        That decoder does `float(payload.get("ocr_confidence", 0.0))`. A JSON
        `null` makes it raise `TypeError`, which the blanket `except` around
        the recovery loop swallows as a malformed row -- so the interrupted
        actuation is skipped, silently, on that boot and every boot after it.
        Omitting the key hands the old decoder its 0.0 default and this one
        `None`, which is the same absent score either way.
        """
        received_at = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        unmeasured = _encode_pending_event(GateEvent(
            source="ocr", reason="queue_coalesced", opened=False,
            idempotency_key="image:coalesced", received_at=received_at,
            decision_at=received_at,
        ))
        measured = _encode_pending_event(GateEvent(
            source="ocr", reason="no_match", opened=False,
            idempotency_key="image:read", received_at=received_at,
            decision_at=received_at, observed_plate="10CE1990", ocr_confidence=0.42,
        ))

        self.assertNotIn("ocr_confidence", json.loads(unmeasured))
        # Verbatim from the release this one can be rolled back to.
        self.assertEqual(0.0, float(json.loads(unmeasured).get("ocr_confidence", 0.0)))
        self.assertIsNone(_decode_pending_event(unmeasured).ocr_confidence)
        self.assertEqual(0.42, json.loads(measured)["ocr_confidence"])
        self.assertEqual(0.42, _decode_pending_event(measured).ocr_confidence)

    def test_a_cooldown_record_is_not_evidence_that_the_relay_pulsed(self):
        """`opened` says the gate was open; only a pulse holds the window."""
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            store.record_event(GateEvent(
                source="ocr", reason="exact_match", opened=True,
                idempotency_key="coalesced",
                received_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
                authorised_plate="10CE1990", observed_plate="10CE1990",
                ocr_confidence=0.999, actuation_outcome="cooldown",
            ))

            self.assertFalse(store.was_opened_since(
                datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc)
            ))

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


class OutboxBacklogAgeTests(unittest.TestCase):
    """delivery_lag_ms p99 is pinned at the 600 s clamp; queue_depth alone
    cannot tell a draining queue from a stalled one."""

    def store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return LocalStore(Path(directory.name) / "gate.db")

    def event(self, received_at):
        return GateEvent(
            source="ocr", reason="no_match", opened=False,
            idempotency_key=None, received_at=received_at,
        )

    def test_an_empty_outbox_reports_no_backlog_rather_than_zero(self):
        self.assertIsNone(self.store().oldest_pending_outbox_age_seconds())

    def test_the_age_is_measured_from_the_oldest_undelivered_row(self):
        store = self.store()
        now = datetime.now(timezone.utc)
        store.record_event_with_outbox(self.event(now), {"schema_version": 3})
        store.record_event_with_outbox(self.event(now), {"schema_version": 3})

        age = store.oldest_pending_outbox_age_seconds(now=now + timedelta(seconds=41 * 60))

        self.assertAlmostEqual(2460.0, age, delta=5.0)

    def test_a_clock_that_runs_backwards_never_reports_a_negative_backlog(self):
        store = self.store()
        now = datetime.now(timezone.utc)
        store.record_event_with_outbox(self.event(now), {"schema_version": 3})

        self.assertEqual(0.0, store.oldest_pending_outbox_age_seconds(
            now=now - timedelta(hours=1)
        ))

    def test_a_delivered_outbox_row_no_longer_counts_as_a_backlog(self):
        store = self.store()
        now = datetime.now(timezone.utc)
        event_id = store.record_event_with_outbox(self.event(now), {"schema_version": 3})
        for outbox_id, _ in store.pending_outbox_items():
            store.complete_outbox_item(outbox_id)

        self.assertIsNone(store.oldest_pending_outbox_age_seconds())
        self.assertEqual(0, store.pending_outbox_count())
        self.assertIsInstance(event_id, int)


class RepulseHoldQueryTests(unittest.TestCase):
    """`LocalStore.repulse_hold`: the one-pulse-per-car hold's record walk.

    The coordinator asks this under the actuation lock, on the way to the
    relay (docs/invariants.md 12). The behaviour it feeds is tested through
    the whole path in tests/test_one_pulse_per_car.py and
    tests/test_gate_jam_2026_10_10.py; here are the shapes of record the walk
    has to read correctly.
    """

    T0 = datetime(2026, 10, 10, 9, 4, 13, tzinfo=timezone.utc)
    TEN = timedelta(minutes=10)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LocalStore(Path(directory.name) / "gate.db")
        self.keys = 0

    def _row(self, at_seconds, *, plate="172L66", opened=True, pulsed=True, outcome=None,
             observed=None, near_miss=None, source="ocr", reason="exact_match",
             relay_instant=True):
        self.keys += 1
        at = self.T0 + timedelta(seconds=at_seconds)
        return self.store.record_event(GateEvent(
            source=source, reason=reason, opened=opened, idempotency_key=f"row-{self.keys}",
            received_at=at, decision_at=at,
            relay_activated_at=(at if pulsed and relay_instant else None),
            authorised_plate=plate, observed_plate=observed,
            actuation_outcome=outcome, near_miss_plate=near_miss,
        ))

    def _hold(self, now_seconds, plate="172L66", unseen=TEN):
        return self.store.repulse_hold(plate, self.T0 + timedelta(seconds=now_seconds), unseen)

    def test_no_pulse_no_hold(self):
        self._row(0, pulsed=False, opened=False, reason="no_match")
        self.assertIsNone(self._hold(60))

    def test_a_pulse_holds_its_plate_for_the_window(self):
        self._row(0)
        hold = self._hold(599)
        self.assertEqual((hold.plate, hold.pulsed_at, hold.last_seen_at), ("172L66", self.T0, self.T0))
        self.assertIsNone(self._hold(601), "unseen for the window: the hold has lapsed")

    def test_sightings_chain_the_hold_forward(self):
        self._row(0)
        self._row(400, pulsed=False, outcome="cooldown")      # a cooldown grant
        self._row(900, pulsed=False, outcome="repulse_hold")  # a held grant
        hold = self._hold(1400)
        self.assertIsNotNone(hold)
        self.assertEqual(hold.pulsed_at, self.T0)
        self.assertEqual(hold.last_seen_at, self.T0 + timedelta(seconds=900))
        self.assertIsNone(self._hold(1501))

    def test_a_gap_longer_than_the_window_breaks_the_chain_for_good(self):
        self._row(0)
        self._row(700, pulsed=False, opened=False, reason="no_match", observed="172L66")
        self._row(800, pulsed=False, opened=False, reason="no_match", observed="172L66")
        self.assertIsNone(self._hold(850), "sightings after a lapse anchor nothing")

    def test_an_observed_read_and_a_near_miss_both_count_as_seen(self):
        self._row(0)
        self._row(500, plate=None, pulsed=False, opened=False, reason="no_match",
                  observed="172L66")
        self._row(1000, plate=None, pulsed=False, opened=False, reason="no_match",
                  observed="172L61", near_miss="172L66")
        self.assertIsNotNone(self._hold(1500))
        self.assertIsNone(self._hold(1601))

    def test_other_plates_neither_break_nor_extend_the_chain(self):
        self._row(0)
        for at in range(30, 1200, 30):
            self._row(at, plate="131D2696", pulsed=False, opened=False, reason="no_match",
                      observed="131D2696")
        self.assertIsNone(self._hold(700), "another car's reads kept this one's hold alive")
        self.assertIsNotNone(self._hold(400))

    def test_a_pulse_for_another_plate_is_not_this_plates_anchor(self):
        self._row(0, plate="131D2696")
        self.assertIsNone(self._hold(60))
        self.assertIsNotNone(self._hold(60, plate="131D2696"))

    def test_a_persons_pulse_has_no_plate_and_anchors_nothing(self):
        self._row(0, plate=None, source="remote_command", reason="remote_command")
        self._row(30, pulsed=False, opened=False, reason="no_match", observed="172L66")
        self.assertIsNone(self._hold(60))

    def test_a_relay_that_reports_no_instant_is_still_a_pulse(self):
        """The same clause `_was_opened_since` has: an opened row that did not
        skip the relay worked it, timestamp or no timestamp."""
        self._row(0, relay_instant=False)
        hold = self._hold(60)
        self.assertIsNotNone(hold)
        self.assertEqual(hold.pulsed_at, self.T0)

    def test_an_attempt_the_process_died_in_is_a_pulse_for_the_hold(self):
        """The relay may have fired: recovery writes `indeterminate_claim`,
        `opened=0`, and the claim keeps its `activation_attempt_at`. The cooldown
        counts that; so does the hold, or the car could be pulsed again once the
        cooldown had expired -- the very repeat the hold exists to stop."""
        at = self.T0
        event = GateEvent(
            source="local", reason="exact_match", opened=False, idempotency_key="image:died",
            received_at=at, decision_at=at, authorised_plate="172L66", observed_plate="172L66",
        )
        claim = self.store.claim_actuation("image:died", at, event=event)
        self.store.mark_actuation_attempt(
            claim, at, event=event, attempted_monotonic=100.0, boot_id="boot-1",
        )
        # The process dies here. The next start recovers the claim.
        self.assertEqual(LocalStore(self.store.path).recover_interrupted_actuations(), 1)

        hold = self._hold(400)
        self.assertIsNotNone(hold, "an attempt that may have pulsed anchors no hold")
        self.assertEqual(hold.pulsed_at, self.T0)
        self.assertIsNone(self._hold(601))

    def test_an_interruption_before_any_attempt_is_not_a_pulse(self):
        at = self.T0
        event = GateEvent(
            source="local", reason="exact_match", opened=False, idempotency_key="image:early",
            received_at=at, decision_at=at, authorised_plate="172L66", observed_plate="172L66",
        )
        self.store.claim_actuation("image:early", at, event=event)
        self.assertEqual(LocalStore(self.store.path).recover_interrupted_actuations(), 1)
        self.assertIsNone(self._hold(60), "the relay was never asked")

    def test_plates_are_compared_in_normalised_form(self):
        self._row(0, plate="172-l-66")
        self.assertIsNotNone(self._hold(60, plate="172L66"))
        self.assertIsNotNone(self._hold(60, plate=" 172 L 66 "))

    def test_a_zero_or_negative_window_or_an_empty_plate_is_no_hold(self):
        self._row(0)
        self.assertIsNone(self._hold(60, unseen=timedelta(0)))
        self.assertIsNone(self._hold(60, unseen=timedelta(seconds=-1)))
        self.assertIsNone(self._hold(60, plate=""))
        self.assertIsNone(self._hold(60, plate="---"))

    def test_the_newest_pulse_is_the_anchor(self):
        self._row(0)
        self._row(2000)  # let in again after being away
        hold = self._hold(2100)
        self.assertEqual(hold.pulsed_at, self.T0 + timedelta(seconds=2000))

    def test_the_walk_stops_at_the_first_gap_rather_than_reading_history(self):
        """Older rows are never read past a gap: the query is bounded by the chain."""
        for at in range(0, 86400, 600):
            self._row(at, plate="131D2696", pulsed=False, opened=False, reason="no_match",
                      observed="131D2696")
        real_connect = self.store._connect
        counted = []

        def counting_connect():
            connection = real_connect()
            counted.append(connection.set_trace_callback)
            return connection

        with mock.patch.object(self.store, "_connect", counting_connect):
            self.assertIsNone(self._hold(86400 + 60))
        self.assertEqual(len(counted), 1)

    def test_the_near_miss_column_is_added_to_an_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            _write_legacy_events_database(database)

            store = LocalStore(database)

            with closing(sqlite3.connect(database)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
                existing = connection.execute("SELECT near_miss_plate FROM events").fetchall()
                indexes = {row[1] for row in connection.execute("PRAGMA index_list(events)")}
            self.assertIn("near_miss_plate", columns)
            self.assertEqual(existing, [(None,)])
            self.assertIn("events_received_at", indexes)
            # And the legacy opened row -- a pulse with the plate observed -- is
            # an anchor for that plate, as it is a pulse for the cooldown.
            hold = store.repulse_hold(
                "10CE1990", datetime(2026, 8, 13, 10, 5, tzinfo=timezone.utc), self.TEN,
            )
            self.assertIsNotNone(hold, "an existing opened row still holds its plate")
            self.assertEqual(hold.pulsed_at, datetime(2026, 8, 13, 10, 0, 1, tzinfo=timezone.utc))

    def test_a_pending_event_carries_its_near_miss_across_a_restart(self):
        now = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
        event = GateEvent(
            source="ocr", reason="no_match", opened=False, idempotency_key="image:miss",
            received_at=now, decision_at=now, observed_plate="172L61", near_miss_plate="172L66",
        )
        encoded = _encode_pending_event(event)
        self.assertEqual(_decode_pending_event(encoded).near_miss_plate, "172L66")
        # Absent, not null, when there is none: an older decoder never sees the key.
        self.assertNotIn("near_miss_plate", json.loads(_encode_pending_event(
            GateEvent(source="ocr", reason="exact_match", opened=False,
                      idempotency_key="image:clean", received_at=now)
        )))
