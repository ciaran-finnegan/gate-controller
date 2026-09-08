"""Bounded V3 telemetry payloads for gate event processing."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from itertools import islice
import math
import re
import time
from typing import Callable, Iterable
from uuid import UUID, uuid4


MAX_DURATION_MS = 600_000
MAX_ITEMS = 8
MAX_STRING_LENGTH = 128
MAX_DIMENSION = 16_384
MAX_DELIVERY_ATTEMPT = 1_000
MAX_UPSTREAM_INTERVAL_SECONDS = MAX_DURATION_MS / 1_000
MAX_CLOCK_SKEW_SECONDS = 0.1
MAX_EDIT_DISTANCE = 8

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FAILURE_CAUSE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_BAND_LABEL = re.compile(r"^(?:[0-2][0-9]:[0-5][0-9]-[0-2][0-9]:[0-5][0-9]|[a-z_]{1,32})$")
_LOCAL_TIME = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
_TRIGGER_EVENT_TYPES = frozenset({
    "line_crossing", "vehicle", "manual_test", "other", "unverified",
})
_MATCHED_TRIGGER_EVENT_TYPES = _TRIGGER_EVENT_TYPES - {"unverified"}


def _rounded_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return min(max(math.floor(number + 0.5), minimum), maximum)


def _duration(value: object | None) -> int | None:
    if value is None:
        return None
    return _rounded_int(value, 0, MAX_DURATION_MS, 0)


def _required_duration(value: object | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return min(math.floor(number + 0.5), MAX_DURATION_MS)


def _ratio(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(max(number, 0.0), 1.0)


def _token(value: object, fallback: str = "unknown") -> str:
    if isinstance(value, str) and _TOKEN.fullmatch(value):
        return value
    return fallback


def _optional_string(value: object | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:MAX_STRING_LENGTH]


def _optional_failure_cause(value: object | None) -> str | None:
    if isinstance(value, str) and _FAILURE_CAUSE.fullmatch(value):
        return value
    return None


def _band_label(value: object) -> str:
    if isinstance(value, str) and _BAND_LABEL.fullmatch(value):
        return value
    return "unknown"


def _local_time(value: object) -> str:
    if isinstance(value, str) and _LOCAL_TIME.fullmatch(value):
        return value
    return "unknown"


def _trace_id(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = UUID(value)
        except ValueError:
            pass
        else:
            if parsed.version is not None:
                return str(parsed)
    return str(uuid4())


@dataclass(frozen=True)
class FrameTelemetry:
    sequence: int
    digest: str
    width: int
    height: int
    sharpness: float
    brightness: float
    darkness: float
    highlight_clipping: float
    status: str = "ok"

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _token(self.status))

    def to_wire(self) -> dict[str, object] | None:
        if not isinstance(self.digest, str) or not _SHA256.fullmatch(self.digest):
            return None
        return {
            "sequence": _rounded_int(self.sequence, 0, MAX_ITEMS - 1, 0),
            "digest": self.digest,
            "width": _rounded_int(self.width, 1, MAX_DIMENSION, 1),
            "height": _rounded_int(self.height, 1, MAX_DIMENSION, 1),
            "sharpness": _ratio(self.sharpness),
            "brightness": _ratio(self.brightness),
            "darkness": _ratio(self.darkness),
            "highlight_clipping": _ratio(self.highlight_clipping),
            "status": self.status,
        }


@dataclass(frozen=True)
class OcrAttemptTelemetry:
    frame_sequence: int
    duration_ms: float = 0.0
    status: str = "unknown"
    plate: str | None = None
    confidence: float | None = None
    make: str | None = None
    colour: str | None = None
    #: Bounded reason this attempt failed. Journal-only: the Cloudflare ingest
    #: contract validates ``ocr_attempts`` against a strict key allowlist and
    #: rejects the whole event for any unknown key, so this is deliberately
    #: absent from :meth:`to_wire`.
    failure_cause: str | None = None
    #: Which reader produced this attempt's read -- ``cloud`` or ``local`` --
    #: straight off :attr:`gate_controller.models.PlateObservation.source`.
    #: Journal-only, for the same reason as ``failure_cause``.
    source: str = "cloud"
    #: Whether a cloud lookup was actually spent on this attempt. Not the same
    #: question as ``source``: under ``GATE_LOCAL_OCR_CLOUD=always`` the local
    #: reader answers and the cloud request goes out anyway, so a ``local``
    #: attempt can still have been charged. The quota burn-down in
    #: ``metrics.py`` bills on this, never on ``source``. Journal-only.
    cloud_lookup: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "failure_cause", _optional_failure_cause(self.failure_cause)
        )
        object.__setattr__(
            self, "source", "local" if self.source == "local" else "cloud"
        )
        object.__setattr__(self, "cloud_lookup", bool(self.cloud_lookup))

    def to_wire(self) -> dict[str, object]:
        # Wire keys are frozen by the ingest contract. Do not add fields here
        # without first extending the Worker's OCR_ATTEMPT_KEYS allowlist.
        return {
            "frame_sequence": _rounded_int(self.frame_sequence, 0, MAX_ITEMS - 1, 0),
            "duration_ms": _duration(self.duration_ms) or 0,
            "status": _token(self.status),
            "plate": _optional_string(self.plate),
            "confidence": None if self.confidence is None else _ratio(self.confidence),
            "make": _optional_string(self.make),
            "colour": _optional_string(self.colour),
        }


@dataclass(frozen=True)
class MatchPolicyTelemetry:
    """The fuzziness band and level that produced one decision.

    Wire keys are frozen by the ingest contract. Do not add fields here
    without first extending the Worker's ``MATCH_POLICY_KEYS`` allowlist.
    """

    band: str
    level: str
    timezone_name: str
    local_time: str
    #: ``exact``, ``ocr_confusion``, or ``edit_distance``; absent on a denial.
    rule: str | None = None
    edit_distance: int | None = None
    observed_plate: str | None = None
    authorised_plate: str | None = None
    #: On a denial, the closest authorised plate and how far away it was, so
    #: the owner can tell a schedule denial from a camera failure.
    near_miss_plate: str | None = None
    near_miss_distance: int | None = None

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "band": _band_label(self.band),
            "level": _token(self.level),
            "timezone": _optional_string(self.timezone_name) or "unknown",
            "local_time": _local_time(self.local_time),
        }
        if self.rule is not None:
            payload["rule"] = _token(self.rule)
        if self.edit_distance is not None:
            payload["edit_distance"] = _rounded_int(
                self.edit_distance, 0, MAX_EDIT_DISTANCE, 0
            )
        for key, value in (
            ("observed_plate", self.observed_plate),
            ("authorised_plate", self.authorised_plate),
            ("near_miss_plate", self.near_miss_plate),
        ):
            encoded = _optional_string(value)
            if encoded is not None:
                payload[key] = encoded
        if self.near_miss_distance is not None:
            payload["near_miss_distance"] = _rounded_int(
                self.near_miss_distance, 0, MAX_EDIT_DISTANCE, 0
            )
        return payload

    @classmethod
    def from_decision(cls, decision) -> "MatchPolicyTelemetry | None":
        """Build telemetry from a :class:`~gate_controller.models.MatchDecision`."""
        level = getattr(decision, "policy_level", None)
        band = getattr(decision, "policy_band", None)
        if not isinstance(level, str) or not isinstance(band, str):
            return None
        return cls(
            band=band,
            level=level,
            timezone_name=getattr(decision, "policy_timezone", None) or "unknown",
            local_time=getattr(decision, "policy_local_time", None) or "unknown",
            rule=getattr(decision, "match_rule", None),
            edit_distance=getattr(decision, "edit_distance", None),
            observed_plate=getattr(decision, "observed_plate", None),
            authorised_plate=getattr(decision, "authorised_plate", None),
            near_miss_plate=getattr(decision, "near_miss_plate", None),
            near_miss_distance=getattr(decision, "near_miss_distance", None),
        )


@dataclass(frozen=True)
class TriggerTelemetry:
    source: str
    event_type: str
    correlation: str
    rule_id: str | None = None
    event_at: datetime | None = None
    delta_ms: float | None = None

    def to_wire(self) -> dict[str, object]:
        rule_id = _token(self.rule_id, fallback="")
        delta_ms = _required_duration(self.delta_ms)
        if not (
            self.source == "reolink_webhook"
            and self.event_type in _MATCHED_TRIGGER_EVENT_TYPES
            and self.correlation == "matched"
            and rule_id
            and delta_ms is not None
        ):
            return {
                "source": "camera_ftp",
                "event_type": "unverified",
                "correlation": "unverified",
            }
        payload: dict[str, object] = {
            "source": "reolink_webhook",
            "event_type": self.event_type,
            "rule_id": rule_id,
            "correlation": "matched",
        }
        event_at = _wire_timestamp(self.event_at)
        if event_at is not None:
            payload["event_at"] = event_at
        payload["delta_ms"] = delta_ms
        return payload


def ftp_fallback_trigger() -> TriggerTelemetry:
    return TriggerTelemetry(
        source="camera_ftp",
        event_type="unverified",
        correlation="unverified",
    )


_LOCAL_OCR_AGREEMENTS = frozenset({
    "match", "mismatch", "local_only", "cloud_only", "both_none",
})
_LOCAL_OCR_AUTHORISED = frozenset({"local_match", "cloud_match", "both", "none"})
_LOCAL_OCR_DECISION = frozenset({"local", "cloud", "none"})
_LOCAL_OCR_MODES = frozenset({"shadow", "active"})
_LOCAL_OCR_STATUSES = frozenset({"recognized", "no_plate", "unavailable", "error"})


@dataclass(frozen=True)
class LocalOcrTelemetry:
    """What the on-device recogniser did for this event, in one small block.

    Deliberately compact and closed-vocabulary: it travels the same route as
    ``frames`` and ``trigger``, so every value here is a bounded token, a
    bounded duration or a plate string.
    """

    mode: str = "shadow"
    frames: int = 0
    plate: str | None = None
    score: float | None = None
    latency_ms: float | None = None
    agreement: str = "both_none"
    authorised: str = "none"
    decision_source: str = "none"
    status: str = "no_plate"

    @classmethod
    def from_block(cls, block: object) -> "LocalOcrTelemetry | None":
        if not isinstance(block, dict):
            return None
        return cls(
            mode=str(block.get("mode", "shadow")),
            frames=block.get("frames", 0),
            plate=block.get("plate"),
            score=block.get("score"),
            latency_ms=block.get("latency_ms"),
            agreement=str(block.get("agreement", "both_none")),
            authorised=str(block.get("authorised", "none")),
            decision_source=str(block.get("decision_source", "none")),
            status=str(block.get("status", "no_plate")),
        )

    def to_wire(self) -> dict[str, object]:
        mode = self.mode if self.mode in _LOCAL_OCR_MODES else "shadow"
        decision_source = (
            self.decision_source if self.decision_source in _LOCAL_OCR_DECISION else "none"
        )
        # The Worker enforces this pairing across the two fields and rejects
        # the whole event when it does not hold, so a block that would be
        # refused at ingest is repaired here rather than losing the event.
        # Only the active path can decide locally, so a `local` source under
        # any other mode is a bug, not a reading worth transmitting.
        if decision_source == "local" and mode != "active":
            decision_source = "none"
        return {
            "mode": mode,
            "frames": _rounded_int(self.frames, 0, MAX_ITEMS, 0),
            "plate": _optional_string(self.plate),
            "score": None if self.score is None else _ratio(self.score),
            "latency_ms": _duration(self.latency_ms) or 0,
            "agreement": (
                self.agreement if self.agreement in _LOCAL_OCR_AGREEMENTS else "both_none"
            ),
            "authorised": (
                self.authorised if self.authorised in _LOCAL_OCR_AUTHORISED else "none"
            ),
            "decision_source": decision_source,
            "status": self.status if self.status in _LOCAL_OCR_STATUSES else "no_plate",
        }


_DIRECTION_VERDICTS = frozenset({"entering", "exiting", "stationary", "unknown"})
_DIRECTION_METHODS = frozenset({"box_width", "none"})
#: d(log box width)/dt per second. The measured range is -0.66..+0.10; the
#: bound is a bound on nonsense, not a calibration, and it mirrors
#: `MAX_DIRECTION_SLOPE` in the app's ingest contract exactly.
MAX_DIRECTION_SLOPE = 10.0
MAX_DIRECTION_FRAMES = 64
MAX_DIRECTION_SPAN_MS = 600_000


@dataclass(frozen=True)
class DirectionTelemetry:
    """Which way the vehicle was going, in one small shadow block.

    Narrowed twice on purpose. The estimator in ``direction.py`` already
    clamps what it produces; this is the boundary the wire payload is built
    at, and ingest rejects the **whole event** for a key or a value it does
    not recognise -- read the comment on ``OcrAttemptTelemetry.to_wire()``
    first. A repaired block loses one shadow reading; a refused event is
    retried by the outbox forever.
    """

    verdict: str = "unknown"
    method: str = "none"
    score: float | None = None
    slope: float | None = None
    frames: int = 0
    span_ms: int = 0

    @classmethod
    def from_block(cls, block: object) -> "DirectionTelemetry | None":
        if not isinstance(block, dict):
            return None
        return cls(
            verdict=str(block.get("verdict", "unknown")),
            method=str(block.get("method", "none")),
            score=block.get("score"),
            slope=block.get("slope"),
            frames=block.get("frames", 0),
            span_ms=block.get("span_ms", 0),
        )

    def to_wire(self) -> dict[str, object]:
        method = self.method if self.method in _DIRECTION_METHODS else "none"
        verdict = self.verdict if self.verdict in _DIRECTION_VERDICTS else "unknown"
        # The Worker enforces this pairing across the two fields and rejects
        # the whole event when it does not hold: a verdict is a claim about a
        # measurement, and `none` says no estimator ran.
        if method == "none":
            verdict = "unknown"
        return {
            "verdict": verdict,
            "method": method,
            "score": None if self.score is None else _ratio(self.score),
            "slope": _optional_slope(self.slope),
            "frames": _rounded_int(self.frames, 0, MAX_DIRECTION_FRAMES, 0),
            "span_ms": _rounded_int(self.span_ms, 0, MAX_DIRECTION_SPAN_MS, 0),
        }


def _optional_slope(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(min(max(number, -MAX_DIRECTION_SLOPE), MAX_DIRECTION_SLOPE), 6)


@dataclass(frozen=True)
class StageTimestamps:
    filesystem_ingress_at: datetime | None = None
    burst_processing_started_at: datetime | None = None
    ocr_started_at: datetime | None = None
    ocr_finished_at: datetime | None = None
    decision_at: datetime | None = None
    relay_started_at: datetime | None = None
    relay_finished_at: datetime | None = None
    processing_finished_at: datetime | None = None

    def to_wire(self) -> dict[str, str]:
        values = (
            ("filesystem_ingress_at", self.filesystem_ingress_at),
            ("burst_processing_started_at", self.burst_processing_started_at),
            ("ocr_started_at", self.ocr_started_at),
            ("ocr_finished_at", self.ocr_finished_at),
            ("decision_at", self.decision_at),
            ("relay_started_at", self.relay_started_at),
            ("relay_finished_at", self.relay_finished_at),
            ("processing_finished_at", self.processing_finished_at),
        )
        return {
            name: encoded
            for name, value in values
            if (encoded := _wire_timestamp(value)) is not None
        }


@dataclass(frozen=True)
class StageDurations:
    capture_to_burst_ms: float | None = None
    burst_to_ocr_ms: float | None = None
    ocr_ms: float | None = None
    decision_ms: float | None = None
    decision_to_relay_ms: float | None = None
    end_to_end_ms: float | None = None
    delivery_lag_ms: float | None = None
    filesystem_ingress_to_decision_ms: float | None = None
    filesystem_ingress_to_relay_ms: float | None = None
    relay_ms: float | None = None
    cloud_send_to_ack_ms: float | None = None

    def to_wire(self) -> dict[str, int]:
        values = (
            ("capture_to_burst_ms", self.capture_to_burst_ms),
            ("burst_to_ocr_ms", self.burst_to_ocr_ms),
            ("ocr_ms", self.ocr_ms),
            ("decision_ms", self.decision_ms),
            ("decision_to_relay_ms", self.decision_to_relay_ms),
            ("end_to_end_ms", self.end_to_end_ms),
            ("delivery_lag_ms", self.delivery_lag_ms),
            (
                "filesystem_ingress_to_decision_ms",
                self.filesystem_ingress_to_decision_ms,
            ),
            ("filesystem_ingress_to_relay_ms", self.filesystem_ingress_to_relay_ms),
            ("relay_ms", self.relay_ms),
            ("cloud_send_to_ack_ms", self.cloud_send_to_ack_ms),
        )
        return {name: duration for name, value in values if (duration := _duration(value)) is not None}


@dataclass(frozen=True)
class EventTelemetry:
    trace_id: str
    stage_durations: StageDurations
    frames: Iterable[FrameTelemetry]
    ocr_attempts: Iterable[OcrAttemptTelemetry]
    decision_outcome: str
    decision_reason: str
    actuation_claim: str
    actuation_attempted: bool
    relay_outcome: str
    outbox_attempt: int
    delivery_state: str
    stage_timestamps: StageTimestamps = field(default_factory=StageTimestamps)
    trigger: TriggerTelemetry | None = None
    match_policy: MatchPolicyTelemetry | None = None
    local_ocr: LocalOcrTelemetry | None = None
    direction: DirectionTelemetry | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_id", _trace_id(self.trace_id))
        object.__setattr__(self, "frames", tuple(islice(self.frames, MAX_ITEMS)))
        object.__setattr__(
            self,
            "ocr_attempts",
            tuple(islice(self.ocr_attempts, MAX_ITEMS)),
        )

    def to_wire(self) -> dict[str, object]:
        frames: list[dict[str, object]] = []
        for frame in self.frames:
            if len(frames) == MAX_ITEMS:
                break
            encoded = frame.to_wire()
            if encoded is not None:
                frames.append(encoded)

        attempts = [attempt.to_wire() for attempt in self.ocr_attempts][:MAX_ITEMS]
        payload = {
            "schema_version": 3,
            "trace_id": self.trace_id,
            "taxonomy_version": 1,
            "stage_durations": self.stage_durations.to_wire(),
            "frames": frames,
            "ocr_attempts": attempts,
            "decision": {
                "outcome": _token(self.decision_outcome),
                "reason": _token(self.decision_reason),
            },
            "actuation": {
                "claim": _token(self.actuation_claim),
                "attempted": self.actuation_attempted
                if isinstance(self.actuation_attempted, bool)
                else False,
                "relay_outcome": _token(self.relay_outcome),
            },
            "delivery": {
                "outbox_attempt": _rounded_int(
                    self.outbox_attempt, 0, MAX_DELIVERY_ATTEMPT, 0
                ),
                "state": _token(self.delivery_state),
            },
        }
        timestamps = self.stage_timestamps.to_wire()
        if timestamps:
            payload["stage_timestamps"] = timestamps
        if self.trigger is not None:
            payload["trigger"] = self.trigger.to_wire()
        if self.match_policy is not None:
            payload["match_policy"] = self.match_policy.to_wire()
        if self.local_ocr is not None:
            payload["local_ocr"] = self.local_ocr.to_wire()
        if self.direction is not None:
            payload["direction"] = self.direction.to_wire()
        return payload


class ProcessingTrace:
    """Collect a bounded processing trace using injected clocks when desired."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        trace_id: str | None = None,
    ) -> None:
        self._monotonic_clock = monotonic_clock
        self._wall_anchor = wall_clock()
        self._monotonic_anchor = monotonic_clock()
        self.captured_at = self._wall_anchor
        self.trace_id = _trace_id(trace_id) if trace_id is not None else str(uuid4())
        self._captured = self._monotonic_anchor
        self._filesystem_ingress_at: datetime | None = None
        self._burst: float | None = None
        self._burst_processing_started_at: datetime | None = None
        self._first_ocr_start: float | None = None
        self._pending_ocr_start: float | None = None
        # True while the pending mark is also what `_first_ocr_start` holds,
        # so discarding an attempt that never happened can take both back.
        self._first_ocr_start_pending = False
        self._last_ocr_end: float | None = None
        self._ocr_work_ms = 0.0
        self._decision: float | None = None
        self._actuation: float | None = None
        self._relay_finished: float | None = None
        self._actuation_details_marked = False
        self._finished: float | None = None
        self._frames: list[FrameTelemetry] = []
        self._ocr_attempts: list[OcrAttemptTelemetry] = []
        self._decision_outcome = "unknown"
        self._decision_reason = "unknown"
        self._actuation_claim = "not_requested"
        self._actuation_attempted = False
        self._relay_outcome = "not_attempted"
        self._trigger: TriggerTelemetry | None = None
        self._match_policy: MatchPolicyTelemetry | None = None
        self._local_ocr: LocalOcrTelemetry | None = None
        self._direction: DirectionTelemetry | None = None
        self._finished_telemetry: EventTelemetry | None = None

    def seed_upstream(
        self,
        received_at: datetime | None,
        decision_started_at: float | None,
        processing_started_at: datetime | None = None,
    ) -> None:
        """Anchor upstream wall and monotonic boundaries to this trace."""
        capture = _wall_to_monotonic(
            received_at, self._wall_anchor, self._captured
        )
        burst = _upstream_monotonic(decision_started_at, self._captured)

        if received_at is not None or decision_started_at is not None:
            self._captured = capture
        if capture is not None:
            self.captured_at = received_at
            self._filesystem_ingress_at = received_at
        if decision_started_at is not None:
            self._burst = burst
        if _wire_timestamp(processing_started_at) is not None:
            self._burst_processing_started_at = processing_started_at

        if capture is None or burst is None or capture <= burst:
            return
        if capture - burst <= MAX_CLOCK_SKEW_SECONDS:
            self._captured = burst
        else:
            self._captured = None

    def mark_burst(self) -> None:
        if self._burst is None:
            self._burst = self._monotonic_clock()

    def set_trigger(self, trigger: TriggerTelemetry | None) -> None:
        self._trigger = trigger

    def set_match_policy(self, match_policy: MatchPolicyTelemetry | None) -> None:
        """Record the fuzziness band and level this decision was taken under."""
        self._match_policy = match_policy

    def set_local_ocr(self, local_ocr) -> None:
        """Attach the on-device recogniser's block; a plain dict is accepted."""
        if local_ocr is None:
            return
        if not isinstance(local_ocr, LocalOcrTelemetry):
            local_ocr = LocalOcrTelemetry.from_block(local_ocr)
        self._local_ocr = local_ocr

    def set_direction(self, direction) -> None:
        """Attach the shadow direction block; a plain dict is accepted.

        Unlike the blocks above, this one is attached to *every* event: the
        honest answer when nothing was measured is `unknown`/`none`, not an
        absent block, so a week of shadow data has a denominator.
        """
        if direction is None:
            return
        if not isinstance(direction, DirectionTelemetry):
            direction = DirectionTelemetry.from_block(direction)
        self._direction = direction

    def add_frame(self, frame: FrameTelemetry) -> None:
        if len(self._frames) < MAX_ITEMS:
            self._frames.append(frame)

    def mark_ocr_start(self, at: float | None = None) -> None:
        """Mark the start of the next OCR attempt before invoking OCR.

        ``at`` back-dates the mark to a monotonic instant already taken. The
        on-device pass runs before the caller knows whether it will answer for
        the frame, so a frame it did answer is marked from when that read
        actually began rather than from when it finished.
        """
        if len(self._ocr_attempts) >= MAX_ITEMS or self._pending_ocr_start is not None:
            return
        now = self._monotonic_clock()
        self._pending_ocr_start = now if at is None or at > now else at
        if self._first_ocr_start is None:
            self._first_ocr_start = self._pending_ocr_start
            self._first_ocr_start_pending = True

    def discard_ocr_start(self) -> None:
        """Take back a mark for an attempt that never ran.

        A frame can be marked as starting and then be refused before any work
        happens -- the OCR slot is taken, the processor closed underneath it,
        or the remaining budget is too small to bill a lookup. Without this
        the mark stays pending forever: the next attempt's `mark_ocr_start`
        is ignored, so its duration is charged from the wrong instant, and
        `ocr_started_at` is stamped on an event with no attempts at all.
        """
        if self._pending_ocr_start is None:
            return
        self._pending_ocr_start = None
        if self._first_ocr_start_pending:
            self._first_ocr_start = None
            self._first_ocr_start_pending = False

    def add_ocr_attempt(self, attempt: OcrAttemptTelemetry) -> None:
        if len(self._ocr_attempts) >= MAX_ITEMS:
            return
        if self._pending_ocr_start is None:
            raise RuntimeError("mark_ocr_start must be called before add_ocr_attempt")
        ended_at = self._monotonic_clock()
        duration_ms = _elapsed(self._pending_ocr_start, ended_at) or 0.0
        self._last_ocr_end = ended_at
        self._pending_ocr_start = None
        self._first_ocr_start_pending = False
        self._ocr_work_ms += duration_ms
        self._ocr_attempts.append(replace(attempt, duration_ms=duration_ms))

    def add_ocr_rejection(self, attempt: OcrAttemptTelemetry) -> None:
        """Record an OCR attempt rejected before network work could start."""
        if len(self._ocr_attempts) < MAX_ITEMS:
            self._ocr_attempts.append(replace(attempt, duration_ms=0.0))

    def mark_decision(self, outcome: str, reason: str) -> None:
        if self._decision is None:
            self._decision = self._monotonic_clock()
            self._decision_outcome = outcome
            self._decision_reason = reason

    def revise_decision(self, outcome: str, reason: str) -> None:
        if self._decision is None:
            self.mark_decision(outcome, reason)
            return
        self._decision_outcome = outcome
        self._decision_reason = reason

    def mark_actuation(self, claim: str, attempted: bool, relay_outcome: str) -> None:
        """Backward-compatible combined activation and outcome marker."""
        self.mark_relay_activation()
        self.set_actuation_outcome(claim, attempted, relay_outcome)

    def mark_relay_activation(self) -> None:
        if self._actuation is None:
            self._actuation = self._monotonic_clock()

    def mark_relay_finished(self) -> None:
        if self._actuation is not None and self._relay_finished is None:
            self._relay_finished = self._monotonic_clock()

    def set_actuation_outcome(
        self, claim: str, attempted: bool, relay_outcome: str
    ) -> None:
        if not self._actuation_details_marked:
            self._actuation_claim = claim
            self._actuation_attempted = attempted
            self._relay_outcome = relay_outcome
            self._actuation_details_marked = True

    def finish(
        self,
        *,
        outbox_attempt: int = 0,
        delivery_state: str = "pending",
        delivery_lag_ms: float | None = None,
    ) -> EventTelemetry:
        if self._finished_telemetry is not None:
            return self._finished_telemetry
        self._finished = self._monotonic_clock()
        self._finished_telemetry = EventTelemetry(
            trace_id=self.trace_id,
            stage_durations=StageDurations(
                capture_to_burst_ms=_elapsed(self._captured, self._burst),
                burst_to_ocr_ms=_elapsed(self._burst, self._first_ocr_start),
                ocr_ms=self._ocr_work_ms,
                decision_ms=_elapsed(self._last_ocr_end, self._decision),
                decision_to_relay_ms=_elapsed(self._decision, self._actuation),
                end_to_end_ms=_elapsed(self._captured, self._finished),
                delivery_lag_ms=delivery_lag_ms,
                filesystem_ingress_to_decision_ms=_elapsed(
                    self._captured if self._filesystem_ingress_at is not None else None,
                    self._decision,
                ),
                filesystem_ingress_to_relay_ms=_elapsed(
                    self._captured if self._filesystem_ingress_at is not None else None,
                    self._actuation,
                ),
                relay_ms=_elapsed(self._actuation, self._relay_finished),
            ),
            frames=tuple(self._frames),
            ocr_attempts=tuple(self._ocr_attempts),
            decision_outcome=self._decision_outcome,
            decision_reason=self._decision_reason,
            actuation_claim=self._actuation_claim,
            actuation_attempted=self._actuation_attempted,
            relay_outcome=self._relay_outcome,
            outbox_attempt=outbox_attempt,
            delivery_state=delivery_state,
            stage_timestamps=StageTimestamps(
                filesystem_ingress_at=self._filesystem_ingress_at,
                burst_processing_started_at=(
                    self._burst_processing_started_at or self._wall_at(self._burst)
                ),
                ocr_started_at=self._wall_at(self._first_ocr_start),
                ocr_finished_at=self._wall_at(self._last_ocr_end),
                decision_at=self._wall_at(self._decision),
                relay_started_at=self._wall_at(self._actuation),
                relay_finished_at=self._wall_at(self._relay_finished),
                processing_finished_at=self._wall_at(self._finished),
            ),
            trigger=self._trigger,
            match_policy=self._match_policy,
            local_ocr=self._local_ocr,
            direction=self._direction,
        )
        return self._finished_telemetry

    def _wall_at(self, monotonic_value: float | None) -> datetime | None:
        if monotonic_value is None:
            return None
        try:
            offset = monotonic_value - self._monotonic_anchor
        except TypeError:
            return None
        if not math.isfinite(offset) or abs(offset) > MAX_UPSTREAM_INTERVAL_SECONDS:
            return None
        return self._wall_anchor + timedelta(seconds=offset)


def _elapsed(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start) * 1_000)


def _wire_timestamp(value: datetime | None) -> str | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        return value.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError):
        return None


def _wall_to_monotonic(
    received_at: datetime | None,
    wall_anchor: datetime,
    monotonic_anchor: float,
) -> float | None:
    if received_at is None:
        return None
    try:
        age = (wall_anchor - received_at).total_seconds()
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(age) or not math.isfinite(monotonic_anchor):
        return None
    if age < -MAX_CLOCK_SKEW_SECONDS or age > MAX_UPSTREAM_INTERVAL_SECONDS:
        return None
    return monotonic_anchor - max(age, 0.0)


def _upstream_monotonic(value: object, anchor: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or not math.isfinite(anchor):
        return None
    offset = timestamp - anchor
    if offset > MAX_CLOCK_SKEW_SECONDS or offset < -MAX_UPSTREAM_INTERVAL_SECONDS:
        return None
    return min(timestamp, anchor)
