"""What the app actually ends up holding, driven by the real rollup worker.

The controller can post a valid body, on time, and still lose most of what it
counted. ``POST /api/controller/metrics`` does not merge a post into a bucket
it already holds:

* ``upsertControllerHealth`` (``worker/repos/controllerHealth.ts``) is
  ``on conflict(controller_id, bucket_start) do update set metrics =
  excluded.metrics, heartbeats = excluded.heartbeats`` -- it **replaces** the
  row with whatever arrived;
* ``quotaDeltas`` (``worker/repos/controllerQuota.ts``) then advances the
  monthly ledger by *(incoming minus stored)* for those same buckets, so the
  ledger is walked back to match the replacement;
* ``storedBucketMetrics`` reads back at most ``MAX_BUCKETS_PER_POST`` (12) of
  them, so a post spanning a thirteenth bucket has that bucket counted again
  every time a lost response makes the controller re-send.

A post landing three minutes into a five-minute bucket therefore delivers three
minutes and destroys the other two when they follow. With the worker waking
every ``poll_interval`` from *thread start*, where the phase is whatever second
the process happened to come up on, that was the normal case rather than the
edge one: measured at a start phase of 190 s, the app saw 15 of 30 billed
lookups and 60 of 120 heartbeats.

So this drives the real :class:`MetricsRing` and :class:`MetricsRollupWorker`
through the transcription of the app's fold, upsert and delta below, from
several thread-start phases, and asserts the app ends up holding all of it.
"""
import unittest
from datetime import datetime, timedelta, timezone

from gate_controller.metrics import (
    BUCKET_MINUTES, MAX_BUCKETS_PER_POST, MetricsRing, MetricsRollupWorker,
)
from gate_controller.telemetry import (
    EventTelemetry, OcrAttemptTelemetry, StageDurations,
)


# --- the app's side, transcribed from ciaran-finnegan/access-gate-ui@main ----

#: `SUMMED_COUNTERS` in `worker/contracts/controller-metrics/contract.ts`.
SUMMED_COUNTERS = frozenset({
    "ocr_attempts", "billed_lookups", "recognized", "unread_frames",
    "ocr_error", "ocr_timeout", "ocr_busy", "http_429",
    "local_attempts", "local_recognized",
})
#: `WORST_IS_LOWEST` in `worker/repos/controllerHealth.ts`.
WORST_IS_LOWEST = frozenset({
    "mem_available_kib", "swap_free_kib", "disk_free_bytes",
    "uptime_seconds", "process_uptime_seconds",
})
#: `QUOTA_COUNTERS`: ledger column -> the metric that feeds it.
QUOTA_COUNTERS = {
    "billed_lookups": "billed_lookups",
    "attempts": "ocr_attempts",
    "recognized": "recognized",
    "local_recognized": "local_recognized",
}


def bucket_start_for(timestamp: str) -> str:
    """`bucketStartFor`: the ISO minute rounded down to five minutes."""
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:00Z").replace(
        tzinfo=timezone.utc,
    )
    return moment.replace(
        minute=moment.minute // BUCKET_MINUTES * BUCKET_MINUTES,
    ).isoformat()


def _merge(into: dict, other: dict) -> None:
    """`mergeMetrics`: counters add, gauges keep the worst reading."""
    for key, value in other.items():
        previous = into.get(key)
        if not isinstance(value, (int, float)) or not isinstance(previous, (int, float)):
            into[key] = value
        elif key in SUMMED_COUNTERS:
            into[key] = previous + value
        elif key in WORST_IS_LOWEST:
            into[key] = min(previous, value)
        else:
            into[key] = max(previous, value)


