"""Record the sound of the gate, labelled for free by the relay that moved it.

The controller fires the relay and then assumes the gate moved. Nothing
anywhere confirms it. If the motor failed, the relay stuck or the gate jammed
on a stone, the event log would still read ``activated`` and no part of the
system would know any different. This module collects the evidence that would
close that gap: a short clip of what the gate sounded like around each
actuation, and around each camera event that produced no actuation.

It collects. It does not decide. There is no model here, no inference, no
threshold and no claim that anything is detectable yet -- see the pull request
for what a classifier would take.

Why the gate and not the vehicle, given the same microphone:

* **It labels itself.** The controller knows the exact instant it energised
  the relay, so every actuation is a labelled positive that costs nobody any
  effort. Vehicle audio has no such signal and needs weeks of hand labelling.
* **Gate movement without a preceding relay command means somebody used a
  remote, a keypad, or opened it by hand.** That is the direct explanation for
  passages where the gate opened with no recognition event.
* **It is the easier acoustic problem**: a repeatable mechanism, close to the
  microphone, loud, with a stereotyped envelope -- not a distant vehicle
  competing with wind.

The audio path is already measured and is not re-derived here
(``analysis/local-model-and-audio-2026-09-07.md``). MediaMTX carries an
MPEG-4 Audio track on both the ``camera`` and ``clear`` paths, so nothing new
is pulled from the camera. The track is AAC-LC, 16 kHz, mono, ~65 kbit/s --
about 8 KB per second, so a 40 s clip is roughly 325 KB. It is not a telephony
codec, so the gate motor's low frequencies are inside the passband.

The bounds, all of them load-bearing, because this is a fanless board that has
already been killed once by a sub-agent's 4K decode test:

* **Nothing is ever decoded.** ``-vn`` drops the video stream before any
  packet reaches a decoder and ``-c:a copy`` remuxes the AAC packets
  untouched. No pixel, no PCM sample, no resample, no analysis. The stored
  bytes are the camera's own bitstream, which is what a training corpus wants
  anyway.
* **One capture at a time, ever.** A single owning thread does all spawning,
  so a second request cannot start a second child; it is coalesced and
  counted.
* **One pending request, never a queue.** A capture that cannot start is
  skipped and journalled. Nothing is retried, so no failure can become a loop.
* **The same governor as the network probe** -- skip at 80 C, at a one-minute
  load average of 3.0, or under 300 MB available -- with the reason recorded
  so a gap in the corpus is explicit rather than silent.
* **Bounds at three levels**: ffmpeg's own ``-t``, a wall-clock deadline on
  the read, and a byte cap that stops a runaway stream without ever holding
  it. Then a bounded store on top.
* **Neither call site may block.** ``on_camera_event`` runs on the webhook
  thread and ``note_actuation`` runs on the relay thread *while the relay is
  energised*, between activation and the end of the pulse. Both do nothing but
  take an uncontended lock, write a few fields and set an event.

Off by default. ``GATE_AUDIO_CAPTURE_ENABLED=true`` is a deliberate step.

Retention: clips are kept for 30 days and the store is capped at 256 MiB,
whichever bites first, pruned oldest first. They are recordings of the owner's
own gate on his own premises, held on his own hardware.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import resource
import select
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .host_metrics import read_host_metrics

LOGGER = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"
DEFAULT_SOURCE = "rtsp://127.0.0.1:8554/clear"

# 40 s covers the gate opening, the vehicle passing, and enough of the tail to
# catch the close on a gate that shuts promptly. The ceiling is a hard refusal,
# not a clamp: a configuration asking for ten minutes is a mistake, not a
# preference.
DEFAULT_CLIP_SECONDS = 40.0
MIN_CLIP_SECONDS = 5.0
MAX_CLIP_SECONDS = 120.0

# At the measured 65 kbit/s a 120 s clip is about 976 KB. 2 MiB leaves room for
# a bitrate excursion without ever letting a runaway stream fill memory.
DEFAULT_MAX_CLIP_BYTES = 2 * 1024 * 1024
MIN_MAX_CLIP_BYTES = 64 * 1024
MAX_MAX_CLIP_BYTES = 16 * 1024 * 1024

DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
MIN_MAX_TOTAL_BYTES = 8 * 1024 * 1024

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365

# Two gate cycles cannot overlap in under this, so a burst of camera events for
# one passage produces one clip rather than a series of near-duplicates.
DEFAULT_MIN_INTERVAL_SECONDS = 20.0

# The governor's ceilings, deliberately identical to the network probe's: the
# board idles at 71-74 C and hardware-throttles at 85 C.
DEFAULT_MAX_TEMP_C = 80.0
DEFAULT_MAX_LOAD = 3.0
DEFAULT_MIN_AVAILABLE_BYTES = 300 * 1024 * 1024

# An actuation with no capture in flight is carried to the next capture only if
# one starts within this long. Beyond it the actuation is counted as unmatched
# and dropped, rather than mislabelling a clip of some later, unrelated event.
ACTUATION_CARRY_SECONDS = 5.0

CHILD_ADDRESS_SPACE_BYTES = 128 * 1024 * 1024
# ffmpeg has to connect, read the SDP and start receiving before the -t clock
# means anything. This is the slack on top of the requested duration.
CAPTURE_STARTUP_GRACE_SECONDS = 10.0
MAX_SIDECAR_BYTES = 64 * 1024
SKIPPED_EVENT_TYPES = frozenset({"Heartbeat", "heartbeat"})

# The ADTS frame header every AAC frame in this container starts with: twelve
# sync bits, then a zero MPEG-4 layer field. Anything else is not the stream we
# asked for and must not be stored as if it were.
_ADTS_SYNC = 0xFFF


@dataclass(frozen=True)
class AudioCaptureConfig:
    enabled: bool = False
    source_url: str = DEFAULT_SOURCE
    directory: Path | None = None
    clip_seconds: float = DEFAULT_CLIP_SECONDS
    max_clip_bytes: int = DEFAULT_MAX_CLIP_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    retention_days: int = DEFAULT_RETENTION_DAYS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    max_temp_c: float = DEFAULT_MAX_TEMP_C
    max_load: float = DEFAULT_MAX_LOAD
    min_available_bytes: int = DEFAULT_MIN_AVAILABLE_BYTES


def load_audio_capture_config(environment=None, corpus_directory=None) -> AudioCaptureConfig:
    """Read the capture configuration, refusing anything outside safe bounds.

    Off unless explicitly switched on. When the training corpus is configured
    the clips default to an ``audio`` directory beside it, so one uploader
    walking the corpus root carries both the images and the audio.
    """
    environment = os.environ if environment is None else environment
    enabled = _boolean(environment.get("GATE_AUDIO_CAPTURE_ENABLED", "false"))
    directory = _clip_directory(environment, corpus_directory)
    if enabled and directory is None:
        raise ValueError(
            "GATE_AUDIO_CAPTURE_ENABLED requires GATE_AUDIO_CAPTURE_DIR "
            "or GATE_TRAINING_CORPUS_DIR"
        )
    source = str(environment.get("GATE_AUDIO_CAPTURE_SOURCE", DEFAULT_SOURCE)).strip()
    _validate_loopback_rtsp(source)
    return AudioCaptureConfig(
        enabled=enabled,
        source_url=source,
        directory=directory,
        clip_seconds=_float_setting(
            environment, "GATE_AUDIO_CAPTURE_SECONDS", DEFAULT_CLIP_SECONDS,
            minimum=MIN_CLIP_SECONDS, maximum=MAX_CLIP_SECONDS,
        ),
        max_clip_bytes=_integer_setting(
            environment, "GATE_AUDIO_CAPTURE_MAX_CLIP_BYTES", DEFAULT_MAX_CLIP_BYTES,
            minimum=MIN_MAX_CLIP_BYTES, maximum=MAX_MAX_CLIP_BYTES,
        ),
        max_total_bytes=_integer_setting(
            environment, "GATE_AUDIO_CAPTURE_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES,
            minimum=MIN_MAX_TOTAL_BYTES, maximum=64 * 1024 * 1024 * 1024,
        ),
        retention_days=_integer_setting(
            environment, "GATE_AUDIO_CAPTURE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS,
            minimum=MIN_RETENTION_DAYS, maximum=MAX_RETENTION_DAYS,
        ),
        min_interval_seconds=_float_setting(
            environment, "GATE_AUDIO_CAPTURE_MIN_INTERVAL_SECONDS",
            DEFAULT_MIN_INTERVAL_SECONDS, minimum=0.0, maximum=3600.0,
        ),
        max_temp_c=_float_setting(
            environment, "GATE_AUDIO_CAPTURE_MAX_TEMP_C", DEFAULT_MAX_TEMP_C,
            minimum=40.0, maximum=85.0,
        ),
        max_load=_float_setting(
            environment, "GATE_AUDIO_CAPTURE_MAX_LOAD", DEFAULT_MAX_LOAD,
            minimum=0.5, maximum=16.0,
        ),
    )


class AudioClipStore:
    """A bounded directory of ``<stem>.aac`` plus ``<stem>.json`` pairs.

    Deliberately the same shape, the same stem convention and the same
    permissions as ``TrainingCorpus``: an exporter that walks the corpus root
    pairing a payload with its sidecar carries these without a second
    uploader and without knowing anything about audio. ``kind`` in the sidecar
    is what tells it apart.

    Bounded twice over. Anything older than the retention window goes, then
    the oldest pairs go until the directory is under its byte cap. Writing
    never raises into the capture path.
    """

    def __init__(self, directory: Path, *, max_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                 retention_days: int = DEFAULT_RETENTION_DAYS, clock=None):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < MIN_MAX_TOTAL_BYTES:
            raise ValueError("audio clip store max_bytes must be at least 8 MiB")
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
            raise ValueError("audio clip store retention_days must be at least 1")
        self.directory = Path(directory)
        self._max_bytes = max_bytes
        self._retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._records = 0
        self._failures = 0
        self._pruned = 0
        self._total_bytes: int | None = None

    def record(self, audio: bytes, sidecar: dict) -> Path | None:
        """Write one clip and its sidecar. Returns the clip path, or None."""
        try:
            return self._record(audio, sidecar)
        except Exception:
            self._failures += 1
            LOGGER.warning("gate_audio_capture outcome=store_failed")
            return None

    def _record(self, audio: bytes, sidecar: dict) -> Path:
        if not isinstance(audio, (bytes, bytearray)) or not _is_adts_aac(audio):
            raise ValueError("audio clips must be ADTS AAC bytes")
        directory = self._ensure_directory()
        now = self._clock()
        digest = hashlib.sha256(audio).hexdigest()
        stem = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{digest[:12]}"
        document = dict(sidecar)
        document["audio"] = {
            **document.get("audio", {}),
            "sha256": digest,
            "bytes": len(audio),
        }
        encoded = json.dumps(document, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_SIDECAR_BYTES:
            # A sidecar nothing can read would be worse than a small one. Keep
            # the fields that make the clip a training example and drop the
            # rest rather than dropping the clip.
            document = {
                "schema_version": document.get("schema_version"),
                "kind": document.get("kind"),
                "clip_id": document.get("clip_id"),
                "captured_at": document.get("captured_at"),
                "label": document.get("label"),
                "actuation": document.get("actuation"),
                "audio": document["audio"],
                "truncated": True,
            }
            encoded = json.dumps(document, sort_keys=True).encode("utf-8")
        with self._lock:
            clip_path = _write_private(directory, stem + ".aac", bytes(audio))
            try:
                _write_private(directory, stem + ".json", encoded)
            except Exception:
                clip_path.unlink(missing_ok=True)
                raise
            self._records += 1
            self._account(len(audio) + len(encoded))
            self._prune_locked(directory, now)
        return clip_path

    def status(self) -> dict:
        return {
            "directory": str(self.directory),
            "max_bytes": self._max_bytes,
            "retention_days": self._retention_days,
            "bytes": self._total_bytes,
            "records": self._records,
            "failures": self._failures,
            "pruned": self._pruned,
        }

    # -- internals --------------------------------------------------------

    def _ensure_directory(self) -> Path:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        return self.directory

    def _account(self, added: int) -> None:
        if self._total_bytes is None:
            self._total_bytes = sum(
                entry.stat().st_size for entry in self.directory.iterdir() if entry.is_file()
            )
        else:
            self._total_bytes += added

    def _prune_locked(self, directory: Path, now: datetime) -> None:
        stems = sorted(
            {entry.stem for entry in directory.iterdir()
             if entry.is_file() and entry.suffix in (".aac", ".json")}
        )
        # The stem begins with a UTC timestamp, so lexical order is time order
        # and the retention cut is a string comparison rather than a stat call
        # per file.
        cutoff = (now - timedelta(days=self._retention_days)).strftime("%Y%m%dT%H%M%S%fZ")
        for stem in stems:
            expired = stem < cutoff
            oversized = self._total_bytes is not None and self._total_bytes > self._max_bytes
            if not expired and not oversized:
                break
            self._remove_locked(directory, stem)

    def _remove_locked(self, directory: Path, stem: str) -> None:
        removed = False
        for suffix in (".aac", ".json"):
            path = directory / (stem + suffix)
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            removed = True
            if self._total_bytes is not None:
                self._total_bytes -= size
        if removed:
            self._pruned += 1


class AudioClipRecorder:
    """One thread, one child at a time, and a governor in front of both.

    Both public triggers are non-blocking by construction. They record a
    request under an uncontended lock and set an event; this thread does every
    expensive thing -- the governor read, the spawn, the read, the write.
    That matters most for ``note_actuation``, which runs on the relay thread
    between activation and the end of the pulse, where anything slow would
    lengthen the pulse the gate motor sees.
    """

    def __init__(self, config: AudioCaptureConfig, *, store: AudioClipStore | None = None,
                 popen=subprocess.Popen, clock=time.monotonic,
                 wall_clock=None, host_metrics=read_host_metrics):
        self.config = config
        self._store = store if store is not None else (
            AudioClipStore(
                config.directory, max_bytes=config.max_total_bytes,
                retention_days=config.retention_days,
            ) if config.directory is not None else None
        )
        self._popen = popen
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._host_metrics = host_metrics
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._process = None
        # Exactly one slot. A second request while one is pending or in flight
        # is coalesced into it, never queued behind it.
        self._pending: dict | None = None
        self._in_flight: dict | None = None
        self._pending_actuation: dict | None = None
        self._last_started: float | None = None
        self._counters = {
            "requested": 0, "captured": 0, "coalesced": 0, "skipped": 0,
            "failed": 0, "actuations": 0, "actuations_unmatched": 0,
        }
        self._last_skip_reason: str | None = None
        self._last_capture: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self._store is not None)

    # -- triggers, both non-blocking --------------------------------------

    def on_camera_event(self, event) -> str:
        """Ask for a clip because the camera fired. Returns the outcome.

        Every camera event is a candidate. The ones that go on to actuate the
        relay become labelled positives; the ones that do not are the
        negatives, and among them the passages where the gate moved anyway are
        the remote, keypad and manual openings.
        """
        if not self.enabled:
            return "disabled"
        if getattr(event, "event_type", None) in SKIPPED_EVENT_TYPES:
            return "skipped_type"
        return self._request("camera_event", _camera_event_fields(event))

    def note_actuation(self, *, activated_at=None, source: str | None = None,
                       reason: str | None = None, status: str | None = None,
                       detail: str | None = None, event_id: int | None = None,
                       idempotency_key: str | None = None,
                       observed_plate: str | None = None,
                       authorised_plate: str | None = None) -> str:
        """Stamp the relay actuation onto the clip. Never blocks the relay.

        This is the whole reason the corpus is free: the controller knows the
        instant it energised the relay, so the label arrives with the event and
        nobody has to listen to anything to produce it.

        Called from inside the relay activation, so it does nothing but take an
        uncontended lock and write a dict. Where a clip is already recording --
        the ordinary case, because the camera fires seconds before the
        controller decides -- the actuation lands partway into it and the
        pre-roll comes free. Where none is, one is requested and the clip
        starts at the actuation instead.
        """
        if not self.enabled:
            return "disabled"
        record = {
            "relay_activated_at": _isoformat(activated_at) or _isoformat(self._wall_clock()),
            "monotonic": self._clock(),
            "source": source,
            "reason": reason,
            "status": status,
            "detail": detail,
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "observed_plate": observed_plate,
            "authorised_plate": authorised_plate,
        }
        with self._lock:
            self._counters["actuations"] += 1
            if self._closed:
                return "closed"
            if self._in_flight is not None:
                self._in_flight.setdefault("actuations", []).append(record)
                return "attached"
            if self._pending is not None:
                self._pending.setdefault("actuations", []).append(record)
                return "attached_pending"
            # No camera event brought us here: a remote open through the
            # command server, or a camera event the governor turned away.
            # Start a clip now; it has no pre-roll and the sidecar says so.
            self._pending = {
                "trigger": "actuation",
                "requested_at": self._wall_clock(),
                "requested_monotonic": self._clock(),
                "actuations": [record],
            }
            self._counters["requested"] += 1
        self._wake.set()
        return "requested"

    def note_actuation_outcome(self, *, idempotency_key: str | None = None,
                               status: str | None = None, detail: str | None = None,
                               event_id: int | None = None,
                               reason: str | None = None) -> str:
        """Fill in how the actuation finished, once the coordinator knows.

        Called after the pulse and after the store write, so it is off the
        relay's critical path. It patches the record ``note_actuation`` already
        attached rather than adding a second one, which is why it matches on
        the idempotency key.
        """
        if not self.enabled:
            return "disabled"
        with self._lock:
            for holder in (self._in_flight, self._pending):
                if not holder:
                    continue
                for record in holder.get("actuations") or ():
                    if record.get("idempotency_key") == idempotency_key:
                        record.update(
                            status=status, detail=detail, event_id=event_id,
                            reason=reason or record.get("reason"),
                        )
                        return "updated"
            carried = self._pending_actuation
            if carried is not None and carried.get("idempotency_key") == idempotency_key:
                carried.update(
                    status=status, detail=detail, event_id=event_id,
                    reason=reason or carried.get("reason"),
                )
                return "updated"
        # The clip was already written, or the capture was skipped. The label
        # survives either way; only the terminal detail is missing.
        return "unmatched"

    def _request(self, trigger: str, context: dict | None) -> str:
        with self._lock:
            if self._closed:
                return "closed"
            self._counters["requested"] += 1
            if self._in_flight is not None or self._pending is not None:
                self._counters["coalesced"] += 1
                return "coalesced"
            now = self._clock()
            if (self.config.min_interval_seconds > 0 and self._last_started is not None
                    and now - self._last_started < self.config.min_interval_seconds):
                self._counters["skipped"] += 1
                self._last_skip_reason = "min_interval"
                return "skipped_min_interval"
            self._pending = {
                "trigger": trigger,
                "requested_at": self._wall_clock(),
                "requested_monotonic": now,
                "context": context,
                "actuations": [],
            }
        self._wake.set()
        return "requested"

    # -- the owning thread ------------------------------------------------

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            # A short poll rather than a pure wait, so a stop is noticed
            # promptly and a carried actuation cannot sit forever.
            self._wake.wait(1.0)
            self._wake.clear()
            if stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception:
                LOGGER.warning("gate_audio_capture outcome=failed", exc_info=True)

    def run_once(self) -> bool:
        """Take the pending request, if any, and capture it. One at a time."""
        with self._lock:
            if self._closed or self._in_flight is not None or self._pending is None:
                return False
            request = self._pending
            self._pending = None
            skipped = self._governor_verdict()
            if skipped is not None:
                self._counters["skipped"] += 1
                self._last_skip_reason = skipped
                # Carry an actuation that arrived with this request, so the
                # next clip within a few seconds still gets its label rather
                # than the label being lost with the skipped capture.
                actuations = request.get("actuations") or []
                self._pending_actuation = actuations[-1] if actuations else None
                LOGGER.info("gate_audio_capture outcome=skipped reason=%s", skipped)
                return False
            self._in_flight = request
            now = self._clock()
            self._last_started = now
            self._last_skip_reason = None
            # Attach a carried actuation under the same lock that
            # note_actuation appends under, so the two never race on the list.
            carried = self._pending_actuation
            self._pending_actuation = None
            if carried is not None:
                if now - carried.get("monotonic", 0.0) <= ACTUATION_CARRY_SECONDS:
                    request.setdefault("actuations", []).insert(0, carried)
                else:
                    # Too old to belong to this clip. Counted, not guessed at:
                    # a mislabelled positive is worse than a missing one.
                    self._counters["actuations_unmatched"] += 1
        started_at = self._wall_clock()
        started_monotonic = self._clock()
        try:
            audio, failure = self._capture()
        finally:
            with self._lock:
                self._in_flight = None
        elapsed = self._clock() - started_monotonic
        if failure is not None or not audio:
            with self._lock:
                self._counters["failed"] += 1
                self._last_capture = {"outcome": "failed", "reason": failure}
            LOGGER.warning("gate_audio_capture outcome=failed reason=%s", failure)
            return False
        sidecar = _build_sidecar(
            request, started_at=started_at, elapsed_seconds=elapsed,
            requested_seconds=self.config.clip_seconds,
            retention_days=self.config.retention_days,
            source_url=self.config.source_url,
        )
        path = self._store.record(audio, sidecar)
        if path is None:
            with self._lock:
                self._counters["failed"] += 1
                self._last_capture = {"outcome": "failed", "reason": "store"}
            return False
        with self._lock:
            self._counters["captured"] += 1
            self._last_capture = {
                "outcome": "captured",
                "label": sidecar["label"],
                "bytes": len(audio),
                "seconds": sidecar["audio"]["captured_seconds"],
            }
        LOGGER.info(
            "gate_audio_capture outcome=captured label=%s bytes=%d seconds=%.1f trigger=%s",
            sidecar["label"], len(audio), sidecar["audio"]["captured_seconds"],
            sidecar["trigger"],
        )
        return True

    def status(self) -> dict:
        with self._lock:
            snapshot = {
                "enabled": self.enabled,
                "clip_seconds": self.config.clip_seconds,
                "capturing": self._in_flight is not None,
                "last_skip_reason": self._last_skip_reason,
                "last_capture": dict(self._last_capture) if self._last_capture else None,
                **dict(self._counters),
            }
        if self._store is not None:
            snapshot["store"] = self._store.status()
        return snapshot

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None
            process = self._process
            self._process = None
        self._wake.set()
        if process is not None:
            _terminate(process)

    # -- capture ----------------------------------------------------------

    @property
    def command(self) -> tuple[str, ...]:
        """The exact capture command. Copy only: nothing is ever decoded.

        ``-vn`` drops the video stream before any packet reaches a decoder, so
        the 4K HEVC track this path also carries is never touched. ``-c:a
        copy`` remuxes the AAC packets unchanged -- no decode, no resample, no
        re-encode -- which is both the cheap thing and the right thing, since
        the camera's own bitstream is what the corpus should hold. ``-map
        0:a:0`` is deliberately not optional: a camera with audio switched off
        must fail loudly rather than write an empty clip.

        This is deliberately the same command that was measured on the live
        board on 2026-09-07 -- 30 s, ``-c:a copy``, no video decode, from this
        same loopback path -- for no measurable thermal cost, 69.2 C before and
        69.2 C after. The video packets are still demultiplexed and thrown
        away, and that cost is inside that measurement.

        ``-allowed_media_types audio`` would make the RTSP demuxer set up only
        the audio stream, so the server would never send the ~6 Mbit/s video at
        all. It is very probably a further trim and it is deliberately not used
        here: the cost it would remove has already been measured at zero, and a
        command nobody has run on this hardware is a worse default than one
        that has been.
        """
        return (
            FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.config.source_url,
            "-map", "0:a:0", "-vn", "-c:a", "copy",
            "-t", f"{self.config.clip_seconds:g}",
            "-f", "adts", "pipe:1",
        )

    def _capture(self) -> tuple[bytes, str | None]:
        try:
            process = self._popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"LANG": "C", "LC_ALL": "C"},
                close_fds=True,
                preexec_fn=_limit_child,
            )
        except (OSError, ValueError):
            return b"", "spawn"
        with self._lock:
            # Publish under the lock so close() either sees this child or has
            # already marked us closed, in which case it dies here.
            if self._closed:
                stopping = True
            else:
                stopping = False
                self._process = process
        if stopping:
            _terminate(process)
            return b"", "stopping"
        try:
            audio, failure = self._read_bounded(process)
        finally:
            _terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
        if failure is not None:
            return audio, failure
        if process.returncode != 0:
            return audio, "exit_status"
        if not _is_adts_aac(audio):
            return audio, "not_aac"
        return audio, None

    def _read_bounded(self, process) -> tuple[bytes, str | None]:
        """Read to EOF, never holding more than the cap nor waiting past the
        deadline. ffmpeg's ``-t`` is the first bound; these are the other two,
        because a child that hangs before it ever honours ``-t`` would
        otherwise sit on an RTSP connection forever."""
        deadline = self._clock() + self.config.clip_seconds + CAPTURE_STARTUP_GRACE_SECONDS
        buffer = bytearray()
        try:
            descriptor = process.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            return b"", "no_stdout"
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return bytes(buffer), "timeout"
            ready, _, _ = select.select([descriptor], [], [], min(remaining, 1.0))
            if not ready:
                continue
            # Never ask for more than the remaining capacity plus one byte, so
            # an oversized clip is detected without ever being held.
            capacity = self.config.max_clip_bytes - len(buffer)
            try:
                chunk = os.read(descriptor, min(64 * 1024, capacity + 1))
            except OSError:
                return bytes(buffer), "read_error"
            if not chunk:
                break
            if len(chunk) > capacity:
                return bytes(buffer), "clip_too_large"
            buffer.extend(chunk)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return bytes(buffer), "exit_wait"
        return bytes(buffer), None

    def _governor_verdict(self) -> str | None:
        """Why this capture must not run, or None.

        The same rule as the network probe, for the same reason and with the
        same permissiveness: a metric that could not be read does not block,
        because a capture path that silently stopped forever because a sysfs
        path moved would be worse than one that ran. A metric that *was* read
        and breaches always skips, and the reason is journalled so the hole in
        the corpus is visible.
        """
        try:
            metrics = self._host_metrics()
        except Exception:
            return None
        temperature = metrics.get("soc_temp_c")
        if isinstance(temperature, (int, float)) and temperature >= self.config.max_temp_c:
            return "hot"
        load = metrics.get("load_1m")
        if isinstance(load, (int, float)) and load >= self.config.max_load:
            return "loaded"
        available = metrics.get("mem_available_kib")
        if (isinstance(available, int)
                and available * 1024 < self.config.min_available_bytes):
            return "low_memory"
        return None


def _build_sidecar(request: dict, *, started_at: datetime, elapsed_seconds: float,
                   requested_seconds: float, retention_days: int,
                   source_url: str) -> dict:
    """What this clip is, and what the controller was doing while it recorded.

    ``label`` is the free training label and the only field a first pass needs:
    ``actuated`` means the controller energised the relay during this clip, so
    the gate was commanded to move; ``no_actuation`` means it did not, so any
    gate movement audible here was somebody's remote, keypad or hand.
    """
    actuations = [_actuation_fields(record) for record in (request.get("actuations") or [])]
    requested_monotonic = request.get("requested_monotonic")
    for record, raw in zip(actuations, request.get("actuations") or []):
        monotonic = raw.get("monotonic")
        if isinstance(monotonic, (int, float)) and isinstance(requested_monotonic, (int, float)):
            record["offset_seconds"] = round(monotonic - requested_monotonic, 3)
    return {
        "schema_version": 1,
        "kind": "gate_audio",
        "clip_id": started_at.strftime("%Y%m%dT%H%M%S%fZ"),
        "captured_at": _isoformat(started_at),
        "requested_at": _isoformat(request.get("requested_at")),
        "trigger": request.get("trigger"),
        "source": source_url,
        "label": "actuated" if actuations else "no_actuation",
        "actuation": actuations[0] if actuations else None,
        "actuations": actuations if len(actuations) > 1 else None,
        "camera_event": request.get("context"),
        "audio": {
            # Stated, not measured: the packets were copied, so these are the
            # camera's own stream parameters as characterised on 2026-09-07 and
            # not something this process derived by decoding anything.
            "container": "adts",
            "codec": "aac_lc",
            "sample_rate_hz": 16000,
            "channels": 1,
            "raw_copy": True,
            "decoded": False,
            "requested_seconds": requested_seconds,
            "captured_seconds": round(max(0.0, elapsed_seconds), 2),
        },
        "retention_days": retention_days,
    }


def _actuation_fields(record: dict) -> dict:
    return {
        key: record.get(key)
        for key in ("relay_activated_at", "source", "reason", "status", "detail",
                    "event_id", "idempotency_key", "observed_plate", "authorised_plate")
    }


def _camera_event_fields(event) -> dict | None:
    fields = {}
    for key in ("event_type", "channel", "sequence", "received_at", "source"):
        value = getattr(event, key, None)
        if isinstance(value, datetime):
            value = _isoformat(value)
        if value is None or isinstance(value, (bool, int, float, str)):
            fields[key] = value
    return fields or None


def _isoformat(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _is_adts_aac(data) -> bool:
    """True for a buffer that starts with an ADTS AAC frame header."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 7:
        return False
    header = (data[0] << 4) | (data[1] >> 4)
    return header == _ADTS_SYNC and (data[1] >> 1) & 0x03 == 0


