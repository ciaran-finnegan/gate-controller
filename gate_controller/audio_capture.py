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

The pre-roll, and why there is a second child (issue #106):

A clip that starts at the camera event cannot answer the one question issue
#106 asks -- does the microphone hear an approaching vehicle *before* the
camera's vehicle AI fires? On 2026-09-10 the 21:52 clip began at the alarm and
the car had already been on the drive for several seconds. Audio from before
the trigger has to have been recorded before anyone asked for it, so something
has to run continuously. The cheapest honest thing is one more copy-only child:
``AudioPrerollRing`` remuxes the same AAC track into ``-segment_time`` chunks
in a small wrapping ring on tmpfs, and a camera-event clip is assembled as the
last ``GATE_AUDIO_CAPTURE_PREROLL_SECONDS`` of that ring followed by the
ordinary post-trigger capture.

* **The ring decodes nothing either.** Same ``-vn -c:a copy``; the segment
  muxer writes the camera's own ADTS frames to files.
* **Byte concatenation is sound for this format and only this format.** ADTS
  carries a header on every frame and no file-level index or timestamp, so two
  ADTS streams from one source join by ``+``. What is *not* sound is joining
  mid-frame, so the ring's bytes are walked header by header -- reading the
  7-byte headers, never a sample -- and any trailing partial frame is dropped
  before the join.
* **Bounded RAM, no card wear.** ``-segment_wrap`` fixes the number of files
  and the ring lives under ``/dev/shm``; at the measured ~8 KB/s a 20 s pre-roll
  is about 160 KB and the whole ring under 250 KB.
* **One owning thread still owns everything.** The same thread that spawns
  captures supervises the ring child, restarts it with backoff, stops it when
  the governor says so and starts it again when that clears. The ring child is
  never a second *capture*: it produces no clip and takes no capture slot.
* **Off with the pre-roll.** ``GATE_AUDIO_CAPTURE_PREROLL_SECONDS=0`` starts no
  ring child at all, and every clip is exactly what it was before.

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
import re
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

# RLIMIT_AS counts every mapping, including the shared libraries ffmpeg maps
# before it runs a line of its own code -- and on this board those alone are
# well past 128 MiB. Measured on the Pi on 2026-09-08: every capture died 55 ms
# after the session started with `outcome=failed reason=exit_status`, and the
# child's stderr said `error while loading shared libraries: libcodec2.so.1.0:
# failed to map segment from shared object`. The same command under
# `ulimit -v 131072` reproduces it; without the limit it produced 24,393 bytes
# of valid AAC in 3 s. Resident memory was never the problem: a stream copy
# decodes nothing, so RSS stays at 2.5-50 MB. 1 GiB clears the mappings with
# room to spare and is still far below the 3.4 GB runaway ffmpeg that
# OOM-killed the board on 2026-09-07, which is what this limit exists to stop.
# The network probe's children are small `ping` processes and keep their own
# much tighter 64 MiB (net_probe.CHILD_ADDRESS_SPACE_BYTES); this raise applies
# to the audio child only.
DEFAULT_CHILD_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
MIN_CHILD_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
MAX_CHILD_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024

# ffmpeg has to connect, read the SDP and start receiving before the -t clock
# means anything. This is the slack on top of the requested duration.
CAPTURE_STARTUP_GRACE_SECONDS = 10.0
MAX_SIDECAR_BYTES = 64 * 1024

# The child's stderr is kept only as a tail, and only to make a failure
# diagnosable from the journal: `-loglevel error` makes it the one line that
# says why. Bounded on both sides -- what is held while the child runs, and
# what is journalled -- so a child looping on errors can neither grow this nor
# block on a full pipe.
MAX_STDERR_TAIL_BYTES = 2 * 1024
STDERR_TAIL_CHARACTERS = 200

SKIPPED_EVENT_TYPES = frozenset({"Heartbeat", "heartbeat"})

# The ADTS frame header every AAC frame in this container starts with: twelve
# sync bits, then a zero MPEG-4 layer field. Anything else is not the stream we
# asked for and must not be stored as if it were.
_ADTS_SYNC = 0xFFF

# One AAC-LC frame is 1024 samples, so at the camera's 16 kHz a frame is 64 ms
# and the header table below is the whole of what the pre-roll needs to know
# about the bitstream. Reading a header is not decoding: no sample is touched.
AAC_SAMPLES_PER_FRAME = 1024
ADTS_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                     16000, 12000, 11025, 8000, 7350)
# What the stream is characterised at, used only to size a request when a
# header would not say. See the module docstring for where the number is from.
STATED_SAMPLE_RATE_HZ = 16000

