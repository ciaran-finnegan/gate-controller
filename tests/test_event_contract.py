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
* a frame that never reached a reader still has to carry ``ocr_confidence: 0``,
  because the contract requires a number there. That zero is the app's "0%",
  and no controller change can remove it.
"""

import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from gate_controller.actuation import ActuationCoordinator
from gate_controller.models import GateEvent, RelayResult
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
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
    # requireNumber, not optionalNumber: null and a missing key are both a 400.
    confidence = payload.get("ocr_confidence")
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not 0 <= confidence <= 1):
        raise IngestRejected("ocr_confidence is invalid")
    for field in ("received_at", "decision_at", "relay_activated_at"):
        value = payload.get(field)
        if field == "received_at" and value is None:
            raise IngestRejected("received_at is invalid")
        if value is not None and not re.match(r"^\d{4}-\d{2}-\d{2}T", str(value)):
            raise IngestRejected(f"{field} is invalid")


def assert_ingest_accepts_telemetry(wire):
    """The parts of ``validateTelemetry`` this change touches."""
    _allowed_keys(wire, TELEMETRY_KEYS | {"schema_version"}, "telemetry")
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

    def test_cooldown_grant_telemetry_names_cooldown_on_the_actuation_block(self):
        """The contract's own field for "granted, relay not pulsed"."""
        wire = EventTelemetry(
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
        ).to_wire()

        assert_ingest_accepts_telemetry(wire)
        self.assertEqual(wire["decision"], {"outcome": "allowed", "reason": "exact_match"})
        self.assertEqual(wire["actuation"], {
            "claim": "cooldown", "attempted": False, "relay_outcome": "not_attempted",
        })

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
    """Frames no reader ever saw, and the zero the contract forces on them.

    ``queue_coalesced`` (which is also what the worker's
    ``cause=event_already_opened`` skip records), ``decision_timeout`` and
    ``upload_incomplete`` are all recorded through ``record_skipped``: no OCR
    attempt is made, so there is no score to report. The contract's
    ``ocr_confidence`` is ``requireNumber(0, 1)``, so the controller must send
    a number anyway, and 0 is the only honest one available. The app reads it
    as "0%".
    """

    NOW = datetime(2026, 9, 8, 14, 17, 30, tzinfo=timezone.utc)

    def _skip(self, reason, *, truncated=False):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            if truncated:
                frame.write_bytes(b"\xff\xd8\xff\xe0")
            else:
                Image.new("L", (16, 8), color=128).save(frame, format="JPEG")
            store = LocalStore(Path(directory) / "gate.db")
            processor = GateProcessor(
                recognizer=SilentRecognizer(), store=store, relay=OpenRelay(),
                authorised={"131D2696"}, cooldown=timedelta(seconds=20),
                outbox=None, clock=lambda: self.NOW,
            )
            result = processor.record_skipped((frame,), reason)
            payload = store.event_payload(result.event_id)
            telemetry = store.event_telemetry(result.event_id)
        payload["controller_id"] = "primary"
        return result, payload, telemetry

    def _assert_non_deciding(self, reason, **kwargs):
        result, payload, telemetry = self._skip(reason, **kwargs)

        self.assertEqual(result.reason, reason)
        self.assertEqual(payload["reason"], reason)
        self.assertFalse(payload["opened"])
        self.assertIsNone(payload["observed_plate"])
        self.assertEqual(telemetry["ocr_attempts"], [], "no reader ran on this frame")
        # The defect the app has to fix: a required number with nothing behind
        # it. `ocr_attempts == []` is the only signal that the zero is not a
        # measurement, and it is already on the wire today.
        self.assertEqual(payload["ocr_confidence"], 0.0)
        assert_ingest_accepts_event(payload)

    def test_queue_coalesced_frame_carries_a_zero_no_reader_measured(self):
        self._assert_non_deciding("queue_coalesced")

    def test_decision_timeout_frame_carries_a_zero_no_reader_measured(self):
        self._assert_non_deciding("decision_timeout")

    def test_upload_incomplete_frame_carries_a_zero_no_reader_measured(self):
        self._assert_non_deciding("upload_incomplete", truncated=True)

    def test_the_contract_rejects_a_null_confidence_so_the_app_must_move_first(self):
        """Why item 2 of the defect cannot be fixed on this side of the wire."""
        _result, payload, _telemetry = self._skip("queue_coalesced")

        payload["ocr_confidence"] = None
        with self.assertRaises(IngestRejected):
            assert_ingest_accepts_event(payload)

        payload.pop("ocr_confidence")
        with self.assertRaises(IngestRejected):
            assert_ingest_accepts_event(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
