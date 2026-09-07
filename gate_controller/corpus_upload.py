"""Ship the training corpus to Cloudflare, in the gaps between gate events.

The corpus exists on exactly one SD card, in a warm cabinet, with no copy
anywhere. At the size cap the oldest examples are deleted permanently, and
cards in warm Pis fail. Both losses are silent and neither is recoverable, so
the corpus belongs in R2 with its index in D1.

What makes this safe is not the size of the transfer -- a day of corpus is
about 5.6 MB, roughly ten seconds of a 4.5 Mbit/s uplink -- but never spending
those seconds at the wrong moment. The rules, in the order they are checked:

1. **Nothing while the gate is working.** A camera event, a presence session
   or an OCR request in flight blocks a start outright.
2. **A quiet period first.** The link must have been idle for
   ``GATE_CORPUS_QUIET_SECONDS`` (60 s by default). The gaps inside a presence
   session -- the spacing between frames, the wait for a verdict -- are far
   shorter than that, so a session is never mistaken for quiet.
3. **Real events first.** A non-empty outbox blocks the corpus entirely. An
   owner waiting for an evidence image outranks a training frame, and the
   queue is already p90 84 s deep.
4. **Abandon, do not finish.** A transfer watches the activity epoch between
   chunks and raises out of the request the moment a gate event begins. The
   local copy is untouched and the attempt is not counted as a failure.
5. **A modest fraction of the link.** The body is paced to
   ``GATE_CORPUS_UPLOAD_BYTES_PER_SECOND`` (64 KB/s by default, about an
   eighth of the uplink), so even a transfer that is running when something
   goes wrong leaves most of the link free.
6. **Defer rather than compete.** Any block ends the pass; the worker waits
   for its next poll instead of spinning.

Nothing here can affect a gate decision. It runs on its own background worker,
it holds no lock the pipeline takes, every failure is caught, and a failure
always leaves the local copy exactly where it was.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from time import monotonic, sleep as _sleep

from .backpressure import NULL_GATE, bounded_quiet_seconds
from .corpus import FRAME_KIND, FRAME_MEDIA_TYPE, MAX_SIDECAR_BYTES

LOGGER = logging.getLogger(__name__)

CORPUS_SCHEMA_VERSION = 1

#: One artefact per request. A short request is a cheap one to abandon, and it
#: keeps the D1 write to a single row.
DEFAULT_BATCH = 8
MAX_BATCH = 64
DEFAULT_POLL_SECONDS = 300.0
MIN_POLL_SECONDS = 5.0
MAX_POLL_SECONDS = 86400.0
#: 64 KB/s is about 512 kbit/s: roughly an eighth of the measured 4.5 Mbit/s
#: uplink, and enough to move a day of corpus in a little over a minute.
DEFAULT_BYTES_PER_SECOND = 64 * 1024
MIN_BYTES_PER_SECOND = 4 * 1024
MAX_BYTES_PER_SECOND = 512 * 1024
CHUNK_BYTES = 8 * 1024
#: The longest a single pacing sleep may last, so a stop signal is never more
#: than this far away.
MAX_PACING_SLEEP_SECONDS = 0.5
#: Generous for a frame (a 4K crop is well under 1 MB) and ample headroom for
#: the audio clip that is coming.
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
#: Slower than the outbox on purpose. Nobody is waiting for this.
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 3600.0
UPLOAD_READ_TIMEOUT_SECONDS = 120.0

#: What each payload suffix is, and which artefact family it belongs to. The
#: audio rows are here already: adding capture means writing the file, not
#: teaching this module a second pipeline.
MEDIA_TYPES = {
    ".jpg": ("frame", "image/jpeg"),
    ".jpeg": ("frame", "image/jpeg"),
    ".png": ("frame", "image/png"),
    ".wav": ("audio", "audio/wav"),
    ".flac": ("audio", "audio/flac"),
    ".opus": ("audio", "audio/ogg"),
    ".ogg": ("audio", "audio/ogg"),
    ".m4a": ("audio", "audio/mp4"),
}


class CorpusUploadError(RuntimeError):
    """The cloud refused, or never confirmed, an artefact."""


class CorpusUploadAborted(Exception):
    """A gate event started; this transfer stood down mid-request."""


class CorpusUploadUnshippable(Exception):
    """This artefact cannot be sent as it stands, and retrying will not help."""


@dataclass(frozen=True)
class CorpusUploadConfig:
    enabled: bool = False
    quiet_seconds: float = 60.0
    poll_interval: float = DEFAULT_POLL_SECONDS
    batch: int = DEFAULT_BATCH
    bytes_per_second: int = DEFAULT_BYTES_PER_SECOND


def load_corpus_upload_config(environment) -> CorpusUploadConfig:
    """Read the upload settings. On unless explicitly turned off.

    The uploader only ever runs when a corpus directory and the Cloudflare
    credentials are both configured, which the caller checks; this decides how
    it behaves once it does. ``GATE_CORPUS_UPLOAD=off`` keeps the corpus
    local, which is the state this change exists to end -- so it is a switch
    an operator turns, not the default.
    """
    raw = str(environment.get("GATE_CORPUS_UPLOAD", "on")).strip().lower()
    if raw not in ("on", "off", "true", "false", "1", "0", ""):
        raise ValueError("GATE_CORPUS_UPLOAD must be 'on' or 'off'")
    enabled = raw not in ("off", "false", "0")
    return CorpusUploadConfig(
        enabled=enabled,
        quiet_seconds=bounded_quiet_seconds(
            environment.get("GATE_CORPUS_QUIET_SECONDS", "60")
        ),
        poll_interval=_bounded(
            environment.get("GATE_CORPUS_POLL_SECONDS", DEFAULT_POLL_SECONDS),
            MIN_POLL_SECONDS, MAX_POLL_SECONDS, "GATE_CORPUS_POLL_SECONDS",
        ),
        batch=_bounded_integer(
            environment.get("GATE_CORPUS_BATCH", DEFAULT_BATCH),
            1, MAX_BATCH, "GATE_CORPUS_BATCH",
        ),
        bytes_per_second=_bounded_integer(
            environment.get(
                "GATE_CORPUS_UPLOAD_BYTES_PER_SECOND", DEFAULT_BYTES_PER_SECOND
            ),
            MIN_BYTES_PER_SECOND, MAX_BYTES_PER_SECOND,
            "GATE_CORPUS_UPLOAD_BYTES_PER_SECOND",
        ),
    )


class CloudflareCorpusSender:
    """POST one artefact envelope through the controller's existing Access client."""

    def __init__(self, client, controller_id: str):
        self.client = client
        self._controller_id = controller_id

    def __call__(self, artefact_id: str, chunks) -> None:
        acknowledgement = self.client.post_stream(
            "/api/controller/corpus",
            chunks,
            content_type="application/json",
            headers={"Idempotency-Key": _idempotency_key(self._controller_id, artefact_id)},
            max_response_bytes=4096,
            timeout=(2, UPLOAD_READ_TIMEOUT_SECONDS),
        )
        if not _is_corpus_acknowledgement(acknowledgement, artefact_id):
            raise CorpusUploadError("corpus endpoint did not confirm the artefact")


