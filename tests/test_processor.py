import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PIL import Image

from gate_controller.models import PlateObservation, RelayResult
from gate_controller.outbox import EvidenceSpool, OutboxWorker
from gate_controller.processor import GateProcessor
from gate_controller.store import LocalStore
from gate_controller.authorisation import AuthorisedPlateCache
from gate_controller.images import rank_images
from gate_controller.telemetry import ProcessingTrace


class StaticRecognizer:
    def __init__(self, observation=None, error=None):
        self.observation = observation
        self.error = error

    def recognise(self, path):
        if self.error:
            raise self.error
        return self.observation


class SequenceRecognizer:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def recognise(self, path):
        self.calls.append(path)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class MutableClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class RecordingRelay:
    def __init__(self, calls):
        self.calls = calls

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(
                    activated=False,
                    reason=inhibition[1],
                    idempotency_key=idempotency_key,
                )
        self.calls.append("relay")
        return RelayResult(activated=True, reason="activated", idempotency_key=idempotency_key)


class RecordingStore(LocalStore):
    def __init__(self, path, calls):
        super().__init__(path)
        self.calls = calls

    def claim_actuation(self, *args, **kwargs):
        self.calls.append("claim")
        return super().claim_actuation(*args, **kwargs)

    def finalize_actuation(self, *args, **kwargs):
        self.calls.append("finalize")
        return super().finalize_actuation(*args, **kwargs)


class RecordingOutbox:
    def __init__(self, calls):
        self.calls = calls

    def enqueue(self, event_id):
        self.calls.append("outbox")


class FailingFinalizeStore(LocalStore):
    def finalize_actuation(self, claim, event):
        raise sqlite3.OperationalError("database unavailable after activation")


