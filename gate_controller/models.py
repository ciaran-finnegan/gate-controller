from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .telemetry import EventTelemetry


@dataclass(frozen=True)
class PlateObservation:
    plate: str | None
    confidence: float
    make: str | None = None
    colour: str | None = None


@dataclass(frozen=True)
class MatchDecision:
    allowed: bool
    reason: str
    authorised_plate: str | None = None
    observed_plate: str | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class GateEvent:
    source: str
    reason: str
    opened: bool
    idempotency_key: str | None
    received_at: datetime
    decision_at: datetime | None = None
    relay_activated_at: datetime | None = None
    authorised_plate: str | None = None
    observed_plate: str | None = None
    ocr_confidence: float = 0.0


@dataclass(frozen=True)
class RelayResult:
    activated: bool
    reason: str
    idempotency_key: str | None = None
    activated_at: datetime | None = None
    latched: bool = False


@dataclass(frozen=True)
class ActuationClaim:
    idempotency_key: str
    status: str
    claimed_at: datetime | None = None
    claim_id: int | None = None


@dataclass(frozen=True)
class TerminalOutcome:
    status: str
    detail: str | None
    event_id: int | None = None


@dataclass(frozen=True)
class ActuationExecution:
    opened: bool
    reason: str
    event_id: int | None = None
    terminal_status: str = "failed"
    terminal_detail: str | None = None


@dataclass(frozen=True)
class ProcessingResult:
    opened: bool
    reason: str
    event_id: int | None = None
    decision: MatchDecision | None = None
    telemetry: EventTelemetry | None = None
    idempotency_key: str | None = None
    terminal: bool = True