class PacedBody:
    """The request body, metered and abandonable.

    Yielding the envelope in chunks does two jobs at once: it holds the
    average rate to a fraction of the uplink, and it gives the transfer a
    place to notice, every few kilobytes, that a vehicle has arrived. Raising
    from inside the generator tears the request down there and then instead of
    letting it run to completion.
    """

    def __init__(self, payload: bytes, *, gate, epoch: int, bytes_per_second: int,
                 chunk_bytes: int = CHUNK_BYTES, clock=monotonic, sleep=_sleep,
                 stop_event: Event | None = None):
        self._payload = payload
        self._gate = gate
        self._epoch = epoch
        self._rate = max(1, int(bytes_per_second))
        self._chunk = max(1, int(chunk_bytes))
        self._clock = clock
        self._sleep = sleep
        self._stop_event = stop_event
        self.sent = 0

    def __len__(self) -> int:
        return len(self._payload)

    def __iter__(self):
        started = self._clock()
        for offset in range(0, len(self._payload), self._chunk):
            if self._gate.disturbed_since(self._epoch):
                raise CorpusUploadAborted("a gate event started")
            if self._stop_event is not None and self._stop_event.is_set():
                raise CorpusUploadAborted("the controller is stopping")
            chunk = self._payload[offset:offset + self._chunk]
            yield chunk
            self.sent += len(chunk)
            self._pace(started)

    def _pace(self, started: float) -> None:
        owed = (self.sent / self._rate) - (self._clock() - started)
        while owed > 0:
            if self._gate.disturbed_since(self._epoch):
                raise CorpusUploadAborted("a gate event started")
            if self._stop_event is not None and self._stop_event.is_set():
                raise CorpusUploadAborted("the controller is stopping")
            self._sleep(min(owed, MAX_PACING_SLEEP_SECONDS))
            owed -= MAX_PACING_SLEEP_SECONDS


