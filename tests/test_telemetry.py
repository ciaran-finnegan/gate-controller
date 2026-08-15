import re
import unittest
from datetime import datetime, timezone

from gate_controller.telemetry import (
    EventTelemetry,
    FrameTelemetry,
    OcrAttemptTelemetry,
    ProcessingTrace,
    StageDurations,
)


class TelemetryWireTests(unittest.TestCase):
    def test_to_wire_clamps_rounds_and_omits_unset_stage_durations(self):
        telemetry = EventTelemetry(
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
            stage_durations=StageDurations(
                capture_to_burst_ms=-1.1,
                burst_to_ocr_ms=None,
                ocr_ms=20.5,
                decision_ms=900_000,
            ),
            frames=(),
            ocr_attempts=(),
            decision_outcome="denied",
            decision_reason="plate_not_authorised",
            actuation_claim="not_requested",
            actuation_attempted=False,
            relay_outcome="not_attempted",
            outbox_attempt=0,
            delivery_state="pending",
        )

        wire = telemetry.to_wire()

        self.assertEqual(wire["stage_durations"], {
            "capture_to_burst_ms": 0,
            "ocr_ms": 21,
            "decision_ms": 600_000,
        })
        self.assertEqual(
            set(wire["stage_durations"]),
            {"capture_to_burst_ms", "ocr_ms", "decision_ms"},
        )

    def test_to_wire_emits_the_exact_v3_consumer_vocabulary(self):
        telemetry = EventTelemetry(
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
            stage_durations=StageDurations(
                capture_to_burst_ms=1,
                burst_to_ocr_ms=2,
                ocr_ms=3,
                decision_ms=4,
                decision_to_relay_ms=5,
                end_to_end_ms=6,
                delivery_lag_ms=7,
            ),
            frames=(
                FrameTelemetry(
                    sequence=0,
                    digest="a" * 64,
                    width=1920,
                    height=1080,
                    sharpness=0.8,
                    brightness=0.4,
                    darkness=0.2,
                    highlight_clipping=0.1,
                ),
            ),
            ocr_attempts=(
                OcrAttemptTelemetry(
                    frame_sequence=0,
                    duration_ms=31,
                    status="matched",
                    plate="131D2696",
                    confidence=0.88,
                    make="Ford",
                    colour="Blue",
                ),
            ),
            decision_outcome="denied",
            decision_reason="plate_not_authorised",
            actuation_claim="not_requested",
            actuation_attempted=False,
            relay_outcome="not_attempted",
            outbox_attempt=1,
            delivery_state="delivered",
        )

        wire = telemetry.to_wire()

        self.assertEqual(wire, {
            "schema_version": 3,
            "trace_id": "ae2398aa-7107-44f4-a723-290de0f8c7b2",
            "taxonomy_version": 1,
            "stage_durations": {
                "capture_to_burst_ms": 1,
                "burst_to_ocr_ms": 2,
                "ocr_ms": 3,
                "decision_ms": 4,
                "decision_to_relay_ms": 5,
                "end_to_end_ms": 6,
                "delivery_lag_ms": 7,
            },
            "frames": [{
                "sequence": 0,
                "digest": "a" * 64,
                "width": 1920,
                "height": 1080,
                "sharpness": 0.8,
                "brightness": 0.4,
                "darkness": 0.2,
                "highlight_clipping": 0.1,
                "status": "ok",
            }],
            "ocr_attempts": [{
                "frame_sequence": 0,
                "duration_ms": 31,
                "status": "matched",
                "plate": "131D2696",
                "confidence": 0.88,
                "make": "Ford",
                "colour": "Blue",
            }],
            "decision": {"outcome": "denied", "reason": "plate_not_authorised"},
            "actuation": {
                "claim": "not_requested",
                "attempted": False,
                "relay_outcome": "not_attempted",
            },
            "delivery": {"outbox_attempt": 1, "state": "delivered"},
        })

    def test_to_wire_caps_collections_and_normalizes_all_consumer_bounds(self):
        frames = tuple(
            FrameTelemetry(
                sequence=index + 10,
                digest=("%x" % index) * 64,
                width=20_000,
                height=-10,
                sharpness=2,
                brightness=-1,
                darkness=0.25,
                highlight_clipping=3,
                status="bad/status",
            )
            for index in range(9)
        )
        attempts = tuple(
            OcrAttemptTelemetry(
                frame_sequence=index + 10,
                duration_ms=700_000.2,
                status="x" * 129,
                plate="P" * 129,
                confidence=2,
                make="M" * 129,
                colour="",
            )
            for index in range(9)
        )
        telemetry = EventTelemetry(
            trace_id="not-a-uuid",
            stage_durations=StageDurations(),
            frames=frames,
            ocr_attempts=attempts,
            decision_outcome="bad/status",
            decision_reason="",
            actuation_claim="C" * 129,
            actuation_attempted=True,
            relay_outcome="bad/status",
            outbox_attempt=2_000,
            delivery_state="",
        )

        wire = telemetry.to_wire()

        self.assertRegex(wire["trace_id"], re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            re.IGNORECASE,
        ))
        self.assertEqual(len(wire["frames"]), 8)
        self.assertEqual(len(wire["ocr_attempts"]), 8)
        self.assertEqual(wire["frames"][0], {
            "sequence": 7,
            "digest": "0" * 64,
            "width": 16_384,
            "height": 1,
            "sharpness": 1.0,
            "brightness": 0.0,
            "darkness": 0.25,
            "highlight_clipping": 1.0,
            "status": "unknown",
        })
        self.assertEqual(wire["ocr_attempts"][0], {
            "frame_sequence": 7,
            "duration_ms": 600_000,
            "status": "unknown",
            "plate": "P" * 128,
            "confidence": 1.0,
            "make": "M" * 128,
            "colour": None,
        })
        self.assertEqual(wire["decision"], {"outcome": "unknown", "reason": "unknown"})
        self.assertEqual(wire["actuation"], {
            "claim": "unknown",
            "attempted": True,
            "relay_outcome": "unknown",
        })
        self.assertEqual(wire["delivery"], {"outbox_attempt": 1_000, "state": "unknown"})

    def test_to_wire_has_no_path_or_raw_response_fields_and_is_deterministic(self):
        telemetry = EventTelemetry(
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
            stage_durations=StageDurations(),
            frames=(
                FrameTelemetry(0, "a" * 64, 1, 1, 0, 0, 0, 0),
            ),
            ocr_attempts=(
                OcrAttemptTelemetry(0, 0, "no_plate"),
            ),
            decision_outcome="denied",
            decision_reason="plate_not_authorised",
            actuation_claim="not_requested",
            actuation_attempted=False,
            relay_outcome="not_attempted",
            outbox_attempt=0,
            delivery_state="pending",
        )

        first = telemetry.to_wire()
        second = telemetry.to_wire()

        self.assertEqual(first, second)
        self.assertFalse({"path", "raw_response", "response", "exception"} & set(first))
        self.assertFalse({"path", "raw_response", "response", "exception"} & set(first["frames"][0]))
        self.assertFalse({"path", "raw_response", "response", "exception"} & set(first["ocr_attempts"][0]))

    def test_to_wire_keeps_a_stable_snapshot_for_invalid_ids_and_generators(self):
        telemetry = EventTelemetry(
            trace_id="invalid",
            stage_durations=StageDurations(),
            frames=(
                frame
                for frame in (FrameTelemetry(0, "a" * 64, 1, 1, 0, 0, 0, 0),)
            ),
            ocr_attempts=(
                attempt
                for attempt in (OcrAttemptTelemetry(0, 0, "no_plate"),)
            ),
            decision_outcome="denied",
            decision_reason="plate_not_authorised",
            actuation_claim="not_requested",
            actuation_attempted=False,
            relay_outcome="not_attempted",
            outbox_attempt=0,
            delivery_state="pending",
        )

        self.assertEqual(telemetry.to_wire(), telemetry.to_wire())

    def test_to_wire_caps_frame_candidates_before_discarding_invalid_frames(self):
        frames = [
            FrameTelemetry(0, "not-a-digest", 1, 1, 0, 0, 0, 0),
            *(
                FrameTelemetry(index, f"{index:x}" * 64, 1, 1, 0, 0, 0, 0)
                for index in range(1, 9)
            ),
        ]
        telemetry = EventTelemetry(
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
            stage_durations=StageDurations(),
            frames=frames,
            ocr_attempts=(),
            decision_outcome="denied",
            decision_reason="plate_not_authorised",
            actuation_claim="not_requested",
            actuation_attempted=False,
            relay_outcome="not_attempted",
            outbox_attempt=0,
            delivery_state="pending",
        )

        self.assertEqual(
            [frame["sequence"] for frame in telemetry.to_wire()["frames"]],
            [1, 2, 3, 4, 5, 6, 7],
        )


