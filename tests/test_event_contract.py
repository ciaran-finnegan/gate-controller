"""What the controller puts on the wire, judged by the app's own rules.

The ingest Worker validates every event against
``worker/contracts/gate-event-ingest/contract.ts`` in ``access-gate-ui`` and
answers 400 for any unknown key or out-of-range value. The controller's outbox
retries a 400 forever, so a payload the contract will not accept is not a bad
row in the app -- it is a queue that never drains. The rules below are a
transcription of that file, kept deliberately literal so a drift shows up as a
failing test here rather than as a stuck outbox on the Pi.

Two facts these tests pin, from the 8 September 2026 journal-vs-D1 comparison:

* a plate matched while the relay was still in cooldown is a **grant** whose
  actuation was skipped, and the contract can say so; and
* a frame that never reached a reader has no score, and the app now has a way
  to say so. ``ocr_confidence`` is ``optionalNumber(0, 1)`` since
  ``access-gate-ui`` #53, so those frames carry ``null`` rather than the ``0``
  the old contract forced on them and the app rendered as "0%".

The second is the line these tests hold from this side: ``null`` on a frame no
reader measured, and the measured score -- never ``null`` -- on every frame one
did, including the denials.
"""

import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from gate_controller.actuation import ActuationCoordinator
from gate_controller.models import GateEvent, PlateObservation, RelayResult
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore, _telemetry_payload
from gate_controller.telemetry import EventTelemetry, StageDurations

# Transcribed from contract.ts. EVENT_TOKEN is the shape of every reason,
# source and telemetry token; PLATE the shape of a registration.
EVENT_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PLATE = re.compile(r"^[A-Za-z0-9 -]{1,32}$")
CONTROLLER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FORBIDDEN_TELEMETRY_KEY = re.compile(
    r"token|secret|password|credential|path|raw_response|exception", re.IGNORECASE
)
V3_EVENT_KEYS = frozenset({
    "schema_version", "controller_id", "event_id", "received_at", "decision_at",
    "relay_activated_at", "source", "reason", "opened", "idempotency_key",
    "authorised_plate", "observed_plate", "ocr_confidence", "image",
    "image_sha256", "image_status", "telemetry",
})
TELEMETRY_KEYS = frozenset({
    "trace_id", "taxonomy_version", "trigger", "stage_timestamps",
    "stage_durations", "frames", "ocr_attempts", "decision", "actuation",
    "delivery", "match_policy", "local_ocr", "direction",
})
DECISION_KEYS = frozenset({"outcome", "reason"})
ACTUATION_KEYS = frozenset({"claim", "attempted", "relay_outcome"})
MAX_TELEMETRY_STRING_LENGTH = 128


class IngestRejected(AssertionError):
    """What the Worker answers 400 for, and the outbox then retries forever."""


def _token(value, field):
    if not isinstance(value, str) or not EVENT_TOKEN.match(value):
        raise IngestRejected(f"{field} is invalid")


def _telemetry_token(value, field):
    if (not isinstance(value, str) or not value
            or len(value) > MAX_TELEMETRY_STRING_LENGTH
            or not EVENT_TOKEN.match(value)):
        raise IngestRejected(f"{field} is invalid")


def _allowed_keys(record, allowed, field):
    for key in record:
        if FORBIDDEN_TELEMETRY_KEY.search(key) or key not in allowed:
            raise IngestRejected(f"{field}.{key} is not allowed")


