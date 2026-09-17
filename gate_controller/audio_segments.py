"""Record the gate's sound continuously; cut the interesting parts out later.

``audio_capture`` records a clip per event: it opens an RTSP connection when
the relay fires, records 40 s, and closes it. Every clip therefore starts at
the actuation, which is *after* the vehicle arrived and after the motor
started, and an event that produces no clip -- a camera webhook rejected as
stale, a governor skip, a passage with no actuation at all -- leaves no
recording of that passage anywhere. In the first week the live system produced
two clips, both starting at the relay, and no negatives.

This records the whole day instead and decides afterwards what was worth
keeping. A window around an event can then be cut with as much pre-roll as the
question needs, changed later without redeploying anything, and cut for events
nobody thought were interesting at the time.

That is not a new idea here. ``ClearStreamSource`` already does exactly this
for video: one long-lived ffmpeg copying compressed packets into a ring, with
decoding deferred until something asks. This is the same trick for audio, and
it exists because ``record_command`` -- the command feeding that ring, on a
connection to this very stream that is open every second of every day --
carries ``-an``, and has been throwing the audio away all along.

Why segments on disk and not a ring in memory
---------------------------------------------
Because nothing needs the audio *now*. The analysis is retrospective by
definition, so the buffer may as well be the filesystem: a 30-minute segment
is 14 MiB, the name carries its own start time, and finding the segment
covering an instant is arithmetic rather than an index that could disagree
with the files. A ring in RAM would buy immediacy nobody is waiting for and
lose everything on restart.

The cost, measured rather than assumed: the audio is 8.1 KB/s, so a day is
665 MiB and a 48-hour horizon is 1.3 GiB. The card already absorbs about
550 MB/day of journal, and 30-minute sequential writes are the gentlest
pattern flash sees.

Bounds, because this is a fanless board that has been OOM-killed before:

* **Nothing is ever decoded.** ``-vn -c:a copy`` drops the video before any
  packet reaches a decoder and remuxes the AAC untouched. The one measured
  cost is the packet copy itself, which for audio alone is far below the 1.6%
  of a core the video ring already pays.
* **One recorder, supervised with backoff.** A child that dies is restarted,
  but never faster than ``MIN_RESTART_SECONDS``, so a stream that refuses
  every connection cannot become a spin.
* **The pruner runs on its own timer**, not as a step of the extraction job.
  A job that fails to run must not be the reason the card fills.
* **A free-space floor beneath the pruner.** Under it the oldest segments go
  first; still under it, recording stops and says so. Audio must never be the
  reason the controller cannot write evidence or its database.

Retention is deliberately short: segments are raw material, not the archive.
What survives is the windows the extraction job cuts into the training corpus,
which the existing uploader ships to R2 exactly as it ships frames and clips.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile

LOGGER = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"
DEFAULT_SOURCE = "rtsp://127.0.0.1:8554/clear"

#: Five minutes, and the number is set by the corpus contract rather than by
#: taste: an artefact payload is capped at 4 MiB, and at the measured 8.1 KB/s
#: a half-hour segment is 13.9 MiB -- three and a half times over. Five minutes
#: is 2.3 MiB, which leaves room for a bitrate excursion. It also shortens the
#: window nobody can read yet, because the segment being written is the one
#: segment that is not shippable.
DEFAULT_SEGMENT_SECONDS = 300
MIN_SEGMENT_SECONDS = 60
MAX_SEGMENT_SECONDS = 3600

#: Two days, so a daily extraction job that fails once still finds yesterday's
#: audio when it next runs.
DEFAULT_RETENTION_HOURS = 48
MIN_RETENTION_HOURS = 2
MAX_RETENTION_HOURS = 24 * 14

#: Beneath this the segments start going regardless of age, and recording
#: stops rather than taking the last of the card. The controller's database,
#: its evidence images and the corpus buffer all live on the same filesystem
#: and all outrank this.
DEFAULT_MIN_FREE_BYTES = 1536 * 1024 * 1024
MIN_MIN_FREE_BYTES = 256 * 1024 * 1024

#: A restart is normal -- MediaMTX restarts, the camera reboots -- but it is
#: never urgent. Nothing is waiting on this stream.
MIN_RESTART_SECONDS = 5.0
MAX_RESTART_SECONDS = 60.0

#: ``gate-20260916T120000Z.aac``. The name is the index: it is the segment's
#: start instant, so the file covering a moment is found by arithmetic and
#: there is no manifest that could disagree with what is on the card.
SEGMENT_PREFIX = "gate-"
SEGMENT_SUFFIX = ".aac"
SIDECAR_SUFFIX = ".json"
SIDECAR_SCHEMA_VERSION = 2
SEGMENT_TEMPLATE = f"{SEGMENT_PREFIX}%Y%m%dT%H%M%SZ{SEGMENT_SUFFIX}"
SEGMENT_PATTERN = re.compile(
    rf"^{re.escape(SEGMENT_PREFIX)}(\d{{8}}T\d{{6}})Z{re.escape(SEGMENT_SUFFIX)}$"
)

#: MPEG-4 AAC sampling frequencies, indexed by the 4 bits in the ADTS header.
ADTS_SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000,
    22050, 16000, 12000, 11025, 8000, 7350,
)
#: One AAC-LC frame is 1024 samples. At the camera's 16 kHz that is 64 ms,
#: which is the granularity every cut below is rounded to.
ADTS_SAMPLES_PER_BLOCK = 1024
ADTS_HEADER_BYTES = 7


def segment_command(source_url: str, directory: Path, *, seconds: int = DEFAULT_SEGMENT_SECONDS,
                    ffmpeg: str = FFMPEG_BINARY) -> tuple[str, ...]:
    """ffmpeg command that writes the stream's audio to clock-aligned segments.

    ``-segment_atclocktime`` cuts on the wall-clock half hour rather than
    ``seconds`` after the process happened to start, so a restart does not
    shift every subsequent boundary and the names stay predictable.
    ``-strftime`` puts the start instant in the name; the child is given
    ``TZ=UTC`` so that instant is UTC and not whatever the host is set to.

    ``-c:a copy`` with ``-vn``: no decode, no resample, no analysis. The bytes
    written are the camera's own AAC.
    """
    return (
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", source_url,
        "-vn", "-c:a", "copy",
        "-f", "segment",
        "-segment_time", str(int(seconds)),
        "-segment_atclocktime", "1",
        "-segment_format", "adts",
        "-reset_timestamps", "1",
        "-strftime", "1",
        str(directory / SEGMENT_TEMPLATE),
    )


def iter_adts_frames(data: bytes, *, start: int = 0):
    """Walk ADTS frames, yielding ``(offset, length, seconds)``.

    ADTS is self-framing: every frame carries its own length, so the stream can
    be cut on a frame boundary without a decoder and without an index. The
    header is parsed rather than the frame size assumed, because AAC frames are
    variable length -- interpolating a byte offset from a time would land in
    the middle of one and produce a file that will not play.

    A frame whose header does not read as a frame ends the walk. This is the
    camera's own bitstream rather than an arbitrary file, so that means the
    tail of a segment interrupted mid-write, which is exactly where stopping
    is the right answer.
    """
    offset = start
    end = len(data)
    while offset + ADTS_HEADER_BYTES <= end:
        if data[offset] != 0xFF or (data[offset + 1] & 0xF0) != 0xF0:
            return
        rate_index = (data[offset + 2] >> 2) & 0x0F
        if rate_index >= len(ADTS_SAMPLE_RATES):
            return
        length = (
            ((data[offset + 3] & 0x03) << 11)
            | (data[offset + 4] << 3)
            | (data[offset + 5] >> 5)
        )
        if length < ADTS_HEADER_BYTES or offset + length > end:
            return
        blocks = (data[offset + 6] & 0x03) + 1
        seconds = (ADTS_SAMPLES_PER_BLOCK * blocks) / ADTS_SAMPLE_RATES[rate_index]
        yield offset, length, seconds
        offset += length


@dataclass(frozen=True)
class Segment:
    """One recorded file and the instant it starts at."""

    path: Path
    started_at: datetime

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def duration(self) -> float:
        """How much audio this file actually holds, by walking its frames."""
        try:
            data = self.path.read_bytes()
        except OSError:
            return 0.0
        return sum(seconds for _, _, seconds in iter_adts_frames(data))


def segment_started_at(path: Path) -> datetime | None:
    match = SEGMENT_PATTERN.match(path.name)
    if match is None:
        return None
    try:
        moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc)


class SegmentStore:
    """The recorded segments on the card, and the rules that bound them."""

    def __init__(self, directory: Path, *, retention_hours: int = DEFAULT_RETENTION_HOURS,
                 min_free_bytes: int = DEFAULT_MIN_FREE_BYTES, clock=None):
        self.directory = Path(directory)
        self.retention_hours = max(MIN_RETENTION_HOURS,
                                   min(MAX_RETENTION_HOURS, int(retention_hours)))
        self.min_free_bytes = max(MIN_MIN_FREE_BYTES, int(min_free_bytes))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def segments(self) -> list[Segment]:
        """Every recognisable segment, oldest first.

        A name this does not recognise is ignored rather than deleted: this
        directory is the recorder's, but a bounded walk that removes whatever
        it does not understand is not a habit worth having on an SD card that
        also holds the database.
        """
        found = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return []
        for path in entries:
            if not path.is_file():
                continue
            started_at = segment_started_at(path)
            if started_at is not None:
                found.append(Segment(path, started_at))
        found.sort(key=lambda segment: segment.started_at)
        return found

    def free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.directory).free
        except OSError:
            # Unreadable free space is treated as ample, the same way the
            # capture governor treats an unreadable metric: a path that moved
            # must not silently stop the recording for ever.
            return self.min_free_bytes

    def prune(self) -> dict:
        """Drop what is past the horizon, then whatever the floor still needs.

        Returns what it did, so the caller can journal a number rather than a
        claim. The newest segment is never removed by the floor: it is the one
        being written, and deleting it would free nothing that the recorder is
        not about to use again.
        """
        removed_age = removed_space = 0
        cutoff = self._clock() - timedelta(hours=self.retention_hours)
        segments = self.segments()
        for segment in list(segments):
            if segment.started_at < cutoff:
                if self._remove(segment):
                    removed_age += 1
                    segments.remove(segment)
        while len(segments) > 1 and self.free_bytes() < self.min_free_bytes:
            if not self._remove(segments[0]):
                break
            segments.pop(0)
            removed_space += 1
        if removed_age or removed_space:
            LOGGER.info(
                "gate_audio_segments stage=pruned expired=%d for_space=%d remaining=%d free_mb=%d",
                removed_age, removed_space, len(segments), self.free_bytes() // (1024 * 1024),
            )
        return {
            "expired": removed_age,
            "for_space": removed_space,
            "remaining": len(segments),
            "free_bytes": self.free_bytes(),
        }

    def _remove(self, segment: Segment) -> bool:
        try:
            # A sidecar beside it means the uploader had been offered this
            # segment and had not yet shipped it, so deleting it now loses
            # audio permanently. Filling the card would be worse, so it still
            # goes -- but never quietly.
            sidecar = segment.path.with_suffix(SIDECAR_SUFFIX)
            if sidecar.exists():
                LOGGER.warning(
                    "gate_audio_segments stage=pruned_unshipped path=%s bytes=%d "
                    "detail=audio_lost_before_upload", segment.path.name, segment.size)
                sidecar.unlink(missing_ok=True)
            segment.path.unlink(missing_ok=True)
            return True
        except OSError:
            LOGGER.warning("gate_audio_segments stage=unlink_failed path=%s",
                           segment.path.name, exc_info=True)
            return False

    def covering(self, start: datetime, end: datetime) -> list[Segment]:
        """The segments whose audio overlaps ``[start, end)``, in order.

        A segment's span is measured from its frames rather than assumed to be
        the configured length, because the last segment before a restart is
        short and assuming otherwise would silently drop the end of a window.
        """
        found = []
        for segment in self.segments():
            span = segment.duration()
            if span <= 0:
                continue
            finishes = segment.started_at + timedelta(seconds=span)
            if finishes > start and segment.started_at < end:
                found.append(segment)
        return found


def extract_window(store: SegmentStore, start: datetime, end: datetime) -> bytes:
    """The audio between two instants, as a playable ADTS stream.

    Cuts only on frame boundaries, so the result is a valid file rather than a
    byte range that happens to begin mid-frame. A window spanning a restart --
    or a gap where the recorder was down -- returns the frames that do exist,
    concatenated: a shorter clip than asked for is honest, where a padded one
    would put silence the microphone never heard into a training set.
    """
    if end <= start:
        return b""
    kept = bytearray()
    for segment in store.covering(start, end):
        try:
            data = segment.path.read_bytes()
        except OSError:
            LOGGER.warning("gate_audio_segments stage=read_failed path=%s",
                           segment.path.name, exc_info=True)
            continue
        moment = segment.started_at
        for offset, length, seconds in iter_adts_frames(data):
            finishes = moment + timedelta(seconds=seconds)
            if finishes > start and moment < end:
                kept += data[offset:offset + length]
            moment = finishes
            if moment >= end:
                break
    return bytes(kept)


def sidecar_for(segment: "Segment", *, source_url: str, seconds: float) -> dict:
    """What this segment is, in the shape the corpus uploader already ships.

    Deliberately free of the events database. Correlating a segment with the
    passages inside it is a join on time that the cloud can do against the
    access log it already holds, and the recorder has no business opening the
    controller's database to do it.
    """
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "kind": "gate_audio_segment",
        "captured_at": segment.started_at.isoformat(),
        "source": "audio_segments",
        "artefact": {"kind": "audio", "media_type": "audio/aac"},
        "ocr": None,
        "segment": {
            "started_at": segment.started_at.isoformat(),
            "seconds": round(seconds, 2),
            "bytes": segment.size,
            "continuous": True,
        },
        "audio": {
            "container": "adts",
            "codec": "aac_lc",
            "sample_rate_hz": 16000,
            "channels": 1,
            "raw_copy": True,
            "decoded": False,
            "source": source_url,
        },
    }


def write_ready_sidecars(store: "SegmentStore", *, source_url: str) -> int:
    """Give every finished segment its sidecar, so the uploader ships it.

    This is the whole of "keep everything": ``TrainingCorpus.pending`` offers
    only *complete pairs*, so a segment with no sidecar is invisible to the
    uploader. Writing the sidecar is therefore the act of releasing a segment,
    and the newest segment -- the one ffmpeg still has open -- is deliberately
    skipped. Nothing has to lock, move or copy anything.

    A segment already carrying a sidecar is left alone: the uploader deletes
    both halves when the cloud confirms them, so the pair reappearing would
    mean re-uploading bytes R2 already has.
    """
    segments = store.segments()
    if len(segments) < 2:
        return 0
    written = 0
    for segment in segments[:-1]:
        sidecar_path = segment.path.with_suffix(SIDECAR_SUFFIX)
        if sidecar_path.exists():
            continue
        seconds = segment.duration()
        if seconds <= 0:
            # Nothing decodable in it. Left for the pruner rather than shipped
            # as an artefact that is not audio.
            continue
        document = sidecar_for(segment, source_url=source_url, seconds=seconds)
        try:
            _write_private(sidecar_path,
                           json.dumps(document, separators=(",", ":")).encode("utf-8"))
        except OSError:
            LOGGER.warning("gate_audio_segments stage=sidecar_failed path=%s",
                           segment.path.name, exc_info=True)
            continue
        written += 1
    if written:
        LOGGER.info("gate_audio_segments stage=released segments=%d", written)
    return written


def _write_private(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


class SegmentRecorder:
    """One long-lived ffmpeg writing segments, restarted when it dies."""

    def __init__(self, store: SegmentStore, *, source_url: str = DEFAULT_SOURCE,
                 segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
                 ffmpeg: str = FFMPEG_BINARY, popen=subprocess.Popen,
                 prune_every_seconds: float = 300.0, monotonic=None, sleep=None,
                 waiter=None, keep_everything: bool = False):
        self.store = store
        self.source_url = source_url
        #: When set, every finished segment is given a sidecar and so becomes
        #: an artefact the existing corpus uploader ships to R2. Weather,
        #: tractors and everything else that was never going to be an event
        #: is then kept rather than pruned unheard.
        self.keep_everything = keep_everything
        self.segment_seconds = max(MIN_SEGMENT_SECONDS,
                                   min(MAX_SEGMENT_SECONDS, int(segment_seconds)))
        self.command = segment_command(source_url, store.directory,
                                       seconds=self.segment_seconds, ffmpeg=ffmpeg)
        # TZ is the load-bearing one: ``-strftime`` names the file from the
        # child's own idea of local time, and a host in Irish time would write
        # names an hour from the instant they claim for half the year.
        self.child_environment = {"LANG": "C", "LC_ALL": "C", "TZ": "UTC"}
        self._popen = popen
        self._prune_every = prune_every_seconds
        self._restarts = 0
        self._starts = 0
        self._refusals = 0
        self._last_error = None
        from time import monotonic as _monotonic, sleep as _sleep
        self._monotonic = monotonic or _monotonic
        self._sleep = sleep or _sleep
        # Every wait in this class goes through one seam, so a test can drive
        # the restart path without spending the backoff in real seconds.
        self._waiter = waiter or (lambda event, seconds: event.wait(seconds))

    def run_forever(self, stop_event) -> None:
        self.store.prepare()
        last_prune = 0.0
        while not stop_event.is_set():
            now = self._monotonic()
            if now - last_prune >= self._prune_every:
                last_prune = now
                if self.keep_everything:
                    # Before pruning, not after: a segment that has just been
                    # released is one the uploader can still save.
                    try:
                        write_ready_sidecars(self.store, source_url=self.source_url)
                    except Exception:
                        LOGGER.warning("gate_audio_segments stage=release_failed",
                                       exc_info=True)
                try:
                    self.store.prune()
                except Exception:
                    LOGGER.warning("gate_audio_segments stage=prune_failed", exc_info=True)
            if self.store.free_bytes() < self.store.min_free_bytes:
                # The pruner has already taken what it can. Recording into the
                # last of the card is not a trade worth making for audio.
                self._refusals += 1
                LOGGER.warning(
                    "gate_audio_segments stage=refused reason=low_disk free_mb=%d floor_mb=%d",
                    self.store.free_bytes() // (1024 * 1024),
                    self.store.min_free_bytes // (1024 * 1024),
                )
                self._waiter(stop_event, MAX_RESTART_SECONDS)
                continue
            started = self._monotonic()
            self._run_once(stop_event)
            if stop_event.is_set():
                break
            # A child that ran for a while and stopped is a stream that went
            # away; one that died immediately is a configuration that will die
            # immediately again. Both wait, the second one longer.
            elapsed = self._monotonic() - started
            delay = MIN_RESTART_SECONDS if elapsed >= 60 else min(
                MAX_RESTART_SECONDS, MIN_RESTART_SECONDS * (1 + self._restarts)
            )
            self._waiter(stop_event, delay)

    def _run_once(self, stop_event) -> None:
        try:
            process = self._popen(
                self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, env=self.child_environment, close_fds=True,
            )
        except Exception as error:
            self._restarts += 1
            self._last_error = str(error)
            LOGGER.warning("gate_audio_segments stage=spawn_failed error=%s", error)
            return
        self._starts += 1
        LOGGER.info("gate_audio_segments stage=recording source=%s segment_seconds=%d",
                    self.source_url, self.segment_seconds)
        try:
            while not stop_event.is_set():
                if process.poll() is not None:
                    break
                self._waiter(stop_event, 1.0)
        finally:
            tail = self._stop(process)
        if not stop_event.is_set():
            self._restarts += 1
            self._last_error = tail
            LOGGER.warning("gate_audio_segments stage=stopped restarts=%d stderr=%s",
                           self._restarts, tail)

    def _stop(self, process) -> str:
        tail = ""
        try:
            if process.poll() is None:
                process.terminate()
            try:
                _, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _, errors = process.communicate(timeout=5)
            if errors:
                tail = errors.decode("utf-8", "replace").strip()[-200:]
        except Exception:
            LOGGER.warning("gate_audio_segments stage=stop_failed", exc_info=True)
        return tail

    def status(self) -> dict:
        segments = self.store.segments()
        recorded = sum(segment.size for segment in segments)
        return {
            "source": self.source_url,
            "segment_seconds": self.segment_seconds,
            "segments": len(segments),
            "bytes": recorded,
            "oldest": segments[0].started_at.isoformat() if segments else None,
            "newest": segments[-1].started_at.isoformat() if segments else None,
            "starts": self._starts,
            "restarts": self._restarts,
            "keep_everything": self.keep_everything,
            "released": sum(1 for segment in self.store.segments()
                            if segment.path.with_suffix(SIDECAR_SUFFIX).exists()),
            "low_disk_refusals": self._refusals,
            "free_bytes": self.store.free_bytes(),
            "last_error": self._last_error,
        }


def load_segment_config(environment=None) -> dict:
    """Read the recorder's settings, refusing anything outside safe bounds."""
    environment = os.environ if environment is None else environment
    enabled = str(environment.get("GATE_AUDIO_SEGMENTS_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    directory = (environment.get("GATE_AUDIO_SEGMENTS_DIR") or "").strip()
    if enabled and not directory:
        raise ValueError("GATE_AUDIO_SEGMENTS_ENABLED requires GATE_AUDIO_SEGMENTS_DIR")
    if directory and not Path(directory).is_absolute():
        raise ValueError("GATE_AUDIO_SEGMENTS_DIR must be an absolute path")
    source = str(environment.get("GATE_AUDIO_SEGMENTS_SOURCE", DEFAULT_SOURCE)).strip()
    _require_loopback(source)
    return {
        "enabled": enabled,
        "keep_everything": str(
            environment.get("GATE_AUDIO_SEGMENTS_KEEP_EVERYTHING", "false")
        ).strip().lower() in {"1", "true", "yes", "on"},
        "directory": Path(directory) if directory else None,
        "source_url": source,
        "segment_seconds": _bounded_int(
            environment, "GATE_AUDIO_SEGMENTS_SECONDS", DEFAULT_SEGMENT_SECONDS,
            MIN_SEGMENT_SECONDS, MAX_SEGMENT_SECONDS),
        "retention_hours": _bounded_int(
            environment, "GATE_AUDIO_SEGMENTS_RETENTION_HOURS", DEFAULT_RETENTION_HOURS,
            MIN_RETENTION_HOURS, MAX_RETENTION_HOURS),
        "min_free_bytes": _bounded_int(
            environment, "GATE_AUDIO_SEGMENTS_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES,
            MIN_MIN_FREE_BYTES, 64 * 1024 * 1024 * 1024),
    }


def _require_loopback(value: str) -> None:
    """Loopback only: the audio already arrives at MediaMTX.

    A second connection to the camera would compete with the stream the
    recogniser depends on, over a 4.5 Mbit/s uplink, for bytes that are
    already being delivered locally.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if parts.scheme != "rtsp":
        raise ValueError("GATE_AUDIO_SEGMENTS_SOURCE must be an rtsp:// URL")
    if parts.username or parts.password:
        raise ValueError("GATE_AUDIO_SEGMENTS_SOURCE must not carry credentials")
    if parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("GATE_AUDIO_SEGMENTS_SOURCE must be a loopback address")


def _bounded_int(environment, name: str, default: int, minimum: int, maximum: int) -> int:
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
