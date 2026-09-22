"""Ship the training corpus to Cloudflare, in the gaps between gate events.

The corpus exists on exactly one SD card, in a warm cabinet, with no copy
anywhere. At the size cap the oldest examples are deleted permanently, and
cards in warm Pis fail. Both losses are silent and neither is recoverable, so
the corpus belongs in R2 with its index in D1.

What makes this safe is not the size of the transfer -- a day of frames is
about 5.6 MB, roughly ten seconds of a 4.5 Mbit/s uplink, and the gate's audio
clips add a few hundred kilobytes each -- but never spending those seconds at
the wrong moment. The rules, in the order they are checked:

1. **Nothing while the gate is working.** A camera event, a presence session
   or an OCR request in flight blocks a start outright.
2. **A quiet period first.** The link must have been idle for
   ``GATE_CORPUS_QUIET_SECONDS`` (60 s by default). The gaps inside a presence
   session -- the spacing between frames, the wait for a verdict -- are far
   shorter than that, so a session is never mistaken for quiet.
3. **Real events first.** A non-empty outbox blocks the corpus while that
   outbox is being *delivered*. An owner waiting for an evidence image
   outranks a training frame, and the queue is already p90 84 s deep.
   Delivery is what the rule protects, so a queue that cannot be delivered
   at all does not hold the corpus for ever: when every pending item has
   failed before anything answered, and that has been so for longer than
   ``GATE_CORPUS_OUTAGE_SECONDS`` (or the net probe's last look at the
   internet failed), the network is down for everyone and yielding achieves
   nothing. The corpus then makes one small attempt of its own every
   ``GATE_CORPUS_OUTAGE_PROBE_SECONDS``, oldest artefact first, so a link
   that is merely intermittent still ships the audio that would otherwise
   be pruned unheard -- and it stands down the instant the outbox actually
   puts something on the wire. On 2026-09-22 the farm router dropped
   30-70 % of packets for a day; seven telemetry items sat on attempt 18
   and 51 audio segments waited behind them for the 48-hour horizon.
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

from .backpressure import (
    BLOCKED_EVENT_DELIVERY, BLOCKED_UNKNOWN_QUEUE, NULL_GATE, bounded_quiet_seconds,
)
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

#: ``T``: how long the outbox must have been failing before anything
#: answered -- every pending item, by the outbox worker's own clock -- before
#: the corpus concludes the network is down for everyone rather than merely
#: busy. Delivery lag is p99 600 s, so fifteen minutes is longer than any
#: delivery legitimately takes and three of the outbox's 300 s retry ceilings.
DEFAULT_OUTAGE_SECONDS = 900.0
MIN_OUTAGE_SECONDS = 60.0
MAX_OUTAGE_SECONDS = 86400.0
#: How often the corpus makes its own attempt once the network is down for
#: everyone: one artefact, at the usual byte rate, then nothing until this
#: much later. Slower than the outbox's retry, so the outbox always sees an
#: intermittent link first.
DEFAULT_OUTAGE_PROBE_SECONDS = 600.0
MIN_OUTAGE_PROBE_SECONDS = 60.0
MAX_OUTAGE_PROBE_SECONDS = 86400.0
#: A net probe reading older than this says nothing about the link now.
NET_PROBE_MAX_AGE_SECONDS = 1800.0

#: The reason tokens this module adds to the ladder in ``backpressure``.
#: ``network_down`` is a deferral: the outbox is stuck on the link and the
#: corpus is waiting for its next probe. ``network_down_probe`` is an
#: attempt made under that rule.
BLOCKED_NETWORK_DOWN = "network_down"
ATTEMPT_NETWORK_DOWN_PROBE = "network_down_probe"
#: The stand-down reason when the outbox puts something on the wire while
#: a corpus transfer is running.
ABORT_OUTBOX_SENDING = "outbox_send_started"

#: What each payload suffix is, and which artefact family it belongs to. This
#: table is what a sidecar without an ``artefact`` block falls back to, and for
#: the gate's audio clips it is the only answer there is: their sidecar names
#: its own ``kind`` -- ``gate_audio``, what the capture is -- and not the
#: artefact family the corpus ships under.
#:
#: ``.aac`` is what ``audio_capture`` actually writes -- ADTS frames copied
#: from the camera's own stream -- and its absence here is why three days of
#: clips were refused as an unknown suffix and never left the card.
MEDIA_TYPES = {
    ".jpg": ("frame", "image/jpeg"),
    ".jpeg": ("frame", "image/jpeg"),
    ".png": ("frame", "image/png"),
    ".aac": ("audio", "audio/aac"),
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
    outage_seconds: float = DEFAULT_OUTAGE_SECONDS
    outage_probe_seconds: float = DEFAULT_OUTAGE_PROBE_SECONDS


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
        outage_seconds=_bounded(
            environment.get("GATE_CORPUS_OUTAGE_SECONDS", DEFAULT_OUTAGE_SECONDS),
            MIN_OUTAGE_SECONDS, MAX_OUTAGE_SECONDS, "GATE_CORPUS_OUTAGE_SECONDS",
        ),
        outage_probe_seconds=_bounded(
            environment.get(
                "GATE_CORPUS_OUTAGE_PROBE_SECONDS", DEFAULT_OUTAGE_PROBE_SECONDS
            ),
            MIN_OUTAGE_PROBE_SECONDS, MAX_OUTAGE_PROBE_SECONDS,
            "GATE_CORPUS_OUTAGE_PROBE_SECONDS",
        ),
    )


class CloudflareCorpusSender:
    """POST one artefact envelope through the controller's existing Access client."""

    def __init__(self, client, controller_id: str):
        self.client = client
        self._controller_id = controller_id

    def __call__(self, artefact_id: str, chunks) -> None:
        try:
            acknowledgement = self.client.post_stream(
                "/api/controller/corpus",
                chunks,
                content_type="application/json",
                headers={"Idempotency-Key": _idempotency_key(self._controller_id, artefact_id)},
                max_response_bytes=4096,
                timeout=(2, UPLOAD_READ_TIMEOUT_SECONDS),
            )
        except Exception as error:
            if _caused_by_abort(error):
                raise
            status = _refusal_status(error)
            if status is None:
                raise
            # The contract refused this artefact on its merits, and it will
            # refuse the identical bytes on every future pass. Charging that to
            # the backoff would park the whole corpus behind one artefact --
            # the oldest is offered first, so a single refusal at the head of
            # the queue would stop the frames behind it shipping at all.
            raise CorpusUploadUnshippable(
                f"the corpus endpoint refused the artefact with HTTP {status}"
            ) from error
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
                 stop_event: Event | None = None, interrupt=None):
        self._payload = payload
        self._gate = gate
        self._epoch = epoch
        self._rate = max(1, int(bytes_per_second))
        self._chunk = max(1, int(chunk_bytes))
        self._clock = clock
        self._sleep = sleep
        self._stop_event = stop_event
        #: Asked between chunks and between pacing sleeps, like the epoch: a
        #: reason to stand down, or None. It is how a transfer running under
        #: the network-down rule notices the outbox has started a send.
        self._interrupt = interrupt
        self.sent = 0

    def __len__(self) -> int:
        return len(self._payload)

    def __iter__(self):
        started = self._clock()
        for offset in range(0, len(self._payload), self._chunk):
            self._stand_down_if_wanted()
            chunk = self._payload[offset:offset + self._chunk]
            yield chunk
            self.sent += len(chunk)
            self._pace(started)

    def _pace(self, started: float) -> None:
        owed = (self.sent / self._rate) - (self._clock() - started)
        while owed > 0:
            self._stand_down_if_wanted()
            self._sleep(min(owed, MAX_PACING_SLEEP_SECONDS))
            owed -= MAX_PACING_SLEEP_SECONDS

    def _stand_down_if_wanted(self) -> None:
        if self._gate.disturbed_since(self._epoch):
            raise CorpusUploadAborted("a gate event started")
        if self._stop_event is not None and self._stop_event.is_set():
            raise CorpusUploadAborted("the controller is stopping")
        if self._interrupt is not None:
            reason = self._interrupt()
            if reason:
                raise CorpusUploadAborted(str(reason))