class CorpusUploadWorker:
    """Move the corpus off the card, one artefact at a time, when nothing else wants the link."""

    def __init__(self, corpus, send, gate=NULL_GATE, *,
                 config: CorpusUploadConfig | None = None,
                 controller_id: str = "primary",
                 clock=None, monotonic_clock=monotonic, sleep=_sleep,
                 jitter=None):
        self._corpus = corpus
        self._send = send
        self._gate = gate
        self._config = config or CorpusUploadConfig(enabled=True)
        self._controller_id = controller_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self._failures = 0
        self._retry_at: float | None = None
        self._uploaded = 0
        self._aborted = 0
        self._unshippable = 0
        self._deferrals = 0
        self._last_blocked: str | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._pending = 0
        self._oldest_pending_at: str | None = None

    @property
    def config(self) -> CorpusUploadConfig:
        return self._config

    def run_once(self) -> int:
        """One pass. Returns the number of artefacts the cloud confirmed."""
        try:
            return self._run_once()
        except Exception:
            # A corpus upload must never raise into anything. The worker loop
            # below is shared with the gate's own background threads.
            LOGGER.warning("gate_corpus stage=pass_failed", exc_info=True)
            return 0

    def _run_once(self) -> int:
        artefacts = self._corpus.pending(limit=self._config.batch)
        self._observe(artefacts)
        now = self._monotonic()
        if self._retry_at is not None and now < self._retry_at:
            return 0
        if not artefacts:
            return 0
        uploaded = 0
        for artefact in artefacts:
            blocked = self._gate.blocked_by()
            if blocked is not None:
                self._defer(blocked)
                break
            try:
                self._upload(artefact)
            except CorpusUploadAborted as error:
                self._aborted += 1
                LOGGER.info(
                    "gate_corpus stage=aborted stem=%s reason=%s pending=%d",
                    artefact.stem, _reason(error), self._pending,
                )
                break
            except CorpusUploadUnshippable as error:
                # Retrying will not help, and deleting it would be the very
                # loss this pipeline exists to prevent. Leave it on the card,
                # count it where the heartbeat can see it, and move on.
                self._unshippable += 1
                LOGGER.warning(
                    "gate_corpus stage=unshippable stem=%s detail=%s unshippable=%d",
                    artefact.stem, _reason(error), self._unshippable,
                )
                continue
            except Exception as error:
                self._fail(artefact, error)
                break
            uploaded += 1
        if uploaded:
            self._observe(self._corpus.pending(limit=self._config.batch))
        return uploaded

    def run_forever(self, stop_event: Event) -> None:
        if not self._config.enabled:
            stop_event.wait()
            return
        self._stop_event = stop_event
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self._config.poll_interval)

    def status(self) -> dict:
        """What the heartbeat needs to see a buffer that is not draining."""
        return {
            "enabled": self._config.enabled,
            "pending": self._pending,
            "oldest_pending_at": self._oldest_pending_at,
            "oldest_pending_age_s": _age_seconds(self._oldest_pending_at, self._clock()),
            "uploaded": self._uploaded,
            "aborted": self._aborted,
            "deferrals": self._deferrals,
            "unshippable": self._unshippable,
            "consecutive_failures": self._failures,
            "last_success_at": (
                self._last_success_at.isoformat() if self._last_success_at else None
            ),
            "last_blocked_by": self._last_blocked,
            "last_error": self._last_error,
            "quiet_window_seconds": self._config.quiet_seconds,
            "bytes_per_second": self._config.bytes_per_second,
        }

    # -- internals --------------------------------------------------------
    _stop_event: Event | None = None

    def _upload(self, artefact) -> None:
        document, payload_bytes = self._envelope(artefact)
        artefact_id = document["artefact_id"]
        envelope = json.dumps(document, separators=(",", ":")).encode("utf-8")
        epoch = self._gate.epoch()
        # One last look before a single byte goes out: the gate may have
        # become busy while the payload was being read and encoded.
        blocked = self._gate.blocked_by()
        if blocked is not None:
            raise CorpusUploadAborted(blocked)
        body = PacedBody(
            envelope, gate=self._gate, epoch=epoch,
            bytes_per_second=self._config.bytes_per_second,
            clock=self._monotonic, sleep=self._sleep,
            stop_event=self._stop_event,
        )
        started = self._monotonic()
        try:
            self._send(artefact_id, body)
        except CorpusUploadAborted:
            raise
        except Exception as error:
            # `requests` wraps an exception raised from the body generator, so
            # an abandoned transfer can arrive here disguised as a transport
            # failure. It is a stand-down, not a failure, and must not be
            # charged to the backoff.
            if _caused_by_abort(error):
                raise CorpusUploadAborted("a gate event started") from error
            raise
        self._corpus.discard(artefact.stem)
        self._uploaded += 1
        self._failures = 0
        self._retry_at = None
        self._last_error = None
        self._last_success_at = self._clock()
        LOGGER.info(
            "gate_corpus stage=uploaded artefact_id=%s kind=%s bytes=%d "
            "elapsed_ms=%d pending=%d oldest_pending_age_s=%s",
            artefact_id, document["kind"], len(payload_bytes),
            max(0, round((self._monotonic() - started) * 1000)),
            max(0, self._pending - 1),
            _age_seconds(self._oldest_pending_at, self._clock()),
        )

    def _envelope(self, artefact) -> tuple[dict, bytes]:
        sidecar = _read_sidecar(artefact.sidecar_path)
        payload = _read_payload(artefact.payload_path)
        kind, media_type = _artefact_type(artefact.payload_path, sidecar)
        digest = hashlib.sha256(payload).hexdigest()
        ocr = sidecar.get("ocr") if isinstance(sidecar.get("ocr"), dict) else {}
        extra = sidecar.get("extra") if isinstance(sidecar.get("extra"), dict) else {}
        document = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "controller_id": self._controller_id,
            "artefact_id": digest,
            "kind": kind,
            "media_type": media_type,
            "captured_at": _captured_at(sidecar, artefact),
            "bytes": len(payload),
            "sha256": digest,
            "source": _token(sidecar.get("source")) or "unknown",
            "decision": _decision(extra),
            "plate": _plate(ocr),
            "score": _score(ocr),
            "sidecar": sidecar,
            "data_base64": base64.b64encode(payload).decode("ascii"),
        }
        return document, payload

    def _observe(self, artefacts) -> None:
        """Remember the queue depth and its head, for the heartbeat."""
        self._pending = len(artefacts)
        self._oldest_pending_at = (
            _stem_timestamp(artefacts[0].stem) if artefacts else None
        )

    def _defer(self, reason: str) -> None:
        self._deferrals += 1
        self._last_blocked = reason
        LOGGER.info(
            "gate_corpus stage=deferred reason=%s pending=%d oldest_pending_age_s=%s",
            reason, self._pending,
            _age_seconds(self._oldest_pending_at, self._clock()),
        )

    def _fail(self, artefact, error: Exception) -> None:
        self._failures += 1
        self._last_error = _reason(error)
        seconds = min(
            BACKOFF_MAX_SECONDS,
            BACKOFF_BASE_SECONDS * (2 ** min(self._failures - 1, 16)),
        )
        jitter = self._jitter()
        if not isinstance(jitter, (int, float)) or not 0.5 <= jitter <= 1.5:
            jitter = 1.0
        delay = min(BACKOFF_MAX_SECONDS, seconds * jitter)
        self._retry_at = self._monotonic() + delay
        LOGGER.warning(
            "gate_corpus stage=upload_failed stem=%s error_type=%s detail=%s "
            "retry_in_s=%d pending=%d consecutive_failures=%d",
            artefact.stem, type(error).__name__,
            _reason(error) if isinstance(error, CorpusUploadError) else "unavailable",
            round(delay), self._pending, self._failures,
        )


