import unittest
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from gate_controller.backpressure import ActivityGate
from gate_controller.cloudflare_client import CloudflareMetricsReporter
from gate_controller.metrics import (
    MAX_MINUTES_PER_POST, MetricsRing, MetricsRollupWorker,
    ROLLUP_BACKOFF_MAX_SECONDS, SCHEMA_VERSION,
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
        for _ in range(minutes):
            self.ring.record_heartbeat()
            self.clock.advance(minutes=1)

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
            def unsent_minutes(**kwargs):
                raise RuntimeError("ring is broken")

        worker = MetricsRollupWorker(Exploding(), self.sent, clock=self.clock)

        with self.assertLogs("gate_controller.metrics", level="WARNING") as logs:
            self.assertEqual(worker.run_once(), 0)

        self.assertIn("gate_metrics stage=cycle_failed", logs.output[0])

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