class CorpusUploadWorker:
    """Move the corpus off the card, one artefact at a time, when nothing else wants the link."""

    def __init__(self, corpus, send, gate=NULL_GATE, *,
                 config: CorpusUploadConfig | None = None,
                 controller_id: str = "primary",
                 clock=None, monotonic_clock=monotonic, sleep=_sleep,
                 jitter=None, outbox=None, net_probe=None):
        self._corpus = corpus
        self._send = send
        self._gate = gate
        #: The outbox worker, or anything with its ``delivery_health()`` and
        #: ``sending()``. Without one the event-delivery block is absolute,
        #: exactly as it was before the network-down rule existed.
        self._outbox = outbox
        #: The net probe worker, read through ``status()`` and never
        #: required: an absent or stale probe simply cannot shorten ``T``.
        self._net_probe = net_probe
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
        # The network-down rule's own state: when the next probe may start,
        # how many there have been, and what the last attempt was made under.
        self._probe_at: float | None = None
        self._probes = 0
        self._last_attempt_reason: str | None = None
        # Why the corpus is not draining, for the segment pruner. Set by a
        # deferral that can last (event delivery, the network, an unreadable
        # queue) or a failure; cleared by a confirmed upload or an empty queue.
        # A gate event or the quiet window is momentary and leaves it alone.
        self._stalled: str | None = None

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
            self._stalled = None
            return 0
        uploaded = 0
        probing = False
        for artefact in artefacts:
            blocked = self._gate.blocked_by()
            if blocked is not None:
                if self._outage_verdict(blocked) is None:
                    self._defer(blocked)
                    break
                # The outbox is stuck on the link, not busy. One bounded
                # attempt per probe interval; a pass that is already probing
                # carries on while the link keeps answering, oldest first.
                if not probing and not self._begin_probe(now):
                    break
                probing = True
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
                self._fail(
                    artefact, error,
                    delay=self._config.outage_probe_seconds if probing else None,
                    reason=ATTEMPT_NETWORK_DOWN_PROBE if probing else None,
                )
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
            "outage_seconds": self._config.outage_seconds,
            "outage_probes": self._probes,
            "last_attempt_reason": self._last_attempt_reason,
            "retention_hold": self.retention_hold(),
        }

    def retention_hold(self) -> str | None:
        """Why unshipped artefacts should outlive their retention window right now.

        Read by the segment recorder's pruner before it takes a segment that
        still carries its sidecar. The answer is the reason the corpus is not
        draining -- ``event_delivery``, ``network_down``, ``upload_failed`` --
        or None when it is, or when nothing is waiting. Pruning an unshipped
        segment is permanent loss; while the uploader is being held off, the
        card is the only copy and keeping it costs only disk, which the
        pruner's free-space floor still bounds. Never raises.
        """
        if self._pending <= 0:
            return None
        return self._stalled

    # -- internals --------------------------------------------------------
    _stop_event: Event | None = None

    def _outage_verdict(self, blocked: str) -> str | None:
        """``network_down`` when yielding to the outbox would achieve nothing.

        The outbox outranks the corpus because an owner is waiting on it, and
        that holds for as long as the outbox is being *delivered*: something
        is on the wire, an item has not been tried yet, or the cloud is
        answering at all -- a 500 is still an answer. It stops holding when
        every pending item has failed before anything answered and either
        that has been so for longer than ``T`` or the net probe's last look
        at the internet failed. Anything unreadable is a reason to yield,
        not to compete.
        """
        if blocked != BLOCKED_EVENT_DELIVERY:
            return None
        read_health = getattr(self._outbox, "delivery_health", None)
        if not callable(read_health):
            return None
        try:
            health = read_health()
        except Exception:
            return None
        if not isinstance(health, dict) or health.get("sending"):
            return None
        pending = health.get("pending")
        failing = health.get("failing")
        if not _is_count(pending) or not _is_count(failing):
            return None
        if failing <= 0 or failing < pending or health.get("unreachable") is not True:
            return None
        stuck_for = health.get("stuck_for_s")
        if _is_number(stuck_for) and stuck_for >= self._config.outage_seconds:
            return BLOCKED_NETWORK_DOWN
        if self._internet_failed():
            return BLOCKED_NETWORK_DOWN
        return None

    def _internet_failed(self) -> bool:
        """Did the net probe's last full cycle fail to reach the internet, recently?"""
        read_status = getattr(self._net_probe, "status", None)
        if not callable(read_status):
            return False
        try:
            status = read_status()
        except Exception:
            return False
        if not isinstance(status, dict):
            return False
        hops = status.get("hops")
        internet = hops.get("internet") if isinstance(hops, dict) else None
        # The tokens are the probe's own (``HOP_INTERNET``, ``STATE_FAILED``),
        # spelt out here so a controller without the probe still imports.
        if not isinstance(internet, dict) or internet.get("state") != "failed":
            return False
        age = status.get("age_seconds")
        return not _is_number(age) or age <= NET_PROBE_MAX_AGE_SECONDS

    def _outbox_sending(self) -> str | None:
        """The stand-down reason if the outbox has something on the wire."""
        read_sending = getattr(self._outbox, "sending", None)
        if not callable(read_sending):
            return None
        try:
            return ABORT_OUTBOX_SENDING if read_sending() else None
        except Exception:
            return None

    def _begin_probe(self, now: float) -> bool:
        """Start one attempt under the network-down rule, or defer until the next."""
        if self._probe_at is not None and now < self._probe_at:
            self._defer(BLOCKED_NETWORK_DOWN, next_probe_in_s=round(self._probe_at - now))
            return False
        self._probe_at = now + self._config.outage_probe_seconds
        self._probes += 1
        self._last_attempt_reason = ATTEMPT_NETWORK_DOWN_PROBE
        LOGGER.info(
            "gate_corpus stage=attempting reason=%s pending=%d oldest_pending_age_s=%s "
            "probes=%d",
            ATTEMPT_NETWORK_DOWN_PROBE, self._pending,
            _age_seconds(self._oldest_pending_at, self._clock()), self._probes,
        )
        return True

    def _upload(self, artefact) -> None:
        document, payload_bytes = self._envelope(artefact)
        artefact_id = document["artefact_id"]
        envelope = json.dumps(document, separators=(",", ":")).encode("utf-8")
        epoch = self._gate.epoch()
        # One last look before a single byte goes out: the gate may have
        # become busy while the payload was being read and encoded, and the
        # outbox may have started a send. Under the network-down rule the
        # gate still says event_delivery, which is not news; the outbox
        # actually transmitting is.
        blocked = self._gate.blocked_by()
        if blocked is not None and self._outage_verdict(blocked) is None:
            raise CorpusUploadAborted(blocked)
        sending = self._outbox_sending()
        if sending is not None:
            raise CorpusUploadAborted(sending)
        body = PacedBody(
            envelope, gate=self._gate, epoch=epoch,
            bytes_per_second=self._config.bytes_per_second,
            clock=self._monotonic, sleep=self._sleep,
            stop_event=self._stop_event, interrupt=self._outbox_sending,
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
        # The artefact, not its stem: it carries the directory it was found
        # in, and a clip under `audio` is not deleted by a root path.
        self._corpus.discard(artefact)
        self._uploaded += 1
        self._failures = 0
        self._retry_at = None
        self._last_error = None
        self._stalled = None
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

    def _defer(self, reason: str, **fields) -> None:
        self._deferrals += 1
        self._last_blocked = reason
        if reason in (BLOCKED_EVENT_DELIVERY, BLOCKED_NETWORK_DOWN, BLOCKED_UNKNOWN_QUEUE):
            self._stalled = reason
        extra = "".join(f" {key}={value}" for key, value in fields.items())
        LOGGER.info(
            "gate_corpus stage=deferred reason=%s pending=%d oldest_pending_age_s=%s%s",
            reason, self._pending,
            _age_seconds(self._oldest_pending_at, self._clock()), extra,
        )

    def _fail(self, artefact, error: Exception, *, delay: float | None = None,
              reason: str | None = None) -> None:
        """Charge one failure to the backoff.

        With ``delay`` given the retry is fixed rather than exponential: an
        attempt made under the network-down rule was expected to fail, and
        what it must not do is push the next look at the link out to an hour.
        """
        self._failures += 1
        self._last_error = _reason(error)
        self._stalled = "upload_failed"
        if delay is None:
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
            "retry_in_s=%d pending=%d consecutive_failures=%d%s",
            artefact.stem, type(error).__name__,
            _reason(error) if isinstance(error, CorpusUploadError) else "unavailable",
            round(delay), self._pending, self._failures,
            f" reason={reason}" if reason else "",
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


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


#: Refusals that are about the artefact rather than the moment. Everything
#: else -- 401 and 403 while a token is being rotated, 404 before a deploy,
#: 429, any 5xx, a dropped connection -- is the cloud having a bad minute and
#: is retried with the usual backoff.
REFUSAL_STATUSES = frozenset({400, 413, 415, 422})


def _refusal_status(error: BaseException) -> int | None:
    """The HTTP status of a deterministic refusal, or None.

    Read off the exception rather than by catching ``requests.HTTPError``, so
    this module keeps working against any client that reports a status the
    same way.
    """
    seen = 0
    cause: BaseException | None = error
    while cause is not None and seen < 8:
        status = getattr(getattr(cause, "response", None), "status_code", None)
        if isinstance(status, int) and status in REFUSAL_STATUSES:
            return status
        cause = cause.__cause__ or cause.__context__
        seen += 1
    return None


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
    "ABORT_OUTBOX_SENDING",
    "ATTEMPT_NETWORK_DOWN_PROBE",
    "BLOCKED_NETWORK_DOWN",
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
