import inspect
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

from .automatic_open import AutomaticOpenConfig
from .models import ActuationExecution, GateEvent, RepulseHold

LOGGER = logging.getLogger(__name__)


#: How long after ANY relay pulse an automatic actuation is refused.
#:
#: The relay contact drives the operator's step-by-step input: a pulse on a
#: fully open gate starts it closing, and a pulse on a moving gate stops it
#: dead. So a second automatic pulse inside one gate cycle never "holds the
#: gate open" -- it shuts it on the car, or strands it half-way. Measured on
#: the site gate (gate-controller#171): opening travel ~20.5 s, hold 16 s
#: (sometimes 25-27 s), closing ~23 s, leaves shut at +69.0 .. +71.8 s after
#: the opening pulse. The old 20 s window was shorter than the opening travel
#: alone, and a waiting car was re-pulsed at +29.3 s, +51.8 s and +65.3 s.
#: 90 s covers the whole undisturbed cycle with margin.
DEFAULT_AUTOMATIC_COOLDOWN = timedelta(seconds=90)

#: How long after any relay pulse a person's command from the app is refused.
#:
#: Deliberately shorter than the automatic window. Someone watching the live
#: camera may need a second pulse to recover a gate that stopped mid-travel,
#: and they can see what the gate is doing; the plate reader cannot.
DEFAULT_COMMAND_COOLDOWN = timedelta(seconds=20)

#: How long a plate must be out of the record before the relay may pulse for
#: it again automatically.
#:
#: 2026-10-10, 10:04-10:13 IST: an authorised pickup waited nine minutes at
#: the gate. The camera raised seven vehicle alarms; the Pi pulsed the relay
#: four times for the one car (10:04:13.9, 10:06:45.8, 10:08:18.0,
#: 10:12:37.0), every read between refused only by the 90 s cooldown and the
#: first read after each expiry let through. The relay is on the operator's
#: step-by-step input, so a pulse into a gate whose state is unknown stops
#: or reverses it: the leaves crossed and the gate jammed. Nothing carried
#: "this plate was already let in and is still here" from one alarm to the
#: next; this does. A car that has stayed in the picture gets exactly one
#: automatic pulse; once it has not been seen for this long the hold lapses.
DEFAULT_REPULSE_UNSEEN = timedelta(minutes=10)

#: Event sources with a person behind them. Everything else -- the recognition
#: pipeline, and any source added later that nobody thought to list here --
#: gets the automatic window, which is the safe side to be wrong on.
HUMAN_COMMAND_SOURCES = frozenset({"remote_command"})

#: The ``actuation_outcome`` of a grant the one-pulse-per-car hold refused.
REPULSE_HOLD = "repulse_hold"
#: The ``actuation_outcome`` and ``reason`` of a grant refused because the
#: owner has paused automatic opening.
AUTOMATIC_PAUSED = "automatic_paused"
#: Every terminal outcome that is a decision the coordinator chose not to act
#: on: the relay was not asked, and nothing was attempted.
WITHHELD_OUTCOMES = frozenset({"cooldown", REPULSE_HOLD, AUTOMATIC_PAUSED})


