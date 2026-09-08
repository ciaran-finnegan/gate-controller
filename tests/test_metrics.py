import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller import ocr
from gate_controller.metrics import (
    DEFAULT_LOOKUP_QUOTA, MAX_MINUTES_PER_POST, MetricsRing, QuotaLedger,
    build_metrics_ring, metrics_enabled, metrics_rollup_seconds,
    recognition_lookup_quota,
)
from gate_controller.telemetry import (
    EventTelemetry, LocalOcrTelemetry, OcrAttemptTelemetry, StageDurations,
)


def telemetry(attempts=(), local=None) -> EventTelemetry:
    return EventTelemetry(
        trace_id="0123456789abcdef",
        stage_durations=StageDurations(),
        frames=(),
        ocr_attempts=tuple(attempts),
        decision_outcome="denied",
        decision_reason="no_plate",
        actuation_claim="none",
        actuation_attempted=False,
        relay_outcome="skipped",
        outbox_attempt=0,
        delivery_state="pending",
        local_ocr=local,
    )


def attempt(status, *, cause=None, duration_ms=0.0) -> OcrAttemptTelemetry:
    return OcrAttemptTelemetry(
        frame_sequence=0, status=status, failure_cause=cause, duration_ms=duration_ms,
    )


class FrozenClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


START = datetime(2026, 9, 8, 10, 20, 30, tzinfo=timezone.utc)


