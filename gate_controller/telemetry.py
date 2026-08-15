"""Bounded V3 telemetry payloads for gate event processing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
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

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


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
class StageDurations:
    capture_to_burst_ms: float | None = None
    burst_to_ocr_ms: float | None = None
    ocr_ms: float | None = None
    decision_ms: float | None = None
    decision_to_relay_ms: float | None = None
    end_to_end_ms: float | None = None
    delivery_lag_ms: float | None = None

    def to_wire(self) -> dict[str, int]:
        values = (
            ("capture_to_burst_ms", self.capture_to_burst_ms),
            ("burst_to_ocr_ms", self.burst_to_ocr_ms),
            ("ocr_ms", self.ocr_ms),
            ("decision_ms", self.decision_ms),
            ("decision_to_relay_ms", self.decision_to_relay_ms),
            ("end_to_end_ms", self.end_to_end_ms),
            ("delivery_lag_ms", self.delivery_lag_ms),
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
        return {
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
        self.captured_at = wall_clock()
        self.trace_id = _trace_id(trace_id) if trace_id is not None else str(uuid4())
        self._captured = monotonic_clock()
        self._burst: float | None = None
        self._first_ocr_start: float | None = None
        self._pending_ocr_start: float | None = None
        self._last_ocr_end: float | None = None
        self._ocr_work_ms = 0.0
        self._decision: float | None = None
        self._actuation: float | None = None
        self._actuation_details_marked = False
        self._finished: float | None = None
        self._frames: list[FrameTelemetry] = []
        self._ocr_attempts: list[OcrAttemptTelemetry] = []
        self._decision_outcome = "unknown"
        self._decision_reason = "unknown"
        self._actuation_claim = "not_requested"
        self._actuation_attempted = False
        self._relay_outcome = "not_attempted"
        self._finished_telemetry: EventTelemetry | None = None

    def mark_burst(self) -> None:
        if self._burst is None:
            self._burst = self._monotonic_clock()

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

    def mark_decision(self, outcome: str, reason: str) -> None:
        if self._decision is None:
            self._decision = self._monotonic_clock()
            self._decision_outcome = outcome
            self._decision_reason = reason

    def mark_actuation(self, claim: str, attempted: bool, relay_outcome: str) -> None:
        """Backward-compatible combined activation and outcome marker."""
        self.mark_relay_activation()
        self.set_actuation_outcome(claim, attempted, relay_outcome)

    def mark_relay_activation(self) -> None:
        if self._actuation is None:
            self._actuation = self._monotonic_clock()

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
        )
        return self._finished_telemetry


def _elapsed(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start) * 1_000)
