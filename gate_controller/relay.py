import inspect
from datetime import datetime, timezone
from threading import Event, Lock, RLock
from time import sleep

from .models import RelayResult


class RelayController:
    def __init__(self, relay, pulse_seconds: float = 2.0, sleeper=sleep, *,
                 max_off_attempts: int = 3, clock=None):
        if max_off_attempts < 1:
            raise ValueError("max_off_attempts must be positive")
        self._relay = relay
        self._pulse_seconds = pulse_seconds
        self._sleeper = sleeper
        self._max_off_attempts = max_off_attempts
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._activation_boundary = getattr(relay, "activation_boundary", None) or RLock()
        self._shutdown_requested = Event()
        safe = self._deenergize()
        self._latched = not safe
        self._last_outcome = "initialized_safe" if safe else "relay_deenergize_error"
        self._last_outcome_at = self._clock()

    def trigger(self, source: str, idempotency_key: str | None = None, *,
                pre_activation_inhibit=None, on_activation=None) -> RelayResult:
        with self._lock:
            def activation_inhibition():
                if pre_activation_inhibit is not None:
                    inhibition = pre_activation_inhibit()
                    if inhibition is not None:
                        return inhibition
                if self._shutdown_requested.is_set():
                    return "failed", "relay_latched"
                return None

            inhibition = activation_inhibition()
            if inhibition is not None:
                _, detail = inhibition
                self._record_outcome(detail)
                return RelayResult(
                    False, detail, idempotency_key, latched=detail == "relay_latched"
                )
            if self._latched:
                self._record_outcome("relay_latched")
                return RelayResult(False, "relay_latched", idempotency_key, latched=True)
            activated_at = None
            try:
                if _accepts_keyword(self._relay.on, "pre_activation_inhibit"):
                    last_moment_inhibition = self._relay.on(
                        pre_activation_inhibit=activation_inhibition
                    )
                    if last_moment_inhibition is not None:
                        _, detail = last_moment_inhibition
                        self._record_outcome(detail)
                        return RelayResult(
                            False, detail, idempotency_key,
                            latched=detail == "relay_latched",
                        )
                else:
                    with self._activation_boundary:
                        last_moment_inhibition = activation_inhibition()
                        if last_moment_inhibition is not None:
                            _, detail = last_moment_inhibition
                            self._record_outcome(detail)
                            return RelayResult(
                                False, detail, idempotency_key,
                                latched=detail == "relay_latched",
                            )
                        self._relay.on()
                activated_at = self._clock()
                if on_activation is not None:
                    try:
                        on_activation()
                    except Exception:
                        pass
                if self._sleeper is sleep:
                    self._shutdown_requested.wait(self._pulse_seconds)
                else:
                    self._sleeper(self._pulse_seconds)
            except BaseException as error:
                if not self._deenergize():
                    self._latched = True
                    self._record_outcome("relay_deenergize_error")
                    if not isinstance(error, Exception):
                        raise
                    return RelayResult(False, "relay_deenergize_error", idempotency_key,
                                       activated_at, True)
                if not isinstance(error, Exception):
                    self._record_outcome("relay_error", activated_at)
                    raise
                self._record_outcome("relay_error", activated_at)
                return RelayResult(False, "relay_error", idempotency_key, activated_at)
            if not self._deenergize():
                self._latched = True
                self._record_outcome("relay_deenergize_error")
                return RelayResult(False, "relay_deenergize_error", idempotency_key,
                                   activated_at, True)
            self._record_outcome("activated", activated_at)
            return RelayResult(True, "activated", idempotency_key, activated_at)

    def begin_shutdown(self) -> bool:
        with self._activation_boundary:
            self._shutdown_requested.set()
            self._latched = True
            safe = self._deenergize_at_boundary()
        with self._lock:
            self._record_outcome("shutdown_safe" if safe else "relay_deenergize_error")
            return safe

    def shutdown(self) -> bool:
        return self.begin_shutdown()

    def status(self) -> dict:
        with self._lock:
            return {
                "ready": not self._latched and not self._shutdown_requested.is_set(),
                "last_outcome": self._last_outcome,
                "last_outcome_at": self._last_outcome_at.isoformat(),
            }

    def _record_outcome(self, outcome: str, observed_at=None) -> None:
        self._last_outcome = outcome
        self._last_outcome_at = observed_at or self._clock()

    def _deenergize(self) -> bool:
        with self._activation_boundary:
            return self._deenergize_at_boundary()

    def _deenergize_at_boundary(self) -> bool:
        for _ in range(self._max_off_attempts):
            try:
                self._relay.off()
                return True
            except Exception:
                continue
        return False


class PiRelayAdapter:
    """Load the Pi-only GPIO module only when a real relay is configured."""

    def __init__(self, relay_name: str = "RELAY1"):
        import PiRelay
        self._relay = PiRelay.Relay(relay_name)

    def on(self, *, pre_activation_inhibit=None):
        return self._relay.on(pre_activation_inhibit=pre_activation_inhibit)

    @property
    def activation_boundary(self):
        return self._relay.activation_boundary

    def off(self) -> None:
        self._relay.off()


def _accepts_keyword(callable_object, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