def fold_into_buckets(minutes: list[dict]) -> list[dict]:
    """`foldIntoBuckets`: one-minute rows into five-minute buckets."""
    buckets: dict[str, dict] = {}
    for minute in sorted(minutes, key=lambda entry: entry["minute_start"]):
        start = bucket_start_for(minute["minute_start"])
        metrics = {**minute.get("recognition", {}), **minute.get("cloud", {})}
        existing = buckets.get(start)
        if existing is None:
            buckets[start] = {
                "bucketStart": start,
                "heartbeats": minute["heartbeats"],
                "metrics": dict(metrics),
            }
            continue
        existing["heartbeats"] += minute["heartbeats"]
        _merge(existing["metrics"], metrics)
    return [buckets[start] for start in sorted(buckets)]


class AppSide:
    """`ingestMetrics`, `upsertControllerHealth` and the quota ledger."""

    def __init__(self):
        self.health: dict[str, dict] = {}
        self.quota = dict.fromkeys(QUOTA_COUNTERS, 0)
        self.posts = 0

    def ingest(self, payload: dict) -> None:
        self.posts += 1
        buckets = fold_into_buckets(payload["minutes"])
        # `storedBucketMetrics`: a bounded primary-key read of at most twelve.
        readable = list(dict.fromkeys(
            bucket["bucketStart"] for bucket in buckets
        ))[:MAX_BUCKETS_PER_POST]
        stored = {
            start: self.health[start]["metrics"]
            for start in readable if start in self.health
        }
        deltas = self._quota_deltas(buckets, stored)
        for bucket in buckets:
            # `do update set metrics = excluded.metrics`: a replacement, not
            # a merge. This one line is the whole of the finding.
            self.health[bucket["bucketStart"]] = {
                "heartbeats": bucket["heartbeats"],
                "metrics": dict(bucket["metrics"]),
            }
        for column, amount in deltas.items():
            self.quota[column] = max(0, self.quota[column] + amount)

    @staticmethod
    def _counters(metrics: dict) -> dict:
        return {
            column: metrics.get(metric, 0)
            for column, metric in QUOTA_COUNTERS.items()
        }

    def _quota_deltas(self, buckets: list[dict], stored: dict) -> dict:
        totals = dict.fromkeys(QUOTA_COUNTERS, 0)
        for bucket in buckets:
            incoming = self._counters(bucket["metrics"])
            previous = self._counters(stored.get(bucket["bucketStart"], {}))
            for column in QUOTA_COUNTERS:
                totals[column] += incoming[column] - previous[column]
        return totals

    @property
    def heartbeats(self) -> int:
        return sum(row["heartbeats"] for row in self.health.values())

    def metric(self, name: str) -> int:
        return sum(row["metrics"].get(name, 0) for row in self.health.values())


# --- the controller's side ---------------------------------------------------

#: A bucket boundary, so the phases below are measured from a real one.
START = datetime(2026, 9, 8, 10, 20, 0, tzinfo=timezone.utc)
TRAFFIC_MINUTES = 30
HEARTBEATS_PER_MINUTE = 4
BILLED_PER_MINUTE = 1


class SteppingClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def a_billed_burst() -> EventTelemetry:
    """One cloud read that was answered, so one lookup off the allowance."""
    return EventTelemetry(
        trace_id="0123456789abcdef",
        stage_durations=StageDurations(),
        frames=(),
        ocr_attempts=(OcrAttemptTelemetry(
            frame_sequence=0, status="recognized", duration_ms=820.0,
            source="cloud", cloud_lookup=True,
        ),),
        decision_outcome="opened",
        decision_reason="authorised",
        actuation_claim="won",
        actuation_attempted=True,
        relay_outcome="pulsed",
        outbox_attempt=0,
        delivery_state="pending",
    )


