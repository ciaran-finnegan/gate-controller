import base64
from contextlib import closing
import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

import gate_controller.outbox as outbox_module
from gate_controller.models import GateEvent
from gate_controller.outbox import EvidenceSpoolError, HttpOutboxSender, OutboxWorker
from gate_controller.store import LocalStore
from gate_controller.telemetry import EventTelemetry, StageDurations


def _telemetry():
    return EventTelemetry(
        trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
        stage_durations=StageDurations(end_to_end_ms=125),
        frames=(), ocr_attempts=(),
        decision_outcome="allowed", decision_reason="exact_match",
        actuation_claim="claimed", actuation_attempted=True,
        relay_outcome="activated", outbox_attempt=0, delivery_state="pending",
    )


class EvidenceSpoolTests(unittest.TestCase):
    def test_rejects_non_jpeg_magic_without_invoking_pillow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disguised = root / "disguised.jpg"
            disguised.write_bytes(b"8BPS\x00\x01untrusted image payload")
            spool = outbox_module.EvidenceSpool(root / "event-evidence")

            with mock.patch("gate_controller.outbox.Image.open") as open_image:
                with self.assertRaises(EvidenceSpoolError):
                    spool.stage(disguised)

            open_image.assert_not_called()

    def test_atomic_evidence_replace_fsyncs_the_containing_directory(self):
        real_fsync = outbox_module.os.fsync
        fsynced_directory = []

        def record_fsync(descriptor):
            fsynced_directory.append(stat.S_ISDIR(outbox_module.os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.jpg"
            Image.new("RGB", (32, 32), color="red").save(source, format="JPEG")
            spool = outbox_module.EvidenceSpool(root / "event-evidence")

            with mock.patch("gate_controller.outbox.os.fsync", side_effect=record_fsync):
                spool.stage(source)

        self.assertEqual(fsynced_directory, [False, True])

    def test_stages_a_private_content_addressed_bounded_jpeg_atomically(self):
        spool_type = getattr(outbox_module, "EvidenceSpool", None)
        self.assertIsNotNone(spool_type)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.jpg"
            Image.effect_noise((2400, 1800), 96).convert("RGB").save(
                source, format="JPEG", quality=96
            )
            spool_root = root / "event-evidence"

            digest = spool_type(spool_root).stage(source)

            stored = spool_root / f"{digest}.jpg"
            encoded = stored.read_bytes()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), digest)
            self.assertLessEqual(len(encoded), 512 * 1024)
            self.assertEqual(stat.S_IMODE(spool_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            self.assertEqual(list(spool_root.glob(".tmp-*")), [])
            with Image.open(stored) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(max(image.size), 1280)

    def test_rejects_corrupt_and_non_jpeg_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / "corrupt.jpg"
            corrupt.write_bytes(b"not a jpeg")
            disguised_png = root / "disguised.jpg"
            Image.new("RGB", (16, 16), color="red").save(disguised_png, format="PNG")
            spool = outbox_module.EvidenceSpool(root / "event-evidence")

            for source in (corrupt, disguised_png):
                with self.subTest(source=source.name):
                    with self.assertRaises(EvidenceSpoolError):
                        spool.stage(source)

    def test_rejects_jpegs_that_trigger_a_decompression_bomb_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "oversized.jpg"
            Image.new("RGB", (16, 16), color="red").save(source, format="JPEG")
            spool = outbox_module.EvidenceSpool(root / "event-evidence")

            with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 200):
                with self.assertRaises(EvidenceSpoolError):
                    spool.stage(source)

    def test_loads_only_the_bytes_matching_the_expected_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.jpg"
            Image.new("RGB", (32, 32), color="red").save(source, format="JPEG")
            spool = outbox_module.EvidenceSpool(root / "event-evidence")
            load = getattr(spool, "load", None)
            self.assertIsNotNone(load)
            digest = spool.stage(source)
            expected = (spool.root / f"{digest}.jpg").read_bytes()

            self.assertEqual(load(digest), expected)

            (spool.root / f"{digest}.jpg").write_bytes(b"different vehicle")
            with self.assertRaises(EvidenceSpoolError):
                load(digest)
            with self.assertRaises(EvidenceSpoolError):
                load("../camera")
            (spool.root / f"{digest}.jpg").unlink()
            with self.assertRaises(EvidenceSpoolError):
                load(digest)


class OutboxWorkerTests(unittest.TestCase):
    def test_malformed_telemetry_falls_back_to_the_original_v2_delivery(self):
        store, event_id = self._queued_store()
        item_id = store.queue_outbox(event_id, {
            "controller_id": "pi-front-gate",
            "image_status": "unavailable_before_queue",
        })
        original_v2 = store.pending_outbox_items()[0][1]
        store.attach_event_telemetry(event_id, _telemetry())
        with closing(sqlite3.connect(store.path)) as connection, connection:
            connection.execute(
                "UPDATE event_telemetry SET payload = ? WHERE event_id = ?",
                ("{malformed", event_id),
            )
        sent = []

        with self.assertLogs("gate_controller.store", level="WARNING") as logs:
            completed = OutboxWorker(store, send=sent.append).run_once()

        self.assertEqual(completed, 1)
        self.assertEqual(sent, [original_v2])
        self.assertEqual(sent[0]["schema_version"], 2)
        self.assertNotIn("telemetry", sent[0])
        self.assertEqual(sent[0]["image_status"], "unavailable_before_queue")
        self.assertEqual(store.pending_outbox_count(), 0)
        self.assertTrue(any(
            "outbox_telemetry_enrichment status=fallback_v2 reason=malformed"
            in entry for entry in logs.output
        ))
        with closing(sqlite3.connect(store.path)) as connection:
            saved = json.loads(connection.execute(
                "SELECT payload FROM outbox WHERE id = ?", (item_id,)
            ).fetchone()[0])
        self.assertEqual(saved, original_v2)

    def test_telemetry_rewrite_failure_falls_back_without_blocking_delivery(self):
        store, event_id = self._queued_store()
        store.queue_outbox(event_id, {"controller_id": "pi-front-gate"})
        original_v2 = store.pending_outbox_items()[0][1]
        store.attach_event_telemetry(event_id, _telemetry())
        original_write = store._write_telemetry_payload
        writes = 0

        def fail_first_write(connection, target_event_id, telemetry):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise sqlite3.OperationalError("telemetry rewrite failed")
            return original_write(connection, target_event_id, telemetry)

        sent = []
        with mock.patch.object(
            store, "_write_telemetry_payload", side_effect=fail_first_write
        ), self.assertLogs("gate_controller.store", level="WARNING") as logs:
            completed = OutboxWorker(store, send=sent.append).run_once()

        self.assertEqual(completed, 1)
        self.assertEqual(sent, [original_v2])
        self.assertEqual(store.pending_outbox_count(), 0)
        self.assertTrue(any(
            "outbox_telemetry_enrichment status=fallback_v2 reason=rewrite_failed"
            in entry for entry in logs.output
        ))

    def test_telemetry_commit_failure_returns_the_recovered_v2_payload(self):
        store, event_id = self._queued_store()
        item_id = store.queue_outbox(event_id, {"controller_id": "pi-front-gate"})
        original_v2 = store.pending_outbox_items()[0][1]
        store.attach_event_telemetry(event_id, _telemetry())
        connection = store._connect()

        class CommitFailingConnection:
            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                raise sqlite3.OperationalError("telemetry commit failed")

        with mock.patch.object(
            store, "_connect", return_value=CommitFailingConnection()
        ), self.assertLogs("gate_controller.store", level="WARNING") as logs:
            payload = store.prepare_outbox_attempt(
                item_id, datetime(2026, 8, 15, 10, 0, 1, tzinfo=timezone.utc)
            )

        self.assertEqual(payload, original_v2)
        self.assertTrue(any(
            "outbox_telemetry_enrichment status=fallback_v2 reason=rewrite_failed"
            in entry for entry in logs.output
        ))

    def test_attempt_metadata_is_persisted_before_each_send_and_completed_after_success(self):
        store, event_id = self._queued_store()
        item_id = store.queue_outbox(event_id, {"controller_id": "pi-front-gate"})
        store.attach_event_telemetry(event_id, _telemetry())
        sent = []

        def send(payload):
            sent.append(payload)
            if len(sent) == 1:
                raise RuntimeError("offline")

        worker = OutboxWorker(store, send=send)

        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(sent[0]["telemetry"]["delivery"], {
            "outbox_attempt": 1,
            "state": "sending",
        })
        self.assertEqual(store.event_telemetry(event_id)["delivery"], {
            "outbox_attempt": 1,
            "state": "retry_pending",
        })

        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(sent[1]["telemetry"]["delivery"], {
            "outbox_attempt": 2,
            "state": "sending",
        })
        self.assertEqual(store.event_telemetry(event_id)["delivery"], {
            "outbox_attempt": 2,
            "state": "delivered",
        })
        with closing(sqlite3.connect(store.path)) as connection:
            completed_at = connection.execute(
                "SELECT completed_at FROM outbox WHERE id = ?", (item_id,)
            ).fetchone()[0]
        self.assertIsNotNone(completed_at)

    def test_retention_runs_at_startup_then_no_more_than_hourly(self):
        store, _ = self._queued_store()
        calls = []
        original = store.purge_delivered_telemetry

        def record(cutoff):
            calls.append(cutoff)
            return original(cutoff)

        store.purge_delivered_telemetry = record
        now = [datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)]
        worker = OutboxWorker(store, send=lambda payload: None, clock=lambda: now[0])

        worker.run_once()
        now[0] += timedelta(minutes=59)
        worker.run_once()
        now[0] += timedelta(minutes=2)
        worker.run_once()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(calls[1], datetime(2026, 7, 17, 13, 1, tzinfo=timezone.utc))

    def test_prepares_an_immutable_evidence_reference_without_a_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.jpg"
            Image.new("RGB", (32, 32), color="red").save(source, format="JPEG")
            store = LocalStore(root / "gate.db")
            worker = OutboxWorker(store, send=lambda payload: None)
            prepare_payload = getattr(worker, "prepare_payload", None)
            self.assertIsNotNone(prepare_payload)

            payload = prepare_payload(source)

            self.assertEqual(payload["controller_id"], "primary")
            self.assertIsNone(payload["event_id"])
            self.assertIn("image_sha256", payload)
            self.assertNotIn(str(source), json.dumps(payload))
            self.assertTrue(
                (root / "event-evidence" / f"{payload['image_sha256']}.jpg").is_file()
            )

    def test_http_sender_uses_a_bounded_post_request(self):
        class Response:
            status_code = 202

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        sender = HttpOutboxSender(
            "https://sync.example/events", session=session, bearer_token="event-secret"
        )

        sender({"event_id": 1})

        expected_key = hashlib.sha256(b"primary:1").hexdigest()
        self.assertEqual(session.calls, [(
            "https://sync.example/events",
            {
                "json": {"event_id": 1, "controller_id": "primary"}, "timeout": (2, 4),
                "headers": {
                    "Authorization": "Bearer event-secret",
                    "Idempotency-Key": expected_key,
                },
            },
        )])

    def test_pending_delivery_keeps_its_first_persisted_controller_identity(self):
        class Response:
            status_code = 202

        class Session:
            def __init__(self):
                self.request = None

            def post(self, url, **kwargs):
                self.request = kwargs
                return Response()

        store, event_id = self._queued_store()
        store.queue_outbox(event_id, {"event_id": event_id})

        OutboxWorker(store, send=lambda payload: None, controller_id="pi-front-gate")
        OutboxWorker(store, send=lambda payload: None, controller_id="replacement-id")
        _, payload = store.pending_outbox_items()[0]
        self.assertEqual(payload.get("controller_id"), "pi-front-gate")

        session = Session()
        HttpOutboxSender(
            "https://sync.example/events", session=session, controller_id="replacement-id"
        )(payload)

        self.assertEqual(session.request["json"]["controller_id"], "pi-front-gate")
        self.assertEqual(
            session.request["headers"]["Idempotency-Key"],
            hashlib.sha256(f"pi-front-gate:{event_id}".encode("utf-8")).hexdigest(),
        )

    def test_http_sender_embeds_the_exact_spooled_jpeg_and_digest_contract(self):
        class Response:
            status_code = 202

        class Session:
            def __init__(self):
                self.request = None

            def post(self, url, **kwargs):
                self.request = kwargs
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "best-frame.jpg"
            Image.effect_noise((2400, 1800), 96).convert("RGB").save(
                image_path, format="JPEG", quality=96
            )
            spool = outbox_module.EvidenceSpool(root / "event-evidence")
            digest = spool.stage(image_path)
            encoded = spool.load(digest)
            session = Session()
            sender = HttpOutboxSender(
                "https://sync.example/events", session=session,
                bearer_token="event-secret", controller_id="pi-front-gate",
            )

            try:
                sender({
                    "event_id": 1,
                    "controller_id": "pi-front-gate",
                    "image_sha256": digest,
                }, encoded)
            except TypeError as error:
                self.fail(f"sender does not accept immutable evidence bytes: {error}")

            transmitted = session.request["json"]
            self.assertIn("image", transmitted)
            transmitted_bytes = base64.b64decode(transmitted["image"]["data_base64"])

            self.assertEqual(transmitted["controller_id"], "pi-front-gate")
            self.assertNotIn("_local_image_path", transmitted)
            self.assertNotIn(str(image_path), json.dumps(transmitted))
            self.assertEqual(transmitted["image_sha256"], digest)
            self.assertEqual(transmitted["image"]["filename"], f"{digest}.jpg")
            self.assertEqual(transmitted["image"]["content_type"], "image/jpeg")
            self.assertEqual(transmitted["image"]["sha256"], digest)
            self.assertEqual(transmitted_bytes, encoded)
            self.assertLessEqual(len(transmitted_bytes), 512 * 1024)
            self.assertEqual(transmitted_bytes[:2], b"\xff\xd8")
            self.assertTrue(image_path.exists())

    def test_http_sender_never_reopens_or_transmits_a_legacy_local_path(self):
        class Response:
            status_code = 202

        class Session:
            def __init__(self):
                self.request = None

            def post(self, url, **kwargs):
                self.request = kwargs
                return Response()

        session = Session()
        sender = HttpOutboxSender(
            "https://sync.example/events", session=session,
            bearer_token="event-secret", controller_id="pi-front-gate",
        )

        sender({"event_id": 1, "reason": "no_match", "_local_image_path": "/gone/frame.jpg"})

        self.assertEqual(session.request["json"], {
            "event_id": 1,
            "reason": "no_match",
            "controller_id": "pi-front-gate",
            "image_status": "legacy_evidence_unavailable",
        })

    def test_startup_sanitizes_legacy_paths_even_after_the_migration_marker_exists(self):
        store, event_id = self._queued_store()
        mutable_path = store.path.parent / "camera.jpg"
        mutable_path.write_bytes(b"a different vehicle now")
        store.queue_outbox(event_id, {
            "event_id": event_id,
            "_local_image_path": str(mutable_path),
        })

        OutboxWorker(store, send=lambda payload: None, controller_id="pi-front-gate")
        _, payload = store.pending_outbox_items()[0]

        self.assertNotIn("_local_image_path", payload)
        self.assertNotIn(str(mutable_path), json.dumps(payload))
        self.assertEqual(payload["image_status"], "legacy_evidence_unavailable")

    def test_event_payload_contains_remote_access_log_details(self):
        store, event_id = self._queued_store()
        payload = store.event_payload(event_id)

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["event_id"], event_id)
        self.assertEqual(payload["source"], "ocr")
        self.assertEqual(payload["reason"], "exact_match")
        self.assertTrue(payload["opened"])
        self.assertIn("received_at", payload)

    def _queued_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = LocalStore(Path(directory.name) / "gate.db")
        event_id = store.record_event(
            GateEvent(
                source="ocr", reason="exact_match", opened=True, idempotency_key="event-1",
                received_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            )
        )
        return store, event_id

    def test_failed_sync_remains_durably_queued_for_retry(self):
        store, event_id = self._queued_store()
        worker = OutboxWorker(
            store, send=lambda payload: (_ for _ in ()).throw(RuntimeError("offline")),
            controller_id="pi-front-gate",
        )
        worker.enqueue(event_id)

        self.assertEqual(worker.run_once(), 0)

        self.assertEqual(store.pending_outbox_count(), 1)
        self.assertEqual(
            store.pending_outbox_items()[0][1].get("controller_id"), "pi-front-gate"
        )

    def test_worker_loads_the_exact_queued_evidence_bytes_by_digest(self):
        store, event_id = self._queued_store()
        source = store.path.parent / "camera.jpg"
        Image.new("RGB", (32, 32), color="red").save(source, format="JPEG")
        spool = outbox_module.EvidenceSpool(store.path.parent / "event-evidence")
        digest = spool.stage(source)
        expected = spool.load(digest)
        store.queue_outbox(event_id, {
            "event_id": event_id,
            "controller_id": "pi-front-gate",
            "image_sha256": digest,
        })
        sent = []

        worker = OutboxWorker(
            store, send=lambda payload, evidence: sent.append((payload, evidence)),
            evidence_spool=spool,
        )

        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(sent[0][0]["image_sha256"], digest)
        self.assertEqual(sent[0][1], expected)

    def test_retry_reuses_the_same_key_and_bytes_then_deletes_only_after_success(self):
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code

        class Session:
            def __init__(self):
                self.statuses = iter((503, 202))
                self.requests = []

            def post(self, url, **kwargs):
                self.requests.append((url, kwargs))
                return Response(next(self.statuses))

        store, event_id = self._queued_store()
        source = store.path.parent / "camera.jpg"
        Image.new("RGB", (32, 32), color="red").save(source, format="JPEG")
        spool = outbox_module.EvidenceSpool(store.path.parent / "event-evidence")
        digest = spool.stage(source)
        evidence_path = spool.root / f"{digest}.jpg"
        store.queue_outbox(event_id, {
            "event_id": event_id,
            "controller_id": "pi-front-gate",
            "image_sha256": digest,
        })
        session = Session()
        sender = HttpOutboxSender(
            "https://sync.example/events", session=session,
            bearer_token="event-secret", controller_id="pi-front-gate",
        )
        worker = OutboxWorker(store, sender, evidence_spool=spool)

        self.assertEqual(worker.run_once(), 0)
        self.assertTrue(evidence_path.is_file())
        self.assertEqual(store.pending_outbox_count(), 1)
        self.assertEqual(worker.run_once(), 1)

        first = session.requests[0][1]
        retry = session.requests[1][1]
        self.assertEqual(first["headers"]["Idempotency-Key"], retry["headers"]["Idempotency-Key"])
        self.assertEqual(first["json"]["image"]["data_base64"], retry["json"]["image"]["data_base64"])
        self.assertFalse(evidence_path.exists())
        with closing(sqlite3.connect(store.path)) as connection, connection:
            payload_text, completed_at = connection.execute(
                "SELECT payload, completed_at FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        self.assertEqual(json.loads(payload_text)["image_sha256"], digest)
        self.assertIsNotNone(completed_at)

    def test_shared_evidence_is_deleted_only_after_all_pending_events_succeed(self):
        store, first_event_id = self._queued_store()
        root = store.path.parent
        source = root / "camera.jpg"
        Image.new("RGB", (32, 32), color="red").save(source, format="JPEG")
        spool = outbox_module.EvidenceSpool(root / "event-evidence")
        digest = spool.stage(source)
        second_event_id = store.record_event(GateEvent(
            source="ocr", reason="no_match", opened=False, idempotency_key="event-2",
            received_at=datetime(2026, 8, 13, 10, 1, tzinfo=timezone.utc),
        ))
        for event_id in (first_event_id, second_event_id):
            store.queue_outbox(event_id, {
                "event_id": event_id,
                "controller_id": "pi-front-gate",
                "image_sha256": digest,
            })
        sent = []
        worker = OutboxWorker(
            store, send=lambda payload, evidence: sent.append((payload, evidence)),
            evidence_spool=spool,
        )

        self.assertEqual(worker.run_once(), 2)

        self.assertEqual([item[0]["event_id"] for item in sent], [first_event_id, second_event_id])
        self.assertEqual(sent[0][1], sent[1][1])
        self.assertFalse((spool.root / f"{digest}.jpg").exists())

    def test_startup_removes_completed_evidence_but_retains_corrupt_pending_evidence(self):
        store, pending_event_id = self._queued_store()
        root = store.path.parent
        spool = outbox_module.EvidenceSpool(root / "event-evidence")
        pending_source = root / "pending.jpg"
        completed_source = root / "completed.jpg"
        Image.new("RGB", (32, 32), color="red").save(pending_source, format="JPEG")
        Image.new("RGB", (32, 32), color="blue").save(completed_source, format="JPEG")
        pending_digest = spool.stage(pending_source)
        completed_digest = spool.stage(completed_source)
        store.queue_outbox(pending_event_id, {
            "event_id": pending_event_id,
            "controller_id": "pi-front-gate",
            "image_sha256": pending_digest,
        })
        completed_event_id = store.record_event(GateEvent(
            source="ocr", reason="no_match", opened=False, idempotency_key="completed-event",
            received_at=datetime(2026, 8, 13, 10, 1, tzinfo=timezone.utc),
        ))
        completed_item_id = store.queue_outbox(completed_event_id, {
            "event_id": completed_event_id,
            "controller_id": "pi-front-gate",
            "image_sha256": completed_digest,
        })
        store.complete_outbox_item(completed_item_id)
        pending_path = spool.root / f"{pending_digest}.jpg"
        completed_path = spool.root / f"{completed_digest}.jpg"
        pending_path.write_bytes(b"corrupt pending evidence")
        sent = []

        worker = OutboxWorker(store, send=sent.append, evidence_spool=spool)

        self.assertFalse(completed_path.exists())
        self.assertTrue(pending_path.exists())
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(sent, [])
        self.assertEqual(store.pending_outbox_count(), 1)

    def test_successful_sync_is_marked_complete(self):
        store, event_id = self._queued_store()
        sent = []
        worker = OutboxWorker(store, send=sent.append)
        worker.enqueue(event_id)

        self.assertEqual(worker.run_once(), 1)

        self.assertEqual(sent[0]["event_id"], event_id)
        self.assertEqual(sent[0]["schema_version"], 2)
        self.assertEqual(sent[0]["reason"], "exact_match")
        self.assertEqual(store.pending_outbox_count(), 0)


if __name__ == "__main__":
    unittest.main()
