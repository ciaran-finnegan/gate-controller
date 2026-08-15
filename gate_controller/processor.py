from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from .actuation import ActuationCoordinator
from .images import measure_frame_quality
from .matching import decide_access, normalise_plate
from .models import GateEvent, ProcessingResult
from .telemetry import OcrAttemptTelemetry, ProcessingTrace


MAX_OCR_FRAMES = 3


class GateProcessor:
    def __init__(self, recognizer, store, relay, authorised: Iterable[str],
                 cooldown: timedelta = timedelta(seconds=20), outbox=None, clock=None,
                 coordinator=None, max_image_age: timedelta = timedelta(seconds=8),
                 decision_timeout: float = 4.0, decision_clock=None,
                 telemetry_clock=None, telemetry_wall_clock=None, trace_factory=None):
        self._recognizer = recognizer
        self._store = store
        self._authorised = authorised if callable(authorised) else lambda: tuple(authorised)
        self._outbox = outbox
        self._outbox_enabled = outbox is not None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_image_age = max_image_age
        self._decision_timeout = decision_timeout
        self._decision_clock = decision_clock or monotonic
        self._telemetry_clock = telemetry_clock or monotonic
        self._telemetry_wall_clock = telemetry_wall_clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._trace_factory = trace_factory or ProcessingTrace
        self._coordinator = coordinator or ActuationCoordinator(store, relay, cooldown, self._clock)

    def process(self, paths: Iterable[Path], received_at: datetime | None = None,
                decision_started_at: float | None = None) -> ProcessingResult:
        started = self._decision_clock() if decision_started_at is None else decision_started_at
        candidates = _unique_content_candidates(tuple(Path(path) for path in paths))
        paths = tuple(path for path, _digest in candidates)
        digests = tuple(digest for _path, digest in candidates)
        idempotency_key = _event_key_from_digests(digests)
        if self._store.event_exists(idempotency_key):
            if self._outbox_enabled:
                event_id = self._store.terminal_outcome(idempotency_key)
                event_id = event_id.event_id if event_id else self._store.event_id(idempotency_key)
                if event_id is not None:
                    self._store.ensure_outbox(event_id, self._outbox_payload(paths))
            return ProcessingResult(False, self._store.actuation_claim_status(idempotency_key) or "duplicate_event")
        trace = self._new_trace()
        if received_at is not None or decision_started_at is not None:
            trace.seed_upstream(received_at, decision_started_at)
        if decision_started_at is None:
            trace.mark_burst()
        received_at = received_at or self._clock()
        now = self._clock()
        if self._decision_clock() - started >= self._decision_timeout:
            return self.record_skipped(
                paths, "decision_timeout", received_at, trace=trace,
                idempotency_key=idempotency_key,
            )
        if not _is_fresh(now, received_at, self._max_image_age):
            return self.record_skipped(
                paths, "stale_burst", received_at, trace=trace,
                idempotency_key=idempotency_key,
            )
        try:
            authorised = self._authorised()
        except Exception:
            return self.record_skipped(
                paths, "authorisation_error", received_at, trace=trace,
                idempotency_key=idempotency_key,
            )
        observations = []
        saw_ocr_error = False
        decision = None
        timed_out = False
        for sequence, path in enumerate(paths[:MAX_OCR_FRAMES]):
            remaining = self._decision_timeout - (self._decision_clock() - started)
            if remaining <= 0:
                timed_out = True
                break
            try:
                frame_quality = replace(
                    measure_frame_quality(path, digest=digests[sequence]),
                    sequence=sequence,
                )
            except Exception:
                trace.disable()
            else:
                trace.add_frame(frame_quality)
            remaining = self._decision_timeout - (self._decision_clock() - started)
            if remaining <= 0:
                timed_out = True
                break
            ocr_started = False

            def mark_ocr_start():
                nonlocal ocr_started
                trace.mark_ocr_start()
                ocr_started = True

            try:
                observation = self._recognise(path, remaining, mark_ocr_start)
            except Exception:
                if ocr_started:
                    trace.add_ocr_attempt(OcrAttemptTelemetry(
                        frame_sequence=sequence,
                        status="ocr_error",
                    ))
                saw_ocr_error = True
                continue
            trace.add_ocr_attempt(OcrAttemptTelemetry(
                frame_sequence=sequence,
                status="recognized" if observation.plate else "no_plate",
                plate=observation.plate,
                confidence=observation.confidence,
                make=observation.make,
                colour=observation.colour,
            ))
            observations.append(observation)
            decision = decide_access(observations, authorised)
            if self._decision_clock() - started >= self._decision_timeout:
                timed_out = True
                break
            if decision.allowed:
                break
        if not (decision and decision.allowed) and self._decision_clock() - started >= self._decision_timeout:
            timed_out = True
        if decision is None:
            reason = "decision_timeout" if timed_out else ("ocr_error" if saw_ocr_error else "no_match")
            return self.record_skipped(
                paths, reason, received_at, trace=trace,
                idempotency_key=idempotency_key,
            )
        decision_at = self._clock()
        if timed_out:
            trace.mark_decision("denied", "decision_timeout")
            event = _denied_event(idempotency_key, received_at, decision_at, "decision_timeout",
                                  decision)
            event_id = self._record(event, paths)
            return self._finish_result(
                trace, ProcessingResult(False, "decision_timeout", event_id, decision)
            )
        if not _is_fresh(decision_at, received_at, self._max_image_age):
            trace.mark_decision("denied", "stale_burst")
            event = _denied_event(idempotency_key, received_at, decision_at, "stale_burst",
                                  decision)
            event_id = self._record(event, paths)
            return self._finish_result(
                trace, ProcessingResult(False, "stale_burst", event_id, decision)
            )
        event = GateEvent(
            source="ocr", reason=decision.reason, opened=False, idempotency_key=idempotency_key,
            received_at=received_at, decision_at=decision_at,
            authorised_plate=decision.authorised_plate, observed_plate=decision.observed_plate,
            ocr_confidence=decision.confidence,
        )
        if not decision.allowed:
            if saw_ocr_error:
                event = _denied_event(idempotency_key, received_at, decision_at, "ocr_error",
                                      decision)
            trace.mark_decision("denied", event.reason)
        else:
            trace.mark_decision("allowed", decision.reason)
        outbox_payload = self._outbox_payload(paths)
        if not decision.allowed:
            event_id = self._store.record_event_with_outbox(event, outbox_payload)
            return self._finish_result(
                trace, ProcessingResult(False, event.reason, event_id, decision)
            )
        actuation_at = self._clock()
        if not _is_fresh(actuation_at, received_at, self._max_image_age):
            event = _denied_event(
                idempotency_key, received_at, actuation_at, "stale_burst", decision
            )
            event_id = self._store.record_event_with_outbox(event, outbox_payload)
            return self._finish_result(
                trace, ProcessingResult(False, "stale_burst", event_id, decision)
            )
        def activation_inhibition():
            if not _is_fresh(self._clock(), received_at, self._max_image_age):
                return "failed", "stale_burst"
            try:
                current_authorised = {
                    normalise_plate(plate) for plate in self._authorised()
                }
            except Exception:
                return "failed", "authorisation_error"
            if normalise_plate(decision.authorised_plate) not in current_authorised:
                return "failed", "authorisation_revoked"
            return None

        actuation_kwargs = {
            "outbox_payload": outbox_payload,
            "pre_activation_inhibit": activation_inhibition,
        }
        if _accepts_keyword(self._coordinator.actuate, "on_activation"):
            actuation_kwargs["on_activation"] = trace.mark_relay_activation
        execution = self._coordinator.actuate(event, **actuation_kwargs)
        trace.set_actuation_outcome(*_actuation_telemetry(execution))
        return self._finish_result(
            trace,
            ProcessingResult(execution.opened, execution.reason, execution.event_id, decision),
        )

    def record_skipped(self, paths: Iterable[Path], reason: str,
                       received_at: datetime | None = None,
                       trace: ProcessingTrace | _BestEffortTrace | None = None,
                       idempotency_key: str | None = None) -> ProcessingResult:
        paths = tuple(Path(path) for path in paths)
        idempotency_key = idempotency_key or _event_key(paths)
        if self._store.event_exists(idempotency_key):
            event_id = self._store.event_id(idempotency_key)
            if self._outbox_enabled and event_id is not None:
                self._store.ensure_outbox(event_id, self._outbox_payload(paths))
            return ProcessingResult(
                False,
                self._store.actuation_claim_status(idempotency_key) or "duplicate_event",
                event_id,
            )
        if trace is None:
            trace = self._new_trace()
            if received_at is not None:
                trace.seed_upstream(received_at, None)
            trace.mark_burst()
        else:
            trace = _BestEffortTrace.wrap(trace)
        trace.mark_decision("denied", reason)
        event = GateEvent(
            source="ocr", reason=reason, opened=False,
            idempotency_key=idempotency_key,
            received_at=received_at or self._clock(), decision_at=self._clock(),
        )
        event_id = self._record(event, paths)
        result = ProcessingResult(False, reason, event_id)
        return self._finish_result(trace, result)

    @staticmethod
    def _finish_result(
        trace: _BestEffortTrace, result: ProcessingResult
    ) -> ProcessingResult:
        telemetry = trace.finish()
        return result if telemetry is None else replace(result, telemetry=telemetry)

    def _new_trace(self) -> _BestEffortTrace:
        return _BestEffortTrace.create(
            self._trace_factory,
            monotonic_clock=self._telemetry_clock,
            wall_clock=self._telemetry_wall_clock,
        )

    def _record(self, event: GateEvent, paths: Iterable[Path] = ()) -> int:
        payload = self._outbox_payload(paths)
        return self._store.record_event_with_outbox(event, payload)

    def _outbox_payload(self, paths: Iterable[Path]) -> dict | None:
        if not self._outbox_enabled:
            return None
        paths = tuple(Path(path) for path in paths)
        prepare_payload = getattr(self._outbox, "prepare_payload", None)
        if not callable(prepare_payload):
            return {"event_id": None}
        return prepare_payload(paths[0] if paths else None)

    def _recognise(self, path: Path, remaining: float, on_start=None):
        parameters = inspect.signature(self._recognizer.recognise).parameters
        if "timeout" not in parameters:
            if on_start is not None:
                on_start()
            return self._recognizer.recognise(path)
        connect = min(1.0, max(0.1, remaining / 3))
        read = min(2.0, max(0.1, remaining - connect))
        if on_start is not None:
            on_start()
        return self._recognizer.recognise(path, timeout=(connect, read))


