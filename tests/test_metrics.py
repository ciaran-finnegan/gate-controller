import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

from gate_controller import ocr
from gate_controller.metrics import (
    BUCKET_MINUTES, DEFAULT_LOOKUP_QUOTA, MAX_MINUTES_PER_POST, MetricsRing,
    QuotaLedger, SENT_RETENTION_MINUTES, build_metrics_ring, metrics_enabled,
    metrics_rollup_seconds, minute_key, recognition_lookup_quota,
)
from gate_controller.telemetry import (
    EventTelemetry, LocalOcrTelemetry, OcrAttemptTelemetry, StageDurations,
    StageTimestamps,
)


def telemetry(attempts=(), local=None, started_at=None) -> EventTelemetry:
    return EventTelemetry(
        trace_id="0123456789abcdef",
        stage_durations=StageDurations(),
        stage_timestamps=StageTimestamps(burst_processing_started_at=started_at),
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


def attempt(status, *, cause=None, duration_ms=0.0, source="cloud",
            cloud_lookup=True) -> OcrAttemptTelemetry:
    return OcrAttemptTelemetry(
        frame_sequence=0, status=status, failure_cause=cause, duration_ms=duration_ms,
        source=source, cloud_lookup=cloud_lookup,
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
        # Past the end of the bucket, not merely past the end of the minute:
        # the ring holds a minute back until the whole five-minute bucket it
        # belongs to has closed, because the app replaces a bucket row rather
        # than merging into it.
        self.clock.advance(minutes=BUCKET_MINUTES)
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
            attempt("ocr_error", cause="connect_timeout"),
            attempt("ocr_error", cause="tls_error"),
            attempt("ocr_error", cause="connection_error"),
            attempt("ocr_error", cause="http_429"),
            attempt("ocr_error", cause="http_403"),
        )))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["billed_lookups"], 0)
        self.assertEqual(recognition["ocr_attempts"], 6)

    def test_an_ocr_timeout_is_billed_the_same_as_a_read_timeout(self):
        # Both are one physical event -- the request went out and the reply did
        # not come back in time -- seen from the decision's clock and from the
        # socket's. The processor records an `ocr_timeout` attempt only past
        # `ocr_started`, so by then the request had been launched.
        self.ring.record_event_telemetry(telemetry((
            attempt("ocr_timeout"),
            attempt("ocr_error", cause="read_timeout"),
        )))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["billed_lookups"], 2)
        self.assertEqual(recognition["ocr_timeout"], 1)

    def test_a_read_that_never_left_the_pi_is_not_billed(self):
        # The on-device reader answering in `fallback` mode: status
        # `recognized`, and no request was posted. Billing it would count
        # exactly the lookups on-device recognition stopped spending.
        self.ring.record_event_telemetry(telemetry(
            (attempt("recognized", source="local", cloud_lookup=False),),
            local=LocalOcrTelemetry(mode="active", frames=1, status="recognized"),
        ))

        recognition = self.minute()["recognition"]

        self.assertEqual(recognition["billed_lookups"], 0)
        self.assertEqual(recognition["recognized"], 1)
        self.assertEqual(recognition["local_recognized"], 1)

    def test_a_local_read_in_always_mode_is_billed_because_the_request_went_out(self):
        # `GATE_LOCAL_OCR_CLOUD=always`: the local read decided, the cloud
        # request was posted anyway to label the frame, and the allowance was
        # charged for it. Billing follows the request, not the reader.
        self.ring.record_event_telemetry(telemetry(
            (attempt("recognized", source="local", cloud_lookup=True),),
        ))

        self.assertEqual(self.minute()["recognition"]["billed_lookups"], 1)

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
        current = minute_key(START)
        drained = [{current: {"http_429": 2, "connection_error": 1}}, {}]
        ring = MetricsRing(clock=self.clock, retry_counts=lambda: drained.pop(0))

        ring.record_event_telemetry(telemetry((attempt("recognized"),)))
        ring.record_event_telemetry(telemetry((attempt("recognized"),)))
        self.clock.advance(minutes=BUCKET_MINUTES)

        self.assertEqual(ring.unsent_minutes()[0]["recognition"]["http_429"], 2)

    def test_a_throttle_is_counted_in_the_minute_it_happened_in(self):
        # A throttled burst is by definition the one that ran long, so it is
        # the one most likely to finish -- and be drained -- in the minute
        # after the 429. Crediting the drain minute pushed it into the next
        # five-minute bucket four times in five.
        throttled = minute_key(START)
        drained = [{throttled: {"http_429": 1}}]
        ring = MetricsRing(
            clock=self.clock, retry_counts=lambda: drained.pop(0) if drained else {},
        )
        ring.record_heartbeat()

        self.clock.advance(minutes=1)
        ring.record_event_telemetry(telemetry((attempt("recognized"),)))

        self.clock.advance(minutes=BUCKET_MINUTES)
        minutes = {
            entry["minute_start"]: entry["recognition"]["http_429"]
            for entry in ring.unsent_minutes()
        }
        self.assertEqual(minutes[throttled], 1)
        self.assertEqual(minutes[minute_key(START + timedelta(minutes=1))], 0)

    def test_a_burst_is_counted_in_the_minute_it_started_in(self):
        # Same correction as the throttle counter, for the same reason: a
        # burst is counted when it finishes, and the burst that runs past a
        # boundary is the slow one whose counters most want the right minute.
        self.clock.advance(seconds=32)  # 10:21:02: the burst is over
        self.ring.record_event_telemetry(telemetry(
            (attempt("recognized"),), started_at=START.replace(second=59),
        ))

        self.clock.advance(minutes=BUCKET_MINUTES)
        minutes = {
            entry["minute_start"]: entry["recognition"]
            for entry in self.ring.unsent_minutes()
        }
        self.assertEqual(minutes[minute_key(START)]["recognized"], 1)
        self.assertNotIn(
            minute_key(START + timedelta(minutes=1)), minutes,
            "the minute it was recorded in never opened",
        )

    def test_a_burst_from_a_bucket_already_delivered_is_counted_now(self):
        # The app replaces a bucket row rather than merging into it, so
        # opening a new minute inside a bucket it already holds would make the
        # next post replace that bucket with this one minute and throw the
        # rest away. Counted late instead, exactly as a stranded throttle is.
        self.ring.record_heartbeat()
        self.clock.advance(minutes=BUCKET_MINUTES)
        self.ring.mark_sent(self.ring.unsent_minutes())

        self.ring.record_event_telemetry(telemetry(
            (attempt("recognized"),), started_at=START.replace(second=59),
        ))

        self.clock.advance(minutes=BUCKET_MINUTES)
        minutes = {
            entry["minute_start"]: entry["recognition"]
            for entry in self.ring.unsent_minutes()
        }
        recorded = minute_key(START + timedelta(minutes=BUCKET_MINUTES))
        self.assertEqual(list(minutes), [recorded])
        self.assertEqual(minutes[recorded]["recognized"], 1)

    def test_a_burst_start_older_than_a_bucket_is_not_trusted(self):
        # A stepped clock, or a telemetry object off a queue: never reach
        # further back than the correction is for.
        self.clock.advance(minutes=BUCKET_MINUTES)
        self.ring.record_event_telemetry(telemetry(
            (attempt("recognized"),), started_at=START,
        ))

        self.clock.advance(minutes=BUCKET_MINUTES)
        minutes = [entry["minute_start"] for entry in self.ring.unsent_minutes()]
        self.assertEqual(minutes, [minute_key(START + timedelta(minutes=BUCKET_MINUTES))])

    def test_a_throttle_from_a_minute_the_ring_never_opened_is_not_lost(self):
        ring = MetricsRing(
            clock=self.clock,
            retry_counts=lambda: {"2026-09-08T09:00:00Z": {"http_429": 3}},
        )

        ring.record_event_telemetry(telemetry((attempt("recognized"),)))

        self.clock.advance(minutes=BUCKET_MINUTES)
        self.assertEqual(
            ring.unsent_minutes()[0]["recognition"]["http_429"], 3,
            "counted late in the drain minute rather than dropped",
        )

    def test_the_ocr_client_counts_a_throttled_request_for_the_ring_to_drain(self):
        ocr.count_retryable_failure("http_429")
        ocr.count_retryable_failure("http_429")

        drained = ocr.drain_retry_counts()

        self.assertEqual(len(drained), 1, "one minute")
        minute, causes = next(iter(drained.items()))
        self.assertRegex(minute, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$")
        self.assertEqual(causes["http_429"], 2)
        self.assertEqual(ocr.drain_retry_counts(), {})

    def test_heartbeats_are_counted_into_the_minute_they_landed_in(self):
        for _ in range(4):
            self.ring.record_heartbeat()

        self.assertEqual(self.minute()["heartbeats"], 4)

    def test_a_minute_from_the_bucket_in_progress_is_never_offered(self):
        # A closed minute is not enough. The app replaces a five-minute bucket
        # row with whatever arrives for it and walks its quota ledger to match,
        # so offering :20 while :21 to :24 are still being counted would put
        # one minute of a five-minute bucket on the wire and throw the other
        # four away when they followed.
        self.ring.record_heartbeat()
        self.assertEqual(self.ring.unsent_minutes(), [])

        self.clock.advance(minutes=1)
        self.assertEqual(
            self.ring.unsent_minutes(), [], "the minute closed; its bucket did not",
        )

        self.clock.advance(minutes=BUCKET_MINUTES)
        self.assertEqual(len(self.ring.unsent_minutes()), 1)

    def test_a_bucket_is_offered_whole_or_not_at_all(self):
        for _ in range(7):
            self.ring.record_heartbeat()
            self.clock.advance(minutes=1)

        # 10:20 to 10:26 recorded, the clock now at 10:27: the 10:20 bucket has
        # closed and the 10:25 one has not.
        minutes = [entry["minute_start"] for entry in self.ring.unsent_minutes()]

        self.assertEqual(minutes, [
            "2026-09-08T10:20:00Z", "2026-09-08T10:21:00Z", "2026-09-08T10:22:00Z",
            "2026-09-08T10:23:00Z", "2026-09-08T10:24:00Z",
        ])

    def test_a_minute_is_offered_once_and_never_again(self):
        self.ring.record_heartbeat()
        self.clock.advance(minutes=BUCKET_MINUTES)
        minutes = self.ring.unsent_minutes()
        self.assertEqual(len(minutes), 1)

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

    def run_for(self, minutes: int, *, ring=None, deliver: bool = True) -> None:
        """Live minutes, with the rollup collecting whole closed buckets."""
        ring = ring or self.ring
        for _ in range(minutes):
            ring.record_heartbeat()
            self.clock.advance(minutes=1)
            batch = ring.unsent_minutes()
            if deliver and batch:
                ring.mark_sent(batch)

    def test_four_hours_of_delivered_minutes_say_nothing(self):
        # The 2026-09-09 regression: past three hours of uptime the ring began
        # evicting minutes the app had already stored, and journalled every
        # one of them as `ring_full` -- an hourly warning about nothing.
        with self.assertNoLogs("gate_controller.metrics", level="WARNING"):
            self.run_for(240)

        status = self.ring.status()
        self.assertEqual(status["minutes_dropped"], 0)
        # Delivered minutes are retired at the retry window, so the ring never
        # reaches its capacity at all on a healthy gate.
        self.assertLessEqual(status["minutes_held"], SENT_RETENTION_MINUTES + BUCKET_MINUTES)

    def test_four_hours_with_the_app_down_warn_once_about_the_unsent_minutes(self):
        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.run_for(240, deliver=False)

        self.assertEqual(len(logs.output), 1)
        self.assertIn("gate_metrics stage=ring_full", logs.output[0])
        self.assertIn("capacity=180", logs.output[0])
        # 240 minutes recorded, 180 held: sixty minutes of real loss, and the
        # counters line says so.
        self.assertEqual(self.ring.status()["minutes_dropped"], 60)

    def test_the_loss_warning_is_one_line_an_hour_carrying_the_count_since(self):
        ring = MetricsRing(clock=self.clock, max_minutes=5, retry_counts=lambda: {})

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.run_for(245, ring=ring, deliver=False)

        # 240 minutes of loss, one line at the transition and one an hour
        # after that -- four lines, not 240, and never a bare running total
        # nobody can date: each says how many were lost since the last one.
        self.assertEqual(len(logs.output), 4)
        self.assertIn("unsent_dropped=1 since_last=1 capacity=5", logs.output[0])
        self.assertIn("unsent_dropped=61 since_last=60 capacity=5", logs.output[1])
        self.assertIn("unsent_dropped=181 since_last=60 capacity=5", logs.output[-1])
        self.assertEqual(ring.status()["minutes_dropped"], 240)

    def test_a_delivered_post_closes_the_loss_episode(self):
        ring = MetricsRing(clock=self.clock, max_minutes=5, retry_counts=lambda: {})

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.run_for(10, ring=ring, deliver=False)
        self.assertEqual(len(logs.output), 1)

        with self.assertLogs("gate_controller.metrics", level="INFO") as logs:
            ring.mark_sent(ring.unsent_minutes())
        self.assertIn("gate_metrics stage=ring_recovered", logs.output[0])

        # The next loss is a fresh transition, journalled at once rather than
        # waiting out the hour the previous episode had started.
        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.run_for(10, ring=ring, deliver=False)
        self.assertIn("unsent_dropped=1 since_last=1", logs.output[0])

    def test_delivered_minutes_are_retired_and_leave_the_ring_for_the_unsent(self):
        self.run_for(90)
        status = self.ring.status()

        # Ninety minutes lived through, and only the retry window's worth is
        # still held: the older ones were delivered and are finished with, so
        # the ring's capacity stands ready for an outage rather than for a
        # backlog of what the app already has.
        self.assertLess(status["minutes_held"], 90)
        self.assertLessEqual(
            status["minutes_held"], SENT_RETENTION_MINUTES + BUCKET_MINUTES)
        self.assertEqual(status["minutes_dropped"], 0)

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
        # The write is the rollup thread's job, not the burst thread's.
        self.assertTrue(ledger.flush())
        self.assertEqual(self.ledger().month_to_date(), 5)
        self.assertEqual(
            json.loads(self.path.read_text()),
            {"month": "2026-09", "billed_lookups": 5},
        )

    def test_the_burst_thread_never_touches_the_sd_card(self):
        # `record_billed` runs from the pipeline wrapper, microseconds after a
        # gate decision. An fsync there is a stall on the path that opens the
        # gate; the rollup thread does the write on its own cycle instead.
        ledger = self.ledger()
        touched = []
        for module, name in (("os", "fsync"), ("os", "replace")):
            original = getattr(__import__(module), name)

            def spy(*args, _name=name, _original=original, **kwargs):
                touched.append(_name)
                return _original(*args, **kwargs)

            patch = unittest.mock.patch(f"{module}.{name}", spy)
            patch.start()
            self.addCleanup(patch.stop)

        ledger.record_billed(4)
        self.assertEqual(touched, [], "no filesystem write on the burst path")

        self.assertTrue(ledger.flush())
        self.assertEqual(touched, ["fsync", "replace"])

    def test_a_slow_card_does_not_block_the_heartbeat_or_the_next_burst(self):
        # The heartbeat thread reads `quota_status` every 15 s and the burst
        # thread counts into the same ledger. Neither may wait on an fsync,
        # which means the lock must not span the write.
        ledger = self.ledger()
        ledger.record_billed(1)
        writing = Event()
        release = Event()
        original = os.fsync

        def slow_fsync(descriptor):
            writing.set()
            release.wait(5)
            return original(descriptor)

        patch = unittest.mock.patch("os.fsync", slow_fsync)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(release.set)

        writer = Thread(target=ledger.flush, daemon=True)
        writer.start()
        self.assertTrue(writing.wait(5))

        try:
            self.assertEqual(ledger.month_to_date(), 1)
            self.assertEqual(ledger.record_billed(1), 2)
        finally:
            release.set()
            writer.join(5)
        self.assertFalse(writer.is_alive())

    def test_a_flush_with_nothing_new_writes_nothing(self):
        ledger = self.ledger()
        ledger.record_billed(1)

        self.assertTrue(ledger.flush())
        self.assertFalse(ledger.flush())

    def test_the_counter_starts_again_when_the_month_does(self):
        ledger = self.ledger()
        ledger.record_billed(7)
        ledger.flush()

        self.clock.advance(days=30)

        self.assertEqual(ledger.month_to_date(), 0)
        ledger.flush()
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

    def test_a_count_that_cannot_be_vouched_for_is_withheld_not_reported_as_zero(self):
        # The tile has to read "not reported", never "0 of 2500" -- which says
        # the owner has their whole month left on the day the card stopped
        # taking writes, and is the reading that lets the gate stop opening.
        self.path.write_text("{not json")
        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            ledger = self.ledger()

        self.assertFalse(ledger.reportable())
        self.assertIn(
            "gate_metrics stage=quota_unreported detail=load",
            "\n".join(logs.output),
        )

        ring = MetricsRing(quota=ledger, clock=self.clock, retry_counts=lambda: {})
        ring.record_event_telemetry(telemetry((attempt("recognized"),)))

        self.assertEqual(ring.quota_status(), {})
        self.clock.advance(minutes=BUCKET_MINUTES)
        self.assertNotIn("cloud", ring.unsent_minutes()[0])

    def test_a_new_month_is_a_zero_the_controller_can_vouch_for_again(self):
        self.path.write_text("{not json")
        with self.assertLogs("gate_controller.metrics", level="WARNING"):
            ledger = self.ledger()
        self.assertFalse(ledger.reportable())

        self.clock.advance(days=30)

        with self.assertLogs("gate_controller.metrics", level="INFO") as logs:
            self.assertTrue(ledger.reportable())
        self.assertIn("gate_metrics stage=quota_reported", "\n".join(logs.output))

    def test_an_unwritable_ledger_is_journalled_once_and_keeps_counting(self):
        ledger = QuotaLedger(
            Path("/proc/gate-metrics-does-not-exist/quota.json"), clock=self.clock,
        )

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            ledger.record_billed(1)
            ledger.flush()
            ledger.record_billed(1)
            ledger.flush()

        journal = "\n".join(logs.output)
        self.assertEqual(
            journal.count("stage=quota_write_failed"), 1, "once, not per cycle",
        )
        self.assertIn("gate_metrics stage=quota_unreported detail=write", journal)
        self.assertEqual(ledger.month_to_date(), 2)
        self.assertFalse(
            ledger.reportable(), "counting continues; reporting does not",
        )

    def test_the_ring_reports_the_burn_down_the_heartbeat_block_accepts(self):
        ring = MetricsRing(quota=self.ledger(), clock=self.clock, retry_counts=lambda: {})

        ring.record_event_telemetry(telemetry((
            attempt("recognized"), attempt("no_plate"), attempt("ocr_busy"),
        )))

        self.assertEqual(ring.quota_status(), {
            "recognition_lookups_month_to_date": 2,
            "recognition_lookup_quota": DEFAULT_LOOKUP_QUOTA,
        })
        self.clock.advance(minutes=BUCKET_MINUTES)
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
            ring.flush_quota()

            self.assertTrue((Path(directory) / "metrics-quota.json").is_file())


if __name__ == "__main__":
    unittest.main()