class GateProcessorTests(unittest.TestCase):
    def _processor(self, store, relay, recognizer, outbox=None, clock=None,
                   cooldown=timedelta(seconds=20), **kwargs):
        return GateProcessor(
            recognizer=recognizer,
            store=store,
            relay=relay,
            authorised={"12D3456"},
            cooldown=cooldown,
            outbox=outbox,
            clock=clock or (lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)),
            **kwargs,
        )

    def _jpeg(self, directory: str, name: str, colour: int = 128) -> Path:
        path = Path(directory) / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def test_early_allow_traces_one_ocr_attempt_and_decision_to_relay_time(self):
        trace_clock = SequenceClock((10.0, 10.01, 10.02, 10.07, 10.08, 10.11, 10.12))
        recognizer = SequenceRecognizer([
            PlateObservation("12D3456", 0.95, make="Ford", colour="Blue"),
            TimeoutError("must not be called"),
        ])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frames = (
                self._jpeg(directory, "first.jpg", 32),
                self._jpeg(directory, "second.jpg", 224),
            )
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
                telemetry_clock=trace_clock,
            ).process(frames)

        wire = result.telemetry.to_wire()
        self.assertTrue(result.opened)
        self.assertEqual(recognizer.calls, [frames[0]])
        self.assertEqual(relay_calls, ["relay"])
        self.assertEqual(wire["stage_durations"], {
            "capture_to_burst_ms": 10,
            "burst_to_ocr_ms": 10,
            "ocr_ms": 50,
            "decision_ms": 10,
            "decision_to_relay_ms": 30,
            "end_to_end_ms": 120,
        })
        self.assertEqual(len(wire["frames"]), 1)
        self.assertEqual(wire["ocr_attempts"], [{
            "frame_sequence": 0,
            "duration_ms": 50,
            "status": "matched",
            "plate": "12D3456",
            "confidence": 0.95,
            "make": "Ford",
            "colour": "Blue",
        }])
        self.assertEqual(wire["decision"], {"outcome": "allowed", "reason": "exact_match"})
        self.assertEqual(wire["actuation"], {
            "claim": "claimed",
            "attempted": True,
            "relay_outcome": "activated",
        })

    def test_denied_no_match_accumulates_completed_ocr_work_without_relay(self):
        trace_clock = SequenceClock(
            (20.0, 20.01, 20.02, 20.05, 20.06, 20.10, 20.11, 20.12)
        )
        recognizer = SequenceRecognizer([
            PlateObservation(None, 0.0),
            PlateObservation("NOPE", 0.8, make="Unknown", colour="Silver"),
        ])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frames = (
                self._jpeg(directory, "first.jpg", 64),
                self._jpeg(directory, "second.jpg", 192),
            )
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
                telemetry_clock=trace_clock,
            ).process(frames)

        wire = result.telemetry.to_wire()
        self.assertEqual(result.reason, "no_match")
        self.assertEqual(recognizer.calls, list(frames))
        self.assertEqual(relay_calls, [])
        self.assertEqual(wire["stage_durations"]["ocr_ms"], 70)
        self.assertEqual(wire["stage_durations"]["decision_ms"], 10)
        self.assertNotIn("decision_to_relay_ms", wire["stage_durations"])
        self.assertEqual(wire["decision"], {"outcome": "denied", "reason": "no_match"})
        self.assertEqual(wire["ocr_attempts"][1]["make"], "Unknown")
        self.assertEqual(wire["ocr_attempts"][1]["colour"], "Silver")

    def test_ocr_exception_closes_the_started_attempt_without_leaking_details(self):
        trace_clock = SequenceClock((30.0, 30.01, 30.02, 30.09, 30.10, 30.11))
        recognizer = SequenceRecognizer([TimeoutError("secret host / private/path")])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "error.jpg")
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
                telemetry_clock=trace_clock,
            ).process((frame,))

        wire = result.telemetry.to_wire()
        self.assertEqual(result.reason, "ocr_error")
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(relay_calls, [])
        self.assertEqual(wire["stage_durations"]["ocr_ms"], 70)
        self.assertEqual(wire["ocr_attempts"][0]["status"], "ocr_error")
        self.assertNotIn("secret", str(wire))
        self.assertNotIn("private/path", str(wire))

    def test_quality_error_is_bounded_without_blocking_ocr_or_relay(self):
        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.95)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private-frame.jpg"
            frame.write_bytes(b"not a jpeg: do not expose this payload")
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
            ).process((frame,))

        attempt = result.telemetry.to_wire()["ocr_attempts"][0]
        self.assertTrue(result.opened)
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(relay_calls, ["relay"])
        self.assertEqual(attempt["status"], "quality_unavailable")
        self.assertNotIn(str(frame), str(attempt))
        self.assertNotIn("do not expose", str(attempt))

    def test_timeout_after_ocr_returns_a_finished_denied_trace_without_relay(self):
        decision_clock = MutableClock()

        class LateRecognizer(SequenceRecognizer):
            def recognise(self, path, timeout=None):
                result = super().recognise(path)
                decision_clock.value = 4.1
                return result

        trace_clock = SequenceClock((40.0, 40.01, 40.02, 40.08, 40.09, 40.10))
        recognizer = LateRecognizer([PlateObservation("12D3456", 0.99)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "late.jpg")
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
                decision_timeout=4.0,
                decision_clock=decision_clock,
                telemetry_clock=trace_clock,
            ).process((frame,))

        wire = result.telemetry.to_wire()
        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(relay_calls, [])
        self.assertEqual(wire["decision"], {
            "outcome": "denied",
            "reason": "decision_timeout",
        })
        self.assertEqual(wire["ocr_attempts"][0]["duration_ms"], 60)

    def test_stale_burst_has_zero_ocr_work_and_no_recognizer_or_relay_calls(self):
        wall_now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        trace_clock = SequenceClock((50.0, 50.01, 50.02, 50.03))
        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.99)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "stale.jpg")
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
                clock=lambda: wall_now,
                max_image_age=timedelta(seconds=5),
                telemetry_clock=trace_clock,
            ).process((frame,), received_at=wall_now - timedelta(seconds=6))

        wire = result.telemetry.to_wire()
        self.assertEqual(result.reason, "stale_burst")
        self.assertEqual(recognizer.calls, [])
        self.assertEqual(relay_calls, [])
        self.assertEqual(wire["stage_durations"]["ocr_ms"], 0)
        self.assertEqual(wire["ocr_attempts"], [])
        self.assertEqual(wire["decision"], {"outcome": "denied", "reason": "stale_burst"})

    def test_duplicate_event_does_not_create_a_second_trace_or_call_hardware(self):
        created_traces = []

        def trace_factory(**kwargs):
            trace = ProcessingTrace(**kwargs)
            created_traces.append(trace)
            return trace

        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.95)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "duplicate.jpg")
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(relay_calls),
                recognizer,
                trace_factory=trace_factory,
            )

            first = processor.process((frame,))
            duplicate = processor.process((frame,))

        self.assertIsNotNone(first.telemetry)
        self.assertIsNone(duplicate.telemetry)
        self.assertEqual(len(created_traces), 1)
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(relay_calls, ["relay"])

    def test_activates_relay_before_persisting_or_queuing_optional_work(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            store = RecordingStore(Path(directory) / "gate.db", calls)
            processor = self._processor(
                store,
                RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
                RecordingOutbox(calls),
            )

            result = processor.process((Path("first.jpg"),))

            self.assertTrue(result.opened)
            self.assertEqual(calls, ["claim", "relay", "finalize"])
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_duplicate_completed_upload_does_not_reactivate_the_relay(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store,
                RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            first = processor.process((Path("first.jpg"),))
            second = processor.process((Path("first.jpg"),))

            self.assertTrue(first.opened)
            self.assertFalse(second.opened)
            self.assertEqual(second.reason, "duplicate_event")
            self.assertEqual(calls, ["relay"])

    def test_ocr_error_fails_closed_without_relay_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay(calls),
                StaticRecognizer(error=TimeoutError("OCR unavailable")),
            )

            result = processor.process((Path("first.jpg"),))

            self.assertFalse(result.opened)
            self.assertEqual(result.reason, "ocr_error")
            self.assertIsNotNone(result.event_id)
            self.assertEqual(calls, [])

    def test_no_plate_is_recorded_as_a_denied_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay([]), StaticRecognizer(PlateObservation(None, 0.0)),
                outbox=object(),
            )

            result = processor.process((Path("empty.jpg"),))

            self.assertEqual(result.reason, "no_match")
            self.assertIsNotNone(result.event_id)
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_stale_burst_is_recorded_without_calling_ocr_or_relay(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.99)])
            calls = []
            processor = self._processor(
                store, RecordingRelay(calls), recognizer, outbox=object(), clock=lambda: now,
                max_image_age=timedelta(seconds=5),
            )

            result = processor.process(
                (Path("old.jpg"),), received_at=now - timedelta(seconds=6)
            )

            self.assertEqual(result.reason, "stale_burst")
            self.assertIsNotNone(result.event_id)
            self.assertEqual(recognizer.calls, [])
            self.assertEqual(calls, [])

    def test_duplicate_image_content_cannot_corroborate_a_fuzzy_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            duplicate = root / "duplicate.jpg"
            first.write_bytes(b"same frame")
            duplicate.write_bytes(b"same frame")
            recognizer = SequenceRecognizer([
                PlateObservation("12O3456", 0.97),
                PlateObservation("12O3456", 0.98),
            ])
            processor = GateProcessor(
                recognizer=recognizer, store=LocalStore(root / "gate.db"),
                relay=RecordingRelay([]), authorised={"1203456"},
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            )

            result = processor.process((first, duplicate))

            self.assertFalse(result.opened)
            self.assertEqual(result.reason, "no_match")
            self.assertEqual(recognizer.calls, [first])

    def test_overall_decision_deadline_stops_later_ocr_calls(self):
        class AdvancingRecognizer(SequenceRecognizer):
            def __init__(self, clock):
                super().__init__([PlateObservation(None, 0.0)] * 3)
                self.clock = clock
                self.timeouts = []

            def recognise(self, path, timeout=None):
                self.timeouts.append(timeout)
                result = super().recognise(path)
                self.clock.value += 2.1
                return result

        decision_clock = MutableClock()
        recognizer = AdvancingRecognizer(decision_clock)
        with tempfile.TemporaryDirectory() as directory:
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay([]), recognizer,
                decision_timeout=4.0, decision_clock=decision_clock,
            )

            result = processor.process(tuple(Path(f"frame-{index}.jpg") for index in range(3)))

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(len(recognizer.calls), 2)
        self.assertLessEqual(sum(sum(timeout) for timeout in recognizer.timeouts), 8.0)

    def test_preprocessing_time_consumes_the_event_decision_deadline(self):
        decision_clock = MutableClock()
        decision_clock.value = 4.1
        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.99)])
        with tempfile.TemporaryDirectory() as directory:
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay([]), recognizer,
                decision_timeout=4.0, decision_clock=decision_clock,
            ).process((Path("ranked.jpg"),), decision_started_at=0.0)

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(recognizer.calls, [])

    def test_exact_result_returned_after_deadline_does_not_open(self):
        decision_clock = MutableClock()

        class LateExactRecognizer:
            def recognise(self, path, timeout=None):
                decision_clock.value = 4.1
                return PlateObservation("12D3456", 0.99)

        calls = []
        with tempfile.TemporaryDirectory() as directory:
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay(calls),
                LateExactRecognizer(), decision_timeout=4.0, decision_clock=decision_clock,
            ).process((Path("late.jpg"),))

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(calls, [])

    def test_burst_that_becomes_stale_during_ocr_does_not_open(self):
        captured_at = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        now = [captured_at + timedelta(seconds=4)]

        class SlowExactRecognizer:
            def recognise(self, path, timeout=None):
                now[0] += timedelta(seconds=2)
                return PlateObservation("12D3456", 0.99)

        calls = []
        with tempfile.TemporaryDirectory() as directory:
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay(calls),
                SlowExactRecognizer(), clock=lambda: now[0],
                max_image_age=timedelta(seconds=5),
            ).process((Path("aging.jpg"),), received_at=captured_at)

        self.assertEqual(result.reason, "stale_burst")
        self.assertEqual(calls, [])

    def test_burst_that_becomes_stale_while_staging_evidence_does_not_open(self):
        captured_at = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        now = [captured_at + timedelta(seconds=4)]

        class SlowEvidenceOutbox:
            def prepare_payload(self, image_path=None):
                now[0] += timedelta(seconds=2)
                return {"event_id": None}

        calls = []
        with tempfile.TemporaryDirectory() as directory:
            store = RecordingStore(Path(directory) / "gate.db", calls)
            result = self._processor(
                store, RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.99)),
                outbox=SlowEvidenceOutbox(), clock=lambda: now[0],
                max_image_age=timedelta(seconds=5),
            ).process((Path("aging.jpg"),), received_at=captured_at)

        self.assertEqual(result.reason, "stale_burst")
        self.assertEqual(calls, [])

    def test_burst_that_becomes_stale_after_claim_but_before_gpio_does_not_open(self):
        captured_at = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        now = [captured_at + timedelta(seconds=4)]
        calls = []

        class SlowClaimStore(RecordingStore):
            def mark_actuation_attempt(self, *args, **kwargs):
                result = super().mark_actuation_attempt(*args, **kwargs)
                now[0] += timedelta(seconds=2)
                return result

        with tempfile.TemporaryDirectory() as directory:
            store = SlowClaimStore(Path(directory) / "gate.db", calls)
            result = self._processor(
                store,
                RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.99)),
                clock=lambda: now[0],
                max_image_age=timedelta(seconds=5),
            ).process((Path("aging.jpg"),), received_at=captured_at)

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "stale_burst")
        self.assertNotIn("relay", calls)

    def test_burst_from_the_future_fails_closed_after_wall_clock_rollback(self):
        received_at = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        now = received_at - timedelta(seconds=30)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.99)), clock=lambda: now,
            ).process((Path("future.jpg"),), received_at=received_at)

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "stale_burst")
        self.assertEqual(calls, [])

    def test_authorisation_is_revalidated_under_relay_lock_before_gpio(self):
        snapshots = iter((("12D3456",), ()))
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            processor = GateProcessor(
                recognizer=StaticRecognizer(PlateObservation("12D3456", 0.99)),
                store=LocalStore(Path(directory) / "gate.db"),
                relay=RecordingRelay(calls),
                authorised=lambda: next(snapshots),
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            )

            result = processor.process((Path("revoked.jpg"),))

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "authorisation_revoked")
        self.assertEqual(calls, [])

    def test_stops_after_the_first_sufficient_exact_ocr_result(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            recognizer = SequenceRecognizer([
                PlateObservation("12D3456", 0.95),
                TimeoutError("must not be called"),
            ])
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay(calls), recognizer
            )

            result = processor.process(tuple(Path(f"frame-{index}.jpg") for index in range(4)))

            self.assertTrue(result.opened)
            self.assertEqual(recognizer.calls, [Path("frame-0.jpg")])
            self.assertEqual(calls, ["relay"])

    def test_limits_ocr_to_the_top_three_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            recognizer = SequenceRecognizer([PlateObservation(None, 0.0)] * 4)
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay(calls), recognizer
            )

            result = processor.process(tuple(Path(f"frame-{index}.jpg") for index in range(4)))

            self.assertFalse(result.opened)
            self.assertEqual(result.reason, "no_match")
            self.assertEqual(recognizer.calls, [
                Path("frame-0.jpg"), Path("frame-1.jpg"), Path("frame-2.jpg")
            ])

    def test_failed_finalization_keeps_claim_and_does_not_repulse_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            calls = []
            first = self._processor(
                FailingFinalizeStore(database), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )
            second = self._processor(
                LocalStore(database), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            first_result = first.process((Path("first.jpg"),))
            second_result = second.process((Path("first.jpg"),))

            self.assertEqual(first_result.reason, "indeterminate_claim")
            self.assertEqual(second_result.reason, "indeterminate_claim")
            self.assertEqual(calls, ["relay"])

    def test_refreshed_authorisations_apply_additions_and_revocations_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "plates.csv"
            csv_path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            first = root / "first.jpg"
            second = root / "second.jpg"
            third = root / "third.jpg"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            third.write_bytes(b"three")
            calls = []
            now = [datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)]
            authorised = AuthorisedPlateCache(csv_path)
            processor = GateProcessor(
                recognizer=StaticRecognizer(PlateObservation("12D3456", 0.95)),
                store=LocalStore(root / "gate.db"), relay=RecordingRelay(calls),
                authorised=authorised.get,
                cooldown=timedelta(seconds=0),
                clock=lambda: now[0],
            )

            self.assertTrue(processor.process((first,)).opened)
            now[0] += timedelta(seconds=1)
            csv_path.write_text("plate,name\n", encoding="utf-8")
            authorised.reload_local()
            self.assertFalse(processor.process((second,)).opened)
            now[0] += timedelta(seconds=1)
            csv_path.write_text("plate,name\n12D3456,Ada\n", encoding="utf-8")
            authorised.reload_local()
            self.assertTrue(processor.process((third,)).opened)

            self.assertEqual(calls, ["relay", "relay"])

    def test_reused_filename_with_new_content_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "camera.jpg"
            image.write_bytes(b"first upload")
            calls = []
            now = [datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)]
            processor = self._processor(
                LocalStore(root / "gate.db"), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
                cooldown=timedelta(seconds=0),
                clock=lambda: now[0],
            )

            first = processor.process((image,))
            now[0] += timedelta(seconds=1)
            image.write_bytes(b"replacement upload")
            second = processor.process((image,))

            self.assertTrue(first.opened)
            self.assertTrue(second.opened)

    def test_reordered_frames_with_the_same_content_are_a_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            second = root / "second.jpg"
            Image.new("L", (16, 16), color=128).save(first)
            checkerboard = Image.new("L", (16, 16))
            checkerboard.putdata([0 if index % 2 else 255 for index in range(256)])
            checkerboard.save(second)
            calls = []
            processor = self._processor(
                LocalStore(root / "gate.db"), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            original = processor.process(rank_images((first, second)))
            reordered = processor.process(rank_images((second, first)))

            self.assertTrue(original.opened)
            self.assertEqual(reordered.reason, "duplicate_event")
            self.assertEqual(calls, ["relay"])

    def test_metadata_only_touch_preserves_event_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "camera.jpg"
            image.write_bytes(b"unchanged")
            calls = []
            processor = self._processor(
                LocalStore(root / "gate.db"), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            first = processor.process((image,))
            image.touch()
            replay = processor.process((image,))

            self.assertTrue(first.opened)
            self.assertEqual(replay.reason, "duplicate_event")

    def test_subset_replay_with_same_first_ranked_frame_is_a_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strongest = root / "sharp.jpg"
            extra = root / "extra.jpg"
            strongest.write_bytes(b"sharp frame")
            extra.write_bytes(b"other frame")
            calls = []
            processor = self._processor(
                LocalStore(root / "gate.db"), RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            original = processor.process((strongest, extra))
            subset = processor.process((strongest,))

            self.assertTrue(original.opened)
            self.assertEqual(subset.reason, "duplicate_event")

    def test_denied_event_and_outbox_are_recorded_together(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay([]), StaticRecognizer(PlateObservation("NOPE", 0.95)),
                outbox=object(),
            )

            result = processor.process((Path("denied.jpg"),))

            self.assertFalse(result.opened)
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_outbox_binds_an_immutable_best_image_before_queuing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            best = root / "best.jpg"
            second = root / "second.jpg"
            Image.new("RGB", (32, 32), color="red").save(best, format="JPEG")
            Image.new("RGB", (32, 32), color="blue").save(second, format="JPEG")
            spool = EvidenceSpool(root / "event-evidence")

            class BindingStore(LocalStore):
                evidence_existed_at_insert = False

                def record_event_with_outbox(self, event, outbox_payload=None):
                    digest = (outbox_payload or {}).get("image_sha256")
                    self.evidence_existed_at_insert = bool(
                        digest and (spool.root / f"{digest}.jpg").is_file()
                    )
                    return super().record_event_with_outbox(event, outbox_payload)

            store = BindingStore(root / "gate.db")
            outbox = OutboxWorker(
                store, send=lambda payload: None, evidence_spool=spool,
                controller_id="pi-front-gate",
            )
            processor = self._processor(
                store, RecordingRelay([]), StaticRecognizer(PlateObservation("NOPE", 0.95)),
                outbox=outbox,
            )

            processor.process((best, second))

            _, payload = store.pending_outbox_items()[0]
            self.assertIn("image_sha256", payload)
            digest = payload["image_sha256"]
            immutable_bytes = spool.load(digest)
            best.unlink()

            self.assertTrue(store.evidence_existed_at_insert)
            self.assertEqual(payload["controller_id"], "pi-front-gate")
            self.assertNotIn("_local_image_path", payload)
            self.assertNotIn(str(best), str(payload))
            self.assertEqual(spool.load(digest), immutable_bytes)

    def test_evidence_spool_failure_does_not_block_ocr_or_gate_actuation(self):
        class FailingSpool:
            def stage(self, source):
                raise OSError("spool full")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "camera.jpg"
            Image.new("RGB", (32, 32), color="red").save(image, format="JPEG")
            calls = []
            store = LocalStore(root / "gate.db")
            outbox = OutboxWorker(
                store, send=lambda payload: None, evidence_spool=FailingSpool()
            )
            processor = self._processor(
                store, RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)), outbox=outbox,
            )

            try:
                result = processor.process((image,))
            except Exception as error:
                self.fail(f"evidence failure escaped the delivery boundary: {error}")

            _, payload = store.pending_outbox_items()[0]
            self.assertTrue(result.opened)
            self.assertEqual(calls, ["relay"])
            self.assertEqual(payload["image_status"], "unavailable_before_queue")
            self.assertNotIn("image_sha256", payload)

    def test_replay_repairs_missing_outbox_row_for_legacy_finalized_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay([]), StaticRecognizer(PlateObservation("12D3456", 0.95))
            )
            path = Path(directory) / "legacy.jpg"
            path.write_bytes(b"legacy upload")

            self.assertTrue(processor.process((path,)).opened)
            repaired = self._processor(
                store, RecordingRelay([]), StaticRecognizer(PlateObservation("12D3456", 0.95)),
                outbox=object(),
            ).process((path,))

            self.assertEqual(repaired.reason, "duplicate_event")
            self.assertEqual(store.pending_outbox_count(), 1)


if __name__ == "__main__":
    unittest.main()