# The camera fires when it decides a vehicle is a vehicle. 20 s before that
# covers the approach up the drive on the 2026-09-10 21:52 passage, which is
# the one issue #106 was opened over. 0 disables the ring entirely.
DEFAULT_PREROLL_SECONDS = 20.0
MAX_PREROLL_SECONDS = 120.0

# tmpfs, so no SD-card wear and nothing survives a reboot. Under `PrivateTmp`
# this is the service's own /dev/shm, invisible to anything else on the board,
# and it is writable under `ProtectSystem=strict` where /run would not be.
DEFAULT_RING_DIRECTORY = Path("/dev/shm/gate-audio-ring")

# Short enough that the newest complete segment is never far behind the
# trigger, long enough that the muxer is not opening files constantly.
RING_SEGMENT_SECONDS = 5.0
# One slot for the segment being written and one so the oldest wanted segment
# is not the one being overwritten while it is read.
RING_SPARE_SEGMENTS = 2
MAX_RING_SEGMENTS = 32
RING_SEGMENT_TEMPLATE = "seg%03d.aac"
RING_SEGMENT_PATTERN = re.compile(r"^seg\d{3}\.aac$")
# At ~8 KB/s a 5 s segment is about 40 KB. A file anywhere near this is not
# something this ring wrote, and is skipped rather than read into memory.
MAX_RING_SEGMENT_BYTES = 1024 * 1024