class BucketDeliveryTests(unittest.TestCase):
    """Every phase delivers every count, because every bucket arrives whole."""

    #: Seconds after a five-minute boundary that the worker thread comes up.
    #: 190 s is the phase the reviewer measured the loss at.
    PHASES = (0, 10, 100, 190, 250)

    def _run(self, phase: int, *, resend: bool = False) -> AppSide:
        app = AppSide()
        clock = SteppingClock(START)
        ring = MetricsRing(clock=clock, retry_counts=lambda: {})

        def send(payload):
            app.ingest(payload)
            if resend:
                # The acknowledgement was lost and the controller re-sent the
                # same body. The app is deliberately idempotent about this;
                # the deltas must come out at zero the second time.
                app.ingest(payload)

        worker = MetricsRollupWorker(ring, send, clock=clock)

        # The traffic is the same whatever second the worker came up on.
        traffic_ends = START + timedelta(minutes=TRAFFIC_MINUTES)
        finish = START + timedelta(minutes=TRAFFIC_MINUTES + 2 * BUCKET_MINUTES + 1)
        thread_start = START + timedelta(seconds=phase)
        next_wake = thread_start

        while clock.now < finish:
            if clock.now >= thread_start and clock.now >= next_wake:
                worker.run_once()
                next_wake = clock.now + timedelta(
                    seconds=worker.next_wait_seconds()
                )
            if clock.now < traffic_ends:
                second = clock.now.second
                if second % (60 // HEARTBEATS_PER_MINUTE) == 0:
                    ring.record_heartbeat()
                if second == 7:
                    for _ in range(BILLED_PER_MINUTE):
                        ring.record_event_telemetry(a_billed_burst())
            clock.advance(1)
        return app

    def test_every_thread_start_phase_delivers_every_count(self):
        expected_heartbeats = TRAFFIC_MINUTES * HEARTBEATS_PER_MINUTE
        expected_billed = TRAFFIC_MINUTES * BILLED_PER_MINUTE

        for phase in self.PHASES:
            with self.subTest(phase=phase):
                app = self._run(phase)

                self.assertEqual(
                    app.heartbeats, expected_heartbeats,
                    f"phase {phase}s: the app holds "
                    f"{app.heartbeats}/{expected_heartbeats} heartbeats",
                )
                self.assertEqual(
                    app.quota["billed_lookups"], expected_billed,
                    f"phase {phase}s: the burn-down holds "
                    f"{app.quota['billed_lookups']}/{expected_billed} lookups",
                )
                self.assertEqual(app.quota["attempts"], expected_billed)
                self.assertEqual(app.metric("billed_lookups"), expected_billed)

    def test_a_re_sent_post_adds_nothing_the_second_time(self):
        expected_billed = TRAFFIC_MINUTES * BILLED_PER_MINUTE

        for phase in self.PHASES:
            with self.subTest(phase=phase):
                app = self._run(phase, resend=True)

                self.assertEqual(app.quota["billed_lookups"], expected_billed)
                self.assertEqual(
                    app.heartbeats, TRAFFIC_MINUTES * HEARTBEATS_PER_MINUTE,
                )

    def test_every_bucket_start_reaches_the_app_exactly_once(self):
        for phase in self.PHASES:
            with self.subTest(phase=phase):
                delivered: list[str] = []
                app = AppSide()
                clock = SteppingClock(START)
                ring = MetricsRing(clock=clock, retry_counts=lambda: {})

                def send(payload, _seen=delivered, _app=app):
                    _seen.extend(
                        bucket["bucketStart"]
                        for bucket in fold_into_buckets(payload["minutes"])
                    )
                    _app.ingest(payload)

                worker = MetricsRollupWorker(ring, send, clock=clock)
                next_wake = START + timedelta(seconds=phase)
                finish = START + timedelta(minutes=TRAFFIC_MINUTES + 11)
                while clock.now < finish:
                    if clock.now >= next_wake:
                        worker.run_once()
                        next_wake = clock.now + timedelta(
                            seconds=worker.next_wait_seconds()
                        )
                    if clock.now < START + timedelta(minutes=TRAFFIC_MINUTES):
                        if clock.now.second == 0:
                            ring.record_heartbeat()
                    clock.advance(1)

                self.assertEqual(
                    len(delivered), len(set(delivered)),
                    "a bucket_start delivered twice is a bucket the app "
                    "overwrote with a fragment of itself",
                )
                self.assertEqual(len(delivered), TRAFFIC_MINUTES // BUCKET_MINUTES)


if __name__ == "__main__":
    unittest.main()