def assert_ingest_accepts_event(payload):
    """``validateGateEvent`` in contract.ts, transcribed."""
    if payload.get("schema_version") not in (1, 2, 3):
        raise IngestRejected("unsupported schema version")
    if payload["schema_version"] == 3:
        _allowed_keys(payload, V3_EVENT_KEYS, "event")
    if "controller_id" not in payload or not CONTROLLER_ID.match(
        str(payload.get("controller_id"))
    ):
        raise IngestRejected("controller_id is invalid")
    event_id = payload.get("event_id")
    if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
        raise IngestRejected("event_id is invalid")
    _token(payload.get("source"), "source")
    _token(payload.get("reason"), "reason")
    if not isinstance(payload.get("opened"), bool):
        raise IngestRejected("opened must be boolean")
    for field in ("authorised_plate", "observed_plate"):
        value = payload.get(field)
        if value is not None and not PLATE.match(str(value)):
            raise IngestRejected(f"{field} is invalid")
    # `optionalNumber(event.ocr_confidence, 0, 1, 'ocr_confidence')`, which is
    # `value == null ? null : requireNumber(...)`. Null and a missing key both
    # store no score; anything present and non-null is still a number in
    # [0, 1], so a NaN or a 1.5 is a 400 exactly as it always was.
    confidence = payload.get("ocr_confidence")
    if confidence is not None and (
        not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise IngestRejected("ocr_confidence is invalid")
    for field in ("received_at", "decision_at", "relay_activated_at"):
        value = payload.get(field)
        if field == "received_at" and value is None:
            raise IngestRejected("received_at is invalid")
        if value is not None and not re.match(r"^\d{4}-\d{2}-\d{2}T", str(value)):
            raise IngestRejected(f"{field} is invalid")


def assert_ingest_accepts_telemetry(wire):
    """The parts of ``validateTelemetry`` this change touches."""
    _allowed_keys(wire, TELEMETRY_KEYS, "telemetry")
    decision = wire["decision"]
    _allowed_keys(decision, DECISION_KEYS, "telemetry.decision")
    _telemetry_token(decision["outcome"], "telemetry.decision.outcome")
    _telemetry_token(decision["reason"], "telemetry.decision.reason")
    actuation = wire["actuation"]
    _allowed_keys(actuation, ACTUATION_KEYS, "telemetry.actuation")
    _telemetry_token(actuation["claim"], "telemetry.actuation.claim")
    _telemetry_token(actuation["relay_outcome"], "telemetry.actuation.relay_outcome")
    if not isinstance(actuation["attempted"], bool):
        raise IngestRejected("telemetry.actuation.attempted must be boolean")


class OpenRelay:
    def trigger(self, source, idempotency_key=None):
        return RelayResult(
            True, "activated", idempotency_key,
            datetime(2026, 9, 8, 14, 17, 26, tzinfo=timezone.utc),
        )


class SilentRecognizer:
    def recognise(self, path):  # pragma: no cover - never reached by a skip
        raise AssertionError("a skipped frame must never reach a reader")


class ReadingRecognizer:
    """A cloud read, with whatever the reader made of the frame.

    ``spend`` is how much of the decision budget the read costs, charged to
    ``clock`` once the answer is back -- the way a real lookup spends it.
    """

    def __init__(self, plate, confidence, *, clock=None, spend=0.0):
        self._observation = PlateObservation(plate, confidence)
        self._clock = clock
        self._spend = spend
        self.calls = []

    def recognise(self, path, timeout=None):
        self.calls.append(path)
        if self._clock is not None:
            self._clock.value += self._spend
        return self._observation


class _SpentClock:
    """A decision clock that only moves when a read charges it."""

    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class CooldownGrantWireTests(unittest.TestCase):
    """A matched plate during cooldown is a grant, and reads as one."""

    NOW = datetime(2026, 9, 8, 14, 17, 26, tzinfo=timezone.utc)

    def _passage(self, directory):
        """One grant, then a second matched frame 2 s later, inside cooldown."""
        store = LocalStore(Path(directory) / "gate.db")
        granted = ActuationCoordinator(
            store, OpenRelay(), clock=lambda: self.NOW,
            monotonic_clock=lambda: 100.0, boot_id="boot-1",
        ).actuate(self._matched("image:first", self.NOW), outbox_payload={})
        later = self.NOW + timedelta(seconds=2)
        coalesced = ActuationCoordinator(
            store, OpenRelay(), clock=lambda: later,
            monotonic_clock=lambda: 102.0, boot_id="boot-1",
        ).actuate(self._matched("image:second", later), outbox_payload={})
        store.bind_pending_outbox_controller("primary")
        return store, granted, coalesced

    def _matched(self, key, at):
        return GateEvent(
            source="ocr", reason="exact_match", opened=False, idempotency_key=key,
            received_at=at, decision_at=at, authorised_plate="131D2696",
            observed_plate="131D2696", ocr_confidence=0.999,
        )

    def _payload(self, store, event_id):
        for _item_id, payload in store.pending_outbox_items():
            if payload.get("event_id") == event_id:
                return payload
        raise AssertionError(f"no outbox payload for event {event_id}")

    def test_cooldown_grant_reaches_the_app_as_granted_with_its_plate_and_score(self):
        with tempfile.TemporaryDirectory() as directory:
            store, granted, coalesced = self._passage(directory)
            payload = self._payload(store, coalesced.event_id)

        self.assertTrue(granted.opened)
        self.assertEqual(coalesced.reason, "cooldown")
        self.assertTrue(payload["opened"], "the gate was open for this car")
        self.assertEqual(payload["reason"], "exact_match")
        self.assertEqual(payload["authorised_plate"], "131D2696")
        self.assertEqual(payload["observed_plate"], "131D2696")
        self.assertEqual(payload["ocr_confidence"], 0.999)
        self.assertIsNone(
            payload["relay_activated_at"],
            "this event granted access; it did not pulse the relay",
        )

    def test_cooldown_grant_payload_passes_the_app_ingest_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _granted, coalesced = self._passage(directory)
            payload = self._payload(store, coalesced.event_id)

        assert_ingest_accepts_event(payload)
        self.assertNotIn(
            "actuation_outcome", payload,
            "the local column names the skipped pulse; the wire has no key for it",
        )

    def _cooldown_telemetry(self):
        return EventTelemetry(
            trace_id="de2781c4-1e19-4a0a-9a3e-6f4a9d2f9b21",
            stage_durations=StageDurations(),
            frames=(),
            ocr_attempts=(),
            decision_outcome="allowed",
            decision_reason="exact_match",
            actuation_claim="cooldown",
            actuation_attempted=False,
            relay_outcome="not_attempted",
            outbox_attempt=0,
            delivery_state="pending",
        )

    def test_cooldown_grant_telemetry_names_cooldown_on_the_actuation_block(self):
        """The contract's own field for "granted, relay not pulsed"."""
        # `_telemetry_payload` is what the outbox stores and posts, so it is
        # what the contract has to accept. Validating `to_wire()` instead would
        # judge a shape no Worker ever sees.
        wire = _telemetry_payload(self._cooldown_telemetry())

        assert_ingest_accepts_telemetry(wire)
        self.assertEqual(wire["decision"], {"outcome": "allowed", "reason": "exact_match"})
        self.assertEqual(wire["actuation"], {
            "claim": "cooldown", "attempted": False, "relay_outcome": "not_attempted",
        })

    def test_the_outbox_drops_the_schema_version_the_telemetry_block_may_not_carry(self):
        """`schema_version` is an event key, not a telemetry key.

        ``EventTelemetry.to_wire`` stamps one so the local store can tell its
        own records apart; ``assertAllowedKeys`` in contract.ts answers 400 for
        it inside ``telemetry``. The stripping in ``_telemetry_payload`` is the
        only thing standing between the two, so pin it here -- without this the
        transcription above would pass while allowing a key the app rejects.
        """
        telemetry = self._cooldown_telemetry()

        self.assertEqual(telemetry.to_wire()["schema_version"], 3)
        with self.assertRaises(IngestRejected):
            assert_ingest_accepts_telemetry(telemetry.to_wire())
        self.assertNotIn("schema_version", _telemetry_payload(telemetry))

    def test_cooldown_record_does_not_slide_the_cooldown_window(self):
        """A record of a skipped pulse must never be read back as a pulse.

        The window is bounded by the relay activation, not by however many
        frames of the same burst were coalesced behind it -- otherwise a
        convoy's second car could be refused its own grant.
        """
        with tempfile.TemporaryDirectory() as directory:
            store, _granted, _coalesced = self._passage(directory)
            # The relay pulsed at NOW; the coalesced frame landed at NOW+2 s.
            # Asked about the window that opens one second after the pulse,
            # only the pulse may answer -- and it is already behind us.
            after_the_pulse = store.was_opened_since(self.NOW + timedelta(seconds=1))
            next_car = self.NOW + timedelta(seconds=21)
            reopened = ActuationCoordinator(
                store, OpenRelay(), clock=lambda: next_car,
                monotonic_clock=lambda: 121.0, boot_id="boot-1",
            ).actuate(self._matched("image:next-car", next_car), outbox_payload={})

        self.assertFalse(
            after_the_pulse, "the coalesced frame never worked the relay"
        )
        self.assertTrue(reopened.opened, "the next vehicle gets its own pulse")


class NonDecidingFrameWireTests(unittest.TestCase):
    """Frames no reader ever saw, and the null that now says so.

    ``queue_coalesced`` (which is also what the worker's
    ``cause=event_already_opened`` skip records), ``decision_timeout`` and
    ``upload_incomplete`` are all recorded through ``record_skipped``: no OCR
    attempt is made, so there is no score to report. The contract takes
    ``ocr_confidence`` as ``optionalNumber(0, 1)``, so the honest answer -- no
    score -- goes on the wire as ``null`` and stays NULL in the local store.
    """

    NOW = datetime(2026, 9, 8, 14, 17, 30, tzinfo=timezone.utc)

    def _skip(self, reason, *, truncated=False):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            if truncated:
                frame.write_bytes(b"\xff\xd8\xff\xe0")
            else:
                Image.new("L", (16, 8), color=128).save(frame, format="JPEG")
            database = Path(directory) / "gate.db"
            store = LocalStore(database)
            processor = GateProcessor(
                recognizer=SilentRecognizer(), store=store, relay=OpenRelay(),
                authorised={"131D2696"}, cooldown=timedelta(seconds=20),
                outbox=None, clock=lambda: self.NOW,
            )
            result = processor.record_skipped((frame,), reason)
            payload = store.event_payload(result.event_id)
            telemetry = store.event_telemetry(result.event_id)
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    "SELECT ocr_confidence FROM events WHERE id = ?",
                    (result.event_id,),
                ).fetchone()[0]
        payload["controller_id"] = "primary"
        return result, payload, telemetry, stored

    def _assert_non_deciding(self, reason, **kwargs):
        result, payload, telemetry, stored = self._skip(reason, **kwargs)

        self.assertEqual(result.reason, reason)
        self.assertEqual(payload["reason"], reason)
        self.assertFalse(payload["opened"])
        self.assertIsNone(payload["observed_plate"])
        self.assertEqual(telemetry["ocr_attempts"], [], "no reader ran on this frame")
        # No reader, no score. Not 0.0, which is a measurement -- and one the
        # app would paint as "0%" next to the reads that really scored badly.
        self.assertIsNone(payload["ocr_confidence"])
        self.assertIsNone(stored, "the local column holds NULL, not a zero")
        assert_ingest_accepts_event(payload)

    def test_queue_coalesced_frame_reports_no_score_at_all(self):
        self._assert_non_deciding("queue_coalesced")

    def test_decision_timeout_before_any_read_reports_no_score_at_all(self):
        self._assert_non_deciding("decision_timeout")

    def test_upload_incomplete_frame_reports_no_score_at_all(self):
        self._assert_non_deciding("upload_incomplete", truncated=True)

    def test_the_contract_accepts_a_null_confidence_and_still_refuses_a_bad_number(self):
        """The half of `optionalNumber` that is still `requireNumber`."""
        _result, payload, _telemetry, _stored = self._skip("queue_coalesced")

        assert_ingest_accepts_event(payload)
        payload.pop("ocr_confidence")
        assert_ingest_accepts_event(payload)

        for rejected in (1.5, -0.1, float("nan"), "0.9", True):
            with self.subTest(ocr_confidence=rejected):
                payload["ocr_confidence"] = rejected
                with self.assertRaises(IngestRejected):
                    assert_ingest_accepts_event(payload)


