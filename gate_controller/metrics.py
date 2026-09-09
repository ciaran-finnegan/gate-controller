"""A bounded per-minute recognition ring and the five-minute cloud rollup.

The app has implemented ``POST /api/controller/metrics`` since phase 1 and
nothing on the Pi has ever posted to it, which is why the Recognition and
Quota tiles on ``/dashboard/health`` read "not reported". This module is the
missing producer.

Three rules shape everything here.

**Bounded by construction.** The ring is memory only, keyed by the minute and
capped at :data:`DEFAULT_RING_MINUTES` minutes; a delivered minute is retired
once it is past the retry window and the oldest minute is dropped when the cap
is reached anyway. Nothing accumulates without a ceiling, nothing is
written to the SD card except the one small month-to-date counter the quota
burn-down cannot be reconstructed without.

**Never on the decision path.** The ring is fed after a burst has finished,
from the telemetry object the processor already built, and the POST runs on
its own background thread. No lock in this module is ever held across a
network call, and the rollup stands down entirely while a gate decision is in
flight, because metrics rank below event delivery which ranks below the gate.

**Nothing the app will refuse.** The metrics contract rejects the whole body
with ``400`` for a single unknown key -- unlike the heartbeat, which silently
drops one -- so the wire format here is an explicit allow-list of numbers and
one ISO timestamp, and ``tests/test_metrics_contract.py`` runs a real payload
through a copy of the app's own rules.
"""
import json
import logging
import os
import random
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock

from .ocr import drain_retry_counts

LOGGER = logging.getLogger(__name__)

#: The schema the app's contract accepts; it rejects anything else outright.
SCHEMA_VERSION = 1
#: The app folds minutes into five-minute buckets and stores one row each.
BUCKET_MINUTES = 5
BUCKET_SECONDS = BUCKET_MINUTES * 60
#: `MAX_BUCKETS_PER_POST` in the app's `controllerHealth.ts`: the read-back of
#: already-stored buckets, which is what stops a replayed post from being
#: counted twice, is capped at twelve. A post that spanned a thirteenth bucket
#: would have that bucket's quota delta applied again on every re-send.
MAX_BUCKETS_PER_POST = 12
#: `MAX_MINUTES_PER_POST` in the app contract. More than this is a 400 -- and
#: it is exactly twelve whole buckets, which is why minutes are only ever
#: offered a whole bucket at a time.
MAX_MINUTES_PER_POST = MAX_BUCKETS_PER_POST * BUCKET_MINUTES
#: How long a *delivered* minute is kept before the ring retires it.
#:
#: A minute that has been posted is only still held for two reasons, both of
#: them short-lived: `_burst_minute_key_locked` refuses to re-open a minute --
#: or a bucket -- the app already has, and it reaches back at most one bucket;
#: and a re-send of the same batch has to stay idempotent. One post's worth of
#: minutes covers both with an order of magnitude to spare, and retiring the
#: rest keeps the ring's capacity where it is needed: undelivered data. Left
#: to age out on capacity alone, three hours of ordinary delivered minutes
#: filled the ring and journalled every eviction as if something had been
#: lost (the `ring_full dropped=` lines seen hourly on 2026-09-09).
SENT_RETENTION_MINUTES = MAX_MINUTES_PER_POST
#: The contract's ceiling for a minute's heartbeat count.
MAX_HEARTBEATS_PER_MINUTE = 60
#: The contract's ceiling for every recognition counter (1e7).
MAX_COUNTER = 10_000_000
#: The contract's ceiling for a duration in milliseconds.
MAX_DURATION_MS = 600_000

DEFAULT_ROLLUP_SECONDS = 300.0
MIN_ROLLUP_SECONDS = 60.0
MAX_ROLLUP_SECONDS = 3600.0
#: Three hours of minutes, ~200 bytes each: bounded, and far more backfill
#: than the 60 minutes one POST may carry.
DEFAULT_RING_MINUTES = 180
#: Durations kept per minute for the percentiles. A minute of gate traffic is
#: a handful of frames; this is a ceiling, not a target.
MAX_DURATION_SAMPLES = 64
#: Plate Recognizer's monthly allowance on the plan this gate is on.
DEFAULT_LOOKUP_QUOTA = 2500

ROLLUP_BACKOFF_BASE_SECONDS = 5.0
ROLLUP_BACKOFF_MAX_SECONDS = 300.0
#: How long after a bucket boundary the post is made. The ring is fed after a
#: burst finishes, so a burst that ended on the boundary is still being counted
#: when the boundary passes; a few seconds costs nothing and squares the race.
ROLLUP_BOUNDARY_DELAY_SECONDS = 5.0
#: The floor on any wait in :meth:`MetricsRollupWorker.run_forever`. A backoff
#: deadline may pull a wake earlier than the next boundary, but never into a
#: tight loop.
ROLLUP_MIN_WAIT_SECONDS = 5.0

METRICS_PATH = "/api/controller/metrics"

