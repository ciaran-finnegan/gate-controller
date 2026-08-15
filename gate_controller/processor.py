import hashlib
import inspect
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from .actuation import ActuationCoordinator
from .matching import decide_access, normalise_plate
from .models import GateEvent, ProcessingResult


MAX_OCR_FRAMES = 3


class GateProcessor:
    def __init__(self, recognizer, store, relay, authorised: Iterable[str],
                 cooldown: timedelta = timedelta(seconds=20), outbox=None, clock=None,
                 coordinator=None, max_image_age: timedelta = timedelta(seconds=8),
                 decision_timeout: float = 4.0, decision_clock=None):
        self._recognizer = recognizer
        self._store = store
        self._authorised = authorised if callable(authorised) else lambda: tuple(authorised)
        self._outbox = outbox
        self._outbox_enabled = outbox is not None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_image_age = max_image_age
        self._decision_timeout = decision_timeout
        self._decision_clock = decision_clock or monotonic
        self._coordinator = coordinator or ActuationCoordinator(store, relay, cooldown, self._clock)

    def process(self, paths: Iterable[Path], received_at: datetime | None = None,
                decision_started_at: float | None = None) -> ProcessingResult:
        started = self._decision_clock() if decision_started_at is None else decision_started_at
        paths = _unique_content_paths(tuple(Path(path) for path in paths))
        idempotency_key = _event_key(paths)
        if self._store.event_exists(idempotency_key):
            if self._outbox_enabled:
                event_id = self._store.terminal_outcome(idempotency_key)
                event_id = event_id.event_id if event_id else self._store.event_id(idempotency_key)
                if event_id is not None:
                    self._store.ensure_outbox(event_id, self._outbox_payload(paths))
            return ProcessingResult(False, self._store.actuation_claim_status(idempotency_key) or "duplicate_event")
        received_at = received_at or self._clock()
        now = self._clock()
        if self._decision_clock() - started >= self._decision_timeout:
            return self.record_skipped(paths, "decision_timeout", received_at)
        if not _is_fresh(now, received_at, self._max_image_age):
            return self.record_skipped(paths, "stale_burst", received_at)
        try:
            authorised = self._authorised()
        except Exception:
            return self.record_skipped(paths, "authorisation_error", received_at)
        observations = []
        saw_ocr_error = False
        decision = None
        timed_out = False
        for path in paths[:MAX_OCR_FRAMES]:
            remaining = self._decision_timeout - (self._decision_clock() - started)
            if remaining <= 0:
                timed_out = True
                break
            try:
                observations.append(self._recognise(path, remaining))
            except Exception:
                saw_ocr_error = True
                continue
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
            return self.record_skipped(paths, reason, received_at)
        decision_at = self._clock()
        if timed_out:
            event = _denied_event(idempotency_key, received_at, decision_at, "decision_timeout",
                                  decision)
            event_id = self._record(event, paths)
            return ProcessingResult(False, "decision_timeout", event_id, decision)
        if not _is_fresh(decision_at, received_at, self._max_image_age):
            event = _denied_event(idempotency_key, received_at, decision_at, "stale_burst",
                                  decision)
            event_id = self._record(event, paths)
            return ProcessingResult(False, "stale_burst", event_id, decision)
        event = GateEvent(
            source="ocr", reason=decision.reason, opened=False, idempotency_key=idempotency_key,
            received_at=received_at, decision_at=decision_at,
            authorised_plate=decision.authorised_plate, observed_plate=decision.observed_plate,
            ocr_confidence=decision.confidence,
        )
        outbox_payload = self._outbox_payload(paths)
        if not decision.allowed:
            if saw_ocr_error:
                event = _denied_event(idempotency_key, received_at, decision_at, "ocr_error",
                                      decision)
            event_id = self._store.record_event_with_outbox(event, outbox_payload)
            return ProcessingResult(False, event.reason, event_id, decision)
        actuation_at = self._clock()
        if not _is_fresh(actuation_at, received_at, self._max_image_age):
            event = _denied_event(
                idempotency_key, received_at, actuation_at, "stale_burst", decision
            )
            event_id = self._store.record_event_with_outbox(event, outbox_payload)
            return ProcessingResult(False, "stale_burst", event_id, decision)
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

        execution = self._coordinator.actuate(
            event,
            outbox_payload=outbox_payload,
            pre_activation_inhibit=activation_inhibition,
        )
        return ProcessingResult(execution.opened, execution.reason, execution.event_id, decision)

    def record_skipped(self, paths: Iterable[Path], reason: str,
                       received_at: datetime | None = None) -> ProcessingResult:
        paths = tuple(Path(path) for path in paths)
        event = GateEvent(
            source="ocr", reason=reason, opened=False, idempotency_key=_event_key(paths),
            received_at=received_at or self._clock(), decision_at=self._clock(),
        )
        event_id = self._record(event, paths)
        return ProcessingResult(False, reason, event_id)

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

    def _recognise(self, path: Path, remaining: float):
        parameters = inspect.signature(self._recognizer.recognise).parameters
        if "timeout" not in parameters:
            return self._recognizer.recognise(path)
        connect = min(1.0, max(0.1, remaining / 3))
        read = min(2.0, max(0.1, remaining - connect))
        return self._recognizer.recognise(path, timeout=(connect, read))


def _event_key(paths: tuple[Path, ...]) -> str:
    if not paths:
        return hashlib.sha256(b"empty-burst").hexdigest()
    return _content_digest(paths[0])


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
    unique = []
    digests = set()
    for path in paths:
        digest = _content_digest(path)
        if digest in digests:
            continue
        digests.add(digest)
        unique.append(path)
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
