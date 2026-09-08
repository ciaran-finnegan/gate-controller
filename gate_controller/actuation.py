import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

from .models import ActuationExecution, GateEvent


class ActuationCoordinator:
    """The sole in-process owner of claim, cooldown, relay, and finalization."""

    def __init__(self, store, relay, cooldown: timedelta = timedelta(seconds=20), clock=None,
                 monotonic_clock=None, boot_id: str | None = None,
                 activation_observer=None):
        self._store = store
        self._relay = relay
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or monotonic
        self._boot_id = _linux_boot_id() if boot_id is None else boot_id
        self._last_attempt_monotonic: float | None = None
        self._lock = Lock()
        # Notified when the relay energises and again once the outcome is
        # known. This is the sole in-process owner of the relay, so an
        # observer placed here sees every actuation -- the recognition
        # pipeline's and the command server's alike. It must never raise and
        # never block: the first notification runs while the relay is on.
        self._activation_observer = activation_observer

    def actuate(self, event: GateEvent, *, outbox_payload: dict | None = None,
                command_ack: tuple[str, datetime] | None = None,
                pre_activation_inhibit=None, on_activation=None,
                on_deactivation=None) -> ActuationExecution:
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

            def inhibition_now():
                """The relay-path preconditions, asked before crediting a grant.

                A cooldown row is now a *grant* (see ``_cooldown_event``), so it
                has to clear the same bar the pulse would have: the frame is
                still fresh, the plate is still authorised, the decision
                deadline has not passed, the processor is still open. On the
                pulsing path this same callable runs under the relay's lock
                immediately before the GPIO write; here it runs before the
                cooldown short-circuit, because a decision whose authorisation
                was revoked between the match and the actuation must be
                recorded as the denial it is, not as a grant we happened not to
                have to work the relay for.
                """
                if pre_activation_inhibit is None:
                    return None
                return pre_activation_inhibit()

            if claim.status == "cooldown":
                inhibition = inhibition_now()
                if inhibition is not None:
                    inhibited = _inhibited_event(event, key, self._clock(), inhibition[1])
                    event_id = self._store.record_terminal_outcome(
                        inhibited, status=inhibition[0], detail=inhibition[1],
                        outbox_payload=outbox_payload, command_ack=command_ack,
                    )
                    return ActuationExecution(
                        False, inhibition[1], event_id, inhibition[0], inhibition[1]
                    )
                cooldown_event = _cooldown_event(event, key, claim_time)
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
                inhibition = inhibition_now()
                if inhibition is None:
                    terminal = _cooldown_event(event, key, claim_time)
                    status, detail = "failed", "cooldown"
                else:
                    terminal = _inhibited_event(event, key, self._clock(), inhibition[1])
                    status, detail = inhibition
                try:
                    event_id = self._store.finalize_actuation(
                        claim, terminal, terminal_status=status,
                        terminal_detail=detail, outbox_payload=outbox_payload,
                        command_ack=command_ack,
                        retain_activation_attempt=False,
                    )
                except Exception:
                    return ActuationExecution(
                        False, "indeterminate_claim", None, "failed", "indeterminate_claim"
                    )
                return ActuationExecution(False, detail, event_id, status, detail)
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
            observer = self._activation_observer
            observed = []
            if (on_activation is not None or observer is not None) and _accepts_keyword(
                self._relay.trigger, "on_activation"
            ):
                def notify_activation():
                    if on_activation is not None:
                        try:
                            on_activation()
                        except Exception:
                            pass
                    if observer is not None:
                        observed.append(True)
                        try:
                            observer.note_actuation(
                                activated_at=self._clock(),
                                source=event.source,
                                reason=event.reason,
                                idempotency_key=key,
                                observed_plate=event.observed_plate,
                                authorised_plate=event.authorised_plate,
                            )
                        except Exception:
                            pass

                relay_kwargs["on_activation"] = notify_activation
            if on_deactivation is not None and _accepts_keyword(
                self._relay.trigger, "on_deactivation"
            ):
                def notify_deactivation():
                    try:
                        on_deactivation()
                    except Exception:
                        pass

                relay_kwargs["on_deactivation"] = notify_deactivation
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
            if observer is not None and relay_result.activated:
                # After the pulse and after the store write, so nothing here is
                # on the relay's critical path. Only the terminal outcome is
                # new; the activation instant was recorded above -- unless the
                # relay does not take an activation callback at all, in which
                # case record it now rather than lose the label.
                try:
                    if not observed:
                        observer.note_actuation(
                            activated_at=relay_result.activated_at or self._clock(),
                            source=event.source, reason=finalized.reason,
                            idempotency_key=key,
                            observed_plate=event.observed_plate,
                            authorised_plate=event.authorised_plate,
                        )
                    observer.note_actuation_outcome(
                        idempotency_key=key, status=status, detail=detail,
                        event_id=event_id, reason=finalized.reason,
                    )
                except Exception:
                    pass
            return ActuationExecution(relay_result.activated, finalized.reason, event_id, status, detail)

    def _reconcile_outbox(self, event_id: int | None, payload: dict | None) -> None:
        if event_id is not None and payload is not None:
            self._store.ensure_outbox(event_id, payload)


def _cooldown_event(event: GateEvent, key: str, claim_time: datetime) -> GateEvent:
    """The record of a decision that was granted while the gate was already open.

    The relay is not pulsed a second time, but nothing about the *decision*
    changed: the plate was authorised, at the confidence the reader gave it.
    Recording it as a denial with ``reason="cooldown"`` was a lie the app
    faithfully repeated -- on 8 September 2026 nine such rows read
    "10-CE-1990 / Access Denied / 99.9%" for a car that had just been let in.

    So the decision travels intact -- ``opened`` true, the match reason, the
    plates and the confidence -- and the fact that this event did not work the
    relay is carried by ``actuation_outcome`` locally, by the null
    ``relay_activated_at`` on the wire, and by ``telemetry.actuation``
    (``claim="cooldown"``, ``attempted=false``) for anyone reading the detail.
    """
    return GateEvent(
        source=event.source, reason=event.reason, opened=True, idempotency_key=key,
        received_at=event.received_at, decision_at=claim_time,
        authorised_plate=event.authorised_plate, observed_plate=event.observed_plate,
        ocr_confidence=event.ocr_confidence, actuation_outcome="cooldown",
    )


def _inhibited_event(event: GateEvent, key: str, at: datetime, reason: str) -> GateEvent:
    """The record of a decision that lost its grounds before it could act.

    The frame went stale, the plate was withdrawn from the authorised list, the
    decision deadline passed, or the processor closed -- between the match and
    the actuation. On the pulsing path the relay is held off and the event is
    written as a denial named after the check that stopped it. A decision in
    cooldown gets the same treatment, and for the same reason: it would
    otherwise be the one way a revoked authorisation could still read as
    "Access Granted" in the app.
    """
    return GateEvent(
        source=event.source, reason=reason, opened=False, idempotency_key=key,
        received_at=event.received_at, decision_at=at,
        authorised_plate=event.authorised_plate, observed_plate=event.observed_plate,
        ocr_confidence=event.ocr_confidence,
    )


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