class _BestEffortTrace:
    """Disable a trace after its first failure without affecting gate processing."""

    def __init__(self, trace) -> None:
        self._trace = trace

    @classmethod
    def create(cls, factory, **kwargs):
        try:
            trace = factory(**kwargs)
        except Exception:
            trace = None
        return cls(trace)

    @classmethod
    def wrap(cls, trace):
        return trace if isinstance(trace, cls) else cls(trace)

    def disable(self) -> None:
        self._trace = None

    def _call(self, operation: str, *args, **kwargs):
        if self._trace is None:
            return None
        try:
            return getattr(self._trace, operation)(*args, **kwargs)
        except Exception:
            self.disable()
            return None

    def mark_burst(self) -> None:
        self._call("mark_burst")

    def seed_upstream(
        self, received_at: datetime | None, decision_started_at: float | None
    ) -> None:
        self._call("seed_upstream", received_at, decision_started_at)

    def add_frame(self, frame) -> None:
        self._call("add_frame", frame)

    def mark_ocr_start(self) -> None:
        self._call("mark_ocr_start")

    def add_ocr_attempt(self, attempt) -> None:
        self._call("add_ocr_attempt", attempt)

    def mark_decision(self, outcome: str, reason: str) -> None:
        self._call("mark_decision", outcome, reason)

    def mark_relay_activation(self) -> None:
        self._call("mark_relay_activation")

    def set_actuation_outcome(
        self, claim: str, attempted: bool, relay_outcome: str
    ) -> None:
        self._call("set_actuation_outcome", claim, attempted, relay_outcome)

    def finish(self):
        return self._call("finish")