def _limit_child() -> None:  # pragma: no cover - runs in the child
    """Cap the child's address space and drop it to the lowest priority.

    Copying packets needs almost nothing, so the limit is generous enough
    never to bite in normal operation and tight enough that a runaway child
    dies instead of taking the board with it -- which is what happened here on
    2026-09-07 when a decode test was run on the live device.
    """
    resource.setrlimit(
        resource.RLIMIT_AS, (CHILD_ADDRESS_SPACE_BYTES, CHILD_ADDRESS_SPACE_BYTES),
    )
    try:
        os.nice(19)
    except OSError:
        pass


def _terminate(process) -> None:
    """Stop a capture child and reap it, tolerating one that has already gone."""
    try:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    except OSError:
        pass
    stdout = getattr(process, "stdout", None)
    if stdout is not None:
        try:
            stdout.close()
        except OSError:
            pass


def _write_private(directory: Path, name: str, data: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        final = directory / name
        os.replace(temporary, final)
        return final
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _clip_directory(environment, corpus_directory) -> Path | None:
    raw = (environment.get("GATE_AUDIO_CAPTURE_DIR") or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("GATE_AUDIO_CAPTURE_DIR must be an absolute path")
        return path
    if corpus_directory:
        return Path(corpus_directory) / "audio"
    return None


def _validate_loopback_rtsp(value: str) -> None:
    """Loopback only. The audio already arrives at MediaMTX, so a second
    connection to the camera would be both wasteful and a new failure mode on
    a 4.5 Mbit/s uplink."""
    parts = urlsplit(value)
    if parts.scheme != "rtsp" or parts.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("GATE_AUDIO_CAPTURE_SOURCE must be a loopback rtsp:// URL")
    if parts.username or parts.password:
        raise ValueError("GATE_AUDIO_CAPTURE_SOURCE must not embed credentials")


def _boolean(value) -> bool:
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no", ""}:
        return False
    raise ValueError("GATE_AUDIO_CAPTURE_ENABLED must be true or false")


def _float_setting(environment, name, default, *, minimum, maximum) -> float:
    raw = str(environment.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _integer_setting(environment, name, default, *, minimum, maximum) -> int:
    raw = str(environment.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value