RING_RESTART_BACKOFF_SECONDS = 2.0
RING_MAX_BACKOFF_SECONDS = 60.0
# A child that stayed up this long was working, so its next failure starts the
# backoff again from the bottom rather than inheriting an old ladder.
RING_HEALTHY_SECONDS = 60.0
# The governor is read for the ring on this interval, not on every one-second
# supervisor tick: the point of the governor is to spare the board, so it must
# not itself become a 1 Hz sysfs poll.
RING_GOVERNOR_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class AudioCaptureConfig:
    enabled: bool = False
    source_url: str = DEFAULT_SOURCE
    directory: Path | None = None
    clip_seconds: float = DEFAULT_CLIP_SECONDS
    preroll_seconds: float = DEFAULT_PREROLL_SECONDS
    ring_directory: Path = DEFAULT_RING_DIRECTORY
    max_clip_bytes: int = DEFAULT_MAX_CLIP_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    retention_days: int = DEFAULT_RETENTION_DAYS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    max_temp_c: float = DEFAULT_MAX_TEMP_C
    max_load: float = DEFAULT_MAX_LOAD
    min_available_bytes: int = DEFAULT_MIN_AVAILABLE_BYTES
    child_address_space_bytes: int = DEFAULT_CHILD_ADDRESS_SPACE_BYTES


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
        # 0 is the off switch, not a floor to clamp to: no ring child runs and
        # every clip is exactly what it was before the pre-roll existed.
        preroll_seconds=_float_setting(
            environment, "GATE_AUDIO_CAPTURE_PREROLL_SECONDS", DEFAULT_PREROLL_SECONDS,
            minimum=0.0, maximum=MAX_PREROLL_SECONDS,
        ),
        ring_directory=_ring_directory(environment),
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
        child_address_space_bytes=_integer_setting(
            environment, "GATE_AUDIO_CAPTURE_MAX_ADDRESS_SPACE_BYTES",
            DEFAULT_CHILD_ADDRESS_SPACE_BYTES,
            minimum=MIN_CHILD_ADDRESS_SPACE_BYTES,
            maximum=MAX_CHILD_ADDRESS_SPACE_BYTES,
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
                # Kept with the label because it is half of what makes the clip
                # alignable: where the clip starts, and (inside `audio`) where
                # the trigger sits in it.
                "clip_started_at": document.get("clip_started_at"),
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


class AudioPrerollRing:
    """The recent past, kept in RAM by one copy-only child, for issue #106.

    A camera-event clip that starts at the camera event cannot say whether the
    microphone heard the vehicle first. This holds the last
    ``preroll_seconds`` of the same AAC track in a wrapping ring of small
    segment files on tmpfs, so a clip can be assembled as *approach, then
    trigger, then the ordinary capture* at no labelling cost and with nothing
    decoded anywhere.

    It is driven entirely by ``AudioClipRecorder``'s single owning thread --
    ``supervise`` once a tick, ``harvest`` at the top of a capture -- so it
    adds no thread and takes no capture slot. Only ``stop`` and ``status`` are
    reached from elsewhere, which is what the lock is for.
    """

    def __init__(self, directory, *, seconds: float, source_url: str,
                 child_address_space_bytes: int = DEFAULT_CHILD_ADDRESS_SPACE_BYTES,
                 segment_seconds: float = RING_SEGMENT_SECONDS,
                 popen=subprocess.Popen, clock=time.monotonic):
        self.directory = Path(directory)
        self.seconds = max(0.0, float(seconds))
        self.segment_seconds = segment_seconds
        # Enough slots for the pre-roll, plus the one being written and one
        # spare. -segment_wrap is what bounds the RAM: at ~8 KB/s the whole
        # ring is about 40 KB a slot however long the child runs.
        self.segments = min(
            MAX_RING_SEGMENTS,
            int(-(-self.seconds // segment_seconds)) + RING_SPARE_SEGMENTS,
        ) if self.seconds > 0 else 0
        self._source_url = source_url
        self._child_address_space_bytes = child_address_space_bytes
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._process = None
        self._started_at: float | None = None
        self._ever_started = False
        self._closed = False
        self._retry_at: float | None = None
        self._backoff_seconds = RING_RESTART_BACKOFF_SECONDS
        self._starts = 0
        self._failures = 0
        self._stopped_reason: str | None = None
        self._errors = bytearray()
        self._last_stderr: str | None = None

    @property
    def enabled(self) -> bool:
        return self.seconds > 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None

    @property
    def command(self) -> tuple[str, ...]:
        """The ring command. Copy only, exactly like the capture command.

        ``-vn`` and ``-c:a copy`` are the same two promises the capture makes:
        the 4K video is dropped before any packet reaches a decoder and the AAC
        packets are remuxed untouched. The segment muxer then writes those same
        packets into ``seg000.aac`` ... and wraps, so the files hold the
        camera's own bitstream and nothing on this board ever looks at a sample.

        ``-allowed_media_types audio`` would stop the server sending the video
        at all and is very probably a further trim for a child that runs all
        day. It is deliberately not used, for the same reason the capture does
        not use it: nobody has run it on this hardware, and an unmeasured flag
        is a worse default than a measured one.
        """
        return (
            FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self._source_url,
            "-map", "0:a:0", "-vn", "-c:a", "copy",
            "-f", "segment",
            "-segment_time", f"{self.segment_seconds:g}",
            "-segment_format", "adts",
            "-segment_wrap", str(self.segments),
            "-reset_timestamps", "1",
            str(self.directory / RING_SEGMENT_TEMPLATE),
        )

    # -- supervision, on the recorder's owning thread ----------------------

    def supervise(self, *, blocked: str | None = None) -> None:
        """Keep exactly one ring child alive, or none. Never raises."""
        try:
            self._supervise(blocked)
        except Exception:
            LOGGER.warning("gate_audio_ring outcome=failed", exc_info=True)

    def _supervise(self, blocked: str | None) -> None:
        if self._closed or not self.enabled or blocked is not None:
            reason = blocked or ("closed" if self._closed else "disabled")
            # Only when there is something to stop or the reason has changed:
            # a governor holding the ring down for an hour must not mean an
            # hour of directory listings.
            if self.running or self._stopped_reason != reason:
                self.stop(reason)
            return
        with self._lock:
            process = self._process
        if process is not None:
            # Read whatever the child has said, without ever waiting on it: a
            # child nobody reads would eventually block on a full stderr pipe.
            _drain_errors(_error_descriptor(process), self._errors)
            if process.poll() is None:
                return
            self._reap(process)
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return
        self._stopped_reason = None
        self._start(now)

    def _start(self, now: float) -> None:
        try:
            self._prepare_directory()
            process = self._popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env={"LANG": "C", "LC_ALL": "C"},
                close_fds=True,
                preexec_fn=_child_limiter(self._child_address_space_bytes),
            )
        except (OSError, ValueError):
            self._failures += 1
            self._defer(now)
            LOGGER.warning("gate_audio_ring outcome=failed reason=spawn")
            return
        with self._lock:
            # Published under the lock so a concurrent close() either sees this
            # child or has already marked us closed, in which case it dies here.
            stopping = self._closed
            if not stopping:
                self._process = process
                self._started_at = now
                self._ever_started = True
                self._starts += 1
        if stopping:
            _terminate(process)
            return
        self._errors = bytearray()
        LOGGER.info(
            "gate_audio_ring outcome=started seconds=%g segments=%d directory=%s",
            self.seconds, self.segments, self.directory,
        )

    def _reap(self, process) -> None:
        """Account for a ring child that has exited and set the next retry."""
        started_at = self._started_at
        now = self._clock()
        _drain_errors(_error_descriptor(process), self._errors)
        self._last_stderr = _stderr_tail(self._errors) or None
        _terminate(process)
        with self._lock:
            if self._process is process:
                self._process = None
            self._started_at = None
        self._failures += 1
        self._stopped_reason = "exited"
        # A child that stayed up is not a child that is failing to start, so
        # its first retry is the short one however long the last ladder got.
        healthy = started_at is not None and now - started_at >= RING_HEALTHY_SECONDS
        self._defer(now, reset=healthy)
        LOGGER.warning(
            "gate_audio_ring outcome=exited stderr=%s", self._last_stderr or "-",
        )

    def _defer(self, now: float, *, reset: bool = False) -> None:
        if reset:
            self._backoff_seconds = RING_RESTART_BACKOFF_SECONDS
        self._retry_at = now + self._backoff_seconds
        self._backoff_seconds = min(
            RING_MAX_BACKOFF_SECONDS, self._backoff_seconds * 2,
        )

    def stop(self, reason: str | None = None) -> None:
        """Stop the child and empty the ring. Safe from any thread, and idempotent."""
        with self._lock:
            process = self._process
            self._process = None
            self._started_at = None
            started = self._ever_started
            if reason is not None:
                self._stopped_reason = reason
        if process is not None:
            _terminate(process)
            LOGGER.info("gate_audio_ring outcome=stopped reason=%s", reason or "-")
        if started:
            # Nothing here may be spliced into a later clip, so the segments go
            # with the child rather than sitting in RAM until it comes back.
            self._clean()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop("closed")

    # -- the pre-roll ------------------------------------------------------

    def harvest(self, *, seconds: float, now: float,
                max_bytes: int) -> tuple[bytes, float, int]:
        """The last ``seconds`` of ring audio as whole ADTS frames.

        Returns the bytes, how much audio they actually are, and how many
        segments went into them. Empty whenever there is any doubt: no child,
        nothing fresh enough to belong to this event, or a file that is not the
        stream this ring writes. A clip with a short pre-roll is a fact the
        sidecar can state; a clip with somebody else's audio welded on the
        front is a corrupted training example.
        """
        try:
            return self._harvest(seconds, now, max_bytes)
        except Exception:
            LOGGER.warning("gate_audio_ring outcome=harvest_failed", exc_info=True)
            return b"", 0.0, 0

    def _harvest(self, seconds: float, now: float,
                 max_bytes: int) -> tuple[bytes, float, int]:
        if not self.enabled or seconds <= 0 or max_bytes <= 0 or not self.running:
            return b"", 0.0, 0
        chunks: list[bytes] = []
        samples = 0
        wanted = int(seconds * STATED_SAMPLE_RATE_HZ)
        rate = None
        budget = max_bytes
        for path in self._segments_newest_first(now, seconds):
            try:
                data = path.read_bytes()
            except OSError:
                break
            chunk, taken, found = _adts_tail(
                data, samples=wanted - samples, max_bytes=budget,
            )
            if rate is None and found:
                # The header's own rate, so the pre-roll's length is measured
                # rather than assumed. Re-scale what is still wanted for it.
                rate = found
                wanted = int(seconds * rate)
                chunk, taken, _ = _adts_tail(
                    data, samples=wanted - samples, max_bytes=budget,
                )
            if not chunk:
                # Stop rather than skip past it: the pre-roll is only alignable
                # because it runs unbroken up to the capture, so a gap in the
                # middle would make trigger_offset_seconds a lie.
                break
            chunks.append(chunk)
            samples += taken
            budget -= len(chunk)
            if samples >= wanted or budget <= 0:
                break
        if not chunks:
            return b"", 0.0, 0
        audio = b"".join(reversed(chunks))
        return audio, samples / float(rate or STATED_SAMPLE_RATE_HZ), len(chunks)

    def _segments_newest_first(self, now: float, seconds: float) -> list[Path]:
        """The ring's segment files, newest first, none of them stale.

        ``-segment_wrap`` cycles the file names, so the name order is not the
        time order and the modification time is. The staleness cut is what
        stops a ring that was stopped -- by the governor, by a restart, by a
        camera that dropped its audio track -- from contributing the last
        minutes it happened to hold to an event it has nothing to do with.
        """
        cutoff = now - (seconds + self.segment_seconds * 2)
        entries = []
        try:
            listing = list(self.directory.iterdir())
        except OSError:
            return []
        for entry in listing:
            if not RING_SEGMENT_PATTERN.match(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            if stat.st_size > MAX_RING_SEGMENT_BYTES or stat.st_mtime < cutoff:
                continue
            entries.append((stat.st_mtime, entry))
        entries.sort(key=lambda item: item[0], reverse=True)
        return [entry for _mtime, entry in entries]

    # -- the directory -----------------------------------------------------

    def _prepare_directory(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self._clean()

    def _clean(self) -> None:
        """Empty the ring, and only the ring.

        Deliberately not a recursive delete of a configurable path: only
        regular files whose names match what this ring writes are removed, so a
        misconfigured directory costs a ring rather than somebody's data.
        """
        try:
            listing = list(self.directory.iterdir())
        except OSError:
            return
        for entry in listing:
            if not RING_SEGMENT_PATTERN.match(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                entry.unlink()
            except OSError:
                continue

    def status(self) -> dict:
        with self._lock:
            running = self._process is not None
        return {
            "enabled": self.enabled,
            "running": running,
            "directory": str(self.directory),
            "preroll_seconds": self.seconds,
            "segment_seconds": self.segment_seconds,
            "segments": self.segments,
            "starts": self._starts,
            "failures": self._failures,
            "stopped_reason": None if running else self._stopped_reason,
            "last_stderr": self._last_stderr,
        }


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
                 wall_clock=None, host_metrics=read_host_metrics,
                 ring: "AudioPrerollRing | None" = None):
        self.config = config
        self._store = store if store is not None else (
            AudioClipStore(
                config.directory, max_bytes=config.max_total_bytes,
                retention_days=config.retention_days,
            ) if config.directory is not None else None
        )
        # Constructed either way, started only when there is a pre-roll to
        # keep: with GATE_AUDIO_CAPTURE_PREROLL_SECONDS=0 nothing is spawned,
        # no directory is made and every clip is what it was before.
        self._ring = ring if ring is not None else AudioPrerollRing(
            config.ring_directory, seconds=config.preroll_seconds,
            source_url=config.source_url,
            child_address_space_bytes=config.child_address_space_bytes,
            popen=popen, clock=clock,
        )
        self._ring_blocked: str | None = None
        self._ring_checked_at: float | None = None
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
            # A clip that wanted a pre-roll and got none. Visible, because a
            # corpus of clips that all begin at the trigger is exactly the
            # corpus that could not answer issue #106.
            "preroll_absent": 0,
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
        try:
            while not stop_event.is_set():
                # A short poll rather than a pure wait, so a stop is noticed
                # promptly and a carried actuation cannot sit forever.
                self._wake.wait(1.0)
                self._wake.clear()
                if stop_event.is_set():
                    break
                # First, because the ring is what a capture arriving in the
                # next moment will want, and separately, because a ring that
                # cannot start must never stop a clip being captured.
                self.supervise_ring()
                try:
                    self.run_once()
                except Exception:
                    LOGGER.warning("gate_audio_capture outcome=failed", exc_info=True)
        finally:
            # The thread that owns the child is leaving, so the child goes with
            # it rather than outliving the controller holding an RTSP session.
            self._ring.stop("stopped")

    def supervise_ring(self) -> None:
        """Keep the pre-roll ring alive, or down, as the governor allows.

        Called once a tick by the owning thread -- the same thread that spawns
        captures -- so the ring child has exactly one supervisor and can never
        be started twice. The governor verdict is cached for a few seconds: a
        governor that spared the board by reading three sysfs files every
        second would be paying for itself twice over.
        """
        now = self._clock()
        if (self._ring_checked_at is None
                or now - self._ring_checked_at >= RING_GOVERNOR_INTERVAL_SECONDS):
            self._ring_checked_at = now
            self._ring_blocked = self._governor_verdict()
        with self._lock:
            closed = self._closed
        self._ring.supervise(blocked="closed" if closed else self._ring_blocked)

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
        # Taken before the child is spawned, so the pre-roll ends where the
        # capture is about to begin rather than a connection set-up later.
        preroll = self._harvest_preroll(started_at)
        try:
            audio, failure, stderr_tail = self._capture()
        finally:
            with self._lock:
                self._in_flight = None
        elapsed = self._clock() - started_monotonic
        if failure is not None or not audio:
            with self._lock:
                self._counters["failed"] += 1
                self._last_capture = {
                    "outcome": "failed", "reason": failure, "stderr": stderr_tail or None,
                }
            # The tail is what makes this diagnosable: reason=exit_status on its
            # own said nothing about the address space limit that caused it.
            LOGGER.warning(
                "gate_audio_capture outcome=failed reason=%s stderr=%s",
                failure, stderr_tail or "-",
            )
            return False
        audio, preroll = _join_preroll(preroll, audio, self.config.max_clip_bytes)
        sidecar = _build_sidecar(
            request, started_at=started_at, elapsed_seconds=elapsed,
            requested_seconds=self.config.clip_seconds,
            retention_days=self.config.retention_days,
            source_url=self.config.source_url,
            started_monotonic=started_monotonic, preroll=preroll,
            requested_preroll_seconds=self.config.preroll_seconds,
        )
        path = self._store.record(audio, sidecar)
        if path is None:
            with self._lock:
                self._counters["failed"] += 1
                self._last_capture = {"outcome": "failed", "reason": "store"}
            return False
        with self._lock:
            self._counters["captured"] += 1
            if not preroll["seconds"] and self.config.preroll_seconds > 0:
                self._counters["preroll_absent"] += 1
            self._last_capture = {
                "outcome": "captured",
                "label": sidecar["label"],
                "bytes": len(audio),
                "seconds": sidecar["audio"]["captured_seconds"],
                "preroll_seconds": sidecar["audio"]["preroll_seconds"],
            }
        LOGGER.info(
            "gate_audio_capture outcome=captured label=%s bytes=%d seconds=%.1f "
            "preroll=%.1f trigger=%s",
            sidecar["label"], len(audio), sidecar["audio"]["captured_seconds"],
            sidecar["audio"]["preroll_seconds"], sidecar["trigger"],
        )
        return True

    def _harvest_preroll(self, started_at: datetime) -> dict:
        """The audio from before the trigger, or an honest nothing.

        Runs on the owning thread between taking the request and spawning the
        capture, so neither trigger ever waits for a file to be read. The wall
        clock is what the ring's freshness cut is measured against, because
        what it compares is the segments' modification times.
        """
        if self.config.preroll_seconds <= 0:
            return {"audio": b"", "seconds": 0.0, "segments": 0}
        try:
            now = started_at.timestamp()
        except (AttributeError, OSError, OverflowError, ValueError):
            return {"audio": b"", "seconds": 0.0, "segments": 0}
        audio, seconds, segments = self._ring.harvest(
            seconds=self.config.preroll_seconds, now=now,
            max_bytes=self.config.max_clip_bytes,
        )
        return {"audio": audio, "seconds": seconds, "segments": segments}

    def status(self) -> dict:
        with self._lock:
            snapshot = {
                "enabled": self.enabled,
                "clip_seconds": self.config.clip_seconds,
                "preroll_seconds": self.config.preroll_seconds,
                "capturing": self._in_flight is not None,
                "last_skip_reason": self._last_skip_reason,
                "last_capture": dict(self._last_capture) if self._last_capture else None,
                **dict(self._counters),
            }
        if self._store is not None:
            snapshot["store"] = self._store.status()
        snapshot["ring"] = self._ring.status()
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
        self._ring.close()

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

    def _capture(self) -> tuple[bytes, str | None, str]:
        """Run one child. Returns its audio, why it failed, and its stderr tail.

        The tail is kept because a failure that says only ``reason=exit_status``
        is not diagnosable from the journal: that is exactly what the address
        space limit looked like on 2026-09-08, and the one line that explained
        it was going to ``DEVNULL``.
        """
        errors = bytearray()
        try:
            process = self._popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"LANG": "C", "LC_ALL": "C"},
                close_fds=True,
                preexec_fn=_child_limiter(self.config.child_address_space_bytes),
            )
        except (OSError, ValueError):
            return b"", "spawn", ""
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
            return b"", "stopping", ""
        try:
            audio, failure = self._read_bounded(process, errors)
        finally:
            # Whatever the child said on its way out, taken without blocking
            # and before the pipe is closed under us.
            _drain_errors(_error_descriptor(process), errors)
            _terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
        tail = _stderr_tail(errors)
        if failure is not None:
            return audio, failure, tail
        if process.returncode != 0:
            return audio, "exit_status", tail
        if not _is_adts_aac(audio):
            return audio, "not_aac", tail
        return audio, None, tail

    def _read_bounded(self, process, errors: bytearray | None = None) -> tuple[bytes, str | None]:
        """Read to EOF, never holding more than the cap nor waiting past the
        deadline. ffmpeg's ``-t`` is the first bound; these are the other two,
        because a child that hangs before it ever honours ``-t`` would
        otherwise sit on an RTSP connection forever."""
        deadline = self._clock() + self.config.clip_seconds + CAPTURE_STARTUP_GRACE_SECONDS
        buffer = bytearray()
        errors = bytearray() if errors is None else errors
        try:
            descriptor = process.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            return b"", "no_stdout"
        # stderr is read in the same loop, never after it: a child that filled
        # the stderr pipe while nobody was reading would block on the write and
        # never reach the -t it was given.
        error_descriptor = _error_descriptor(process)
        sources = [descriptor] if error_descriptor is None else [descriptor, error_descriptor]
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return bytes(buffer), "timeout"
            ready, _, _ = select.select(sources, [], [], min(remaining, 1.0))
            if error_descriptor is not None and error_descriptor in ready:
                if not _read_errors(error_descriptor, errors):
                    sources.remove(error_descriptor)
                    error_descriptor = None
            if descriptor not in ready:
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
                   source_url: str, started_monotonic: float | None = None,
                   preroll: dict | None = None,
                   requested_preroll_seconds: float = 0.0) -> dict:
    """What this clip is, and what the controller was doing while it recorded.

    ``label`` is the free training label and the only field a first pass needs:
    ``actuated`` means the controller energised the relay during this clip, so
    the gate was commanded to move; ``no_actuation`` means it did not, so any
    gate movement audible here was somebody's remote, keypad or hand.

    Aligning the clip, now that it can begin before the thing that caused it:

    * ``t = 0`` is the **trigger** -- the camera event, or the actuation that
      asked for the clip. Every ``offset_seconds`` is measured from there, as
      it always was.
    * ``audio.trigger_offset_seconds`` says where that instant sits inside the
      stored bytes, so a position in the clip is ``trigger_offset_seconds +
      offset_seconds``. It is normally the pre-roll's length. It is negative
      when the clip began *after* the trigger, which is what every clip
      recorded before this existed did.
    * ``clip_started_at`` is the wall clock of the clip's first sample, and
      ``captured_at`` stays what it always meant: when the capture child ran.
    """
    preroll = preroll or {"audio": b"", "seconds": 0.0, "segments": 0}
    preroll_seconds = round(max(0.0, float(preroll.get("seconds") or 0.0)), 2)
    actuations = [_actuation_fields(record) for record in (request.get("actuations") or [])]
    requested_monotonic = request.get("requested_monotonic")
    for record, raw in zip(actuations, request.get("actuations") or []):
        monotonic = raw.get("monotonic")
        if isinstance(monotonic, (int, float)) and isinstance(requested_monotonic, (int, float)):
            record["offset_seconds"] = round(monotonic - requested_monotonic, 3)
    trigger_offset = None
    if isinstance(started_monotonic, (int, float)) and isinstance(requested_monotonic, (int, float)):
        trigger_offset = round(preroll_seconds - (started_monotonic - requested_monotonic), 3)
    return {
        "schema_version": 1,
        "kind": "gate_audio",
        "clip_id": started_at.strftime("%Y%m%dT%H%M%S%fZ"),
        "captured_at": _isoformat(started_at),
        "clip_started_at": _isoformat(started_at - timedelta(seconds=preroll_seconds)),
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
            # What the pre-roll actually turned out to be, never what was
            # asked for: 0.0 says this clip begins at its trigger and cannot
            # speak to what the microphone heard before it.
            "preroll_seconds": preroll_seconds,
            "preroll_requested_seconds": requested_preroll_seconds,
            "preroll_segments": int(preroll.get("segments") or 0),
            "trigger_offset_seconds": trigger_offset,
        },
        "retention_days": retention_days,
    }