class ProcessingTraceTests(unittest.TestCase):
    def test_ocr_start_and_end_boundaries_do_not_double_count_work(self):
        monotonic = iter((10.0, 10.1, 10.15, 10.4, 10.45, 10.65, 10.7, 10.8, 11.0))
        trace = ProcessingTrace(
            monotonic_clock=lambda: next(monotonic),
            wall_clock=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc),
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
        )

        trace.mark_burst()
        trace.mark_burst()
        mark_ocr_start = getattr(trace, "mark_ocr_start", None)
        self.assertIsNotNone(mark_ocr_start)
        mark_ocr_start()
        try:
            no_plate = OcrAttemptTelemetry(frame_sequence=0, status="no_plate")
        except TypeError as error:
            self.fail(f"trace-timed OCR attempts still require a duration: {error}")
        trace.add_ocr_attempt(no_plate)
        mark_ocr_start()
        trace.add_ocr_attempt(
            OcrAttemptTelemetry(
                frame_sequence=1,
                status="matched",
                plate="131D2696",
                confidence=0.88,
                make="Ford",
                colour="Blue",
            )
        )
        trace.mark_decision("allowed", "plate_authorised")
        trace.mark_decision("denied", "ignored")
        trace.mark_actuation("claimed", True, "activated")
        telemetry = trace.finish(outbox_attempt=1, delivery_state="pending")

        wire = telemetry.to_wire()
        self.assertEqual(wire["stage_durations"], {
            "capture_to_burst_ms": 100,
            "burst_to_ocr_ms": 50,
            "ocr_ms": 450,
            "decision_ms": 50,
            "decision_to_relay_ms": 100,
            "end_to_end_ms": 1_000,
        })
        self.assertEqual(wire["ocr_attempts"], [
            {
                "frame_sequence": 0,
                "duration_ms": 250,
                "status": "no_plate",
                "plate": None,
                "confidence": None,
                "make": None,
                "colour": None,
            },
            {
                "frame_sequence": 1,
                "duration_ms": 200,
                "status": "matched",
                "plate": "131D2696",
                "confidence": 0.88,
                "make": "Ford",
                "colour": "Blue",
            },
        ])

    def test_add_ocr_attempt_requires_an_explicit_start_mark(self):
        trace = ProcessingTrace(
            monotonic_clock=lambda: 10.0,
            wall_clock=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc),
        )

        with self.assertRaises(RuntimeError):
            trace.add_ocr_attempt(OcrAttemptTelemetry(0, 0, "no_plate"))

    def test_relay_duration_ends_at_activation_before_outcome_finalization(self):
        monotonic = iter((10.0, 10.1, 10.2, 10.25, 10.9))
        trace = ProcessingTrace(
            monotonic_clock=lambda: next(monotonic),
            wall_clock=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc),
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
        )

        trace.mark_burst()
        trace.mark_decision("allowed", "plate_authorised")
        trace.mark_relay_activation()
        trace.set_actuation_outcome("claimed", True, "activated")
        telemetry = trace.finish()

        self.assertEqual(telemetry.to_wire()["stage_durations"], {
            "capture_to_burst_ms": 100,
            "ocr_ms": 0,
            "decision_to_relay_ms": 50,
            "end_to_end_ms": 900,
        })
        self.assertEqual(telemetry.to_wire()["actuation"], {
            "claim": "claimed",
            "attempted": True,
            "relay_outcome": "activated",
        })

    def test_duplicate_actuation_and_finish_keep_the_first_terminal_snapshot(self):
        monotonic = iter((10.0, 10.1, 10.2, 10.3, 10.4))
        trace = ProcessingTrace(
            monotonic_clock=lambda: next(monotonic),
            wall_clock=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc),
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
        )

        trace.mark_burst()
        trace.mark_decision("allowed", "plate_authorised")
        trace.mark_actuation("claimed", True, "activated")
        trace.mark_actuation("ignored", False, "ignored")
        first = trace.finish(outbox_attempt=0, delivery_state="pending")
        second = trace.finish(outbox_attempt=2, delivery_state="delivered")

        self.assertIs(first, second)
        self.assertEqual(first.to_wire()["actuation"], {
            "claim": "claimed",
            "attempted": True,
            "relay_outcome": "activated",
        })
        self.assertEqual(
            first.to_wire()["delivery"],
            {"outbox_attempt": 0, "state": "pending"},
        )
        self.assertEqual(first.to_wire()["stage_durations"]["end_to_end_ms"], 400)

    def test_zero_ocr_terminal_result_reports_zero_work_and_omits_ocr_boundaries(self):
        monotonic = iter((10.0, 10.1, 10.2, 10.4))
        trace = ProcessingTrace(
            monotonic_clock=lambda: next(monotonic),
            wall_clock=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc),
            trace_id="ae2398aa-7107-44f4-a723-290de0f8c7b2",
        )

        trace.mark_burst()
        trace.mark_decision("denied", "capture_failed")
        telemetry = trace.finish()

        self.assertEqual(telemetry.to_wire()["ocr_attempts"], [])
        self.assertEqual(telemetry.to_wire()["stage_durations"], {
            "capture_to_burst_ms": 100,
            "ocr_ms": 0,
            "end_to_end_ms": 400,
        })


if __name__ == "__main__":
    unittest.main()
