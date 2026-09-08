"""A read that never left the Pi must not be charged to the allowance.

The whole point of on-device recognition (gate-controller#100) is that in
``GATE_LOCAL_OCR_MODE=active`` with ``GATE_LOCAL_OCR_CLOUD=fallback`` a
confident local read returns from ``PlateRecognizerClient._recognise_once``
*before any* ``session.post``. Nothing is sent, and nothing is billed.

The burn-down, though, is built from the processor's own telemetry, and the
attempt it records for that read has ``status="recognized"`` exactly like a
cloud read. Billing on status alone therefore counted precisely the lookups
that work stopped spending, and the tile that is supposed to show the saving
would have shown none.

The counterpart matters too: ``GATE_LOCAL_OCR_CLOUD=always`` posts the frame
anyway so the corpus is labelled, and the allowance *is* charged even though
the local read decided. So the rule is "was a cloud request actually made",
which is what ``OcrAttemptTelemetry.cloud_lookup`` records -- never
``source``, and never the local block's ``decision_source``.

These tests drive the real :class:`GateProcessor`, the real
:class:`PlateRecognizerClient` and the real :class:`MetricsRing`; only the
inference engine and the HTTP session are stubs.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep

from PIL import Image

from gate_controller.metrics import BUCKET_MINUTES, MetricsRing, QuotaLedger
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore

from tests.test_local_recognizer import (
    FakeResponse, FakeSession, cloud_payload, read, recognizer,
)
from tests.test_processor import RecordingRelay


START = datetime(2026, 9, 8, 10, 20, 30, tzinfo=timezone.utc)
PLATE = "12D3456"


class FrozenClock:
    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class LocalReadBillingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.frame = self.root / "frame.jpg"
        Image.new("L", (64, 32), color=128).save(self.frame, format="JPEG")
        self.clock = FrozenClock()
        self.ledger = QuotaLedger(None, clock=self.clock)
        self.ring = MetricsRing(
            quota=self.ledger, clock=self.clock, retry_counts=lambda: {},
        )

    def _drive(self, *, cloud: str, responses=()):
        """One burst through the real processor, counted by the real ring."""
        local = recognizer([read(PLATE, 0.99)], mode="active", cloud=cloud)
        self.addCleanup(local.close)
        session = FakeSession(list(responses))
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {PLATE},
        )
        processor = GateProcessor(
            recognizer=client,
            store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay([]),
            authorised={PLATE},
            clock=self.clock,
        )

        result = processor.process((self.frame,))
        self.ring.record_processing_result(result)
        return session, result

    def _recognition(self) -> dict:
        self.clock.advance(minutes=BUCKET_MINUTES)
        return self.ring.unsent_minutes()[0]["recognition"]

    def test_the_journal_says_which_reader_answered_and_what_it_cost(self):
        with self.assertLogs("gate_controller.processor", level="INFO") as logs:
            self._drive(cloud="fallback")

        line = next(
            entry for entry in logs.output if "stage=processing_finished" in entry
        )
        self.assertIn('"source":"local"', line)
        self.assertIn('"cloud_lookup":false', line)

    def test_a_confident_local_read_in_fallback_mode_costs_no_lookup(self):
        session, result = self._drive(cloud="fallback")

        self.assertTrue(result.opened)
        self.assertEqual(
            session.calls, [], "the request never left the Pi",
        )
        attempt = list(result.telemetry.ocr_attempts)[0]
        self.assertEqual(attempt.status, "recognized")
        self.assertEqual(attempt.source, "local")
        self.assertFalse(attempt.cloud_lookup)

        self.assertEqual(
            self.ring.quota_status()["recognition_lookups_month_to_date"], 0,
            "the burn-down did not move for a read that cost nothing",
        )
        recognition = self._recognition()
        self.assertEqual(recognition["billed_lookups"], 0)
        self.assertEqual(recognition["recognized"], 1)
        self.assertEqual(recognition["local_recognized"], 1)

    def test_always_mode_bills_the_request_it_actually_sent(self):
        session, result = self._drive(
            cloud="always", responses=[FakeResponse(cloud_payload(PLATE, 0.93))],
        )

        self.assertTrue(result.opened)
        self.assertEqual(len(session.calls), 1, "the frame was still labelled")
        attempt = list(result.telemetry.ocr_attempts)[0]
        self.assertEqual(
            attempt.source, "local", "the on-device read is what decided",
        )
        self.assertTrue(
            attempt.cloud_lookup, "and the allowance was charged anyway",
        )

        self.assertEqual(
            self.ring.quota_status()["recognition_lookups_month_to_date"], 1,
        )
        self.assertEqual(self._recognition()["billed_lookups"], 1)

    def test_a_cloud_read_is_billed_exactly_as_before(self):
        local = recognizer([read("99ZZ9999", 0.99)], mode="active")
        self.addCleanup(local.close)
        session = FakeSession([FakeResponse(cloud_payload(PLATE))])
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {PLATE},
        )
        processor = GateProcessor(
            recognizer=client,
            store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay([]),
            authorised={PLATE},
            clock=self.clock,
        )

        result = processor.process((self.frame,))
        self.ring.record_processing_result(result)

        # The local read was not authorised, so it fell through to the cloud.
        self.assertEqual(len(session.calls), 1)
        attempt = list(result.telemetry.ocr_attempts)[0]
        self.assertEqual(attempt.source, "cloud")
        self.assertTrue(attempt.cloud_lookup)
        self.assertEqual(self._recognition()["billed_lookups"], 1)


class AbandonedRequestBillingTests(unittest.TestCase):
    """An attempt abandoned before its POST is not a lookup either.

    ``ocr_timeout`` is billed because the processor records it only past
    ``ocr_started`` -- but ``ocr_started`` fires when the request *thread*
    starts, and between that and ``session.post`` sit the client's 1.05 s
    pacing window (Plate Recognizer allows one request a second), the
    downscaled upload and the on-device guard. The second frame of a burst is
    the ordinary case: the first frame's reply sets ``_not_before``, the
    second waits it out, and a decision budget shorter than that window is
    abandoned inside ``_pace`` with nothing sent. Billing it billed the
    throttle.

    So the flag comes from the dispatch site, which is the only place that
    knows -- and an attempt that *was* posted and then abandoned is still
    billed, because from the caller's side that is a lost reply.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # Distinct content: the processor drops duplicate frames by digest.
        self.frames = []
        for index, shade in enumerate((128, 64)):
            frame = self.root / f"frame-{index}.jpg"
            Image.new("L", (64, 32), color=shade).save(frame, format="JPEG")
            self.frames.append(frame)
        self.clock = FrozenClock()
        self.ledger = QuotaLedger(None, clock=self.clock)
        self.ring = MetricsRing(
            quota=self.ledger, clock=self.clock, retry_counts=lambda: {},
        )

    def _drive(self, session, frames):
        client = PlateRecognizerClient("token", session=session)
        processor = GateProcessor(
            recognizer=client,
            store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay([]),
            authorised={PLATE},
            clock=self.clock,
            # Shorter than the 1.05 s the client must wait between requests.
            decision_timeout=0.6,
        )
        self.addCleanup(processor.close)
        result = processor.process(tuple(frames))
        self.ring.record_processing_result(result)
        return result

    def _recognition(self) -> dict:
        self.clock.advance(minutes=BUCKET_MINUTES)
        return self.ring.unsent_minutes()[0]["recognition"]

    def test_a_frame_abandoned_in_the_pacing_window_costs_nothing(self):
        # An unauthorised plate, so the burst goes on to the second frame.
        session = FakeSession([FakeResponse(cloud_payload("99ZZ9999"))])
        result = self._drive(session, self.frames)

        attempts = list(result.telemetry.ocr_attempts)
        self.assertEqual(
            [attempt.status for attempt in attempts],
            ["recognized", "ocr_timeout"],
        )
        self.assertEqual(
            len(session.calls), 1,
            "the second frame never reached session.post",
        )
        posted, abandoned = attempts
        self.assertTrue(posted.cloud_lookup)
        self.assertFalse(
            abandoned.cloud_lookup,
            "the request died waiting out the throttle window; nothing was "
            "sent, so nothing was charged",
        )

        self.assertEqual(
            self.ring.quota_status()["recognition_lookups_month_to_date"], 1,
        )
        recognition = self._recognition()
        self.assertEqual(recognition["billed_lookups"], 1)
        self.assertEqual(
            recognition["ocr_timeout"], 1,
            "the abandonment is still counted; only the billing changed",
        )

    def test_a_request_that_went_out_and_timed_out_is_still_billed(self):
        # One frame, so there is no pacing window: the POST starts, and the
        # decision budget runs out waiting for the reply. That is a
        # `read_timeout` seen from the decision's clock, and it is charged.
        session = StallingSession(1.0)
        result = self._drive(session, self.frames[:1])

        attempts = list(result.telemetry.ocr_attempts)
        self.assertEqual([attempt.status for attempt in attempts], ["ocr_timeout"])
        self.assertEqual(len(session.calls), 1, "the request did go out")
        self.assertTrue(attempts[0].cloud_lookup)

        self.assertEqual(
            self.ring.quota_status()["recognition_lookups_month_to_date"], 1,
        )
        self.assertEqual(self._recognition()["billed_lookups"], 1)


class StallingSession:
    """A session whose POST outlives the decision budget."""

    def __init__(self, seconds: float):
        self._seconds = seconds
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        sleep(self._seconds)
        return FakeResponse({"results": []})

    def close(self):
        return None


class WireContractTests(unittest.TestCase):
    """The new fields are journal-only; the event ingest contract is frozen."""

    def test_the_attempt_wire_payload_gained_no_key(self):
        from gate_controller.telemetry import OcrAttemptTelemetry

        cloud = OcrAttemptTelemetry(frame_sequence=0, status="recognized")
        local = OcrAttemptTelemetry(
            frame_sequence=0, status="recognized",
            source="local", cloud_lookup=False,
        )

        self.assertEqual(set(cloud.to_wire()), {
            "frame_sequence", "duration_ms", "status", "plate", "confidence",
            "make", "colour",
        })
        self.assertEqual(
            local.to_wire(), cloud.to_wire(),
            "`source` and `cloud_lookup` are absent from the wire, so the "
            "Worker's OCR_ATTEMPT_KEYS allow-list needs no change and no "
            "event can be rejected for carrying them",
        )


if __name__ == "__main__":
    unittest.main()
