import unittest
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from gate_controller.backpressure import ActivityGate
from gate_controller.cloudflare_client import CloudflareMetricsReporter
from gate_controller.metrics import (
    BUCKET_MINUTES, BUCKET_SECONDS, MAX_BUCKETS_PER_POST, MAX_MINUTES_PER_POST,
    MetricsRing, MetricsRollupWorker, ROLLUP_BACKOFF_BASE_SECONDS,
    ROLLUP_BACKOFF_MAX_SECONDS, ROLLUP_BOUNDARY_DELAY_SECONDS,
    ROLLUP_MIN_WAIT_SECONDS, SCHEMA_VERSION, minute_key,
)


START = datetime(2026, 9, 8, 10, 20, 30, tzinfo=timezone.utc)


class FrozenClock:
    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class Recorder:
    def __init__(self, error: Exception | None = None):
        self.payloads = []
        self.error = error

    def __call__(self, payload):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error


class FakeClient:
    def __init__(self):
        self.requests = []

    def post_json(self, path, payload, **kwargs):
        self.requests.append((path, payload))
        return {}


class MetricsRollupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FrozenClock()
        self.ring = MetricsRing(clock=self.clock, retry_counts=lambda: {})
        self.sent = Recorder()

    def worker(self, send=None, **kwargs) -> MetricsRollupWorker:
        return MetricsRollupWorker(
            self.ring, send or self.sent, clock=self.clock, jitter=lambda: 1.0, **kwargs,
        )

    def fill(self, minutes: int = 1) -> None:
        """Record `minutes` busy minutes and leave the clock past their bucket.

        The extra five minutes are the point: the ring holds a minute back
        until the whole five-minute bucket it belongs to has closed, because
        the app replaces a bucket row rather than merging into it.
        """
        for _ in range(minutes):
            self.ring.record_heartbeat()
            self.clock.advance(minutes=1)
        self.clock.advance(minutes=BUCKET_MINUTES)

    def test_one_post_carries_the_closed_minutes_and_the_schema(self):
        self.fill(3)

        self.assertEqual(self.worker().run_once(), 3)

        self.assertEqual(len(self.sent.payloads), 1)
        payload = self.sent.payloads[0]
        self.assertEqual(payload["controller_id"], "primary")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(payload["minutes"]), 3)

    def test_nothing_to_report_is_not_a_post(self):
        self.assertEqual(self.worker().run_once(), 0)
        self.assertEqual(self.sent.payloads, [])

    def test_a_delivered_minute_is_never_posted_twice(self):
        self.fill(2)
        worker = self.worker()

        worker.run_once()
        self.fill(1)
        worker.run_once()

        posted = [
            entry["minute_start"]
            for payload in self.sent.payloads for entry in payload["minutes"]
        ]
        self.assertEqual(len(posted), len(set(posted)))
        self.assertEqual(len(posted), 3)

    def test_an_outage_backfills_every_missing_minute_in_order(self):
        worker = self.worker()
        self.fill(25)

        self.assertEqual(worker.run_once(), 25)

        minutes = [entry["minute_start"] for entry in self.sent.payloads[0]["minutes"]]
        self.assertEqual(minutes, sorted(minutes))
        self.assertEqual(minutes[0], "2026-09-08T10:20:00Z")

    def test_a_long_outage_is_replayed_a_postful_at_a_time(self):
        worker = self.worker()
        self.fill(MAX_MINUTES_PER_POST + 5)

        self.assertEqual(worker.run_once(), MAX_MINUTES_PER_POST)
        self.assertEqual(worker.run_once(), 5)

    def test_a_failed_post_leaves_the_minutes_pending_and_does_not_raise(self):
        worker = self.worker(send=Recorder(TimeoutError("offline")))
        self.fill(2)

        self.assertEqual(worker.run_once(), 0)

        self.assertEqual(len(self.ring.unsent_minutes()), 2)

    def test_a_failing_endpoint_is_backed_off_instead_of_retried_in_a_loop(self):
        failing = Recorder(TimeoutError("offline"))
        worker = self.worker(send=failing)
        self.fill(1)

        for _ in range(5):
            worker.run_once()

        # One attempt, then the 5 s backoff holds the next four cycles off.
        self.assertEqual(len(failing.payloads), 1)
        self.clock.advance(seconds=6)
        worker.run_once()
        self.assertEqual(len(failing.payloads), 2)

    def test_the_backoff_is_capped_at_five_minutes(self):
        failing = Recorder(TimeoutError("offline"))
        worker = self.worker(send=failing)
        self.fill(1)

        for _ in range(20):
            worker.run_once()
            self.clock.advance(seconds=ROLLUP_BACKOFF_MAX_SECONDS)

        self.assertEqual(len(failing.payloads), 20)

    def test_recovery_is_journalled_once_per_transition(self):
        failing = Recorder(TimeoutError("offline"))
        worker = self.worker(send=failing)
        self.fill(1)

        with self.assertLogs("gate_controller.metrics", level="INFO") as logs:
            worker.run_once()
            self.clock.advance(seconds=ROLLUP_BACKOFF_MAX_SECONDS)
            failing.error = None
            worker.run_once()

        self.assertIn("gate_metrics stage=rollup_failed", logs.output[0])
        self.assertIn("gate_metrics stage=rollup_recovered", logs.output[1])

    def test_an_http_failure_is_journalled_by_status_alone(self):
        class Response:
            status_code = 503

        error = RuntimeError("boom")
        error.response = Response()
        worker = self.worker(send=Recorder(error))
        self.fill(1)

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            worker.run_once()

        self.assertIn("detail=http_503", logs.output[0])

    def test_the_rollup_stands_down_while_the_gate_is_deciding(self):
        activity = ActivityGate(quiet_seconds=5.0)
        worker = self.worker(activity=activity)
        self.fill(1)

        with activity.activity("burst"):
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(self.sent.payloads, [])

        self.assertEqual(worker.run_once(), 1)

    def test_standing_down_defers_the_sd_card_write_as_well_as_the_post(self):
        """A decision in flight must not be sharing the card with an fsync.

        The flush ran before the deferral check, so the cycle stood down from
        the POST -- the cheap, off-card half -- and went ahead with the
        `fsync` and `os.replace` anyway, which is the half the module docstring
        says ranks below the gate.
        """
        writes = []

        class RecordingRing(MetricsRing):
            def flush_quota(inner) -> bool:
                writes.append(True)
                return False

        ring = RecordingRing(clock=self.clock, retry_counts=lambda: {})
        ring.record_heartbeat()
        self.clock.advance(minutes=BUCKET_MINUTES)
        activity = ActivityGate(quiet_seconds=5.0)
        worker = MetricsRollupWorker(
            ring, self.sent, clock=self.clock, jitter=lambda: 1.0,
            activity=activity,
        )

        with activity.activity("burst"):
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(writes, [], "nothing was written while the gate decided")

        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(writes, [True], "and the next cycle writes as usual")

    def test_a_quiet_spell_does_not_strand_the_last_bursts_throttling(self):
        """The rollup drains the 429 counter too, not only a finished burst.

        A drain needs a burst, and the last vehicle before a quiet spell is
        exactly the one whose 429s would then sit in the client until the next
        vehicle -- long after the bucket they belong to had closed and gone.
        """
        pending = [{minute_key(START): {"http_429": 2}}]
        ring = MetricsRing(
            clock=self.clock,
            retry_counts=lambda: pending.pop() if pending else {},
        )
        # One burst opens the minute the throttling happened in, and then the
        # gate goes quiet: nothing else will ever call the drain.
        ring.record_heartbeat()
        self.clock.advance(minutes=BUCKET_MINUTES)
        worker = MetricsRollupWorker(
            ring, self.sent, clock=self.clock, jitter=lambda: 1.0,
        )

        self.assertEqual(worker.run_once(), 1)

        minute = self.sent.payloads[0]["minutes"][0]
        self.assertEqual(minute["minute_start"], minute_key(START))
        self.assertEqual(
            minute["recognition"]["http_429"], 2,
            "attributed before its own bucket was delivered",
        )

    def test_a_stalled_endpoint_does_not_hold_the_ring_against_the_pipeline(self):
        """A 60 s metrics POST must not block the burst that is deciding."""
        posting = Event()
        release = Event()

        def stalled(payload):
            posting.set()
            release.wait(5)

        self.fill(1)
        worker = self.worker(send=stalled)
        thread = Thread(target=worker.run_once, daemon=True)
        thread.start()
        self.assertTrue(posting.wait(5))

        try:
            # The ring is still writable and readable while the POST hangs:
            # no lock in the ring spans a network call.
            self.ring.record_heartbeat()
            self.assertIsNotNone(self.ring.status())
        finally:
            release.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())

    def test_a_cycle_that_throws_is_contained(self):
        class Exploding:
            @staticmethod
            def flush_quota():
                return False

            @staticmethod
            def absorb_retry_counts():
                return None

            @staticmethod
            def unsent_minutes(**kwargs):
                raise RuntimeError("ring is broken")

        worker = MetricsRollupWorker(Exploding(), self.sent, clock=self.clock)

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.assertEqual(worker.run_once(), 0)

        self.assertIn("gate_metrics stage=cycle_failed", logs.output[0])

    def test_the_wake_is_aligned_to_the_wall_clock_not_to_thread_start(self):
        # Whenever the thread starts, the next cycle lands just after a
        # five-minute boundary. Waking every 300 s from thread start instead
        # put the post in the middle of a bucket, which is the whole reason a
        # bucket used to reach the app in pieces.
        worker = self.worker()
        for phase in (0, 10, 100, 190, 250, 299):
            with self.subTest(phase=phase):
                now = START.replace(minute=20, second=0) + timedelta(seconds=phase)
                woken = now + timedelta(seconds=worker.next_wait_seconds(now))

                self.assertEqual(woken.minute % BUCKET_MINUTES, 0)
                self.assertEqual(woken.second, int(ROLLUP_BOUNDARY_DELAY_SECONDS))
                self.assertLessEqual(
                    (woken - now).total_seconds(), BUCKET_SECONDS,
                    "never more than one bucket away",
                )

    def test_a_slower_cadence_still_lands_on_a_bucket_boundary(self):
        worker = self.worker(poll_interval=600.0)

        for phase in range(0, 600, 37):
            with self.subTest(phase=phase):
                now = START.replace(minute=20, second=0) + timedelta(seconds=phase)
                wait = worker.next_wait_seconds(now)
                woken = now + timedelta(seconds=wait)

                self.assertEqual(woken.minute % BUCKET_MINUTES, 0)
                self.assertEqual(woken.second, int(ROLLUP_BOUNDARY_DELAY_SECONDS))
                self.assertGreater(wait, 0)
                self.assertLessEqual(wait, 600)

    def test_the_documented_backoff_is_a_schedule_the_worker_keeps(self):
        # The docs promise "5 s, doubling to 5 minutes". Sleeping the full poll
        # interval regardless made that a number the worker computed and then
        # ignored: the first retry was five minutes away, not five seconds.
        failing = Recorder(TimeoutError("offline"))
        worker = self.worker(send=failing)
        self.fill(1)

        worker.run_once()

        self.assertEqual(
            worker.next_wait_seconds(self.clock()), ROLLUP_BACKOFF_BASE_SECONDS,
        )

    def test_a_backoff_wait_is_never_a_tight_loop(self):
        failing = Recorder(TimeoutError("offline"))
        worker = self.worker(send=failing)
        self.fill(1)
        worker.run_once()

        # Well past the retry deadline: the wait floors rather than spinning.
        late = self.clock() + timedelta(minutes=1)

        self.assertGreaterEqual(worker.next_wait_seconds(late), ROLLUP_MIN_WAIT_SECONDS)

    def test_no_wait_anywhere_in_a_bucket_is_below_the_floor(self):
        # The floor used to be on the retry path only, so a healthy cycle
        # running a moment before a boundary computed waits of 0.0999 s,
        # 0.00999 s, 0.000999 s and came straight back -- each of those cycles
        # flushing the ledger to the SD card. The bucket is already closed;
        # waiting past the boundary costs it nothing.
        worker = self.worker()
        boundary = START.replace(minute=20, second=0, microsecond=0)

        for second in range(BUCKET_SECONDS):
            for micro in (0, 1, 100_000, 900_000, 999_000, 999_900):
                now = boundary + timedelta(seconds=second, microseconds=micro)
                wait = worker.next_wait_seconds(now)
                if wait < ROLLUP_MIN_WAIT_SECONDS:
                    self.fail(f"{wait} s wait at {now.isoformat()}")

    def test_a_healthy_cycle_waits_for_the_boundary_not_for_the_backoff(self):
        worker = self.worker()
        self.fill(1)
        worker.run_once()

        self.assertGreater(
            worker.next_wait_seconds(self.clock()), ROLLUP_MIN_WAIT_SECONDS,
        )

    def test_a_post_never_spans_more_buckets_than_the_app_reads_back(self):
        # The app reads back at most twelve stored buckets to work out the
        # quota delta. A thirteenth would have its counters applied again
        # every time a lost response made the controller re-send.
        self.fill(MAX_MINUTES_PER_POST + BUCKET_MINUTES)

        self.worker().run_once()

        buckets = {
            entry["minute_start"][:15] + ("0" if entry["minute_start"][15] < "5" else "5")
            for entry in self.sent.payloads[0]["minutes"]
        }
        self.assertLessEqual(len(buckets), MAX_BUCKETS_PER_POST)

    def test_run_forever_stops_on_the_stop_event(self):
        stop = Event()
        stop.set()

        self.worker(poll_interval=60).run_forever(stop)

        self.assertEqual(self.sent.payloads, [])

    def test_the_reporter_posts_the_body_the_worker_built(self):
        client = FakeClient()
        reporter = CloudflareMetricsReporter(client, "primary")
        self.fill(1)

        self.assertEqual(self.worker(send=reporter.send).run_once(), 1)

        path, payload = client.requests[0]
        self.assertEqual(path, "/api/controller/metrics")
        self.assertEqual(payload["controller_id"], "primary")
        self.assertEqual(len(payload["minutes"]), 1)


if __name__ == "__main__":
    unittest.main()