def _join_preroll(preroll: dict, audio: bytes, max_clip_bytes: int) -> tuple[bytes, dict]:
    """Put the pre-roll in front of the capture, inside the same byte cap.

    Both halves are the camera's own ADTS frames, which is the whole reason
    this is a byte join: ADTS repeats its header on every frame and carries no
    file-level index or timestamps, so the concatenation is a valid stream that
    any decoder plays straight through. The join is frame-aligned because the
    ring's bytes were cut on frame boundaries.

    The clip stays inside ``max_clip_bytes`` -- the pre-roll gives way, oldest
    frames first, rather than the bound quietly becoming cap-plus-a-pre-roll.
    """
    body = preroll.get("audio") or b""
    if not body:
        return audio, {"audio": b"", "seconds": 0.0, "segments": 0}
    room = max_clip_bytes - len(audio)
    if len(body) > room:
        body, samples, rate = _adts_tail(body, max_bytes=max(0, room))
        preroll = dict(preroll)
        preroll["seconds"] = samples / float(rate or STATED_SAMPLE_RATE_HZ)
        if not body:
            return audio, {"audio": b"", "seconds": 0.0, "segments": 0}
    return body + audio, {**preroll, "audio": body}


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


def _adts_frame_length(data, offset: int) -> int | None:
    """The length of the ADTS frame at ``offset``, or None if there is not one.

    Thirteen bits spread across bytes 3, 4 and 5 of the header, and that is the
    entire trick that makes a pre-roll possible without a decoder: the format
    says how long each frame is, in the frame.
    """
    if offset < 0 or offset + 7 > len(data):
        return None
    if ((data[offset] << 4) | (data[offset + 1] >> 4)) != _ADTS_SYNC:
        return None
    if (data[offset + 1] >> 1) & 0x03 != 0:
        return None
    length = (((data[offset + 3] & 0x03) << 11)
              | (data[offset + 4] << 3)
              | (data[offset + 5] >> 5))
    return length if length >= 7 else None