def _event_key(paths: tuple[Path, ...]) -> str:
    return _event_key_from_digests(
        tuple(_content_digest(path) for path in paths[:1])
    )


def _event_key_from_digests(digests: tuple[str, ...]) -> str:
    return digests[0] if digests else hashlib.sha256(b"empty-burst").hexdigest()


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        digest.update(f"missing:{path.resolve()}".encode("utf-8"))
    return digest.hexdigest()


def _unique_content_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(path for path, _digest in _unique_content_candidates(paths))


def _unique_content_candidates(paths: tuple[Path, ...]) -> tuple[tuple[Path, str], ...]:
    unique = []
    digests = set()
    for path in paths:
        digest = _content_digest(path)
        if digest in digests:
            continue
        digests.add(digest)
        unique.append((path, digest))
    return tuple(unique)


def _denied_event(key, received_at, decision_at, reason, decision=None):
    return GateEvent(
        source="ocr", reason=reason, opened=False, idempotency_key=key,
        received_at=received_at, decision_at=decision_at,
        authorised_plate=decision.authorised_plate if decision else None,
        observed_plate=decision.observed_plate if decision else None,
        ocr_confidence=decision.confidence if decision else 0.0,
    )


def _is_fresh(now: datetime, received_at: datetime, max_age: timedelta) -> bool:
    try:
        age = now - received_at
    except (TypeError, ValueError):
        return False
    return timedelta(0) <= age <= max_age


def _actuation_telemetry(execution) -> tuple[str, bool, str]:
    if execution.opened:
        return "claimed", True, "activated"
    if execution.reason == "cooldown":
        return "cooldown", False, "not_attempted"
    if execution.reason == "actuation_inhibit_error":
        return "claim_error", False, "not_attempted"
    if execution.reason in {"stale_burst", "authorisation_error", "authorisation_revoked"}:
        return "claimed", False, "inhibited"
    if execution.reason == "indeterminate_claim":
        return "claimed", True, "indeterminate"
    return "claimed", True, execution.reason


def _accepts_keyword(callable_object, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