class MetricsRingTests(unittest.TestCase):
    def setUp(self):
        ocr.drain_retry_counts()
        self.clock = FrozenClock(START)
        self.ring = MetricsRing(clock=self.clock, retry_counts=lambda: {})

    def minute(self, index=0):
        self.clock.advance(minutes=1)
        return self.ring.unsent_minutes()[index]

    def test_recognition_outcomes_are_counted_under_the_contract_key_names(self):
        self.ring.record_event_telemetry(telemetry((
            attempt("recognized", duration_ms=800),
            attempt("no_plate", duration_ms=1200),
            attempt("ocr_timeout"),
            attempt("ocr_busy"),
            attempt("ocr_error", cause="connection_error"),
        )))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["ocr_attempts"], 5)
        self.assertEqual(recognition["recognized"], 1)
        # `unread_frames`, never `no_plate`: any key matching /plate/ is a 400.
        self.assertEqual(recognition["unread_frames"], 1)
        self.assertEqual(recognition["ocr_timeout"], 1)
        self.assertEqual(recognition["ocr_busy"], 1)
        self.assertEqual(recognition["ocr_error"], 1)
        self.assertNotIn("no_plate", recognition)

    def test_billed_lookups_count_only_attempts_that_reached_the_service(self):
        self.ring.record_event_telemetry(telemetry((
            attempt("recognized"),
            attempt("no_plate"),
            attempt("ocr_error", cause="read_timeout"),
            attempt("ocr_error", cause="invalid_json"),
        )))

        self.assertEqual(self.minute()["recognition"]["billed_lookups"], 4)

    def test_attempts_that_never_reached_the_service_are_not_billed(self):
        self.ring.record_event_telemetry(telemetry((
            attempt("ocr_busy"),
            attempt("ocr_timeout"),
            attempt("ocr_error", cause="connect_timeout"),
            attempt("ocr_error", cause="tls_error"),
            attempt("ocr_error", cause="connection_error"),
            attempt("ocr_error", cause="http_429"),
            attempt("ocr_error", cause="http_403"),
        )))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["billed_lookups"], 0)
        self.assertEqual(recognition["ocr_attempts"], 7)

    def test_local_reads_are_counted_beside_the_cloud_ones(self):
        self.ring.record_event_telemetry(telemetry(
            (attempt("no_plate"),),
            local=LocalOcrTelemetry(mode="active", frames=3, status="recognized"),
        ))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["local_attempts"], 3)
        self.assertEqual(recognition["local_recognized"], 1)

    def test_ocr_durations_become_bounded_percentiles(self):
        self.ring.record_event_telemetry(telemetry(tuple(
            attempt("no_plate", duration_ms=value) for value in (100, 200, 900)
        )))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["ocr_ms_p50"], 200)
        self.assertEqual(recognition["ocr_ms_p90"], 900)

    def test_throttled_requests_are_drained_from_the_ocr_client(self):
        drained = [{"http_429": 2, "connection_error": 1}, {}]
        ring = MetricsRing(clock=self.clock, retry_counts=lambda: drained.pop(0))

        ring.record_event_telemetry(telemetry((attempt("recognized"),)))
        ring.record_event_telemetry(telemetry((attempt("recognized"),)))
        self.clock.advance(minutes=1)

        self.assertEqual(ring.unsent_minutes()[0]["recognition"]["http_429"], 2)

    def test_the_ocr_client_counts_a_throttled_request_for_the_ring_to_drain(self):
        ocr.count_retryable_failure("http_429")
        ocr.count_retryable_failure("http_429")

        self.assertEqual(ocr.drain_retry_counts()["http_429"], 2)
        self.assertEqual(ocr.drain_retry_counts(), {})

    def test_heartbeats_are_counted_into_the_minute_they_landed_in(self):
        for _ in range(4):
            self.ring.record_heartbeat()

        self.assertEqual(self.minute()["heartbeats"], 4)

    def test_the_minute_in_progress_is_never_offered_for_sending(self):
        self.ring.record_heartbeat()

        self.assertEqual(self.ring.unsent_minutes(), [])

        self.clock.advance(minutes=1)
        self.assertEqual(len(self.ring.unsent_minutes()), 1)

    def test_a_minute_is_offered_once_and_never_again(self):
        self.ring.record_heartbeat()
        self.clock.advance(minutes=1)
        minutes = self.ring.unsent_minutes()

        self.ring.mark_sent(minutes)

        self.assertEqual(self.ring.unsent_minutes(), [])

    def test_minutes_are_offered_oldest_first_and_capped_for_one_post(self):
        for _ in range(MAX_MINUTES_PER_POST + 10):
            self.ring.record_heartbeat()
            self.clock.advance(minutes=1)

        minutes = self.ring.unsent_minutes()

        self.assertEqual(len(minutes), MAX_MINUTES_PER_POST)
        self.assertEqual(
            [entry["minute_start"] for entry in minutes],
            sorted(entry["minute_start"] for entry in minutes),
        )
        self.assertEqual(minutes[0]["minute_start"], "2026-09-08T10:20:00Z")

    def test_the_ring_is_bounded_and_drops_the_oldest_minute(self):
        ring = MetricsRing(clock=self.clock, max_minutes=5, retry_counts=lambda: {})

        for _ in range(40):
            ring.record_heartbeat()
            self.clock.advance(minutes=1)

        status = ring.status()
        self.assertEqual(status["minutes_held"], 5)
        self.assertEqual(status["capacity"], 5)
        self.assertEqual(status["minutes_dropped"], 35)
        self.assertLessEqual(len(ring.unsent_minutes()), 5)

    def test_recording_never_raises_into_the_pipeline(self):
        class Exploding:
            @property
            def ocr_attempts(self):
                raise RuntimeError("telemetry is broken")

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.ring.record_event_telemetry(Exploding())

        self.assertIn("gate_metrics stage=record_failed", logs.output[0])

    def test_a_result_without_telemetry_is_ignored(self):
        class Result:
            telemetry = None

        self.ring.record_processing_result(Result())

        self.assertEqual(self.ring.unsent_minutes(), [])


class QuotaLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "metrics-quota.json"
        self.clock = FrozenClock(START)

    def ledger(self) -> QuotaLedger:
        return QuotaLedger(self.path, clock=self.clock)

    def test_billed_lookups_accumulate_and_survive_a_restart(self):
        ledger = self.ledger()
        ledger.record_billed(3)
        ledger.record_billed(2)

        self.assertEqual(ledger.month_to_date(), 5)
        self.assertEqual(self.ledger().month_to_date(), 5)
        self.assertEqual(
            json.loads(self.path.read_text()),
            {"month": "2026-09", "billed_lookups": 5},
        )

    def test_the_counter_starts_again_when_the_month_does(self):
        ledger = self.ledger()
        ledger.record_billed(7)

        self.clock.advance(days=30)

        self.assertEqual(ledger.month_to_date(), 0)
        self.assertEqual(json.loads(self.path.read_text())["month"], "2026-10")

    def test_a_counter_written_in_a_previous_month_is_not_carried_forward(self):
        self.path.write_text(json.dumps({"month": "2026-08", "billed_lookups": 2400}))

        self.assertEqual(self.ledger().month_to_date(), 0)

    def test_a_corrupt_ledger_costs_the_count_not_the_controller(self):
        self.path.write_text("{not json")

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            ledger = self.ledger()

        self.assertIn("gate_metrics stage=quota_read_failed", logs.output[0])
        self.assertEqual(ledger.month_to_date(), 0)

    def test_an_unwritable_ledger_is_journalled_once_and_keeps_counting(self):
        ledger = QuotaLedger(
            Path("/proc/gate-metrics-does-not-exist/quota.json"), clock=self.clock,
        )

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            ledger.record_billed(1)
            ledger.record_billed(1)

        self.assertEqual(len(logs.output), 1)
        self.assertIn("gate_metrics stage=quota_write_failed", logs.output[0])
        self.assertEqual(ledger.month_to_date(), 2)

    def test_the_ring_reports_the_burn_down_the_heartbeat_block_accepts(self):
        ring = MetricsRing(quota=self.ledger(), clock=self.clock, retry_counts=lambda: {})

        ring.record_event_telemetry(telemetry((
            attempt("recognized"), attempt("no_plate"), attempt("ocr_busy"),
        )))

        self.assertEqual(ring.quota_status(), {
            "recognition_lookups_month_to_date": 2,
            "recognition_lookup_quota": DEFAULT_LOOKUP_QUOTA,
        })
        self.clock.advance(minutes=1)
        cloud = ring.unsent_minutes()[0]["cloud"]
        self.assertEqual(cloud["recognition_lookups_month_to_date"], 2)
        self.assertEqual(cloud["recognition_lookup_quota"], DEFAULT_LOOKUP_QUOTA)


class MetricsSettingsTests(unittest.TestCase):
    def test_the_rollup_period_defaults_to_five_minutes(self):
        self.assertEqual(metrics_rollup_seconds({}), 300.0)

    def test_the_rollup_period_is_bounds_checked(self):
        self.assertEqual(metrics_rollup_seconds({"GATE_METRICS_ROLLUP_SECONDS": "600"}), 600.0)
        for invalid in ("0", "30", "7200", "soon", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    metrics_rollup_seconds({"GATE_METRICS_ROLLUP_SECONDS": invalid})

    def test_metrics_are_enabled_by_default_and_can_be_turned_off(self):
        self.assertTrue(metrics_enabled({}))
        self.assertFalse(metrics_enabled({"GATE_METRICS_ENABLED": "false"}))
        with self.assertRaises(ValueError):
            metrics_enabled({"GATE_METRICS_ENABLED": "maybe"})

    def test_the_lookup_allowance_is_configurable_and_bounds_checked(self):
        self.assertEqual(recognition_lookup_quota({}), DEFAULT_LOOKUP_QUOTA)
        self.assertEqual(
            recognition_lookup_quota({"GATE_RECOGNITION_LOOKUP_QUOTA": "5000"}), 5000,
        )
        for invalid in ("0", "-1", "lots"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    recognition_lookup_quota({"GATE_RECOGNITION_LOOKUP_QUOTA": invalid})

    def test_disabled_means_nothing_is_constructed(self):
        self.assertIsNone(build_metrics_ring({"GATE_METRICS_ENABLED": "0"}))

    def test_the_ledger_lives_beside_the_database(self):
        with tempfile.TemporaryDirectory() as directory:
            ring = build_metrics_ring({}, state_directory=Path(directory))
            ring.record_event_telemetry(telemetry((attempt("recognized"),)))

            self.assertTrue((Path(directory) / "metrics-quota.json").is_file())


if __name__ == "__main__":
    unittest.main()