def _adts_frames(data) -> tuple[list[tuple[int, int]], int, int | None]:
    """Walk the frame headers. Returns their (offset, samples), the end of the
    last whole frame, and the sample rate the first header names.

    Headers only. Nothing here reads a sample, allocates a decoder or so much
    as looks at the payload -- it steps over each frame by the length the frame
    states. The walk stops at the first thing that is not a header, which is
    what makes a half-written segment contribute its whole frames and not its
    torn tail.
    """
    frames: list[tuple[int, int]] = []
    if not isinstance(data, (bytes, bytearray)):
        return frames, 0, None
    offset = 0
    rate = None
    limit = len(data)
    while offset < limit:
        length = _adts_frame_length(data, offset)
        if length is None or offset + length > limit:
            break
        if rate is None:
            index = (data[offset + 2] >> 2) & 0x0F
            rate = ADTS_SAMPLE_RATES[index] if index < len(ADTS_SAMPLE_RATES) else None
        # An ADTS frame may carry more than one raw data block; ffmpeg writes
        # one, but the header says, so the header is what is believed.
        frames.append((offset, AAC_SAMPLES_PER_FRAME * ((data[offset + 6] & 0x03) + 1)))
        offset += length
    return frames, offset, rate


def _adts_tail(data, *, samples: int | None = None,
               max_bytes: int | None = None) -> tuple[bytes, int, int | None]:
    """The last whole ADTS frames of ``data`` inside both budgets.

    Returns the bytes, the samples they carry and the stream's sample rate.
    Empty when the buffer does not start with a header at all, which is the
    refusal that keeps anything that is not this camera's stream out of a clip.
    """
    frames, end, rate = _adts_frames(data)
    if not frames:
        return b"", 0, rate
    kept = 0
    start = end
    for offset, count in reversed(frames):
        if samples is not None and kept + count > samples:
            break
        if max_bytes is not None and end - offset > max_bytes:
            break
        kept += count
        start = offset
    if start >= end:
        return b"", 0, rate
    return bytes(data[start:end]), kept, rate