class ActuationCoordinator:
    """The sole in-process owner of claim, cooldown, relay, and finalization."""

    def __init__(self, store, relay, cooldown: timedelta = DEFAULT_AUTOMATIC_COOLDOWN,
                 clock=None, monotonic_clock=None, boot_id: str | None = None,
                 activation_observer=None, *, command_cooldown: timedelta | None = None,
                 repulse_unseen: timedelta | None = DEFAULT_REPULSE_UNSEEN,
                 automatic_open=None):
        self._store = store
        self._relay = relay
        # The one-pulse-per-car hold (docs/invariants.md 12): ``None`` or a
        # zero window switches it off, which only an operator setting
        # GATE_REPULSE_UNSEEN_MINUTES=0 on purpose does.
        self._repulse_unseen = (
            repulse_unseen if repulse_unseen is not None and repulse_unseen > timedelta(0)
            else None
        )
        # The owner's pause switch: a callable answering an
        # ``AutomaticOpenConfig`` (or a bare bool), re-read on every automatic
        # grant so the app's setting takes effect without a restart. ``None``
        # is a controller with no switch: automatic opening is on.
        self._automatic_open = automatic_open
        # ``cooldown`` is the automatic window. The command window is never
        # longer than it unless a caller says so explicitly, so a coordinator
        # built with ``cooldown=0`` has no window for anyone.
        self._cooldown = cooldown
        self._command_cooldown = (
            min(cooldown, DEFAULT_COMMAND_COOLDOWN)
            if command_cooldown is None else command_cooldown
        )
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

    @property
    def automatic_cooldown(self) -> timedelta:
        return self._cooldown

    @property
    def command_cooldown(self) -> timedelta:
        return self._command_cooldown

    @property
    def repulse_unseen(self) -> timedelta | None:
        """The one-pulse-per-car hold's unseen window, or None when it is off."""
        return self._repulse_unseen

    def automatic_open_config(self) -> AutomaticOpenConfig:
        """The pause switch as it stands now. Never raises; a broken reader is ``on``.

        A reader that fails answers nothing about the owner's wishes, and the
        shipped behaviour is the fallback the environment's loader already
        chose. ``main`` wraps the app's setting so this is belt and braces.
        """
        reader = self._automatic_open
        if reader is None:
            return AutomaticOpenConfig()
        try:
            answer = reader() if callable(reader) else reader
        except Exception:
            LOGGER.warning("gate_actuation automatic_open=unreadable using=on", exc_info=True)
            return AutomaticOpenConfig()
        if isinstance(answer, AutomaticOpenConfig):
            return answer
        if isinstance(answer, bool):
            return AutomaticOpenConfig(enabled=answer, source="environment")
        return AutomaticOpenConfig()

    def status(self) -> dict:
        """The heartbeat's view of what withholds automatic pulses.

        Additive: the Gate Mate Worker narrows the heartbeat to the keys it
        knows and drops this block until it learns it.
        """
        automatic = self.automatic_open_config()
        return {
            "automatic_open": automatic.enabled,
            "automatic_open_source": automatic.source,
            "repulse_unseen_minutes": (
                self._repulse_unseen.total_seconds() / 60.0
                if self._repulse_unseen is not None else None
            ),
            "automatic_cooldown_seconds": self._cooldown.total_seconds(),
            "command_cooldown_seconds": self._command_cooldown.total_seconds(),
        }

    def _is_automatic(self, event: GateEvent, command_ack) -> bool:
        return command_ack is None and event.source not in HUMAN_COMMAND_SOURCES

    def _repulse_hold(self, event: GateEvent, now: datetime) -> RepulseHold | None:
        """The hold on this plate, if any. Raises if the store cannot answer.

        Only for an automatic grant that names an authorised plate: a human
        command has no plate and is never held, and an appearance grant
        (farm machinery) has none either and keeps only the cooldown.
        """
        if self._repulse_unseen is None or not event.authorised_plate:
            return None
        query = getattr(self._store, "repulse_hold", None)
        if not callable(query):
            return None
        return query(event.authorised_plate, now, self._repulse_unseen)

    def _cooldown_for(self, event: GateEvent, command_ack) -> timedelta:
        """The window this request has to clear, measured from the last pulse.

        Both windows are measured from the most recent pulse by *any* source:
        the store and ``_last_attempt_monotonic`` record pulses, not who asked
        for them. Only the length differs by who is asking now. So a person's
        pulse starts the automatic window like any other, and a plate read
        30 s after a remote open is still in cooldown.
        """
        if command_ack is not None or event.source in HUMAN_COMMAND_SOURCES:
            return self._command_cooldown
        return self._cooldown

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
            cooldown = self._cooldown_for(event, command_ack)

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

            def withhold(terminal: GateEvent, outcome: str) -> ActuationExecution:
                """Record a grant the coordinator chose not to act on, and say so.

                Before the claim, like a persisted cooldown: nothing is
                attempted, so nothing is claimed, and the row carries the
                decision with ``actuation_outcome`` naming what withheld it.
                The same bar as a pulse is asked first, for the same reason
                the cooldown asks it.
                """
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
                event_id = self._store.record_terminal_outcome(
                    terminal, status="failed", detail=outcome,
                    outbox_payload=outbox_payload, command_ack=command_ack,
                )
                return ActuationExecution(False, outcome, event_id, "failed", outcome)

            automatic = self._is_automatic(event, command_ack)
            if automatic and not (pause := self.automatic_open_config()).enabled:
                # The owner's switch, asked first and before any claim: every
                # automatic path -- sweep frame, FTP still, presence frame,
                # early sweep, appearance grant -- converges on this method
                # and nothing routes round it. A person's command from the
                # app is not asked.
                LOGGER.warning(
                    "gate_actuation outcome=%s source=%s plate=%s setting_source=%s key=%s",
                    AUTOMATIC_PAUSED, event.source, event.authorised_plate or "-",
                    pause.source, key,
                )
                return withhold(_paused_event(event, key, claim_time), AUTOMATIC_PAUSED)

            try:
                monotonic_now = self._monotonic_clock()
                claim = self._store.claim_actuation(
                    key, claim_time, claim_time - cooldown,
                    monotonic_cutoff=monotonic_now - cooldown.total_seconds(),
                    boot_id=self._boot_id,
                    event=event, outbox_payload=outbox_payload,
                    command_ack=command_ack,
                )
            except Exception:
                return ActuationExecution(False, "actuation_inhibit_error", None, "failed",
                                          "actuation_inhibit_error")

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
                    < cooldown.total_seconds()):
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
            if automatic:
                # The one-pulse-per-car hold (docs/invariants.md 12), asked
                # after both cooldowns on purpose: a read inside the gate's
                # own cycle is still "cooldown", the truth it has always been,
                # and "repulse_hold" names exactly the pulses the cooldown
                # would have let through. The claim is held and finalized
                # here like an in-process cooldown, with nothing attempted.
                try:
                    hold = self._repulse_hold(event, claim_time)
                except Exception:
                    # The store could not say whether this car was already let
                    # in. Fail closed, as a claim that cannot be written does;
                    # the claim row is left for recovery to write off.
                    LOGGER.error(
                        "gate_actuation outcome=actuation_inhibit_error stage=repulse_hold key=%s",
                        key, exc_info=True,
                    )
                    return ActuationExecution(False, "actuation_inhibit_error", None, "failed",
                                              "actuation_inhibit_error")
                if hold is not None:
                    LOGGER.warning(
                        "gate_actuation outcome=%s plate=%s pulsed_at=%s last_seen_at=%s "
                        "unseen_minutes=%g source=%s key=%s",
                        REPULSE_HOLD, hold.plate, hold.pulsed_at.isoformat(),
                        hold.last_seen_at.isoformat(),
                        self._repulse_unseen.total_seconds() / 60.0, event.source, key,
                    )
                    inhibition = inhibition_now()
                    if inhibition is None:
                        terminal = _cooldown_event(event, key, claim_time, REPULSE_HOLD)
                        status, detail = "failed", REPULSE_HOLD
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


