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
* **One recorder, supervised.** A child that was recording and stopped is
  restarted at once, because every second it is down is a second nobody is
  listening; one that never wrote a byte backs off, so a configuration that
  will die again cannot become a spin. While MediaMTX says outright that the
  stream is not there, nothing is spawned at all: one small DESCRIBE a second
  asks again. See "What losing audio looked like" below.
* **The pruner runs on its own timer**, not as a step of the extraction job.
  A job that fails to run must not be the reason the card fills.
* **A free-space floor beneath the pruner.** Under it the oldest segments go
  first; still under it, recording stops and says so. Audio must never be the
  reason the controller cannot write evidence or its database.

Retention is deliberately short: segments are raw material, not the archive.
What survives is the windows the extraction job cuts into the training corpus,
which the existing uploader ships to R2 exactly as it ships frames and clips.

What losing audio looked like
-----------------------------
Measured over 44 hours on the gate (2026-09-19 to 21), the recorder held 95% of
the wall clock. Four things made up the rest, and each has an answer here:

* **The camera delivered less audio than real time** -- 86% of the loss, with
  the recorder running throughout. Nothing in this process can put back a frame
  the camera never sent, so the answer is to *say so*: every finished segment
  is measured against the span it covers, and the shortfall is data.
* **The backoff** -- a restart counter that only ever went up, so after eleven
  restarts every quick failure cost a minute. 670 s lost while the stream was
  there to be read. The delay now follows what the child did, not how many
  children there have been.
* **ffmpeg's write buffer** -- the ``file`` protocol holds 256 KiB, which at
  8.1 KB/s is 32 s of audio that exists nowhere but in the child's memory.
  Every service stop cut the open segment to an exact multiple of 262144 bytes.
  ``-fflags +flush_packets`` writes each 64 ms frame as it arrives.
* **The stream going away** -- MediaMTX restarts (a TURN refresh every four
  hours), the camera reboots, the 4K session resets. Unavoidable, five seconds
  at a time, and now recorded with a cause.
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
import socket
import subprocess
import tempfile

from .corpus import SHIPPED_SUFFIX

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

#: What a refusal for low disk waits before looking again. It was once also
#: the ceiling of the restart backoff, and that is how a restart came to cost a
#: minute of audio: see ``RETRY_DELAYS``.
MIN_RESTART_SECONDS = 5.0
MAX_RESTART_SECONDS = 60.0

#: How long to wait before starting another child, indexed by how many children
#: in a row have died *without recording anything*. A child that recorded and
#: then lost its stream resets the count, so the ordinary case -- MediaMTX
#: restarted, the 4K session was reset -- is always the first entry. The old
#: rule multiplied five seconds by a restart counter that never went down; by
#: the twelfth restart of a process's life every quick failure waited the full
#: minute, and 57 of the 121 gaps measured on the gate were longer than 60 s.
RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

#: While MediaMTX answers DESCRIBE with "no stream on this path", ask again
#: this often rather than spawning an ffmpeg to be told the same thing. It
#: re-dials the camera every five seconds, so this finds the stream within a
#: second of its return; one loopback socket a second is nothing.
PROBE_SECONDS = 1.0
PROBE_TIMEOUT_SECONDS = 2.0

#: A child that is running but has written nothing for this long is holding a
#: connection that has stopped delivering. With every frame flushed as it
#: arrives the file grows fifteen times a second -- but this is deliberately
#: longer than the 32 s an *unflushed* ffmpeg goes between writes, so that an
#: ffmpeg build which ignored the flag would merely be slow to notice a stall,
#: rather than be killed before its first write, every time, for ever. No stall
#: was seen in the 44 hours measured; this is a guard, not a fix.
STALL_SECONDS = 60.0

#: Gaps shorter than this are not worth a row: a restart costs about a second
#: whatever is done, and segment names are whole seconds.
MIN_GAP_SECONDS = 1.0

#: The recorder's own account of when it was not listening, one JSON object a
#: line. Not a segment name, so the store ignores it; not a payload/sidecar
#: pair, so the uploader does too.
GAP_LEDGER_NAME = "listening-gaps.jsonl"
GAP_LEDGER_KEEP_HOURS = 24 * 14