def _child_limiter(address_space_bytes: int):
    """Build the hook that caps one capture child and deprioritises it.

    Copying packets needs almost nothing *resident*, so the limit is tight
    enough that a runaway child dies instead of taking the board with it --
    which is what happened here on 2026-09-07 when a decode test was run on the
    live device. It has to be generous in *address space* all the same, because
    RLIMIT_AS also counts the shared libraries the loader maps before ffmpeg
    runs at all; see DEFAULT_CHILD_ADDRESS_SPACE_BYTES for what the old 128 MiB
    did on 2026-09-08.
    """

    def limit_child() -> None:  # pragma: no cover - runs in the child
        resource.setrlimit(
            resource.RLIMIT_AS, (address_space_bytes, address_space_bytes),
        )
        try:
            os.nice(19)
        except OSError:
            pass

    return limit_child


def _error_descriptor(process):
    """The child's stderr file descriptor, or None when there is not one."""
    stream = getattr(process, "stderr", None)
    if stream is None:
        return None
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _read_errors(descriptor: int, errors: bytearray) -> bool:
    """Read what is waiting on stderr, keeping only the tail. False at EOF."""
    try:
        chunk = os.read(descriptor, 4096)
    except OSError:
        return False
    if not chunk:
        return False
    errors.extend(chunk)
    if len(errors) > MAX_STDERR_TAIL_BYTES:
        del errors[:-MAX_STDERR_TAIL_BYTES]
    return True


