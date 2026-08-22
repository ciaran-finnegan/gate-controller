import hashlib
import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue as ThreadQueue
from threading import Event, Lock, Thread
from time import monotonic
from unittest.mock import patch
from PIL import Image

import gate_controller.images as image_tools
import gate_controller.processor as processor_module
from gate_controller.models import PlateObservation, RelayResult
from gate_controller.outbox import EvidenceSpool, OutboxWorker
from gate_controller.processor import GateProcessor
from gate_controller.relay import RelayController
from gate_controller.store import LocalStore
from gate_controller.authorisation import AuthorisedPlateCache
from gate_controller.images import rank_images
from gate_controller.telemetry import FrameTelemetry, ProcessingTrace, TriggerTelemetry


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

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None,
                on_activation=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(
                    activated=False,
                    reason=inhibition[1],
                    idempotency_key=idempotency_key,
                )
        self.calls.append("relay")
        if on_activation is not None:
            on_activation()
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


class FailingTelemetryTrace:
    def __init__(self, failure, **kwargs):
        self._failure = failure
        self._trace = ProcessingTrace(**kwargs)

    def _call(self, operation, *args, **kwargs):
        if self._failure == operation:
            raise RuntimeError(f"{operation} telemetry failed")
        return getattr(self._trace, operation)(*args, **kwargs)

    def mark_burst(self):
        return self._call("mark_burst")

    def seed_upstream(self, received_at, decision_started_at, processing_started_at=None):
        return self._call(
            "seed_upstream", received_at, decision_started_at, processing_started_at
        )

    def add_frame(self, frame):
        return self._call("add_frame", frame)

    def mark_ocr_start(self):
        return self._call("mark_ocr_start")

    def add_ocr_attempt(self, attempt):
        return self._call("add_ocr_attempt", attempt)

    def mark_decision(self, outcome, reason):
        return self._call("mark_decision", outcome, reason)

    def mark_relay_activation(self):
        return self._call("mark_relay_activation")

    def set_actuation_outcome(self, claim, attempted, relay_outcome):
        return self._call("set_actuation_outcome", claim, attempted, relay_outcome)

    def mark_actuation(self, claim, attempted, relay_outcome):
        if self._failure in {"mark_relay_activation", "set_actuation_outcome"}:
            raise RuntimeError(f"{self._failure} telemetry failed")
        return self._trace.mark_actuation(claim, attempted, relay_outcome)

    def finish(self):
        return self._call("finish")