# --- what Plate Recognizer bills -------------------------------------------
#
# The allowance is charged for a request the service actually processed. An
# attempt that never left the Pi, or that never got a reply, is not a lookup.
#
# The first question is therefore not "what did this attempt read" but "did a
# request go out at all". `OcrAttemptTelemetry.cloud_lookup` answers it, set at
# the one place that knows -- the dispatch site in `ocr.py` -- because the two
# are not the same question. With `GATE_LOCAL_OCR_MODE=active` and
# `GATE_LOCAL_OCR_CLOUD=fallback`, a confident on-device read returns before
# any `session.post`: the attempt's status is `recognized`, and billing it
# would count precisely the lookups on-device recognition stopped spending.
# With `GATE_LOCAL_OCR_CLOUD=always` the same local read answers and the cloud
# request goes out regardless, so that one *is* billed. Hence `cloud_lookup`
# rather than `source`, which only says which reader the gate believed.
#
# Given a request did go out:
#
# Billed:   `recognized` and `no_plate` -- a 2xx with, or without, a plate.
#           `read_timeout` -- the request was sent in full and the reply never
#           arrived; ocr.py already refuses to retry it for exactly this
#           reason ("the request may already have been accepted and billed").
#           `ocr_timeout` -- the processor abandoning a request whose decision
#           budget ran out, *when a request had actually gone out*. Then it is
#           the same physical event as a `read_timeout` seen from the
#           decision's side of the clock rather than the socket's, and
#           classifying the two differently made the burn-down disagree with
#           itself. `cloud_lookup` is what separates the two cases, and it
#           carries the dispatch site's answer here as everywhere else:
#           `ocr_started` only says the request *thread* was launched, and
#           between that and `session.post` sit the 1.05 s pacing window, the
#           downscaled upload and the on-device guard, any of which can eat
#           the last of the budget with nothing sent. The second frame of a
#           burst waiting out the throttle is the ordinary case, not a corner.
#           The response-shape causes below, which can only arise *after* a
#           2xx body was received and therefore after the lookup was spent.
# Not billed: `ocr_busy` (never left the Pi), `connect_timeout`, `tls_error`,
#           `connection_error`, `request_error` (never arrived), and every
#           `http_*` cause including `http_429` -- a throttled request is
#           refused before it is processed.
#
# Residual error, bounded and in the safe direction: a recogniser that cannot
# report its dispatches -- one whose `recognise` has no `on_post_started`
# parameter -- keeps this counter's original assumption that a cloud read
# posts, so an `ocr_timeout` from it is billed. Every recogniser the
# controller ships does report.
BILLED_STATUSES = frozenset({"recognized", "no_plate", "ocr_timeout"})
BILLED_FAILURE_CAUSES = frozenset({
    "read_timeout",
    "invalid_json", "invalid_payload", "invalid_results", "invalid_result_entry",
    "invalid_confidence", "invalid_response", "no_usable_plate",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def minute_key(moment: datetime) -> str:
    """The minute this moment belongs to, as the contract's ISO timestamp."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def bucket_key(moment: datetime) -> str:
    """The five-minute bucket this moment belongs to, in the minute format.

    The app rounds `minute_start` down to a five-minute boundary and stores one
    row per bucket, so this is the same arithmetic the other side does. Minute
    keys sort lexicographically, so `key < bucket_key(now)` is exactly "this
    minute's bucket closed before now".
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.replace(
        minute=(moment.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
    ).strftime("%Y-%m-%dT%H:%M:00Z")


def _bucket_of(minute: str) -> str:
    """The bucket a ring minute key belongs to. Runs on the rollup thread."""
    parsed = datetime.strptime(minute, "%Y-%m-%dT%H:%M:00Z")
    return bucket_key(parsed.replace(tzinfo=timezone.utc))


def _grouped_by_bucket(minutes: list[str]):
    """Sorted minute keys, grouped into the buckets they belong to."""
    grouped: "OrderedDict[str, list[str]]" = OrderedDict()
    for key in minutes:
        grouped.setdefault(_bucket_of(key), []).append(key)
    return grouped.items()


def _bounded_counter(value: int) -> int:
    return max(0, min(MAX_COUNTER, int(value)))


def _percentile(samples: list[float], fraction: float) -> int:
    if not samples:
        return 0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return max(0, min(MAX_DURATION_MS, round(ordered[index])))


@dataclass
class _MinuteCounters:
    """One minute of recognition outcomes. Every field is a bounded count."""

    heartbeats: int = 0
    ocr_attempts: int = 0
    billed_lookups: int = 0
    recognized: int = 0
    unread_frames: int = 0
    ocr_error: int = 0
    ocr_timeout: int = 0
    ocr_busy: int = 0
    http_429: int = 0
    local_attempts: int = 0
    local_recognized: int = 0
    durations_ms: list[float] = field(default_factory=list)
    lookups_month_to_date: int | None = None
    lookup_quota: int | None = None

    def add_duration(self, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if value < 0 or value != value or len(self.durations_ms) >= MAX_DURATION_SAMPLES:
            return
        self.durations_ms.append(min(float(value), MAX_DURATION_MS))

    def recognition_block(self) -> dict:
        return {
            "ocr_attempts": _bounded_counter(self.ocr_attempts),
            "billed_lookups": _bounded_counter(self.billed_lookups),
            "recognized": _bounded_counter(self.recognized),
            # `unread_frames`, never `no_plate`: the contract refuses any key
            # matching /plate/ and rejects the whole body for it.
            "unread_frames": _bounded_counter(self.unread_frames),
            "ocr_error": _bounded_counter(self.ocr_error),
            "ocr_timeout": _bounded_counter(self.ocr_timeout),
            "ocr_busy": _bounded_counter(self.ocr_busy),
            "http_429": _bounded_counter(self.http_429),
            "ocr_ms_p50": _percentile(self.durations_ms, 0.5),
            "ocr_ms_p90": _percentile(self.durations_ms, 0.9),
            "local_attempts": _bounded_counter(self.local_attempts),
            "local_recognized": _bounded_counter(self.local_recognized),
        }

    def cloud_block(self) -> dict:
        block = {}
        if self.lookups_month_to_date is not None:
            block["recognition_lookups_month_to_date"] = _bounded_counter(
                self.lookups_month_to_date
            )
        if self.lookup_quota is not None:
            block["recognition_lookup_quota"] = _bounded_counter(self.lookup_quota)
        return block

    def to_wire(self, minute: str) -> dict:
        wire = {
            "minute_start": minute,
            "heartbeats": max(0, min(MAX_HEARTBEATS_PER_MINUTE, int(self.heartbeats))),
            "recognition": self.recognition_block(),
        }
        cloud = self.cloud_block()
        if cloud:
            wire["cloud"] = cloud
        return wire


class _StageLogger:
    """Journal a state change once, not once per cycle.

    Same shape as :class:`gate_controller.cloud_health.TransitionLogger`, with
    the ``gate_metrics`` prefix this worker is documented under.
    """

    def __init__(self, stage: str, *, logger: logging.Logger | None = None,
                 repeat_interval: float = 600.0,
                 clock: Callable[[], float] | None = None):
        self._stage = stage
        self._logger = logger or LOGGER
        self._repeat_interval = repeat_interval
        self._clock = clock or (lambda: _utc_now().timestamp())
        self._consecutive_failures = 0
        self._last_logged_at: float | None = None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def failure(self, detail: str) -> None:
        self._consecutive_failures += 1
        try:
            now = self._clock()
        except Exception:
            now = None
        if (
            now is not None and self._last_logged_at is not None
            and now - self._last_logged_at < self._repeat_interval
        ):
            return
        self._last_logged_at = now
        self._logger.warning(
            "gate_metrics stage=%s_failed detail=%s consecutive=%d",
            self._stage, detail, self._consecutive_failures,
        )

    def success(self) -> None:
        if self._consecutive_failures:
            self._logger.info(
                "gate_metrics stage=%s_recovered failures=%d",
                self._stage, self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._last_logged_at = None


class _DropLogger:
    """Journal lost minutes once per episode, not once per minute.

    The counting sibling of :class:`_StageLogger`, and rate-limited the same
    way: the first loss after a delivery is journalled at once -- that is the
    transition worth waking to -- and while the loss continues at most one
    further line an hour carries the count since the last one. A successful
    post closes the episode, so the next loss reads as a new transition rather
    than a running total nobody can date.

    Only *undelivered* minutes come here. A delivered minute leaving the ring
    is housekeeping, not loss, and says nothing at all.
    """

    def __init__(self, *, logger: logging.Logger | None = None,
                 repeat_interval: float = 3600.0,
                 clock: Callable[[], float] | None = None):
        self._logger = logger or LOGGER
        self._repeat_interval = repeat_interval
        self._clock = clock or (lambda: _utc_now().timestamp())
        self._episode_total = 0
        self._since_last_line = 0
        self._last_logged_at: float | None = None

    def dropped(self, count: int, *, capacity: int) -> None:
        if count <= 0:
            return
        self._episode_total += count
        self._since_last_line += count
        try:
            now = self._clock()
        except Exception:
            now = None
        if (
            now is not None and self._last_logged_at is not None
            and now - self._last_logged_at < self._repeat_interval
        ):
            return
        self._last_logged_at = now
        self._logger.warning(
            "gate_metrics stage=ring_full unsent_dropped=%d since_last=%d capacity=%d",
            self._episode_total, self._since_last_line, capacity,
        )
        self._since_last_line = 0

    def delivered(self) -> None:
        """A post landed: whatever was lost before it is a closed episode."""
        if self._episode_total:
            self._logger.info(
                "gate_metrics stage=ring_recovered unsent_dropped=%d",
                self._episode_total,
            )
        self._episode_total = 0
        self._since_last_line = 0
        self._last_logged_at = None


class QuotaLedger:
    """The controller's own count of the cloud lookups it spent this month.

    The count is local because there is nothing to read it from: the Snapshot
    API reference documents a usage endpoint (``total_calls``, ``usage.calls``)
    only for the on-premise ``/info/`` service, and this controller posts to
    the cloud ``/v1/plate-reader/`` endpoint. A cloud usage call would in any
    case put a second dependency on the token the decision path holds, and the
    app's contract asks for "the controller's own count" precisely because it
    cannot reconstruct one: most attempts return no plate and never become an
    event in D1.

    So this is the only state that outlives the process: a few dozen bytes
    rewritten atomically, which is nothing beside the SQLite writes a burst
    already makes.

    **The write never happens on the burst thread.** :meth:`record_billed` runs
    from the pipeline wrapper, immediately after a gate decision; an SD-card
    ``fsync`` there is a stall on the path that opens the gate. So it takes a
    lock around one integer and sets a flag, and the rollup worker thread calls
    :meth:`flush` on its own cycle. The cost of a power cut between the two is
    at most one rollup period of billed lookups; the cost of the alternative is
    measured in milliseconds on every burst.

    Every failure here is swallowed. A month-to-date counter that cannot be
    written must cost a metric, never a gate opening -- but it must not be
    allowed to report a confident zero either, which is what
    :meth:`reportable` is for.
    """

    def __init__(self, path: Path | None, *, quota: int = DEFAULT_LOOKUP_QUOTA,
                 clock: Callable[[], datetime] | None = None):
        self._path = Path(path) if path is not None else None
        self._quota = _bounded_counter(quota)
        self._clock = clock or _utc_now
        self._lock = Lock()
        self._month = ""
        self._billed = 0
        self._dirty = False
        self._write_failed = False
        self._load_failed = False
        self._reported = True
        self._writes = _StageLogger("quota_write")
        self._load()

    @property
    def quota(self) -> int:
        return self._quota

    def month_to_date(self) -> int:
        with self._lock:
            self._roll_over_locked()
            return self._billed

    def reportable(self) -> bool:
        """Whether the count is worth putting in front of the owner.

        False once a write has failed, or once the stored counter came back
        unreadable, because then the number in memory is not the month's total
        and a burn-down tile reading "0 of 2500" is worse than one reading "not
        reported". A failed load latches until the month rolls over, at which
        point zero is the right answer again; a failed write clears as soon as
        one succeeds.
        """
        with self._lock:
            self._roll_over_locked()
            return not (self._write_failed or self._load_failed)

    def record_billed(self, count: int) -> int:
        """Count billed lookups. Runs on the burst thread: no file I/O here."""
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return self.month_to_date()
        with self._lock:
            self._roll_over_locked()
            self._billed = _bounded_counter(self._billed + count)
            self._dirty = True
            return self._billed

    def flush(self) -> bool:
        """Persist the counter if it has moved. Called from the rollup thread.

        The lock is taken twice and held across neither the ``fsync`` nor the
        ``os.replace``: a burst counting a lookup while the card is busy waits
        on an integer, not on the filesystem.
        """
        with self._lock:
            self._roll_over_locked()
            if not self._dirty or self._path is None:
                self._dirty = False
                return False
            payload = json.dumps(
                {"month": self._month, "billed_lookups": self._billed}
            )
            self._dirty = False
        written = self._write(payload)
        with self._lock:
            if not written:
                # Try again next cycle rather than losing the count silently.
                self._dirty = True
            self._write_failed = not written
            self._announce_locked()
        return written

    def _current_month(self) -> str:
        try:
            return self._clock().astimezone(timezone.utc).strftime("%Y-%m")
        except Exception:
            return self._month

    def _roll_over_locked(self) -> None:
        month = self._current_month()
        if month and month != self._month:
            self._month = month
            self._billed = 0
            self._dirty = True
            # A new month starts from a zero this controller is sure of, so a
            # counter that could not be read last month stops poisoning it.
            self._load_failed = False
            self._announce_locked()

    def _announce_locked(self) -> None:
        """One journal line per change of reporting state, never per cycle."""
        reportable = not (self._write_failed or self._load_failed)
        if reportable == self._reported:
            return
        self._reported = reportable
        if reportable:
            LOGGER.info("gate_metrics stage=quota_reported")
        else:
            LOGGER.warning(
                "gate_metrics stage=quota_unreported detail=%s",
                "load" if self._load_failed else "write",
            )

    def _load(self) -> None:
        month = self._current_month()
        self._month = month
        self._billed = 0
        if self._path is None:
            return
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            LOGGER.warning("gate_metrics stage=quota_read_failed detail=unreadable")
            # There was a counter and it cannot be read, so this month's total
            # is unknown: withhold it rather than restart the burn-down at zero
            # and tell the owner they have their whole allowance left.
            self._load_failed = True
            self._announce_locked()
            return
        if not isinstance(stored, dict):
            self._load_failed = True
            self._announce_locked()
            return
        if stored.get("month") != month:
            # A counter from a previous month is history, not this month's
            # burn-down. Starting from zero is the only correct reading.
            return
        billed = stored.get("billed_lookups")
        if isinstance(billed, int) and not isinstance(billed, bool) and billed >= 0:
            self._billed = _bounded_counter(billed)
        else:
            self._load_failed = True
            self._announce_locked()

    def _write(self, payload: str) -> bool:
        """The atomic rewrite. No lock is held; nothing here raises."""
        if self._path is None:
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".metrics-quota-", dir=self._path.parent,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    descriptor = -1
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self._path)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
        except Exception as error:
            self._writes.failure(type(error).__name__)
            return False
        self._writes.success()
        return True


class MetricsRing:
    """A fixed-size ring of one-minute recognition summaries.

    Fed from the finished telemetry of each burst -- the same object the
    processor persists and the outbox ships -- so the decision path itself is
    untouched. Reads and writes are short and lock-guarded; the lock is never
    held across a network call.
    """

    def __init__(self, *, quota: QuotaLedger | None = None,
                 max_minutes: int = DEFAULT_RING_MINUTES,
                 clock: Callable[[], datetime] | None = None,
                 retry_counts: Callable[[], dict] | None = None):
        if isinstance(max_minutes, bool) or not isinstance(max_minutes, int) or max_minutes < 1:
            raise ValueError("max_minutes must be a positive integer")
        self._minutes: "OrderedDict[str, _MinuteCounters]" = OrderedDict()
        self._max_minutes = max_minutes
        self._quota = quota
        self._clock = clock or _utc_now
        self._retry_counts = retry_counts or drain_retry_counts
        self._lock = Lock()
        self._sent: set[str] = set()
        #: Undelivered minutes the ring had to drop -- real loss. Minutes that
        #: were delivered and then retired are not counted here.
        self._dropped_minutes = 0
        self._drops = _DropLogger(clock=self._drop_clock)

    def _drop_clock(self) -> float:
        """The ring's own clock, as the seconds the drop logger paces on."""
        return self._clock().timestamp()

    # -- recording ---------------------------------------------------------
    def record_heartbeat(self) -> None:
        """One heartbeat POST landed. Never raises: it runs on that path."""
        try:
            with self._lock:
                self._minute_locked().heartbeats += 1
        except Exception:
            return

    def record_processing_result(self, result: object) -> None:
        """Count one finished burst from its ``ProcessingResult``."""
        self.record_event_telemetry(getattr(result, "telemetry", None))

    def record_event_telemetry(self, telemetry: object) -> None:
        """Count one finished burst. Best effort by design: a metric must
        never be able to raise into the pipeline that produced it."""
        try:
            self._record_event_telemetry(telemetry)
        except Exception:
            LOGGER.warning("gate_metrics stage=record_failed detail=telemetry")

    def _record_event_telemetry(self, telemetry: object) -> None:
        attempts = list(getattr(telemetry, "ocr_attempts", None) or ())
        local = getattr(telemetry, "local_ocr", None)
        billed = 0
        with self._lock:
            minute = self._minute_locked(self._burst_minute_key_locked(telemetry))
            for attempt in attempts:
                status = getattr(attempt, "status", "unknown")
                cause = getattr(attempt, "failure_cause", None)
                # Absent on anything but a read the on-device recogniser
                # answered without a request going out, so the default is the
                # cloud attempt this counter has always assumed.
                spent = bool(getattr(attempt, "cloud_lookup", True))
                minute.ocr_attempts += 1
                minute.add_duration(getattr(attempt, "duration_ms", None))
                if status == "recognized":
                    minute.recognized += 1
                elif status == "no_plate":
                    minute.unread_frames += 1
                elif status == "ocr_timeout":
                    minute.ocr_timeout += 1
                elif status == "ocr_busy":
                    minute.ocr_busy += 1
                elif status == "ocr_error":
                    minute.ocr_error += 1
                if self._is_billed(status, cause, spent):
                    billed += 1
            minute.billed_lookups += billed
            if local is not None:
                frames = getattr(local, "frames", 0)
                if isinstance(frames, int) and not isinstance(frames, bool) and frames > 0:
                    minute.local_attempts += frames
                # The local block is per event, not per frame: it reports the
                # read the event ended up with, so this counts events the
                # on-device reader answered, not frames it read.
                if getattr(local, "status", None) == "recognized":
                    minute.local_recognized += 1
            self._absorb_retry_counts_locked(minute)
        if billed and self._quota is not None:
            # Outside the ring's lock: the ledger writes a file.
            self._quota.record_billed(billed)
        self._stamp_quota()

    @staticmethod
    def _is_billed(status: object, cause: object, cloud_lookup: bool = True) -> bool:
        """Whether this attempt spent one of the month's cloud lookups.

        A request that never went out cannot have been billed, whatever it
        read: that is the whole of the local-recognition saving, and counting
        it would erase the saving from the burn-down.
        """
        if not cloud_lookup:
            return False
        if status in BILLED_STATUSES:
            return True
        return status == "ocr_error" and cause in BILLED_FAILURE_CAUSES

    def _absorb_retry_counts_locked(
        self, current: _MinuteCounters | None = None,
    ) -> None:
        """Fold the OCR client's throttle counter into the minutes it happened in.

        A 429 is retried inside the client and the retry usually succeeds, so
        without this counter a throttled request leaves no trace anywhere but
        the journal -- and throttling is the failure mode that costs a whole
        burst its second frame.

        The counter stamps its own minute, because the drain happens when a
        burst finishes and a throttled burst is exactly the one that ran long:
        crediting the drain minute pushed a boundary-straddling burst's 429s
        into the next minute, and four times in five into the next bucket. A
        minute the ring never opened, or one already delivered, falls back to
        the minute in progress -- it is better counted late than not at all.

        ``current`` is the minute a finished burst was counted into. The
        rollup thread has no such minute and passes none: the fallback minute
        is then opened only if something actually has to go in it, because a
        cycle that opened one every five minutes would keep the ring
        permanently non-empty and post a bucket of nothing forever.
        """
        try:
            counts = self._retry_counts()
        except Exception:
            return
        if not isinstance(counts, dict):
            return
        for key, causes in counts.items():
            if not isinstance(causes, dict):
                continue
            throttled = causes.get("http_429", 0)
            if (not isinstance(throttled, int) or isinstance(throttled, bool)
                    or throttled <= 0):
                continue
            target = None
            if isinstance(key, str) and key not in self._sent:
                target = self._minutes.get(key)
            if target is None:
                if current is None:
                    current = self._minute_locked()
                target = current
            target.http_429 += throttled

    def _stamp_quota(self) -> None:
        """Record the burn-down against the current minute as a gauge.

        Nothing is stamped when the ledger cannot vouch for its own count: an
        absent key renders as "not reported", where a stamped zero would read
        as a full allowance still to spend.
        """
        if self._quota is None:
            return
        try:
            if not self._quota.reportable():
                return
            month_to_date = self._quota.month_to_date()
            quota = self._quota.quota
        except Exception:
            return
        try:
            with self._lock:
                minute = self._minute_locked()
                minute.lookups_month_to_date = month_to_date
                minute.lookup_quota = quota
        except Exception:
            return

    def _burst_minute_key_locked(self, telemetry: object) -> str | None:
        """The minute a burst *started*, when the ring can still safely use it.

        A burst is counted when it finishes, and the burst that straddles a
        boundary is the slow one -- the one whose counters are most worth
        having in the right minute. This is the same correction the 429
        counter makes, and it is made under the same conditions.

        Three of them, all refusals that fall back to the minute in progress:

        * The start has to be a real timestamp in the recent past. Nothing in
          the future, and nothing older than one bucket -- a longer reach
          buys nothing (a burst has a decision budget of seconds) and would
          let a stepped clock stamp an arbitrary minute.
        * The minute must not already have been delivered.
        * Nor may any minute of its **bucket**, delivered or not present. The
          app replaces a bucket row rather than merging into it, so opening a
          new minute inside a bucket already posted would re-post that bucket
          as just this one minute and throw the rest of it away -- the very
          failure this rollup was rewritten to avoid.

        A refusal is never an error: anything unexpected about the timestamp
        answers ``None``, and the burst is counted in the minute in progress
        exactly as it was before. Losing a burst's counters over its clock
        would be a far worse trade than crediting them a minute late.
        """
        started = getattr(
            getattr(telemetry, "stage_timestamps", None),
            "burst_processing_started_at", None,
        )
        if not isinstance(started, datetime):
            return None
        try:
            now = self._clock()
            age = (now - started).total_seconds()
            if not 0 <= age < BUCKET_SECONDS:
                return None
            key = minute_key(started)
            if key >= minute_key(now) or key in self._sent:
                return None
            bucket = _bucket_of(key)
            if any(_bucket_of(sent) == bucket for sent in self._sent):
                return None
        except Exception:
            return None
        return key

    def _minute_locked(self, key: str | None = None) -> _MinuteCounters:
        key = minute_key(self._clock()) if key is None else key
        counters = self._minutes.get(key)
        if counters is None:
            counters = _MinuteCounters()
            self._minutes[key] = counters
            self._minutes.move_to_end(key)
            self._evict_locked()
        return counters

    def _evict_locked(self) -> None:
        """Retire what has been delivered; count only what has not.

        Two different things happen when a minute leaves the ring, and the
        journal used to call them both a loss. A minute the app has already
        stored leaves because it is finished with -- ordinary housekeeping,
        silent. A minute that never got out is data the owner will not see on
        the health tiles, and that is worth a line.

        Retirement runs first, so on a healthy gate the capacity limit is
        never reached at all: the ring settles at roughly
        :data:`SENT_RETENTION_MINUTES` delivered minutes plus whatever is
        still in flight, and the remaining capacity stands ready for an
        outage.
        """
        self._retire_sent_locked()
        dropped = 0
        while len(self._minutes) > self._max_minutes:
            # By key, not by insertion order, which a clock stepped backwards
            # by NTP would otherwise make the wrong thing to trust -- the same
            # reason `unsent_minutes` sorts rather than iterating.
            oldest = min(self._minutes)
            self._minutes.pop(oldest, None)
            if oldest in self._sent:
                self._sent.discard(oldest)
                continue
            self._dropped_minutes += 1
            dropped += 1
        if dropped:
            self._drops.dropped(dropped, capacity=self._max_minutes)

    def _retire_sent_locked(self) -> None:
        """Drop delivered minutes older than the retry/backfill window.

        Bounded by the clock rather than by count so that the window means the
        same thing whether the gate is busy or idle. Anything unexpected from
        the clock retires nothing and leaves the capacity limit to do its job.
        """
        if not self._sent:
            return
        try:
            horizon = minute_key(
                self._clock() - timedelta(minutes=SENT_RETENTION_MINUTES))
        except Exception:
            return
        for key in [key for key in self._sent if key < horizon]:
            self._sent.discard(key)
            self._minutes.pop(key, None)

    # -- reading -----------------------------------------------------------
    def unsent_minutes(self, *, limit: int = MAX_MINUTES_PER_POST,
                       now: datetime | None = None) -> list[dict]:
        """Undelivered minutes from *closed five-minute buckets*, oldest first.

        Whole buckets, or nothing. The app does not merge a post into a bucket
        it already holds -- ``upsertControllerHealth`` is
        ``do update set metrics = excluded.metrics``, which **replaces** the
        row -- and it advances the quota ledger by (incoming minus stored) for
        that bucket. So delivering minutes ``:00`` and ``:01`` now and ``:02``
        to ``:04`` later does not fill the bucket in: the second post throws
        the first two minutes away and the ledger is walked back to match.

        A minute closing is therefore not enough; its *bucket* has to have
        closed, which is what ``key < bucket_key(now)`` tests. Then every
        bucket_start is delivered exactly once, complete.

        The batch is a whole number of buckets for the second half of the same
        reason. The app reads back at most ``MAX_BUCKETS_PER_POST`` (12) stored
        buckets to compute that delta, so a post spanning a thirteenth bucket
        has that bucket's counters applied again every time a lost response
        makes the controller re-send.
        """
        limit = max(BUCKET_MINUTES, min(MAX_MINUTES_PER_POST, int(limit)))
        horizon = bucket_key(now or self._clock())
        with self._lock:
            # Sorted rather than trusted to insertion order: a clock stepped
            # backwards by NTP would otherwise replay minutes out of order.
            pending = sorted(
                key for key in self._minutes
                if key < horizon and key not in self._sent
            )
            chosen: list[str] = []
            buckets = 0
            for bucket, minutes in _grouped_by_bucket(pending):
                if buckets >= MAX_BUCKETS_PER_POST or len(chosen) + len(minutes) > limit:
                    break
                chosen.extend(minutes)
                buckets += 1
            return [self._minutes[key].to_wire(key) for key in chosen]

    def mark_sent(self, minutes) -> None:
        keys = {
            entry.get("minute_start") if isinstance(entry, dict) else entry
            for entry in minutes or ()
        }
        delivered = False
        with self._lock:
            for key in keys:
                if isinstance(key, str):
                    self._sent.add(key)
                    delivered = True
            # `_sent` can only name minutes the ring still holds.
            self._sent &= set(self._minutes)
            # A post landed, so any earlier loss is a closed episode and the
            # minutes it delivered start ageing out of their own accord.
            self._retire_sent_locked()
        if delivered:
            self._drops.delivered()

    def quota_status(self) -> dict:
        """The two quota keys the heartbeat's `cloud` block already accepts.

        Empty when the ledger cannot vouch for its count, so the tile reads
        "not reported" instead of a confident "0 of 2500" -- a number that
        would say the owner has their whole month left on the day the card
        stopped taking writes. The app's heartbeat narrows `cloud` with
        `boundedNumbers(value, CLOUD_CEILINGS)`, which accepts numbers only, so
        there is no key to carry a status token: the transition is journalled
        (`gate_metrics stage=quota_unreported`) and the absence is the signal.
        """
        if self._quota is None:
            return {}
        try:
            if not self._quota.reportable():
                return {}
            return {
                "recognition_lookups_month_to_date": self._quota.month_to_date(),
                "recognition_lookup_quota": self._quota.quota,
            }
        except Exception:
            return {}

    def absorb_retry_counts(self) -> None:
        """Fold in the client's pending 429s without a burst to hang them on.

        The burst path drains the counter too, but a drain needs a burst: the
        429s of the last vehicle before a quiet spell would sit in the client
        until the next one, and be credited after their own bucket had closed
        and been delivered. The rollup cycle drains as well, just before it
        reads the ring, so a bucket is only ever posted once everything known
        about it is in.

        Never raises: it is called from the rollup thread, which is the ring's
        only reader. It opens no minute of its own -- a cycle that did would
        leave the ring permanently non-empty and post empty buckets forever.
        """
        try:
            with self._lock:
                self._absorb_retry_counts_locked()
        except Exception:
            LOGGER.warning("gate_metrics stage=record_failed detail=retry_counts")

    def flush_quota(self) -> bool:
        """Persist the ledger, from a thread that is not the burst thread."""
        if self._quota is None:
            return False
        try:
            return self._quota.flush()
        except Exception:
            LOGGER.warning("gate_metrics stage=quota_flush_failed detail=unexpected")
            return False

    def status(self) -> dict:
        """What the ring holds. ``minutes_dropped`` counts *undelivered*
        minutes only -- data the app will never see. Delivered minutes retired
        after :data:`SENT_RETENTION_MINUTES` are not a loss and are not
        counted, which is why a healthy controller reports zero however long
        it has been up."""
        with self._lock:
            held = len(self._minutes)
            pending = sum(1 for key in self._minutes if key not in self._sent)
            dropped = self._dropped_minutes
        return {
            "minutes_held": held,
            "minutes_pending": pending,
            "minutes_dropped": dropped,
            "capacity": self._max_minutes,
        }


class MetricsRollupWorker:
    """POST the closed minutes once per period, and never at a bad moment.

    Strictly below event delivery and the gate itself: the send is skipped
    while a decision is in flight, a failure backs off from 5 s to 5 minutes
    instead of retrying in a tight loop -- the behaviour that made the
    2026-09-05 D1 outage worse -- and nothing here can raise into the caller.
    """

    def __init__(self, ring: MetricsRing, send: Callable[[dict], object], *,
                 controller_id: str = "primary",
                 poll_interval: float = DEFAULT_ROLLUP_SECONDS,
                 activity=None,
                 clock: Callable[[], datetime] | None = None,
                 backoff_base: float = ROLLUP_BACKOFF_BASE_SECONDS,
                 backoff_max: float = ROLLUP_BACKOFF_MAX_SECONDS,
                 jitter: Callable[[], float] | None = None,
                 health: _StageLogger | None = None):
        if not (0 < backoff_base <= backoff_max):
            raise ValueError("metrics backoff must satisfy 0 < base <= max")
        self._ring = ring
        self._send = send
        self._controller_id = controller_id
        self._poll_interval = poll_interval
        # The cadence, quantised down to a whole number of five-minute buckets
        # and never below one. A bucket is the unit the app stores, so posting
        # oftener than one closes cannot deliver anything new -- and posting on
        # a period that is not a multiple of a bucket would drift the wake off
        # the boundary again, one cycle at a time.
        self._post_period = BUCKET_SECONDS * max(
            1, int(float(poll_interval) // BUCKET_SECONDS)
        )
        self._activity = activity
        self._clock = clock or _utc_now
        self._backoff_base = float(backoff_base)
        self._backoff_max = float(backoff_max)
        self._jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self._health = health or _StageLogger("rollup")
        self._failures = 0
        self._retry_at: datetime | None = None
        self._deferred = False

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    def payload(self, minutes: list[dict]) -> dict:
        return {
            "controller_id": self._controller_id,
            "schema_version": SCHEMA_VERSION,
            "minutes": minutes,
        }

    def run_once(self) -> int:
        """Send at most one POST. Returns the minutes delivered."""
        try:
            return self._run_once()
        except Exception:
            # A rollup cycle that throws would kill its thread and take the
            # ring's only reader with it.
            LOGGER.warning("gate_metrics stage=cycle_failed detail=unexpected")
            return 0

    def _run_once(self) -> int:
        now = self._clock()
        # Standing down means standing down: the fsync and `os.replace` a
        # ledger flush costs are exactly the kind of SD-card work a gate
        # decision must not be sharing the card with, so the deferral is
        # checked before anything is written and not only before the POST.
        # Nothing is lost by waiting -- the count is in memory, the next cycle
        # is seconds away, and `run_forever` flushes on the way out.
        if self._deferred_by_gate():
            return 0
        # The ledger's only writer thread. `record_billed` on the burst path
        # sets a flag; the fsync happens here, where a slow SD card delays a
        # metric instead of a gate.
        self._ring.flush_quota()
        if self._retry_at is not None and now < self._retry_at:
            return 0
        # Fold in any 429s the client has counted since the last burst. The
        # burst path drains too, but a throttled burst followed by a quiet
        # spell would otherwise leave its 429s in the client's counter until
        # the next vehicle -- long after the bucket they belong to had closed
        # and gone.
        self._ring.absorb_retry_counts()
        minutes = self._ring.unsent_minutes(now=now)
        if not minutes:
            return 0
        payload = self.payload(minutes)
        try:
            self._send(payload)
        except Exception as error:
            self._schedule_retry(now, error)
            return 0
        self._ring.mark_sent(minutes)
        self._failures = 0
        self._retry_at = None
        self._health.success()
        return len(minutes)

    def _deferred_by_gate(self) -> bool:
        """Stand down while the gate is deciding; say so once, not per cycle."""
        busy = None
        reason = getattr(self._activity, "busy_reason", None)
        if callable(reason):
            try:
                busy = reason()
            except Exception:
                busy = None
        if busy is None:
            if self._deferred:
                self._deferred = False
                LOGGER.info("gate_metrics stage=resumed")
            return False
        if not self._deferred:
            self._deferred = True
            LOGGER.info("gate_metrics stage=deferred reason=%s", busy)
        return True

    def _schedule_retry(self, now: datetime, error: BaseException) -> None:
        self._failures += 1
        seconds = min(
            self._backoff_max,
            self._backoff_base * (2 ** min(self._failures - 1, 16)),
        )
        try:
            jitter = self._jitter()
        except Exception:
            jitter = 1.0
        if not isinstance(jitter, (int, float)) or isinstance(jitter, bool) or not 0.5 <= jitter <= 1.5:
            jitter = 1.0
        self._retry_at = now + timedelta(
            seconds=min(self._backoff_max, seconds * jitter)
        )
        self._health.failure(_error_detail(error))

    def next_wait_seconds(self, now: datetime | None = None) -> float:
        """How long to sleep before the next cycle.

        Two rules, in this order:

        * **The wake is aligned to the wall clock**, not to when this thread
          happened to start. A five-minute bucket closes at :00, :05, :10 and
          so on, and this posts a few seconds after that, so what goes out is
          the bucket that just closed. Waking every 300 s from thread start
          instead put the post in the middle of a bucket -- which, before
          minutes were held back until their bucket closed, is what fed the app
          half a bucket and let it replace the row with that half.
        * **A pending backoff can pull the wake earlier**, so the documented
          "5 s, then 10, then 20 ... up to 5 minutes" is a schedule the worker
          actually keeps rather than a number it computes and sleeps through.

        Never below :data:`ROLLUP_MIN_WAIT_SECONDS`, on either path. The floor
        is not only about a failing endpoint: the wake lands a few seconds
        *after* a boundary, so a cycle that runs a moment before one computes
        a wait of a millisecond and comes straight back -- and every one of
        those cycles flushes the ledger to the SD card. Waiting past the
        boundary instead costs a bucket nothing; it is already closed.
        """
        now = now or self._clock()
        boundary = self._seconds_to_next_boundary(now)
        if self._retry_at is None:
            return max(ROLLUP_MIN_WAIT_SECONDS, boundary)
        retry = (self._retry_at - now).total_seconds()
        return max(ROLLUP_MIN_WAIT_SECONDS, min(boundary, retry))

    def _seconds_to_next_boundary(self, now: datetime) -> float:
        """Seconds until the next bucket boundary this worker posts on."""
        try:
            epoch = now.timestamp()
        except Exception:
            return self._poll_interval
        elapsed = (epoch - ROLLUP_BOUNDARY_DELAY_SECONDS) % self._post_period
        return self._post_period - elapsed

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            if stop_event.is_set():
                break
            stop_event.wait(self.next_wait_seconds())
        # Whatever the burst thread counted since the last cycle is worth the
        # one write it takes to keep across a restart.
        self._ring.flush_quota()


def _error_detail(error: BaseException) -> str:
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"http_{status}"
    return type(error).__name__


# --- configuration ----------------------------------------------------------
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def metrics_enabled(environment) -> bool:
    raw = str(environment.get("GATE_METRICS_ENABLED", "true")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError("GATE_METRICS_ENABLED must be true or false")


def metrics_rollup_seconds(environment) -> float:
    raw = str(environment.get("GATE_METRICS_ROLLUP_SECONDS", int(DEFAULT_ROLLUP_SECONDS))).strip()
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "GATE_METRICS_ROLLUP_SECONDS must be a number between "
            f"{int(MIN_ROLLUP_SECONDS)} and {int(MAX_ROLLUP_SECONDS)}"
        ) from error
    if not MIN_ROLLUP_SECONDS <= seconds <= MAX_ROLLUP_SECONDS:
        raise ValueError(
            "GATE_METRICS_ROLLUP_SECONDS must be a number between "
            f"{int(MIN_ROLLUP_SECONDS)} and {int(MAX_ROLLUP_SECONDS)}"
        )
    return seconds


def recognition_lookup_quota(environment) -> int:
    raw = str(environment.get("GATE_RECOGNITION_LOOKUP_QUOTA", DEFAULT_LOOKUP_QUOTA)).strip()
    try:
        quota = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "GATE_RECOGNITION_LOOKUP_QUOTA must be an integer between 1 and 1000000"
        ) from error
    if not 1 <= quota <= 1_000_000:
        raise ValueError(
            "GATE_RECOGNITION_LOOKUP_QUOTA must be an integer between 1 and 1000000"
        )
    return quota


def build_metrics_ring(environment, *, state_directory: Path | None = None) -> MetricsRing | None:
    """The ring, or ``None`` when ``GATE_METRICS_ENABLED`` is false.

    Disabled means disabled: no ring, no ledger, no worker, no thread.
    """
    if not metrics_enabled(environment):
        return None
    ledger_path = (
        Path(state_directory) / "metrics-quota.json"
        if state_directory is not None else None
    )
    return MetricsRing(
        quota=QuotaLedger(
            ledger_path, quota=recognition_lookup_quota(environment),
        ),
    )