def _drain_errors(descriptor, errors: bytearray) -> None:
    """Take whatever is already buffered on stderr, without ever waiting."""
    if descriptor is None:
        return
    for _ in range(16):
        try:
            ready, _unused, _also = select.select([descriptor], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready or not _read_errors(descriptor, errors):
            return


def _stderr_tail(errors: bytearray) -> str:
    """One journal-safe line: the last few characters the child complained in."""
    text = bytes(errors).decode("utf-8", "replace")
    # Collapsed to a single line so one failure stays one journal record, and
    # trimmed so a chatty child cannot flood it.
    return " ".join(text.split())[-STDERR_TAIL_CHARACTERS:]


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
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is None:
            continue
        try:
            stream.close()
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


def _ring_directory(environment) -> Path:
    """Where the pre-roll ring lives. RAM by default, and never the root.

    The default is under ``/dev/shm`` for two reasons: the segments are
    rewritten every few seconds and an SD card should not be asked to carry
    that, and under the unit's ``ProtectSystem=strict`` the tmpfs is writable
    where ``/run`` and the rest of the filesystem are not.
    """
    raw = (environment.get("GATE_AUDIO_CAPTURE_RING_DIR") or "").strip()
    if not raw:
        return DEFAULT_RING_DIRECTORY
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("GATE_AUDIO_CAPTURE_RING_DIR must be an absolute path")
    if path.parent == path:
        raise ValueError("GATE_AUDIO_CAPTURE_RING_DIR must be a directory of its own")
    return path


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
