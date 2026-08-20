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

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
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

    def to_wire(self) -> dict[str, object]:
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
class AugmentationTelemetry:
    outcome: str
    reason: str
    candidate_count: int
    duration_ms: float
    correlation: str | None = None

    def to_wire(self) -> dict[str, object]:
        payload = {
            "outcome": _token(self.outcome),
            "reason": _token(self.reason),
            "candidate_count": _rounded_int(
                self.candidate_count, 0, MAX_ITEMS, 0,
            ),
            "duration_ms": _duration(self.duration_ms) or 0,
        }
        correlation = _optional_string(self.correlation)
        if correlation is not None:
            payload["correlation"] = correlation
        return payload


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
    augmentation: AugmentationTelemetry | None = None
    trigger: TriggerTelemetry | None = None

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
        if self.augmentation is not None:
            payload["augmentation"] = self.augmentation.to_wire()
        if self.trigger is not None:
            payload["trigger"] = self.trigger.to_wire()
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
        self._augmentation: AugmentationTelemetry | None = None
        self._trigger: TriggerTelemetry | None = None
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

    def set_augmentation(self, augmentation: AugmentationTelemetry | None) -> None:
        self._augmentation = augmentation

    def set_trigger(self, trigger: TriggerTelemetry | None) -> None:
        self._trigger = trigger

    def add_frame(self, frame: FrameTelemetry) -> None:
        if len(self._frames) < MAX_ITEMS:
            self._frames.append(frame)

    def mark_ocr_start(self) -> None:
        """Mark the start of the next OCR attempt before invoking OCR."""
        if len(self._ocr_attempts) >= MAX_ITEMS or self._pending_ocr_start is not None:
            return
        self._pending_ocr_start = self._monotonic_clock()
        if self._first_ocr_start is None:
            self._first_ocr_start = self._pending_ocr_start

    def add_ocr_attempt(self, attempt: OcrAttemptTelemetry) -> None:
        if len(self._ocr_attempts) >= MAX_ITEMS:
            return
        if self._pending_ocr_start is None:
            raise RuntimeError("mark_ocr_start must be called before add_ocr_attempt")
        ended_at = self._monotonic_clock()
        duration_ms = _elapsed(self._pending_ocr_start, ended_at) or 0.0
        self._last_ocr_end = ended_at
        self._pending_ocr_start = None
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
            augmentation=self._augmentation,
            trigger=self._trigger,
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