class ReadFrameWireTests(unittest.TestCase):
    """A frame a reader *did* measure keeps its score, granted or denied.

    The null above is for the absence of a measurement, not for a refusal. A
    plate that was read and turned away is exactly the row a reviewer needs the
    score on, and a read that arrived after the decision deadline is still a
    read.
    """

    NOW = datetime(2026, 9, 8, 14, 17, 34, tzinfo=timezone.utc)

    def _decide(self, recognizer, *, decision_clock=None, frames=1):
        with tempfile.TemporaryDirectory() as directory:
            # Distinct content per frame: identical frames are deduplicated
            # before the burst is read.
            burst = []
            for sequence in range(frames):
                frame = Path(directory) / f"frame-{sequence}.jpg"
                Image.new("L", (16, 8), color=16 * sequence + 96).save(
                    frame, format="JPEG"
                )
                burst.append(frame)
            store = LocalStore(Path(directory) / "gate.db")
            processor = GateProcessor(
                recognizer=recognizer, store=store, relay=OpenRelay(),
                authorised={"131D2696"}, cooldown=timedelta(seconds=20),
                outbox=None, clock=lambda: self.NOW, decision_timeout=4.0,
                decision_clock=decision_clock or (lambda: 0.0),
            )
            result = processor.process(tuple(burst))
            payload = store.event_payload(result.event_id)
            telemetry = store.event_telemetry(result.event_id)
        payload["controller_id"] = "primary"
        return result, payload, telemetry

    def test_no_match_on_a_read_plate_keeps_the_score_the_reader_gave_it(self):
        result, payload, telemetry = self._decide(ReadingRecognizer("152D9911", 0.874))

        self.assertEqual(result.reason, "no_match")
        self.assertEqual(payload["reason"], "no_match")
        self.assertFalse(payload["opened"])
        self.assertEqual(payload["observed_plate"], "152D9911")
        self.assertEqual(payload["ocr_confidence"], 0.874)
        self.assertEqual(
            [attempt["confidence"] for attempt in telemetry["ocr_attempts"]], [0.874],
            "a reader ran, and the event reports what it measured",
        )
        assert_ingest_accepts_event(payload)

    def test_decision_timeout_after_a_real_read_keeps_that_read_s_score(self):
        """The deadline expired; the measurement it expired on still stands.

        A two-frame burst against a 4 s budget where each cloud lookup costs
        2.5 s: the first frame comes back with a plate that is not authorised,
        and the second answers past the deadline, which is where the burst is
        given up. The event is a ``decision_timeout``, but a reader did run and
        did measure something, so the score is a real one -- this is the case
        the null must not eat.
        """
        clock = _SpentClock()
        recognizer = ReadingRecognizer("152D9911", 0.874, clock=clock, spend=2.5)
        result, payload, telemetry = self._decide(
            recognizer, decision_clock=clock, frames=2,
        )

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(payload["reason"], "decision_timeout")
        self.assertFalse(payload["opened"], "the deadline passed before a match")
        self.assertEqual(payload["observed_plate"], "152D9911")
        self.assertEqual(payload["ocr_confidence"], 0.874)
        self.assertEqual(
            [(attempt["status"], attempt["confidence"])
             for attempt in telemetry["ocr_attempts"]],
            [("recognized", 0.874), ("ocr_timeout", None)],
            "the first frame was read; the second answered too late to count",
        )
        self.assertEqual(len(recognizer.calls), 2)
        assert_ingest_accepts_event(payload)

    def test_a_read_that_found_no_plate_at_all_reports_no_score(self):
        """An attempt is not a measurement: `no_plate` has nothing to report."""
        result, payload, telemetry = self._decide(ReadingRecognizer(None, 0.0))

        self.assertEqual(result.reason, "no_match")
        self.assertIsNone(payload["observed_plate"])
        self.assertIsNone(payload["ocr_confidence"])
        self.assertEqual(
            [attempt["status"] for attempt in telemetry["ocr_attempts"]], ["no_plate"],
        )
        assert_ingest_accepts_event(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