def _cooldown_event(event: GateEvent, key: str, claim_time: datetime,
                    outcome: str = "cooldown") -> GateEvent:
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

    The one-pulse-per-car hold writes the same shape with
    ``outcome="repulse_hold"``: the gate was opened for this plate, minutes
    rather than seconds ago, and the car has not left the picture since. The
    decision is still the grant it was; only the pulse is withheld.
    """
    return GateEvent(
        source=event.source, reason=event.reason, opened=True, idempotency_key=key,
        received_at=event.received_at, decision_at=claim_time,
        authorised_plate=event.authorised_plate, observed_plate=event.observed_plate,
        ocr_confidence=event.ocr_confidence, actuation_outcome=outcome,
        near_miss_plate=event.near_miss_plate,
    )


def _paused_event(event: GateEvent, key: str, at: datetime) -> GateEvent:
    """The record of a grant refused because automatic opening is paused.

    Not a cooldown row: the gate was *not* opened for this car, by this
    event or any earlier one, so ``opened=True`` would be the lie the
    cooldown change took out of the record. It is written as a denial named
    ``automatic_paused``, with the plate and the score the reader gave it,
    and ``actuation_outcome`` says the same so the local record is unambiguous.
    """
    return GateEvent(
        source=event.source, reason=AUTOMATIC_PAUSED, opened=False, idempotency_key=key,
        received_at=event.received_at, decision_at=at,
        authorised_plate=event.authorised_plate, observed_plate=event.observed_plate,
        ocr_confidence=event.ocr_confidence, actuation_outcome=AUTOMATIC_PAUSED,
        near_miss_plate=event.near_miss_plate,
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
        ocr_confidence=event.ocr_confidence, near_miss_plate=event.near_miss_plate,
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
