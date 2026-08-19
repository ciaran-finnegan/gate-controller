import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

from .models import ActuationExecution, GateEvent


class ActuationCoordinator:
    """The sole in-process owner of claim, cooldown, relay, and finalization."""

    def __init__(self, store, relay, cooldown: timedelta = timedelta(seconds=20), clock=None,
                 monotonic_clock=None, boot_id: str | None = None):
        self._store = store
        self._relay = relay
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or monotonic
        self._boot_id = _linux_boot_id() if boot_id is None else boot_id
        self._last_attempt_monotonic: float | None = None
        self._lock = Lock()

    def actuate(self, event: GateEvent, *, outbox_payload: dict | None = None,
                command_ack: tuple[str, datetime] | None = None,
                pre_activation_inhibit=None, on_activation=None) -> ActuationExecution:
        key = event.idempotency_key
        if not key:
            raise ValueError("actuation events require an idempotency key")
        with self._lock:
            terminal = self._store.terminal_outcome(key)
            if terminal:
                self._reconcile_outbox(terminal.event_id, outbox_payload)
                if command_ack is not None:
                    self._store.queue_command_ack(
                        command_ack[0], terminal.status, terminal.detail, command_ack[1]
                    )
                return ActuationExecution(False, terminal.detail or terminal.status, terminal.event_id,
                                         terminal.status, terminal.detail)
            claim_time = event.decision_at or self._clock()
            try:
                monotonic_now = self._monotonic_clock()
                claim = self._store.claim_actuation(
                    key, claim_time, claim_time - self._cooldown,
                    monotonic_cutoff=monotonic_now - self._cooldown.total_seconds(),
                    boot_id=self._boot_id,
                    event=event, outbox_payload=outbox_payload,
                    command_ack=command_ack,
                )
            except Exception:
                return ActuationExecution(False, "actuation_inhibit_error", None, "failed",
                                          "actuation_inhibit_error")
            if claim.status == "cooldown":
                cooldown_event = GateEvent(
                    source=event.source, reason="cooldown", opened=False, idempotency_key=key,
                    received_at=event.received_at, decision_at=claim_time,
                    authorised_plate=event.authorised_plate, observed_plate=event.observed_plate,
                    ocr_confidence=event.ocr_confidence,
                )
                event_id = self._store.record_terminal_outcome(
                    cooldown_event, status="failed", detail="cooldown",
                    outbox_payload=outbox_payload, command_ack=command_ack,
                )
                return ActuationExecution(False, "cooldown", event_id, "failed", "cooldown")
            if claim.status != "claimed":
                return ActuationExecution(False, claim.status, None, "failed", claim.status)
            if (self._last_attempt_monotonic is not None
                    and monotonic_now - self._last_attempt_monotonic
                    < self._cooldown.total_seconds()):
                cooldown_event = GateEvent(
                    source=event.source, reason="cooldown", opened=False,
                    idempotency_key=key, received_at=event.received_at,
                    decision_at=claim_time, authorised_plate=event.authorised_plate,
                    observed_plate=event.observed_plate,
                    ocr_confidence=event.ocr_confidence,
                )
                try:
                    event_id = self._store.finalize_actuation(
                        claim, cooldown_event, terminal_status="failed",
                        terminal_detail="cooldown", outbox_payload=outbox_payload,
                        command_ack=command_ack,
                    )
                except Exception:
                    return ActuationExecution(
                        False, "indeterminate_claim", None, "failed", "indeterminate_claim"
                    )
                return ActuationExecution(
                    False, "cooldown", event_id, "failed", "cooldown"
                )
            try:
                self._store.mark_actuation_attempt(
                    claim, claim_time, event=event, outbox_payload=outbox_payload,
                    command_ack=command_ack, attempted_monotonic=monotonic_now,
                    boot_id=self._boot_id,
                )
            except Exception:
                return ActuationExecution(False, "actuation_inhibit_error", None, "failed",
                                          "actuation_inhibit_error")
            inhibition = None
            relay_kwargs = {"idempotency_key": key}
            if pre_activation_inhibit is not None:
                def check_inhibition():
                    nonlocal inhibition
                    inhibition = pre_activation_inhibit()
                    return inhibition

                relay_kwargs["pre_activation_inhibit"] = check_inhibition
            if on_activation is not None and _accepts_keyword(
                self._relay.trigger, "on_activation"
            ):
                def notify_activation():
                    try:
                        on_activation()
                    except Exception:
                        pass

                relay_kwargs["on_activation"] = notify_activation
            relay_result = self._relay.trigger(event.source, **relay_kwargs)
            activation_attempted = (
                inhibition is None and relay_result.reason != "relay_latched"
            )
            if activation_attempted:
                self._last_attempt_monotonic = self._monotonic_clock()
            finalized = GateEvent(
                source=event.source,
                reason=event.reason if relay_result.activated else relay_result.reason,
                opened=relay_result.activated,
                idempotency_key=key,
                received_at=event.received_at,
                decision_at=self._clock() if inhibition is not None else event.decision_at,
                relay_activated_at=relay_result.activated_at,
                authorised_plate=event.authorised_plate,
                observed_plate=event.observed_plate,
                ocr_confidence=event.ocr_confidence,
            )
            status = inhibition[0] if inhibition is not None else (
                "completed" if relay_result.activated else "failed"
            )
            detail = inhibition[1] if inhibition is not None else (
                None if relay_result.activated else relay_result.reason
            )
            try:
                event_id = self._store.finalize_actuation(
                    claim, finalized, terminal_status=status, terminal_detail=detail,
                    outbox_payload=outbox_payload, command_ack=command_ack,
                    retain_activation_attempt=activation_attempted,
                )
            except Exception:
                return ActuationExecution(False, "indeterminate_claim", None, "failed", "indeterminate_claim")
            return ActuationExecution(relay_result.activated, finalized.reason, event_id, status, detail)

    def _reconcile_outbox(self, event_id: int | None, payload: dict | None) -> None:
        if event_id is not None and payload is not None:
            self._store.ensure_outbox(event_id, payload)


def _linux_boot_id() -> str | None:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return boot_id or None


def _accepts_keyword(callable_object, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