def _read_sidecar(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            raise CorpusUploadUnshippable("sidecar exceeds the size limit")
        document = json.loads(path.read_text(encoding="utf-8"))
    except CorpusUploadUnshippable:
        raise
    except (OSError, ValueError) as error:
        raise CorpusUploadUnshippable("sidecar is unreadable") from error
    if not isinstance(document, dict):
        raise CorpusUploadUnshippable("sidecar is not an object")
    return document


def _read_payload(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_PAYLOAD_BYTES:
            raise CorpusUploadUnshippable("payload exceeds the size limit")
        if size == 0:
            raise CorpusUploadUnshippable("payload is empty")
        return path.read_bytes()
    except CorpusUploadUnshippable:
        raise
    except OSError as error:
        raise CorpusUploadUnshippable("payload is unreadable") from error


def _artefact_type(path: Path, sidecar: dict) -> tuple[str, str]:
    """What this artefact is: the sidecar's word first, then the suffix.

    Version 1 sidecars have no ``artefact`` block; they are the frames already
    on the card, and the suffix says so without guessing.
    """
    block = sidecar.get("artefact")
    if isinstance(block, dict):
        kind = _token(block.get("kind"))
        media_type = _media_type(block.get("media_type"))
        if kind and media_type:
            return kind, media_type
    known = MEDIA_TYPES.get(path.suffix.lower())
    if known is None:
        raise CorpusUploadUnshippable(f"unknown artefact suffix {path.suffix!r}")
    return known


def _captured_at(sidecar: dict, artefact) -> str:
    value = sidecar.get("captured_at")
    if isinstance(value, str) and value:
        return value[:64]
    stamped = _stem_timestamp(artefact.stem)
    if stamped is None:
        raise CorpusUploadUnshippable("artefact has no capture time")
    return stamped


def _stem_timestamp(stem: str) -> str | None:
    """The ISO time the stem was named for: ``20260907T101112123456Z-<digest>``."""
    head = str(stem).split("-", 1)[0]
    try:
        parsed = datetime.strptime(head, "%Y%m%dT%H%M%S%fZ")
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _age_seconds(timestamp: str | None, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        observed_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return round(
        max(0.0, (now.astimezone(timezone.utc) - observed_at).total_seconds()), 1
    )


def _decision(extra: dict) -> str:
    """What the reader saw for this frame, in the reader's own terms.

    Deliberately not the gate's verdict: the corpus is written inside the OCR
    client, before the processor decides, and a column that claimed otherwise
    would be a lie in the one place a training set cannot afford one. The
    gate's verdict lives in ``gate_events``.
    """
    authorised = extra.get("authorised")
    if authorised is True:
        return "authorised"
    if authorised is False:
        return "unauthorised"
    return "unknown"


def _plate(ocr: dict) -> str | None:
    plate = ocr.get("plate")
    if not isinstance(plate, str):
        return None
    kept = "".join(
        character for character in plate.strip().upper()
        if character.isalnum() or character in " -"
    )
    return kept[:32] or None


def _score(ocr: dict):
    score = ocr.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if score != score or score in (float("inf"), float("-inf")):
        return None
    return round(float(score), 4) if 0 <= score <= 1 else None


def _token(value) -> str | None:
    if not isinstance(value, str):
        return None
    kept = "".join(
        character for character in value.strip()
        if character.isalnum() or character in "_-."
    )
    return kept[:32] or None


def _media_type(value) -> str | None:
    if not isinstance(value, str):
        return None
    kept = "".join(
        character for character in value.strip().lower()
        if character.isalnum() or character in "/+-."
    )
    return kept[:64] if "/" in kept else None


def _reason(error: Exception) -> str:
    return " ".join(str(error).split())[:120] or type(error).__name__


def _caused_by_abort(error: BaseException) -> bool:
    seen = 0
    cause: BaseException | None = error
    while cause is not None and seen < 8:
        if isinstance(cause, CorpusUploadAborted):
            return True
        cause = cause.__cause__ or cause.__context__
        seen += 1
    return False


def _idempotency_key(controller_id: str, artefact_id: str) -> str:
    return hashlib.sha256(
        f"{controller_id}:corpus:{artefact_id}".encode("utf-8")
    ).hexdigest()


def _is_corpus_acknowledgement(value: object, artefact_id: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("artefactId") == artefact_id
        and isinstance(value.get("stored"), bool)
    )


def _bounded(value, minimum: float, maximum: float, name: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number") from error
    if seconds != seconds or not minimum <= seconds <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return seconds


def _bounded_integer(value, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "CloudflareCorpusSender",
    "CorpusUploadAborted",
    "CorpusUploadConfig",
    "CorpusUploadError",
    "CorpusUploadUnshippable",
    "CorpusUploadWorker",
    "FRAME_KIND",
    "FRAME_MEDIA_TYPE",
    "PacedBody",
    "load_corpus_upload_config",
]