#: Why the recorder was not listening.
GAP_RESTART = "service_restart"          # the controller itself was restarted
GAP_RECORDER_OFF = "recorder_off"        # ...and had been down for a while
GAP_SOURCE = "source_unavailable"        # MediaMTX had no stream to give
GAP_ENDED = "stream_ended"               # the child stopped; the stream was there
GAP_STALLED = "stalled"                  # connected, and nothing arriving
GAP_LOW_DISK = "low_disk"
GAP_SPAWN = "spawn_failed"
#: Not one of the recorder's: the segment is simply shorter than the span it
#: covers, because the camera sent less audio than real time. Where in the
#: segment the missing seconds fall is not knowable from an ADTS file.
GAP_SHORTFALL = "stream_shortfall"
SHORTFALL_SECONDS = 2.0

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

    ``-allowed_media_types audio`` stops the video being *sent*: ``-vn`` alone
    discards it after MediaMTX has written all of it down the socket, which on
    the 4K path is 900 KB/s fetched to be thrown away. With it MediaMTX serves
    this reader one track.

    ``-fflags +flush_packets`` is the one that loses audio when it is missing.
    ffmpeg's ``file`` protocol buffers 256 KiB of output -- 32 s at this
    bitrate -- and a child that is stopped rather than finishing loses all of
    it: every segment cut short by a deploy was an exact multiple of 262144
    bytes. It must be ``-fflags``; ``-flush_packets 1`` is not passed down to
    the muxer the segmenter opens, which was measured rather than read.
    """
    return (
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-allowed_media_types", "audio",
        "-i", source_url,
        "-vn", "-c:a", "copy",
        "-fflags", "+flush_packets",
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
                 min_free_bytes: int = DEFAULT_MIN_FREE_BYTES, clock=None,
                 hold_unshipped=None):
        self.directory = Path(directory)
        self.retention_hours = max(MIN_RETENTION_HOURS,
                                   min(MAX_RETENTION_HOURS, int(retention_hours)))
        self.min_free_bytes = max(MIN_MIN_FREE_BYTES, int(min_free_bytes))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        #: Asked before an unshipped segment past the horizon is taken: a
        #: reason to keep it, or None. The corpus uploader answers with why it
        #: is not draining (``CorpusUploadWorker.retention_hold``). While it
        #: does, the horizon is the free-space floor rather than the clock:
        #: a segment still carrying its sidecar is the only copy of that
        #: audio, and a router outage on 2026-09-22 held the uploader off for
        #: a day while the 48-hour horizon closed on 51 of them.
        self.hold_unshipped = hold_unshipped
        self.held = 0
        self._last_hold_log: tuple[str, int] | None = None
        self._last_hold_logged_at: datetime | None = None

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
        now = self._clock()
        cutoff = now - timedelta(hours=self.retention_hours)
        hold = self._hold_reason()
        segments = self.segments()
        held: list[Segment] = []
        for segment in list(segments):
            if segment.started_at < cutoff:
                if hold is not None and _is_unshipped(segment):
                    held.append(segment)
                    continue
                if self._remove(segment):
                    removed_age += 1
                    segments.remove(segment)
        # The floor takes what the cloud already has before what it does not:
        # a shipped segment is in R2 and a never-released one was never going
        # there, but an unshipped one is the only copy. Within each, oldest
        # first. The newest is never a candidate.
        while len(segments) > 1 and self.free_bytes() < self.min_free_bytes:
            victim = self._space_victim(segments)
            if not self._remove(victim):
                break
            segments.remove(victim)
            removed_space += 1
        kept = [segment for segment in held if segment in segments]
        self.held = len(kept)
        if kept:
            self._journal_hold(hold, kept, now)
        else:
            self._last_hold_log = None
        if removed_age or removed_space:
            LOGGER.info(
                "gate_audio_segments stage=pruned expired=%d for_space=%d held=%d "
                "remaining=%d free_mb=%d",
                removed_age, removed_space, self.held, len(segments),
                self.free_bytes() // (1024 * 1024),
            )
        return {
            "expired": removed_age,
            "for_space": removed_space,
            "held": self.held,
            "remaining": len(segments),
            "free_bytes": self.free_bytes(),
        }

    def _hold_reason(self) -> str | None:
        """Why unshipped segments are being kept past the horizon, or None. Never raises."""
        if self.hold_unshipped is None:
            return None
        try:
            reason = self.hold_unshipped()
        except Exception:
            return None
        if not reason:
            return None
        return "".join(
            character for character in str(reason) if character.isalnum() or character in "_-"
        )[:32] or None

    @staticmethod
    def _space_victim(segments: list[Segment]) -> Segment:
        candidates = segments[:-1]
        for segment in candidates:
            if not _is_unshipped(segment):
                return segment
        return candidates[0]

    def _journal_hold(self, reason: str | None, held: list[Segment], now: datetime) -> None:
        """Say what is being kept and why -- when it changes, and hourly regardless."""
        key = (reason or "unknown", len(held))
        if (
            key == self._last_hold_log
            and self._last_hold_logged_at is not None
            and now - self._last_hold_logged_at < timedelta(hours=1)
        ):
            return
        self._last_hold_log = key
        self._last_hold_logged_at = now
        oldest = min(held, key=lambda segment: segment.started_at)
        LOGGER.info(
            "gate_audio_segments stage=retention_extended reason=%s held=%d held_mb=%d "
            "oldest=%s horizon_h=%d free_mb=%d detail=unshipped_kept_until_disk_demands",
            reason, len(held), sum(segment.size for segment in held) // (1024 * 1024),
            oldest.started_at.isoformat(), self.retention_hours,
            self.free_bytes() // (1024 * 1024),
        )

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
            # A marker instead means R2 has it and this copy was being kept
            # only to be read locally. Reaching the horizon is what is supposed
            # to happen to it, so it goes without a word.
            segment.path.with_suffix(SHIPPED_SUFFIX).unlink(missing_ok=True)
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


def _is_unshipped(segment: Segment) -> bool:
    """Released to the uploader and not yet confirmed by the cloud."""
    return segment.path.with_suffix(SIDECAR_SUFFIX).exists()


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

    A segment already carrying a sidecar is left alone: the uploader takes the
    sidecar when the cloud confirms the pair, so writing another would mean
    re-uploading bytes R2 already has. So is one carrying a ``.shipped``
    marker, which is that same statement about a segment whose audio is kept
    on the card afterwards for the window cutter and the labeller to read.
    """
    segments = store.segments()
    if len(segments) < 2:
        return 0
    written = 0
    for segment in segments[:-1]:
        sidecar_path = segment.path.with_suffix(SIDECAR_SUFFIX)
        if sidecar_path.exists():
            continue
        if segment.path.with_suffix(SHIPPED_SUFFIX).exists():
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


