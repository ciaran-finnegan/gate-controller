from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic

from .actuation import ActuationCoordinator
from .images import measure_frame_quality
from .matching import decide_access, normalise_plate
from .models import GateEvent, ProcessingResult
from .telemetry import OcrAttemptTelemetry, ProcessingTrace


MAX_OCR_FRAMES = 3
DEFAULT_ACTIVATION_GUARD_SECONDS = 0.1
FINAL_INHIBITION_REASONS = frozenset({
    "stale_burst", "authorisation_error", "authorisation_revoked",
    "decision_timeout", "processor_closed",
})


class _OcrBusy(TimeoutError):
    pass


class _OcrDeadlineExceeded(TimeoutError):
    pass


class GateProcessor:
    def __init__(self, recognizer, store, relay, authorised: Iterable[str],
                 cooldown: timedelta = timedelta(seconds=20), outbox=None, clock=None,
                 coordinator=None, max_image_age: timedelta = timedelta(seconds=8),
                 decision_timeout: float = 4.0,
                 activation_guard_seconds: float | None = None,
                 decision_clock=None,
                 telemetry_clock=None, telemetry_wall_clock=None, trace_factory=None):
        if not math.isfinite(decision_timeout) or decision_timeout <= 0:
            raise ValueError("decision timeout must be finite and greater than zero")
        if activation_guard_seconds is None:
            activation_guard_seconds = min(
                DEFAULT_ACTIVATION_GUARD_SECONDS, decision_timeout / 10,
            )
        if (not math.isfinite(activation_guard_seconds)
                or not 0 <= activation_guard_seconds < decision_timeout):
            raise ValueError(
                "activation guard must be finite, non-negative, and below the decision timeout"
            )
        self._recognizer = recognizer
        self._store = store
        self._authorised = authorised if callable(authorised) else lambda: tuple(authorised)
        self._outbox = outbox
        self._outbox_enabled = outbox is not None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_image_age = max_image_age
        self._decision_timeout = decision_timeout
        self._activation_guard_seconds = activation_guard_seconds
        self._decision_clock = decision_clock or monotonic
        self._telemetry_clock = telemetry_clock or monotonic
        self._telemetry_wall_clock = telemetry_wall_clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._trace_factory = trace_factory or ProcessingTrace
        self._coordinator = coordinator or ActuationCoordinator(store, relay, cooldown, self._clock)
        self._ocr_slot = BoundedSemaphore(1)
        self._ocr_slot_lock = Lock()
        self._actuation_lock = Lock()
        self._ocr_worker = None
        self._closed = False
        self._recognise_call = self._recognizer.recognise
        self._recognizer_accepts_timeout = _accepts_keyword(self._recognise_call, "timeout")

    def process(self, paths: Iterable[Path], received_at: datetime | None = None,
                decision_started_at: float | None = None,
                processing_started_at: datetime | None = None) -> ProcessingResult:
        started = self._decision_clock() if decision_started_at is None else decision_started_at
        deadline = started + self._decision_timeout
        activation_deadline = deadline - self._activation_guard_seconds
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
        if (
            received_at is not None
            or decision_started_at is not None
            or processing_started_at is not None
        ):
            trace.seed_upstream(
                received_at, decision_started_at, processing_started_at
            )
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
        ocr_failure_reason = None
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
                observation = self._recognise(path, deadline, mark_ocr_start)
            except _OcrBusy:
                trace.add_ocr_rejection(OcrAttemptTelemetry(
                    frame_sequence=sequence,
                    status="ocr_busy",
                ))
                ocr_failure_reason = "ocr_busy"
                break
            except _OcrDeadlineExceeded:
                if ocr_started:
                    trace.add_ocr_attempt(OcrAttemptTelemetry(
                        frame_sequence=sequence,
                        status="ocr_timeout",
                    ))
                timed_out = True
                break
            except Exception:
                if ocr_started:
                    trace.add_ocr_attempt(OcrAttemptTelemetry(
                        frame_sequence=sequence,
                        status="ocr_error",
                    ))
                ocr_failure_reason = "ocr_error"
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
            reason = "decision_timeout" if timed_out else (ocr_failure_reason or "no_match")
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
            if ocr_failure_reason == "ocr_error":
                event = _denied_event(idempotency_key, received_at, decision_at, "ocr_error",
                                      decision)
            trace.mark_decision("denied", event.reason)
        else:
            trace.mark_decision("allowed", decision.reason)
        outbox_payload = self._outbox_payload(paths, await_telemetry=True)
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
            with self._ocr_slot_lock:
                if self._closed:
                    return "failed", "processor_closed"
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
            if self._decision_clock() >= activation_deadline:
                return "failed", "decision_timeout"
            return None

        actuation_kwargs = {
            "outbox_payload": outbox_payload,
            "pre_activation_inhibit": activation_inhibition,
        }
        if _accepts_keyword(self._coordinator.actuate, "on_activation"):
            actuation_kwargs["on_activation"] = trace.mark_relay_activation
        if _accepts_keyword(self._coordinator.actuate, "on_deactivation"):
            actuation_kwargs["on_deactivation"] = trace.mark_relay_finished
        with self._actuation_lock:
            execution = self._coordinator.actuate(event, **actuation_kwargs)
        if execution.reason in FINAL_INHIBITION_REASONS:
            trace.revise_decision("denied", execution.reason)
        trace.set_actuation_outcome(*_actuation_telemetry(execution))
        return self._finish_result(
            trace,
            ProcessingResult(execution.opened, execution.reason, execution.event_id, decision),
        )

    def record_skipped(self, paths: Iterable[Path], reason: str,
                       received_at: datetime | None = None,
                       trace: ProcessingTrace | _BestEffortTrace | None = None,
                       idempotency_key: str | None = None, *,
                       decision_started_at: float | None = None,
                       processing_started_at: datetime | None = None) -> ProcessingResult:
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
            if (
                received_at is not None
                or decision_started_at is not None
                or processing_started_at is not None
            ):
                trace.seed_upstream(
                    received_at, decision_started_at, processing_started_at
                )
            if decision_started_at is None:
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

    def _finish_result(
        self, trace: _BestEffortTrace, result: ProcessingResult
    ) -> ProcessingResult:
        telemetry = trace.finish()
        if telemetry is None:
            self._release_outbox_without_telemetry(result.event_id)
            return result
        completed = replace(result, telemetry=telemetry)
        _log_completed_trace(telemetry)
        if result.event_id is not None:
            try:
                self._store.attach_event_telemetry(result.event_id, telemetry)
            except Exception:
                logging.getLogger(__name__).warning(
                    "event_telemetry_attach status=persistence_failed"
                )
                self._release_outbox_without_telemetry(result.event_id)
        return completed

    def _release_outbox_without_telemetry(self, event_id: int | None) -> None:
        if not self._outbox_enabled or event_id is None:
            return
        try:
            self._store.release_outbox_without_telemetry(event_id)
        except Exception:
            logging.getLogger(__name__).warning(
                "outbox_telemetry_wait status=release_failed"
            )

    def _new_trace(self) -> _BestEffortTrace:
        return _BestEffortTrace.create(
            self._trace_factory,
            monotonic_clock=self._telemetry_clock,
            wall_clock=self._telemetry_wall_clock,
        )

    def _record(self, event: GateEvent, paths: Iterable[Path] = ()) -> int:
        payload = self._outbox_payload(paths, await_telemetry=True)
        return self._store.record_event_with_outbox(event, payload)

    def _outbox_payload(
        self, paths: Iterable[Path], *, await_telemetry: bool = False,
    ) -> dict | None:
        if not self._outbox_enabled:
            return None
        paths = tuple(Path(path) for path in paths)
        prepare_payload = getattr(self._outbox, "prepare_payload", None)
        if not callable(prepare_payload):
            payload = {"event_id": None}
        else:
            payload = prepare_payload(paths[0] if paths else None)
        if await_telemetry:
            payload["_awaiting_telemetry"] = True
        return payload

    def _recognise(self, path: Path, deadline: float, on_start=None):
        remaining = deadline - self._decision_clock()
        if remaining <= 0:
            raise _OcrDeadlineExceeded("OCR request exceeded the decision deadline")
        operation = lambda: self._recognise_call(path)
        if not self._recognizer_accepts_timeout:
            return self._run_ocr_bounded(operation, deadline, on_start)
        connect = min(1.0, max(0.1, remaining / 3))
        read = min(2.0, max(0.1, remaining - connect))
        operation = lambda: self._recognise_call(path, timeout=(connect, read))
        return self._run_ocr_bounded(operation, deadline, on_start)

    def _run_ocr_bounded(self, operation, deadline: float, on_start=None):
        with self._ocr_slot_lock:
            if self._closed:
                raise _OcrBusy("OCR processor is closed")
            slot = self._ocr_slot
            if not slot.acquire(blocking=False):
                raise _OcrBusy("previous OCR request is still running")
        if on_start is not None:
            on_start()
        result = Queue(maxsize=1)

        def invoke():
            try:
                outcome = (True, operation())
            except Exception as error:
                outcome = (False, error)
            finally:
                slot.release()
            result.put(outcome)

        worker = Thread(target=invoke, name="gate-ocr-request", daemon=True)
        with self._ocr_slot_lock:
            if self._closed:
                slot.release()
                raise _OcrBusy("OCR processor is closed")
            self._ocr_worker = worker
            try:
                worker.start()
            except BaseException:
                if self._ocr_worker is worker:
                    self._ocr_worker = None
                slot.release()
                raise
        remaining = deadline - self._decision_clock()
        if remaining <= 0:
            self._cancel_and_reap_ocr_worker(worker)
            raise _OcrDeadlineExceeded("OCR request exceeded the decision deadline")
        try:
            succeeded, value = result.get(timeout=max(remaining, 0.0))
        except Empty as error:
            self._cancel_and_reap_ocr_worker(worker)
            raise _OcrDeadlineExceeded(
                "OCR request exceeded the decision deadline"
            ) from error
        worker.join(0.5)
        with self._ocr_slot_lock:
            if self._ocr_worker is worker and not worker.is_alive():
                self._ocr_worker = None
        if succeeded:
            return value
        raise value

    def _cancel_and_reap_ocr_worker(self, worker) -> None:
        abandon = getattr(self._recognizer, "abandon_in_flight", None)
        if not callable(abandon):
            return
        outcome = []

        def abandon_recognizer():
            try:
                outcome.append(abandon() is True)
            except Exception:
                outcome.append(False)

        cleanup = Thread(
            target=abandon_recognizer, name="gate-ocr-abandon", daemon=True,
        )
        cleanup.start()
        cleanup.join(0.05)
        if not cleanup.is_alive() and outcome and outcome[0]:
            worker.join(0.5)
        with self._ocr_slot_lock:
            if self._ocr_worker is worker and not worker.is_alive():
                self._ocr_worker = None

    def close(self) -> None:
        with self._ocr_slot_lock:
            if self._closed:
                return
            self._closed = True
            worker = self._ocr_worker
        with self._actuation_lock:
            pass
        close = getattr(self._recognizer, "close", None)
        if callable(close):
            def close_recognizer():
                try:
                    close()
                except Exception:
                    pass

            cleanup = Thread(
                target=close_recognizer, name="gate-ocr-close", daemon=True,
            )
            cleanup.start()
            cleanup.join(0.5)
        if worker is not None:
            worker.join(0.5)
            with self._ocr_slot_lock:
                if self._ocr_worker is worker and not worker.is_alive():
                    self._ocr_worker = None


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
        self, received_at: datetime | None, decision_started_at: float | None,
        processing_started_at: datetime | None = None,
    ) -> None:
        self._call(
            "seed_upstream", received_at, decision_started_at, processing_started_at
        )

    def add_frame(self, frame) -> None:
        self._call("add_frame", frame)

    def mark_ocr_start(self) -> None:
        self._call("mark_ocr_start")

    def add_ocr_attempt(self, attempt) -> None:
        self._call("add_ocr_attempt", attempt)

    def add_ocr_rejection(self, attempt) -> None:
        self._call("add_ocr_rejection", attempt)

    def mark_decision(self, outcome: str, reason: str) -> None:
        self._call("mark_decision", outcome, reason)

    def revise_decision(self, outcome: str, reason: str) -> None:
        if self._trace is None:
            return
        operation = getattr(self._trace, "revise_decision", None)
        if not callable(operation):
            return
        try:
            operation(outcome, reason)
        except Exception:
            self.disable()

    def mark_relay_activation(self) -> None:
        self._call("mark_relay_activation")

    def mark_relay_finished(self) -> None:
        self._call("mark_relay_finished")

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
    if execution.reason == "relay_latched":
        return "claimed", False, "relay_latched"
    if execution.reason in FINAL_INHIBITION_REASONS:
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


def _log_completed_trace(telemetry) -> None:
    try:
        wire = telemetry.to_wire()
        attempts = [
            {
                "frame_sequence": attempt.get("frame_sequence"),
                "duration_ms": attempt.get("duration_ms"),
                "status": attempt.get("status"),
            }
            for attempt in wire.get("ocr_attempts", ())
        ]
        logging.getLogger(__name__).info(
            "gate_pipeline stage=processing_finished trace_id=%s outcome=%s reason=%s "
            "relay_outcome=%s durations=%s timestamps=%s ocr_attempts=%s",
            wire["trace_id"],
            wire["decision"]["outcome"],
            wire["decision"]["reason"],
            wire["actuation"]["relay_outcome"],
            json.dumps(
                wire["stage_durations"], sort_keys=True, separators=(",", ":")
            ),
            json.dumps(
                wire.get("stage_timestamps", {}),
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(attempts, sort_keys=True, separators=(",", ":")),
        )
    except Exception:
        return
