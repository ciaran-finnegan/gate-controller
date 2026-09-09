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
    #: Which reader produced this observation: the cloud OCR service, or the
    #: on-device recogniser. Matching never looks at it; it only tells the
    #: event and the app which reader opened the gate.
    source: str = "cloud"
    #: Whether a cloud lookup was actually spent producing this read. Not the
    #: same question as ``source``: under ``GATE_LOCAL_OCR_CLOUD=always`` the
    #: on-device reader answers *and* the cloud request still goes out, so the
    #: gate was opened locally and the allowance was charged anyway. The quota
    #: burn-down counts this, never ``source``.
    cloud_lookup: bool = True


@dataclass(frozen=True)
class MatchDecision:
    allowed: bool
    reason: str
    authorised_plate: str | None = None
    observed_plate: str | None = None
    confidence: float = 0.0
    #: Which rule admitted the match: ``exact``, ``ocr_confusion``, or
    #: ``edit_distance``. ``None`` on a denial.
    match_rule: str | None = None
    #: Characters between the read and the authorised plate it matched.
    edit_distance: int | None = None
    #: The fuzziness band and level in force when this decision was taken,
    #: plus the local wall time that selected them.
    policy_band: str | None = None
    policy_level: str | None = None
    policy_timezone: str | None = None
    policy_local_time: str | None = None
    #: On a denial, the closest authorised plate and its distance. Recorded
    #: for review only; it never widens a match.
    near_miss_plate: str | None = None
    near_miss_distance: int | None = None


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
    #: Why an event that was *granted* never pulsed the relay -- today only
    #: ``"cooldown"``, meaning the gate was already open for the vehicle in
    #: front of it. ``None`` on every other event, including denials.
    #:
    #: ``opened`` answers "was the gate open for this vehicle"; this field and
    #: ``relay_activated_at`` answer "did *this* event work the relay". The
    #: distinction matters twice: the cooldown window must count only real
    #: pulses (see ``LocalStore._was_opened_since``), and the app must not be
    #: told a car was refused when it had just been let in. It is a local
    #: column only: ``LocalStore._event_payload`` does not select it, so it
    #: never reaches the ingest contract, which would reject the unknown key.
    actuation_outcome: str | None = None


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