def describe_status(source_url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS,
                    connect=socket.create_connection) -> int | None:
    """The status MediaMTX gives a DESCRIBE of this stream, or None.

    This is the first thing ffmpeg would send, asked without the ffmpeg. A
    path whose camera source is down answers 404, and the old recorder found
    that out by spawning a child, reading its stderr and then sleeping for up
    to a minute. None means nothing answered at all -- MediaMTX is itself
    restarting -- which is not a reason to hold a spawn back: ffmpeg's own
    error is worth more than a guess.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(source_url)
    request = (
        f"DESCRIBE {source_url} RTSP/1.0\r\nCSeq: 1\r\n"
        "Accept: application/sdp\r\nUser-Agent: gate-audio-segments\r\n\r\n"
    ).encode("ascii", "replace")
    try:
        with connect((parts.hostname, parts.port or 554), timeout=timeout) as channel:
            channel.settimeout(timeout)
            channel.sendall(request)
            reply = channel.recv(256)
    except (OSError, ValueError):
        return None
    match = re.match(rb"RTSP/1\.\d (\d{3})", reply or b"")
    return int(match.group(1)) if match else None


class GapLedger:
    """When the recorder was not listening, and why, as lines on the card.

    A file rather than a table because the recorder has no business opening
    the controller's database (see ``sidecar_for``), and because the things
    that read it -- the sound scanner, a person with ``cat`` -- run as other
    processes. Append-only, one fsync a gap, and a gap is a few times a day.
    """

    def __init__(self, directory: Path, *, keep_hours: int = GAP_LEDGER_KEEP_HOURS):
        self.path = Path(directory) / GAP_LEDGER_NAME
        self.keep_hours = keep_hours

    def append(self, start: datetime, end: datetime, cause: str, detail: str = "") -> dict | None:
        seconds = (end - start).total_seconds()
        if seconds < MIN_GAP_SECONDS:
            return None
        row = {
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "seconds": round(seconds, 1),
            "cause": cause,
            "detail": (detail or "")[:200],
        }
        line = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return row

    def read(self, *, since: datetime | None = None) -> list[dict]:
        """Every gap still ending after ``since``, oldest first.

        A line that does not parse is skipped, not fatal: the last line of a
        file being appended to by another process may be half written.
        """
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        found = []
        for line in lines:
            try:
                row = json.loads(line)
                start = datetime.fromisoformat(row["start"])
                end = datetime.fromisoformat(row["end"])
            except (ValueError, KeyError, TypeError):
                continue
            if since is not None and end < since:
                continue
            found.append({
                "start": start, "end": end,
                "seconds": (end - start).total_seconds(),
                "cause": str(row.get("cause") or "unknown"),
                "detail": str(row.get("detail") or ""),
            })
        found.sort(key=lambda row: row["start"])
        return found

    def prune(self, now: datetime) -> int:
        """Drop what is older than anything that could still want it."""
        cutoff = now - timedelta(hours=self.keep_hours)
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        kept = []
        for line in lines:
            try:
                if datetime.fromisoformat(json.loads(line)["end"]) >= cutoff:
                    kept.append(line)
            except (ValueError, KeyError, TypeError):
                continue
        dropped = len(lines) - len(kept)
        if dropped:
            _write_private(self.path, ("".join(item + "\n" for item in kept)).encode("utf-8"))
        return dropped


def segment_coverage(store: "SegmentStore", names=None) -> list[dict]:
    """How much of the span each finished segment covers is actually audio.

    The span is from one segment's start to the next one's, so it needs no
    knowledge of why a segment is short: a restart, a minute of backoff and a
    camera that sent half its frames all show up as audio that is not there.
    The newest segment has no successor and is still being written, so it is
    never measured.

    ``names`` restricts the (expensive) frame walk to those segments; the
    spans still come from the whole listing.
    """
    segments = store.segments()
    wanted = None if names is None else set(names)
    rows = []
    for segment, following in zip(segments, segments[1:]):
        if wanted is not None and segment.path.name not in wanted:
            continue
        span = (following.started_at - segment.started_at).total_seconds()
        audio = segment.duration()
        rows.append({
            "segment": segment.path.name,
            "started_at": segment.started_at,
            "ended_at": following.started_at,
            "span_seconds": span,
            "audio_seconds": audio,
            "missing_seconds": max(0.0, span - audio),
        })
    return rows


def listening_gaps(coverage: list[dict], recorded: list[dict]) -> list[dict]:
    """The gaps inside these segments' spans, each with the best cause known.

    What the recorder wrote down itself comes first: those have exact times
    and a reason. Whatever a segment is still missing beyond them is a
    shortfall in the stream -- real, measured, and not placeable within the
    segment, so it is reported against the whole span rather than pinned to
    its end, which is where a naive reading of the file would put it.
    """
    gaps = []
    for row in coverage:
        explained = 0.0
        for gap in recorded:
            overlap = (min(gap["end"], row["ended_at"])
                       - max(gap["start"], row["started_at"])).total_seconds()
            if overlap <= 0:
                continue
            explained += overlap
            if row["started_at"] <= gap["start"] < row["ended_at"]:
                gaps.append({
                    "started_at": gap["start"], "ended_at": gap["end"],
                    "missing_seconds": gap["seconds"],
                    "cause": gap["cause"], "detail": gap["detail"],
                })
        shortfall = row["missing_seconds"] - explained
        if shortfall >= SHORTFALL_SECONDS:
            gaps.append({
                "started_at": row["started_at"], "ended_at": row["ended_at"],
                "missing_seconds": shortfall,
                "cause": GAP_SHORTFALL,
                "detail": "audio short of the wall clock; position within the segment unknown",
            })
    gaps.sort(key=lambda gap: gap["started_at"])
    return gaps


class SegmentRecorder:
    """One long-lived ffmpeg writing segments, restarted when it dies."""

    def __init__(self, store: SegmentStore, *, source_url: str = DEFAULT_SOURCE,
                 segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
                 ffmpeg: str = FFMPEG_BINARY, popen=subprocess.Popen,
                 prune_every_seconds: float = 300.0, monotonic=None, sleep=None,
                 waiter=None, keep_everything: bool = False, probe=None, clock=None):
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
        self.ledger = GapLedger(store.directory)
        self._popen = popen
        self._probe = probe or describe_status
        self._prune_every = prune_every_seconds
        self._last_housekeeping = None
        self._restarts = 0
        self._starts = 0
        self._refusals = 0
        self._stalls = 0
        self._probe_refusals = 0
        self._gaps = 0
        self._gap_seconds = 0.0
        self._last_gap = None
        self._open_gap = None
        self._barren = 0
        self._last_error = None
        from time import monotonic as _monotonic, sleep as _sleep
        self._monotonic = monotonic or _monotonic
        self._sleep = sleep or _sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Every wait in this class goes through one seam, so a test can drive
        # the restart path without spending the backoff in real seconds.
        self._waiter = waiter or (lambda event, seconds: event.wait(seconds))

    # -- the loop ---------------------------------------------------------

    def run_forever(self, stop_event) -> None:
        self.store.prepare()
        self._note_startup_gap()
        while not stop_event.is_set():
            if self.store.free_bytes() < self.store.min_free_bytes:
                # Make room before refusing: the pruner may simply not have
                # run yet, and at startup it has not.
                self._housekeeping(force=True)
            if self.store.free_bytes() < self.store.min_free_bytes:
                # The pruner has already taken what it can. Recording into the
                # last of the card is not a trade worth making for audio.
                self._refusals += 1
                self._begin_gap(GAP_LOW_DISK, replace=(GAP_ENDED,))
                LOGGER.warning(
                    "gate_audio_segments stage=refused reason=low_disk free_mb=%d floor_mb=%d",
                    self.store.free_bytes() // (1024 * 1024),
                    self.store.min_free_bytes // (1024 * 1024),
                )
                self._waiter(stop_event, MAX_RESTART_SECONDS)
                continue
            if not self._stream_is_there():
                # A camera that is off for a day must not stop finished
                # segments being released or the card being pruned.
                self._housekeeping()
                self._waiter(stop_event, PROBE_SECONDS)
                continue
            recorded = self._run_once(stop_event)
            if stop_event.is_set():
                break
            self._housekeeping()
            # A child that recorded and then stopped lost its stream, and the
            # only useful thing is to be back the moment the stream is. One
            # that never wrote a byte will very likely do the same again, and
            # that is the case a backoff exists for. How many children there
            # have been in this process's life says nothing about either.
            self._barren = 0 if recorded else self._barren + 1
            self._waiter(stop_event, RETRY_DELAYS[min(self._barren, len(RETRY_DELAYS) - 1)])

    def _stream_is_there(self) -> bool:
        """False only when MediaMTX says outright that it has no stream."""
        try:
            status = self._probe(self.source_url)
        except Exception:
            return True
        if status != 404:
            return True
        self._probe_refusals += 1
        if self._open_gap is None or self._open_gap["cause"] == GAP_ENDED:
            LOGGER.warning("gate_audio_segments stage=waiting reason=no_stream status=404 source=%s",
                           self.source_url)
        self._begin_gap(GAP_SOURCE, "DESCRIBE 404", replace=(GAP_ENDED,))
        return False

    def _run_once(self, stop_event) -> bool:
        """Run one child until it stops. True if it recorded anything."""
        try:
            process = self._popen(
                self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, env=self.child_environment, close_fds=True,
            )
        except Exception as error:
            self._restarts += 1
            self._last_error = str(error)
            self._begin_gap(GAP_SPAWN, str(error))
            LOGGER.warning("gate_audio_segments stage=spawn_failed error=%s", error)
            return False
        self._starts += 1
        LOGGER.info("gate_audio_segments stage=recording source=%s segment_seconds=%d",
                    self.source_url, self.segment_seconds)
        seen = self._written()
        recorded = False
        last_growth = self._monotonic()
        last_audio_at = None
        stalled = False
        try:
            while not stop_event.is_set():
                if process.poll() is not None:
                    break
                self._waiter(stop_event, 1.0)
                written = self._written()
                now = self._monotonic()
                if written != seen:
                    seen = written
                    last_growth = now
                    last_audio_at = self._clock()
                    if not recorded:
                        recorded = True
                        self._end_gap(last_audio_at)
                elif now - last_growth >= STALL_SECONDS:
                    stalled = True
                    self._stalls += 1
                    LOGGER.warning("gate_audio_segments stage=stalled quiet_seconds=%d",
                                   int(now - last_growth))
                    break
                self._housekeeping()
                if recorded and self.store.free_bytes() < self.store.min_free_bytes:
                    break
        finally:
            tail = self._stop(process)
        if not recorded:
            # A child can write and exit between two looks.
            written = self._written()
            if written != seen:
                recorded = True
                last_audio_at = self._clock()
                self._end_gap(last_audio_at)
        if not stop_event.is_set():
            self._restarts += 1
            self._last_error = tail
            # From the last audio written, not from now: with every frame
            # flushed as it arrives that is when the listening stopped.
            self._begin_gap(GAP_STALLED if stalled else GAP_ENDED, tail,
                            at=last_audio_at if recorded else None)
            LOGGER.warning("gate_audio_segments stage=stopped restarts=%d recorded=%s stderr=%s",
                           self._restarts, str(recorded).lower(), tail)
        return recorded

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

    # -- what is on the card ----------------------------------------------

    def _written(self) -> tuple:
        """The newest segment's name and size: cheap, and it moves 15x a second."""
        newest, size = "", -1
        try:
            with os.scandir(self.store.directory) as entries:
                for entry in entries:
                    if entry.name > newest and SEGMENT_PATTERN.match(entry.name):
                        newest = entry.name
            if newest:
                size = os.stat(self.store.directory / newest).st_size
        except OSError:
            pass
        return newest, size

    def _housekeeping(self, *, force: bool = False) -> None:
        """Release finished segments and prune, on a timer of its own.

        This used to run only *between* children, so a recorder that stayed up
        did neither: on the gate, segments were released and pruned 49 at a
        time, once every four hours, whenever a TURN refresh happened to
        restart MediaMTX. A recorder that never restarted would never have
        pruned at all. It also ran before the first child was started, which
        put a walk of the card in front of the audio at every boot.
        """
        now = self._monotonic()
        if self._last_housekeeping is None:
            # Record first: the card can wait half a minute for its first walk.
            self._last_housekeeping = now - self._prune_every + min(30.0, self._prune_every)
        if not force and now - self._last_housekeeping < self._prune_every:
            return
        self._last_housekeeping = now
        if self.keep_everything:
            # Before pruning, not after: a segment that has just been
            # released is one the uploader can still save.
            try:
                write_ready_sidecars(self.store, source_url=self.source_url)
            except Exception:
                LOGGER.warning("gate_audio_segments stage=release_failed", exc_info=True)
        try:
            self.store.prune()
        except Exception:
            LOGGER.warning("gate_audio_segments stage=prune_failed", exc_info=True)
        try:
            self.ledger.prune(self._clock())
        except Exception:
            LOGGER.warning("gate_audio_segments stage=ledger_prune_failed", exc_info=True)

    # -- gaps ---------------------------------------------------------------

    def _note_startup_gap(self) -> None:
        """Open a gap from the last audio on the card to whenever we resume.

        The process that stopped could not write this down -- it was being
        stopped -- so the one that starts does, from the only evidence there
        is: where the newest segment's audio ends.
        """
        try:
            segments = self.store.segments()
            if not segments:
                return
            newest = segments[-1]
            ended = newest.started_at + timedelta(seconds=newest.duration())
            now = self._clock()
            if ended >= now:
                return
            away = (now - ended).total_seconds()
            self._begin_gap(GAP_RESTART if away < 300 else GAP_RECORDER_OFF,
                            f"last audio in {newest.path.name}", at=ended)
        except Exception:
            LOGGER.warning("gate_audio_segments stage=startup_gap_failed", exc_info=True)

    def _begin_gap(self, cause: str, detail: str = "", *, at=None, replace=()) -> None:
        if self._open_gap is not None:
            # The first cause stands, except where a better one is now known:
            # "the child stopped" becomes "MediaMTX had no stream".
            if self._open_gap["cause"] in replace:
                self._open_gap["cause"] = cause
                self._open_gap["detail"] = detail or self._open_gap["detail"]
            return
        self._open_gap = {"start": at or self._clock(), "cause": cause, "detail": detail or ""}

    def _end_gap(self, at: datetime) -> None:
        gap, self._open_gap = self._open_gap, None
        if gap is None:
            return
        try:
            row = self.ledger.append(gap["start"], at, gap["cause"], gap["detail"])
        except Exception:
            LOGGER.warning("gate_audio_segments stage=gap_write_failed", exc_info=True)
            return
        if row is None:
            return
        self._gaps += 1
        self._gap_seconds += row["seconds"]
        self._last_gap = row
        LOGGER.warning(
            "gate_audio_segments stage=resumed not_listening_from=%s to=%s seconds=%.1f cause=%s",
            row["start"], row["end"], row["seconds"], row["cause"],
        )

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
            "stalls": self._stalls,
            "no_stream_probes": self._probe_refusals,
            "gaps": self._gaps,
            "gap_seconds": round(self._gap_seconds, 1),
            "last_gap": self._last_gap,
            "listening": self._open_gap is None and self._starts > 0,
            "keep_everything": self.keep_everything,
            "released": sum(1 for segment in self.store.segments()
                            if segment.path.with_suffix(SIDECAR_SUFFIX).exists()),
            "low_disk_refusals": self._refusals,
            "held_unshipped": self.store.held,
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
