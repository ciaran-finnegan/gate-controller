"""A bounded per-minute recognition ring and the five-minute cloud rollup.

The app has implemented ``POST /api/controller/metrics`` since phase 1 and
nothing on the Pi has ever posted to it, which is why the Recognition and
Quota tiles on ``/dashboard/health`` read "not reported". This module is the
missing producer.

Three rules shape everything here.

**Bounded by construction.** The ring is memory only, keyed by the minute and
capped at :data:`DEFAULT_RING_MINUTES` minutes; the oldest minute is dropped
when the cap is reached. Nothing accumulates without a ceiling, nothing is
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
#: `MAX_MINUTES_PER_POST` in the app contract. More than this is a 400.
MAX_MINUTES_PER_POST = 60
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

METRICS_PATH = "/api/controller/metrics"

# --- what Plate Recognizer bills -------------------------------------------
#
# The allowance is charged for a request the service actually processed. An
# attempt that never left the Pi, or that never got a reply, is not a lookup.
#
# Billed:   `recognized` and `no_plate` -- a 2xx with, or without, a plate.
#           `read_timeout` -- the request was sent in full and the reply never
#           arrived; ocr.py already refuses to retry it for exactly this
#           reason ("the request may already have been accepted and billed").
#           The response-shape causes below, which can only arise *after* a
#           2xx body was received and therefore after the lookup was spent.
# Not billed: `ocr_busy` (never left the Pi), `connect_timeout`, `tls_error`,
#           `connection_error`, `request_error` (never arrived), and every
#           `http_*` cause including `http_429` -- a throttled request is
#           refused before it is processed.
#
# Known undercount: `ocr_timeout` -- the processor abandoning a request whose
# decision budget ran out -- may or may not have been processed by the service
# by then. It is counted in `ocr_attempts` and `ocr_timeout` and deliberately
# not in `billed_lookups`, because guessing high would overstate the burn-down
# that decides whether the gate still opens at the end of the month.
BILLED_STATUSES = frozenset({"recognized", "no_plate"})
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
    rewritten atomically after each burst, which is nothing beside the SQLite
    writes the same burst already makes.

    Every failure here is swallowed. A month-to-date counter that cannot be
    written must cost a metric, never a gate opening.
    """

    def __init__(self, path: Path | None, *, quota: int = DEFAULT_LOOKUP_QUOTA,
                 clock: Callable[[], datetime] | None = None):
        self._path = Path(path) if path is not None else None
        self._quota = _bounded_counter(quota)
        self._clock = clock or _utc_now
        self._lock = Lock()
        self._month = ""
        self._billed = 0
        self._writes = _StageLogger("quota_write")
        self._load()

    @property
    def quota(self) -> int:
        return self._quota

    def month_to_date(self) -> int:
        with self._lock:
            self._roll_over_locked()
            return self._billed

    def record_billed(self, count: int) -> int:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return self.month_to_date()
        with self._lock:
            self._roll_over_locked()
            self._billed = _bounded_counter(self._billed + count)
            self._persist_locked()
            return self._billed

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
            self._persist_locked()

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
            return
        if not isinstance(stored, dict):
            return
        if stored.get("month") != month:
            # A counter from a previous month is history, not this month's
            # burn-down. Starting from zero is the only correct reading.
            return
        billed = stored.get("billed_lookups")
        if isinstance(billed, int) and not isinstance(billed, bool) and billed >= 0:
            self._billed = _bounded_counter(billed)

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        payload = json.dumps({"month": self._month, "billed_lookups": self._billed})
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
            return
        self._writes.success()


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
        self._dropped_minutes = 0

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
            minute = self._minute_locked()
            for attempt in attempts:
                status = getattr(attempt, "status", "unknown")
                cause = getattr(attempt, "failure_cause", None)
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
                if self._is_billed(status, cause):
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
    def _is_billed(status: object, cause: object) -> bool:
        if status in BILLED_STATUSES:
            return True
        return status == "ocr_error" and cause in BILLED_FAILURE_CAUSES

    def _absorb_retry_counts_locked(self, minute: _MinuteCounters) -> None:
        """Fold the OCR client's throttle counter into the current minute.

        A 429 is retried inside the client and the retry usually succeeds, so
        without this counter a throttled request leaves no trace anywhere but
        the journal -- and throttling is the failure mode that costs a whole
        burst its second frame.
        """
        try:
            counts = self._retry_counts()
        except Exception:
            return
        if not isinstance(counts, dict):
            return
        throttled = counts.get("http_429", 0)
        if isinstance(throttled, int) and not isinstance(throttled, bool) and throttled > 0:
            minute.http_429 += throttled

    def _stamp_quota(self) -> None:
        """Record the burn-down against the current minute as a gauge."""
        if self._quota is None:
            return
        try:
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

    def _minute_locked(self) -> _MinuteCounters:
        key = minute_key(self._clock())
        counters = self._minutes.get(key)
        if counters is None:
            counters = _MinuteCounters()
            self._minutes[key] = counters
            self._minutes.move_to_end(key)
            self._evict_locked()
        return counters

    def _evict_locked(self) -> None:
        while len(self._minutes) > self._max_minutes:
            oldest, _counters = self._minutes.popitem(last=False)
            self._sent.discard(oldest)
            self._dropped_minutes += 1
            if self._dropped_minutes == 1 or self._dropped_minutes % 60 == 0:
                LOGGER.warning(
                    "gate_metrics stage=ring_full dropped=%d capacity=%d",
                    self._dropped_minutes, self._max_minutes,
                )

    # -- reading -----------------------------------------------------------
    def unsent_minutes(self, *, limit: int = MAX_MINUTES_PER_POST,
                       now: datetime | None = None) -> list[dict]:
        """Closed, undelivered minutes, oldest first.

        The minute in progress is never returned: a minute is posted once, and
        the app folds whole minutes into five-minute buckets. Half a minute
        now and the rest later would either double-count or be lost.
        """
        limit = max(1, min(MAX_MINUTES_PER_POST, int(limit)))
        current = minute_key(now or self._clock())
        with self._lock:
            # Sorted rather than trusted to insertion order: a clock stepped
            # backwards by NTP would otherwise replay minutes out of order.
            pending = sorted(
                key for key in self._minutes
                if key < current and key not in self._sent
            )
            return [self._minutes[key].to_wire(key) for key in pending[:limit]]

    def mark_sent(self, minutes) -> None:
        keys = {
            entry.get("minute_start") if isinstance(entry, dict) else entry
            for entry in minutes or ()
        }
        with self._lock:
            for key in keys:
                if isinstance(key, str):
                    self._sent.add(key)
            # `_sent` can only name minutes the ring still holds.
            self._sent &= set(self._minutes)

    def quota_status(self) -> dict:
        """The two quota keys the heartbeat's `cloud` block already accepts."""
        if self._quota is None:
            return {}
        try:
            return {
                "recognition_lookups_month_to_date": self._quota.month_to_date(),
                "recognition_lookup_quota": self._quota.quota,
            }
        except Exception:
            return {}

    def status(self) -> dict:
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
        if self._deferred_by_gate():
            return 0
        if self._retry_at is not None and now < self._retry_at:
            return 0
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

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._poll_interval)


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