class GateProcessorTests(unittest.TestCase):
    def test_trigger_provenance_is_attached_without_changing_the_gate_decision(self):
        relay_calls = []
        trigger = TriggerTelemetry(
            source="reolink_webhook", event_type="line_crossing",
            rule_id="line_crossing_inbound", correlation="matched",
            delta_ms=25,
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "event.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay(relay_calls),
                StaticRecognizer(PlateObservation(None, 0.0)),
            )

            result = processor.process((frame,), trigger=trigger)

            self.assertFalse(result.opened)
            self.assertEqual(
                store.event_telemetry(result.event_id)["trigger"],
                trigger.to_wire(),
            )
            self.assertEqual(relay_calls, [])

    def test_post_correlation_skip_preserves_the_matched_trigger(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        matched = TriggerTelemetry(
            source="reolink_webhook", event_type="line_crossing",
            rule_id="line_crossing_inbound", correlation="matched",
            delta_ms=125,
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "stale-after-correlation.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay([]), SequenceRecognizer([]),
                clock=lambda: now,
            )

            result = processor.process(
                (frame,), received_at=now - timedelta(seconds=9),
                trigger=matched,
            )

            self.assertEqual(result.reason, "stale_burst")
            self.assertEqual(
                store.event_telemetry(result.event_id)["trigger"],
                matched.to_wire(),
            )

    def test_record_skipped_preserves_a_supplied_matched_trigger_without_a_trace(self):
        matched = TriggerTelemetry(
            source="reolink_webhook", event_type="line_crossing",
            rule_id="line_crossing_inbound", correlation="matched",
            delta_ms=25,
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "failed-after-correlation.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay([]), SequenceRecognizer([]),
            )

            result = processor.record_skipped(
                (frame,), "processing_error", trigger=matched,
            )

            self.assertEqual(store.event_telemetry(result.event_id)["trigger"], {
                "source": "reolink_webhook",
                "event_type": "line_crossing",
                "rule_id": "line_crossing_inbound",
                "correlation": "matched",
                "delta_ms": 25,
            })

    def test_direct_pre_ocr_skip_persists_the_exact_fallback_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "rejected-before-ocr.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store, RecordingRelay([]), SequenceRecognizer([]),
            )

            result = processor.record_skipped((frame,), "image_too_large")

            self.assertEqual(store.event_telemetry(result.event_id)["trigger"], {
                "source": "camera_ftp",
                "event_type": "unverified",
                "correlation": "unverified",
            })

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

    def test_completed_trace_log_correlates_sanitized_stage_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "private-owner-registration.jpg")
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay([]),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            with self.assertLogs("gate_controller.processor", level="INFO") as logs:
                result = processor.process((frame,))

        combined = "\n".join(logs.output)
        self.assertIn(
            f"gate_pipeline stage=processing_finished "
            f"trace_id={result.telemetry.trace_id}",
            combined,
        )
        self.assertIn("outcome=allowed reason=exact_match", combined)
        self.assertIn('ocr_attempts=[{"duration_ms":', combined)
        self.assertNotIn(str(frame), combined)
        self.assertNotIn("12D3456", combined)

    def test_rejects_nonfinite_or_nonpositive_decision_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            for timeout in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
                with self.subTest(timeout=timeout), self.assertRaisesRegex(
                    ValueError, "decision timeout must be finite and greater than zero"
                ):
                    self._processor(
                        store, RecordingRelay([]), SequenceRecognizer([]),
                        decision_timeout=timeout,
                    )

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
            "status": "recognized",
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

    def test_upstream_quiet_window_and_preprocessing_are_in_stage_durations(self):
        captured_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        wall = [captured_at]
        monotonic_clock = MutableClock()
        monotonic_clock.value = 100.0

        def advance(seconds):
            monotonic_clock.value += seconds
            wall[0] += timedelta(seconds=seconds)

        advance(0.5)
        decision_started_at = monotonic_clock.value
        advance(0.2)

        original_digest = processor_module._content_digest
        original_quality = processor_module.measure_frame_quality

        def timed_digest(path):
            advance(0.3)
            return original_digest(path)

        def timed_quality(path, **kwargs):
            advance(0.1)
            return original_quality(path, **kwargs)

        class TimedRecognizer(SequenceRecognizer):
            def recognise(self, path):
                result = super().recognise(path)
                advance(0.05)
                return result

        recognizer = TimedRecognizer([PlateObservation("12D3456", 0.95)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "upstream.jpg")
            with patch(
                "gate_controller.processor._content_digest", side_effect=timed_digest
            ), patch(
                "gate_controller.processor.measure_frame_quality", side_effect=timed_quality
            ):
                result = self._processor(
                    LocalStore(Path(directory) / "gate.db"),
                    RecordingRelay(relay_calls),
                    recognizer,
                    clock=lambda: wall[0],
                    decision_clock=monotonic_clock,
                    telemetry_clock=monotonic_clock,
                    telemetry_wall_clock=lambda: wall[0],
                ).process(
                    (frame,),
                    received_at=captured_at,
                    decision_started_at=decision_started_at,
                )

        self.assertTrue(result.opened)
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(relay_calls, ["relay"])
        self.assertEqual(result.telemetry.to_wire()["stage_durations"], {
            "capture_to_burst_ms": 500,
            "burst_to_ocr_ms": 600,
            "ocr_ms": 50,
            "decision_ms": 0,
            "decision_to_relay_ms": 0,
            "end_to_end_ms": 1_150,
            "filesystem_ingress_to_decision_ms": 1_150,
            "filesystem_ingress_to_relay_ms": 1_150,
        })

    def test_pre_ranking_wall_boundary_survives_clock_step_in_persisted_telemetry(self):
        processing_started_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        stepped_wall = processing_started_at - timedelta(minutes=5)
        monotonic_clock = MutableClock()
        monotonic_clock.value = 104.0

        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "wall-step.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            result = self._processor(
                store,
                RecordingRelay([]),
                StaticRecognizer(PlateObservation("NOPE", 0.95)),
                outbox=object(),
                clock=lambda: processing_started_at + timedelta(seconds=4),
                decision_clock=monotonic_clock,
                telemetry_clock=monotonic_clock,
                telemetry_wall_clock=lambda: stepped_wall,
            ).process(
                (frame,),
                received_at=processing_started_at - timedelta(seconds=1),
                decision_started_at=100.0,
                processing_started_at=processing_started_at,
            )

            persisted = store.event_telemetry(result.event_id)

        self.assertEqual(
            persisted["stage_timestamps"]["burst_processing_started_at"],
            processing_started_at.isoformat(),
        )

    def test_queue_coalesced_skip_persists_the_exact_pre_ranking_wall_boundary(self):
        processing_started_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        stepped_wall = processing_started_at - timedelta(minutes=5)
        monotonic_clock = MutableClock()
        monotonic_clock.value = 104.0

        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "coalesced-wall-step.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            result = self._processor(
                store,
                RecordingRelay([]),
                SequenceRecognizer([]),
                outbox=object(),
                clock=lambda: processing_started_at + timedelta(seconds=4),
                decision_clock=monotonic_clock,
                telemetry_clock=monotonic_clock,
                telemetry_wall_clock=lambda: stepped_wall,
            ).record_skipped(
                (frame,),
                "queue_coalesced",
                processing_started_at - timedelta(seconds=1),
                decision_started_at=100.0,
                processing_started_at=processing_started_at,
            )

            persisted = store.event_telemetry(result.event_id)

        self.assertEqual(
            persisted["stage_timestamps"]["burst_processing_started_at"],
            processing_started_at.isoformat(),
        )

    def test_upstream_seed_failure_is_best_effort(self):
        captured_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        monotonic_clock = MutableClock()
        monotonic_clock.value = 10.5
        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.95)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "seed-failure.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            result = self._processor(
                store,
                RecordingRelay(relay_calls),
                recognizer,
                outbox=object(),
                clock=lambda: captured_at + timedelta(seconds=0.5),
                decision_clock=monotonic_clock,
                telemetry_clock=monotonic_clock,
                telemetry_wall_clock=lambda: captured_at + timedelta(seconds=0.5),
                trace_factory=lambda **kwargs: FailingTelemetryTrace(
                    "seed_upstream", **kwargs
                ),
            ).process(
                (frame,), received_at=captured_at, decision_started_at=10.5
            )

            self.assertTrue(result.opened)
            self.assertEqual(result.reason, "exact_match")
            self.assertIsNone(result.telemetry)
            self.assertEqual(recognizer.calls, [frame])
            self.assertEqual(relay_calls, ["relay"])
            self.assertEqual(store.pending_outbox_count(), 1)

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

        wire = result.telemetry.to_wire()
        attempt = wire["ocr_attempts"][0]
        self.assertTrue(result.opened)
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(relay_calls, ["relay"])
        self.assertEqual(wire["frames"][0]["status"], "quality_unavailable")
        self.assertEqual(attempt["status"], "recognized")
        self.assertNotIn(str(frame), str(attempt))
        self.assertNotIn("do not expose", str(attempt))

    def test_decision_to_relay_ends_at_activation_before_pulse_and_finalization(self):
        trace_clock = MutableClock()
        trace_clock.value = 10.0
        calls = []

        class BoundaryBackend:
            def on(self):
                calls.append("on")
                trace_clock.value = 10.08

            def off(self):
                calls.append("off")

        class AdvancingRecognizer(SequenceRecognizer):
            def recognise(self, path):
                result = super().recognise(path)
                trace_clock.value = 10.05
                return result

        class AdvancingStore(LocalStore):
            def finalize_actuation(self, *args, **kwargs):
                calls.append("finalize")
                trace_clock.value = 10.88
                return super().finalize_actuation(*args, **kwargs)

        def pulse(seconds):
            calls.append(("sleep", seconds))
            trace_clock.value = 10.58

        recognizer = AdvancingRecognizer([PlateObservation("12D3456", 0.95)])
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "activation.jpg")
            store = AdvancingStore(Path(directory) / "gate.db")
            relay = RelayController(
                BoundaryBackend(), pulse_seconds=2, sleeper=pulse, clock=lambda: now
            )
            result = self._processor(
                store,
                relay,
                recognizer,
                clock=lambda: now,
                telemetry_clock=trace_clock,
            ).process((frame,))

        durations = result.telemetry.to_wire()["stage_durations"]
        self.assertTrue(result.opened)
        self.assertEqual(recognizer.calls, [frame])
        self.assertEqual(calls, ["off", "on", ("sleep", 2), "off", "finalize"])
        self.assertEqual(durations["decision_to_relay_ms"], 30)
        self.assertEqual(durations["end_to_end_ms"], 880)

    def test_trace_factory_failure_does_not_change_successful_processing(self):
        def fail_factory(**kwargs):
            raise RuntimeError("trace creation failed")

        self._assert_success_survives_telemetry_failure(trace_factory=fail_factory)

    def test_quality_measurement_failure_does_not_change_successful_processing(self):
        with patch(
            "gate_controller.processor.measure_frame_quality",
            side_effect=RuntimeError("quality failed"),
        ):
            self._assert_success_survives_telemetry_failure()

    def test_trace_mark_failures_do_not_change_successful_processing(self):
        for operation in (
            "mark_burst",
            "add_frame",
            "mark_ocr_start",
            "add_ocr_attempt",
            "mark_decision",
        ):
            with self.subTest(operation=operation):
                self._assert_success_survives_telemetry_failure(
                    trace_factory=lambda operation=operation, **kwargs: FailingTelemetryTrace(
                        operation, **kwargs
                    )
                )

    def test_trace_finish_failure_does_not_suppress_the_processing_result(self):
        self._assert_success_survives_telemetry_failure(
            trace_factory=lambda **kwargs: FailingTelemetryTrace("finish", **kwargs)
        )

    def test_telemetry_persistence_failure_does_not_change_terminal_result(self):
        class FailingTelemetryStore(LocalStore):
            def attach_event_telemetry(self, event_id, telemetry):
                raise sqlite3.OperationalError("telemetry disk full")

        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "frame.jpg")
            store = FailingTelemetryStore(Path(directory) / "gate.db")
            relay_calls = []

            result = self._processor(
                store,
                RecordingRelay(relay_calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
                outbox=object(),
            ).process((frame,))

            self.assertTrue(result.opened)
            self.assertEqual(result.reason, "exact_match")
            self.assertIsNotNone(result.event_id)
            self.assertIsNotNone(result.telemetry)
            self.assertEqual(relay_calls, ["relay"])
            self.assertEqual(store.event_payload(result.event_id)["reason"], "exact_match")

    def test_terminal_result_telemetry_is_attached_to_its_event(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "frame.jpg")
            store = LocalStore(Path(directory) / "gate.db")

            result = self._processor(
                store,
                RecordingRelay([]),
                StaticRecognizer(PlateObservation("NOPE", 0.95)),
                outbox=object(),
            ).process((frame,))

            self.assertEqual(
                store.event_telemetry(result.event_id)["trace_id"],
                result.telemetry.trace_id,
            )

    def test_after_relay_telemetry_failure_does_not_change_successful_processing(self):
        for operation in ("mark_relay_activation", "set_actuation_outcome"):
            with self.subTest(operation=operation):
                self._assert_success_survives_telemetry_failure(
                    trace_factory=lambda operation=operation, **kwargs: FailingTelemetryTrace(
                        operation, **kwargs
                    )
                )

    def _assert_success_survives_telemetry_failure(self, trace_factory=None):
        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.95)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "frame.jpg")
            store = LocalStore(Path(directory) / "gate.db")
            kwargs = {} if trace_factory is None else {"trace_factory": trace_factory}
            result = self._processor(
                store,
                RecordingRelay(relay_calls),
                recognizer,
                outbox=object(),
                **kwargs,
            ).process((frame,))

            self.assertTrue(result.opened)
            self.assertEqual(result.reason, "exact_match")
            self.assertIsNotNone(result.event_id)
            self.assertIsNone(result.telemetry)
            self.assertEqual(recognizer.calls, [frame])
            self.assertEqual(relay_calls, ["relay"])
            self.assertEqual(store.pending_outbox_count(), 1)

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

    def test_preprocessing_timeout_retains_upstream_terminal_durations(self):
        captured_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        wall = [captured_at]
        monotonic_clock = MutableClock()
        monotonic_clock.value = 200.0

        def advance(seconds):
            monotonic_clock.value += seconds
            wall[0] += timedelta(seconds=seconds)

        advance(0.5)
        decision_started_at = monotonic_clock.value
        advance(3.3)
        original_digest = processor_module._content_digest

        def timed_digest(path):
            advance(0.8)
            return original_digest(path)

        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.99)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "timeout.jpg")
            with patch(
                "gate_controller.processor._content_digest", side_effect=timed_digest
            ):
                result = self._processor(
                    LocalStore(Path(directory) / "gate.db"),
                    RecordingRelay(relay_calls),
                    recognizer,
                    clock=lambda: wall[0],
                    decision_clock=monotonic_clock,
                    telemetry_clock=monotonic_clock,
                    telemetry_wall_clock=lambda: wall[0],
                ).process(
                    (frame,),
                    received_at=captured_at,
                    decision_started_at=decision_started_at,
                )

        durations = result.telemetry.to_wire()["stage_durations"]
        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(recognizer.calls, [])
        self.assertEqual(relay_calls, [])
        self.assertEqual(durations, {
            "capture_to_burst_ms": 500,
            "ocr_ms": 0,
            "end_to_end_ms": 4_600,
            "filesystem_ingress_to_decision_ms": 4_600,
        })

    def test_preprocessing_stale_path_retains_upstream_terminal_durations(self):
        captured_at = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
        wall = [captured_at]
        monotonic_clock = MutableClock()
        monotonic_clock.value = 300.0

        def advance(seconds):
            monotonic_clock.value += seconds
            wall[0] += timedelta(seconds=seconds)

        advance(0.5)
        decision_started_at = monotonic_clock.value
        advance(5.3)
        original_digest = processor_module._content_digest

        def timed_digest(path):
            advance(0.4)
            return original_digest(path)

        recognizer = SequenceRecognizer([PlateObservation("12D3456", 0.99)])
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "stale-upstream.jpg")
            with patch(
                "gate_controller.processor._content_digest", side_effect=timed_digest
            ):
                result = self._processor(
                    LocalStore(Path(directory) / "gate.db"),
                    RecordingRelay(relay_calls),
                    recognizer,
                    clock=lambda: wall[0],
                    max_image_age=timedelta(seconds=5),
                    decision_timeout=10.0,
                    decision_clock=monotonic_clock,
                    telemetry_clock=monotonic_clock,
                    telemetry_wall_clock=lambda: wall[0],
                ).process(
                    (frame,),
                    received_at=captured_at,
                    decision_started_at=decision_started_at,
                )

        durations = result.telemetry.to_wire()["stage_durations"]
        self.assertEqual(result.reason, "stale_burst")
        self.assertEqual(recognizer.calls, [])
        self.assertEqual(relay_calls, [])
        self.assertEqual(durations, {
            "capture_to_burst_ms": 500,
            "ocr_ms": 0,
            "end_to_end_ms": 6_200,
            "filesystem_ingress_to_decision_ms": 6_200,
        })

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

    def test_direct_nonduplicate_skips_each_create_one_terminal_trace(self):
        created_traces = []

        def trace_factory(**kwargs):
            trace = ProcessingTrace(**kwargs)
            created_traces.append(trace)
            return trace

        recognizer = SequenceRecognizer([])
        relay_calls = []
        reasons = (
            "candidate_coalesced",
            "upload_incomplete",
            "image_too_large",
            "queue_coalesced",
            "stale_startup",
            "service_stopping",
            "processing_error",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            processor = self._processor(
                store,
                RecordingRelay(relay_calls),
                recognizer,
                trace_factory=trace_factory,
            )
            results = [
                processor.record_skipped(
                    (self._jpeg(directory, f"{index}.jpg", 32 + index),), reason
                )
                for index, reason in enumerate(reasons)
            ]

        self.assertEqual(len(created_traces), len(reasons))
        self.assertEqual(recognizer.calls, [])
        self.assertEqual(relay_calls, [])
        for result, reason in zip(results, reasons):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.telemetry.to_wire()["decision"], {
                "outcome": "denied",
                "reason": reason,
            })

    def test_direct_skip_trace_factory_failure_still_records_the_event(self):
        def fail_factory(**kwargs):
            raise RuntimeError("trace creation failed")

        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            frame = self._jpeg(directory, "skipped.jpg")
            result = self._processor(
                store,
                RecordingRelay([]),
                SequenceRecognizer([]),
                outbox=object(),
                trace_factory=fail_factory,
            ).record_skipped((frame,), "processing_error")

            self.assertEqual(result.reason, "processing_error")
            self.assertIsNotNone(result.event_id)
            self.assertIsNone(result.telemetry)
            self.assertEqual(store.pending_outbox_count(), 1)
            queued = store.pending_outbox_items()
            self.assertEqual(len(queued), 1)
            self.assertNotIn("_awaiting_telemetry", queued[0][1])

    def test_duplicate_direct_skip_does_not_create_a_second_trace(self):
        created_traces = []

        def trace_factory(**kwargs):
            trace = ProcessingTrace(**kwargs)
            created_traces.append(trace)
            return trace

        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            frame = self._jpeg(directory, "duplicate-skip.jpg")
            processor = self._processor(
                store,
                RecordingRelay([]),
                SequenceRecognizer([]),
                outbox=object(),
                trace_factory=trace_factory,
            )

            first = processor.record_skipped((frame,), "queue_coalesced")
            duplicate = processor.record_skipped((frame,), "queue_coalesced")

            self.assertIsNotNone(first.telemetry)
            self.assertEqual(duplicate.reason, "duplicate_event")
            self.assertIsNone(duplicate.telemetry)
            self.assertEqual(duplicate.event_id, first.event_id)
            self.assertEqual(len(created_traces), 1)
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_processing_reuses_unique_frame_digests_for_identity_and_quality(self):
        recognizer = SequenceRecognizer([
            PlateObservation(None, 0.0),
            PlateObservation("NOPE", 0.8),
        ])
        with tempfile.TemporaryDirectory() as directory:
            frames = (
                self._jpeg(directory, "first.jpg", 32),
                self._jpeg(directory, "second.jpg", 224),
            )
            expected_digests = [
                hashlib.sha256(frame.read_bytes()).hexdigest() for frame in frames
            ]
            store = LocalStore(Path(directory) / "gate.db")
            with patch(
                "gate_controller.processor._content_digest",
                wraps=processor_module._content_digest,
            ) as processor_digest, patch(
                "gate_controller.images._content_digest",
                wraps=image_tools._content_digest,
            ) as quality_digest:
                result = self._processor(
                    store, RecordingRelay([]), recognizer
                ).process(frames)

            identity_exists = store.event_exists(expected_digests[0])

        self.assertEqual(result.reason, "no_match")
        self.assertTrue(identity_exists)
        self.assertEqual(
            [frame["digest"] for frame in result.telemetry.to_wire()["frames"]],
            expected_digests,
        )
        self.assertEqual(processor_digest.call_count, len(frames))
        quality_digest.assert_not_called()

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

    def test_explicit_ftp_identity_deduplicates_different_ranked_hot_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            store = LocalStore(Path(directory) / "gate.db")
            ftp = self._jpeg(directory, "ftp.jpg", 128)
            first_hot = self._jpeg(directory, "first-hot.jpg", 32)
            second_hot = self._jpeg(directory, "second-hot.jpg", 224)
            ftp_identity = hashlib.sha256(ftp.read_bytes()).hexdigest()
            processor = self._processor(
                store,
                RecordingRelay(calls),
                StaticRecognizer(PlateObservation("12D3456", 0.95)),
            )

            first = processor.process((first_hot, ftp), idempotency_key=ftp_identity)
            second = processor.process((second_hot, ftp), idempotency_key=ftp_identity)

            self.assertTrue(first.opened)
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

    def test_blocked_ocr_wait_is_bounded_without_waiting_for_the_request(self):
        recognising = Event()
        release = Event()
        finished = Event()

        class BlockingRecognizer:
            def recognise(self, path, timeout=None):
                try:
                    recognising.set()
                    release.wait()
                    return PlateObservation("12D3456", 0.99)
                finally:
                    finished.set()

        class FastStore:
            def event_exists(self, idempotency_key):
                return False

            def record_event_with_outbox(self, event, outbox_payload=None):
                return 1

            def attach_event_telemetry(self, event_id, telemetry):
                return True

        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "blocked.jpg")
            processor = self._processor(
                FastStore(),
                RecordingRelay(relay_calls),
                BlockingRecognizer(),
                decision_timeout=0.1,
            )

            results = []
            processing = Thread(target=lambda: results.append(processor.process((frame,))))
            processing.start()
            try:
                self.assertTrue(recognising.wait(1.0))
                processing.join(0.5)
                returned_while_ocr_blocked = not processing.is_alive()
                ocr_finished_before_release = finished.is_set()
            finally:
                release.set()
                processing.join(1.0)

        processing_stopped = not processing.is_alive()

        self.assertTrue(returned_while_ocr_blocked)
        self.assertFalse(ocr_finished_before_release)
        self.assertTrue(processing_stopped)
        self.assertEqual(results[0].reason, "decision_timeout")
        self.assertEqual(relay_calls, [])
        self.assertTrue(finished.wait(0.5))

    def test_ocr_setup_time_consumes_the_same_absolute_decision_deadline(self):
        class FastStore:
            def event_exists(self, idempotency_key):
                return False

            def record_event_with_outbox(self, event, outbox_payload=None):
                return 1

            def attach_event_telemetry(self, event_id, telemetry):
                return True

        class DelayedRecognizer:
            def __init__(self):
                self.lookups = 0

            def __getattribute__(self, name):
                if name == "recognise":
                    lookups = object.__getattribute__(self, "lookups")
                    object.__setattr__(self, "lookups", lookups + 1)
                    Event().wait(0.2)
                return object.__getattribute__(self, name)

            def recognise(self, path, timeout=None):
                Event().wait(0.4)
                return PlateObservation("12D3456", 0.99)

        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            frame = self._jpeg(directory, "delayed-setup.jpg")
            processor = self._processor(
                FastStore(),
                RecordingRelay(relay_calls),
                DelayedRecognizer(),
                decision_timeout=0.1,
            )

            started = monotonic()
            result = processor.process((frame,))
            elapsed = monotonic() - started

        self.assertLess(elapsed, 0.18)
        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(relay_calls, [])

    def test_timed_out_ocr_keeps_later_requests_out_of_the_shared_session(self):
        release = Event()
        finished = Event()

        class BlockingRecognizer:
            def __init__(self):
                self.calls = []

            def recognise(self, path, timeout=None):
                self.calls.append(path)
                try:
                    release.wait(0.4)
                    return PlateObservation(None, 0.0)
                finally:
                    finished.set()

        recognizer = BlockingRecognizer()
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._jpeg(directory, "first-blocked.jpg", 32)
            second = self._jpeg(directory, "second-blocked.jpg", 224)
            processor = self._processor(
                LocalStore(root / "gate.db"), RecordingRelay(relay_calls), recognizer,
                decision_timeout=0.1,
            )

            first_result = processor.process((first,))
            second_result = processor.process((second,))
            release.set()

        self.assertEqual(first_result.reason, "decision_timeout")
        self.assertEqual(second_result.reason, "ocr_busy")
        self.assertEqual(
            second_result.telemetry.to_wire()["ocr_attempts"][0]["status"],
            "ocr_busy",
        )
        self.assertEqual(recognizer.calls, [first])
        self.assertEqual(relay_calls, [])
        self.assertTrue(finished.wait(0.5))

    def test_timed_out_resettable_ocr_uses_a_fresh_generation_for_the_next_event(self):
        release_first = Event()

        class ResettableRecognizer:
            def __init__(self):
                self.calls = []
                self.abandoned = 0

            def recognise(self, path, timeout=None):
                self.calls.append(path)
                if len(self.calls) == 1:
                    release_first.wait(0.5)
                return PlateObservation(None, 0.0)

            def abandon_in_flight(self):
                self.abandoned += 1
                release_first.set()
                return True

        recognizer = ResettableRecognizer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._jpeg(directory, "first-resettable.jpg", 32)
            second = self._jpeg(directory, "second-resettable.jpg", 224)
            processor = self._processor(
                LocalStore(root / "gate.db"), RecordingRelay([]), recognizer,
                decision_timeout=0.1,
            )

            first_result = processor.process((first,))
            second_result = processor.process((second,))
            release_first.set()

        self.assertEqual(first_result.reason, "decision_timeout")
        self.assertEqual(second_result.reason, "no_match")
        self.assertEqual(recognizer.calls, [first, second])
        self.assertEqual(recognizer.abandoned, 1)

    def test_completed_ocr_releases_its_slot_before_publishing_the_result(self):
        allow_worker_put_to_return = Event()

        class HoldingQueue(ThreadQueue):
            def put(self, item, *args, **kwargs):
                super().put(item, *args, **kwargs)
                allow_worker_put_to_return.wait(0.5)

        recognizer = SequenceRecognizer([
            PlateObservation(None, 0.0),
            PlateObservation(None, 0.0),
        ])
        with tempfile.TemporaryDirectory() as directory, patch.object(
            processor_module, "Queue", HoldingQueue
        ):
            frames = (
                self._jpeg(directory, "first-race.jpg", 32),
                self._jpeg(directory, "second-race.jpg", 224),
            )
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                RecordingRelay([]),
                recognizer,
            ).process(frames)
            allow_worker_put_to_return.set()

        self.assertEqual(result.reason, "no_match")
        self.assertEqual(recognizer.calls, list(frames))
        self.assertEqual(
            [attempt["status"] for attempt in result.telemetry.to_wire()["ocr_attempts"]],
            ["no_plate", "no_plate"],
        )

    def test_deadline_expiring_during_final_authorisation_check_inhibits_gpio(self):
        decision_clock = MutableClock()
        authorisation_checks = 0

        def authorised():
            nonlocal authorisation_checks
            authorisation_checks += 1
            if authorisation_checks == 2:
                decision_clock.value = 4.1
            return {"12D3456"}

        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            processor = GateProcessor(
                recognizer=SequenceRecognizer([PlateObservation("12D3456", 0.99)]),
                store=LocalStore(Path(directory) / "gate.db"),
                relay=RecordingRelay(relay_calls),
                authorised=authorised,
                decision_timeout=4.0,
                decision_clock=decision_clock,
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            )
            result = processor.process((Path("deadline-at-relay.jpg"),))

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(relay_calls, [])

    def test_deadline_is_rechecked_by_the_backend_immediately_before_gpio(self):
        decision_clock = MutableClock()

        class DeadlineAwareBackend:
            def __init__(self):
                self.calls = []

            def off(self):
                self.calls.append("off")

            def on(self, *, pre_activation_inhibit=None):
                decision_clock.value = 4.1
                inhibition = pre_activation_inhibit()
                if inhibition is not None:
                    return inhibition
                self.calls.append("on")
                return None

        backend = DeadlineAwareBackend()
        relay = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)
        with tempfile.TemporaryDirectory() as directory:
            result = GateProcessor(
                recognizer=SequenceRecognizer([PlateObservation("12D3456", 0.99)]),
                store=LocalStore(Path(directory) / "gate.db"),
                relay=relay,
                authorised={"12D3456"},
                decision_timeout=4.0,
                decision_clock=decision_clock,
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            ).process((Path("deadline-inside-backend.jpg"),))

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(backend.calls, ["off"])
        self.assertEqual(result.telemetry.to_wire()["actuation"], {
            "claim": "claimed",
            "attempted": False,
            "relay_outcome": "inhibited",
        })

    def test_shutdown_latch_reports_no_physical_activation_attempt(self):
        class Backend:
            def __init__(self):
                self.calls = []

            def on(self, *, pre_activation_inhibit=None):
                inhibition = pre_activation_inhibit()
                if inhibition is not None:
                    return inhibition
                self.calls.append("on")
                return None

            def off(self):
                self.calls.append("off")

        backend = Backend()
        relay = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)
        relay.begin_shutdown()
        with tempfile.TemporaryDirectory() as directory:
            result = self._processor(
                LocalStore(Path(directory) / "gate.db"),
                relay,
                SequenceRecognizer([PlateObservation("12D3456", 0.99)]),
            ).process((Path("shutdown-latched.jpg"),))

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "relay_latched")
        self.assertEqual(backend.calls, ["off", "off"])
        self.assertEqual(result.telemetry.to_wire()["actuation"], {
            "claim": "claimed",
            "attempted": False,
            "relay_outcome": "relay_latched",
        })

    def test_deadline_check_follows_all_final_validation_before_gpio(self):
        decision_clock = MutableClock()
        original_normalise = processor_module.normalise_plate
        authorisation_checks = 0

        def authorised():
            nonlocal authorisation_checks
            authorisation_checks += 1
            if authorisation_checks == 3:
                decision_clock.value = 3.999
            return {"12D 3456"}

        def delayed_normalise(value):
            result = original_normalise(value)
            if authorisation_checks == 3 and value == "12D3456":
                decision_clock.value = 4.001
            return result

        class ObservedGpioBackend:
            def __init__(self):
                self.calls = []

            def off(self):
                self.calls.append("off")

            def on(self, *, pre_activation_inhibit=None):
                inhibition = pre_activation_inhibit()
                if inhibition is not None:
                    return inhibition
                self.calls.append(("gpio", decision_clock.value))
                return None

        backend = ObservedGpioBackend()
        relay = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            processor_module, "normalise_plate", side_effect=delayed_normalise
        ):
            result = GateProcessor(
                recognizer=SequenceRecognizer([PlateObservation("12D3456", 0.99)]),
                store=LocalStore(Path(directory) / "gate.db"),
                relay=relay,
                authorised=authorised,
                decision_timeout=4.0,
                decision_clock=decision_clock,
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            ).process((Path("deadline-after-validation.jpg"),))

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(backend.calls, ["off"])
        self.assertEqual(
            result.telemetry.to_wire()["decision"],
            {"outcome": "denied", "reason": "decision_timeout"},
        )

    def test_activation_guard_reserves_time_before_the_physical_deadline(self):
        decision_clock = MutableClock()
        authorisation_checks = 0

        def authorised():
            nonlocal authorisation_checks
            authorisation_checks += 1
            if authorisation_checks == 3:
                decision_clock.value = 3.901
            return {"12D3456"}

        class DelayedGpioBackend:
            def __init__(self):
                self.calls = []

            def off(self):
                self.calls.append("off")

            def on(self, *, pre_activation_inhibit=None):
                inhibition = pre_activation_inhibit()
                if inhibition is not None:
                    return inhibition
                decision_clock.value = 4.001
                self.calls.append(("gpio", decision_clock.value))
                return None

        backend = DelayedGpioBackend()
        relay = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)
        with tempfile.TemporaryDirectory() as directory:
            result = GateProcessor(
                recognizer=SequenceRecognizer([PlateObservation("12D3456", 0.99)]),
                store=LocalStore(Path(directory) / "gate.db"),
                relay=relay,
                authorised=authorised,
                decision_timeout=4.0,
                activation_guard_seconds=0.1,
                decision_clock=decision_clock,
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            ).process((Path("activation-guard.jpg"),))

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(backend.calls, ["off"])

    def test_activation_guard_must_leave_a_positive_decision_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            for guard in (-0.1, 4.0, 5.0):
                with self.subTest(guard=guard), self.assertRaises(ValueError):
                    GateProcessor(
                        recognizer=SequenceRecognizer([]),
                        store=LocalStore(Path(directory) / "gate.db"),
                        relay=RecordingRelay([]),
                        authorised=set(),
                        decision_timeout=4.0,
                        activation_guard_seconds=guard,
                    )

    def test_repeated_permanent_ocr_hangs_keep_one_bounded_worker(self):
        release = Event()
        state_lock = Lock()

        class UncancellableRecognizer:
            def __init__(self):
                self.calls = []
                self.active = 0
                self.max_active = 0
                self.abandoned = 0

            def recognise(self, path, timeout=None):
                with state_lock:
                    self.calls.append(path)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    release.wait(2)
                    return PlateObservation(None, 0.0)
                finally:
                    with state_lock:
                        self.active -= 1

            def abandon_in_flight(self):
                self.abandoned += 1
                return False

        recognizer = UncancellableRecognizer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                processor = self._processor(
                    LocalStore(root / "gate.db"), RecordingRelay([]), recognizer,
                    decision_timeout=0.02,
                )
                results = [
                    processor.process((self._jpeg(directory, f"hung-{index}.jpg", index * 31),))
                    for index in range(5)
                ]

            self.assertEqual(results[0].reason, "decision_timeout")
            self.assertEqual([result.reason for result in results[1:]], ["ocr_busy"] * 4)
            self.assertEqual(len(recognizer.calls), 1)
            self.assertEqual(recognizer.max_active, 1)
            self.assertEqual(recognizer.abandoned, 1)
        finally:
            release.set()

    def test_processor_close_closes_recognizer_and_reaps_timed_out_worker(self):
        release = Event()
        finished = Event()

        class ClosableRecognizer:
            def __init__(self):
                self.closed = False

            def recognise(self, path, timeout=None):
                try:
                    release.wait(2)
                    return PlateObservation(None, 0.0)
                finally:
                    finished.set()

            def abandon_in_flight(self):
                return False

            def close(self):
                self.closed = True
                release.set()

        recognizer = ClosableRecognizer()
        with tempfile.TemporaryDirectory() as directory:
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay([]), recognizer,
                decision_timeout=0.02,
            )
            processor.process((self._jpeg(directory, "close-hung.jpg"),))
            try:
                processor.close()
            finally:
                release.set()

        self.assertTrue(recognizer.closed)
        self.assertTrue(finished.wait(0.5))

    def test_processor_close_is_bounded_when_recognizer_close_hangs(self):
        release = Event()

        class BlockingCloseRecognizer:
            def recognise(self, path, timeout=None):
                return PlateObservation(None, 0.0)

            def close(self):
                release.wait(2)

        try:
            with tempfile.TemporaryDirectory() as directory:
                processor = self._processor(
                    LocalStore(Path(directory) / "gate.db"), RecordingRelay([]),
                    BlockingCloseRecognizer(),
                )

                started = monotonic()
                processor.close()
                elapsed = monotonic() - started

            self.assertLess(elapsed, 0.7)
        finally:
            release.set()

    def test_processor_close_terminally_inhibits_an_in_flight_exact_match(self):
        recognising = Event()
        release = Event()
        completed = Event()

        class LateExactRecognizer:
            def recognise(self, path, timeout=None):
                recognising.set()
                release.wait(2)
                return PlateObservation("12D3456", 0.99)

        results = []
        worker_errors = []
        relay_calls = []
        with tempfile.TemporaryDirectory() as directory:
            processor = self._processor(
                LocalStore(Path(directory) / "gate.db"), RecordingRelay(relay_calls),
                LateExactRecognizer(), decision_timeout=2.0,
            )

            def process_in_flight_match():
                try:
                    results.append(
                        processor.process(
                            (self._jpeg(directory, "close-exact.jpg"),)
                        )
                    )
                except BaseException as error:
                    worker_errors.append(error)
                finally:
                    completed.set()

            worker = Thread(
                target=process_in_flight_match,
                daemon=True,
            )
            worker.start()
            try:
                self.assertTrue(recognising.wait(5.0))
                processor.close()
                release.set()
                self.assertTrue(completed.wait(5.0))
            finally:
                try:
                    processor.close()
                finally:
                    release.set()
                    completed.wait(5.0)
                    worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].opened)
        self.assertEqual(results[0].reason, "processor_closed")
        self.assertEqual(relay_calls, [])
        self.assertEqual(
            results[0].telemetry.to_wire()["decision"],
            {"outcome": "denied", "reason": "processor_closed"},
        )

    def test_timed_out_ocr_cleanup_is_bounded_when_abandon_hangs(self):
        release = Event()
        abandon_started = Event()
        abandon_finished = Event()
        recognition_finished = Event()
        processing_completed = Event()

        class BlockingCleanupRecognizer:
            def recognise(self, path, timeout=None):
                try:
                    release.wait(2)
                    return PlateObservation(None, 0.0)
                finally:
                    recognition_finished.set()

            def abandon_in_flight(self):
                abandon_started.set()
                try:
                    release.wait(2)
                    return False
                finally:
                    abandon_finished.set()

        results = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                processor = self._processor(
                    LocalStore(Path(directory) / "gate.db"), RecordingRelay([]),
                    BlockingCleanupRecognizer(), decision_timeout=0.02,
                )

                def process_timed_out_request():
                    try:
                        results.append(processor.process(
                            (self._jpeg(directory, "cleanup-hung.jpg"),)
                        ))
                    finally:
                        processing_completed.set()

                worker = Thread(target=process_timed_out_request, daemon=True)
                worker.start()
                self.assertTrue(abandon_started.wait(1.0))
                self.assertTrue(processing_completed.wait(1.0))
                self.assertFalse(release.is_set())

            self.assertEqual(results[0].reason, "decision_timeout")
        finally:
            release.set()
            processing_completed.wait(2.0)
            recognition_finished.wait(2.0)
            abandon_finished.wait(2.0)
            if "worker" in locals():
                worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(recognition_finished.is_set())
        self.assertTrue(abandon_finished.is_set())

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

    def test_processor_outbox_cannot_send_until_telemetry_attachment_finishes(self):
        attach_started = Event()
        allow_attach = Event()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = self._jpeg(directory, "telemetry-race.jpg")

            class PausingStore(LocalStore):
                def attach_event_telemetry(self, event_id, telemetry):
                    attach_started.set()
                    if not allow_attach.wait(timeout=1):
                        raise TimeoutError("test did not release telemetry attachment")
                    return super().attach_event_telemetry(event_id, telemetry)

            store = PausingStore(root / "gate.db")
            spool = EvidenceSpool(root / "event-evidence")
            sent = []
            outbox = OutboxWorker(
                store,
                send=lambda payload, evidence: sent.append((dict(payload), evidence)),
                evidence_spool=spool,
            )
            processor = self._processor(
                store,
                RecordingRelay([]),
                StaticRecognizer(PlateObservation("NOPE", 0.95)),
                outbox=outbox,
            )
            result = []
            processing = Thread(target=lambda: result.append(processor.process((frame,))))
            processing.start()
            try:
                self.assertTrue(attach_started.wait(timeout=1))
                self.assertEqual(outbox.run_once(), 0)
                self.assertEqual(sent, [])
            finally:
                allow_attach.set()
                processing.join(timeout=1)
            self.assertFalse(processing.is_alive())
            self.assertEqual(outbox.run_once(), 1)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0]["schema_version"], 3)
        self.assertEqual(
            sent[0][0]["telemetry"]["trace_id"], result[0].telemetry.trace_id
        )

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
