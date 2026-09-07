"""Keep the clear stream's compressed video in memory; decode only on demand.

Continuously decoding the 4K HEVC clear stream cost most of one Pi 5 core in
software and still ~11% with the hardware block, all to have a frame ready
that, by construction, predates the alarm. This module keeps the last few
seconds of the *compressed* stream instead: ffmpeg copies packets without
decoding (about 5% of a core, ~1.2 MB/s), a small parser splits the Annex-B
byte stream into NAL units and groups them into GOPs, and a decoder is run
only when a camera event needs frames. Measured on the RLC-810A stream: the
newest keyframe decodes, crops, scales and encodes in about 150-250 ms.

Only the NAL headers are interpreted; slice data is treated as opaque bytes.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import subprocess
from threading import Lock
from time import monotonic

LOGGER = logging.getLogger(__name__)

START_CODE = b"\x00\x00\x01"
NAL_VPS, NAL_SPS, NAL_PPS, NAL_AUD = 32, 33, 34, 35
IRAP_TYPES = frozenset(range(16, 22))  # BLA_W_LP .. CRA_NUT (and reserved IRAP 22-23 excluded)
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_MAX_GOPS = 8
MAX_NAL_BYTES = 4 * 1024 * 1024
# The start gate below only ever inspects the bytes ahead of the first
# keyframe, so this bounds that prefix rather than a whole picture. A
# parameter set runs to a few hundred bytes; a unit claiming to be a longer
# one is not one, and the gate stops holding on to it.
MAX_START_PREFIX_BYTES = 1024 * 1024


def nal_type(nal: bytes) -> int | None:
    """The HEVC NAL unit type from its two-byte header, or None if too short."""
    if len(nal) < 2:
        return None
    return (nal[0] >> 1) & 0x3F


def _first_slice_in_picture(nal: bytes) -> bool:
    # The slice segment header starts right after the two-byte NAL header and
    # its first bit is first_slice_segment_in_pic_flag.
    return len(nal) >= 3 and bool(nal[2] & 0x80)


@dataclass
class _Gop:
    parameter_sets: bytes
    frames: list[bytes] = field(default_factory=list)
    frame_times: list[float] = field(default_factory=list)
    started_at: float = 0.0

    @property
    def size(self) -> int:
        return len(self.parameter_sets) + sum(len(frame) for frame in self.frames)


class AnnexBSplitter:
    """Split an Annex-B byte stream into complete NAL units (without start codes)."""

    def __init__(self, max_nal_bytes: int = MAX_NAL_BYTES):
        self._buffer = bytearray()
        self._max_nal_bytes = max_nal_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        units = []
        while True:
            start = self._buffer.find(START_CODE)
            if start < 0:
                # No start code at all: keep only a tail that could hold a partial one.
                if len(self._buffer) > 2:
                    del self._buffer[:-2]
                break
            nxt = self._buffer.find(START_CODE, start + 3)
            if nxt < 0:
                if len(self._buffer) - start > self._max_nal_bytes:
                    # Runaway unit: drop it rather than grow without bound.
                    del self._buffer[:start + 3]
                    continue
                if start > 0:
                    del self._buffer[:start]
                break
            end = nxt
            # A four-byte start code carries a leading zero that belongs to the next unit.
            if end > start + 3 and self._buffer[end - 1] == 0:
                end -= 1
            unit = bytes(self._buffer[start + 3:end])
            del self._buffer[:nxt]
            if unit:
                units.append(unit)
        return units


class IrapStartGate:
    """Hold an Annex-B stream shut until its first IRAP, then get out of the way.

    A decoder handed the middle of a GOP has no reference pictures for the
    inter frames that precede the next keyframe, and RTSP hands it the codec
    parameters out of band in the SDP, so it has everything it needs to
    *attempt* them. libavcodec substitutes a generated reference for the ones
    it never saw; in software that substitute is filled with mid-grey, but on
    the hardware decode path it is never filled at all, so every skipped
    block copies the decoder's zeroed buffer. The result is a flat green
    picture - RGB(0,135,0) is what an all-zero YUV frame renders as - with
    only the blocks that happened to be coded in that picture carrying real
    content. It is a valid JPEG, it differs wildly from the scene baseline,
    and it costs a paid plate lookup.

    Feeding the decoder a pipe instead of RTSP already fixes that on its own:
    ``ffmpeg -c:v copy`` drops leading non-keyframe packets unless asked for
    ``-copyinkf``, and the parser bundles the parameter sets into the IDR
    packet, so the copy's first bytes are ``VPS,SPS,PPS,IDR_W_RADL``. This
    gate is the assertion that this held, not the mechanism it relies on:
    everything before the first IRAP start code is dropped, the parameter
    sets seen along the way are emitted with it, and ``started`` is journaled
    so a session that never reached a keyframe says so.

    It scans only that prefix. Once the IRAP start code is found the rest of
    the buffer goes out as it stands - a part-arrived picture included - and
    every later chunk is returned untouched, so nothing here reframes the
    stream. Reframing was measurably worse than useless: a splitter can only
    release a NAL once the *next* start code arrives, and this camera codes
    two slices per picture, so access unit N would not close until N+1 landed
    about 100 ms later, on top of the 64 KB blocking read in front of it.
    ``captured_at`` is stamped when a frame is emitted, so that delay does
    not show up as latency anywhere - it silently subtracts 150-250 ms from
    every ``frame_age_ms`` the journal reports.
    """

    def __init__(self, max_prefix_bytes: int = MAX_START_PREFIX_BYTES):
        self._held = bytearray()
        self._parameter_sets: dict[int, bytes] = {}
        self._max_prefix_bytes = max_prefix_bytes
        self._started = False

    @property
    def started(self) -> bool:
        """Whether a keyframe has been seen and the stream is flowing."""
        return self._started

    def feed(self, chunk: bytes) -> bytes:
        """The part of ``chunk`` the decoder may safely see, possibly empty."""
        if self._started:
            return chunk  # the hot path: one flag, and the caller's own bytes
        held = self._held
        held += chunk
        position = 0
        keep = 0
        while True:
            start = held.find(START_CODE, position)
            if start < 0:
                # Nothing but a partial start code can still be pending.
                keep = max(position, len(held) - 2)
                break
            if start + 6 > len(held):
                # The two-byte NAL header and the slice header byte after it
                # are what the decision needs; wait for them.
                keep = start
                break
            kind = (held[start + 3] >> 1) & 0x3F
            if kind in (NAL_VPS, NAL_SPS, NAL_PPS):
                end = held.find(START_CODE, start + 3)
                if end < 0:
                    keep = start
                    break
                self._parameter_sets[kind] = bytes(held[start:end])
                position = end
                continue
            # first_slice_segment_in_pic_flag is the first bit after the NAL
            # header: a later slice of a picture whose start went past before
            # the reader connected is no starting point either. Nor is a
            # keyframe whose VPS/SPS/PPS were missed - it is no more decodable
            # on its own than the inter frames before it.
            if (
                kind in IRAP_TYPES
                and held[start + 5] & 0x80
                and len(self._parameter_sets) == 3
            ):
                self._started = True
                self._held = bytearray()
                return b"".join(
                    self._parameter_sets[parameter]
                    for parameter in (NAL_VPS, NAL_SPS, NAL_PPS)
                ) + bytes(held[start:])
            position = start + 3
        if keep:
            del held[:keep]
        if len(held) > self._max_prefix_bytes:
            # A parameter set longer than the prefix bound is not one. Drop
            # it rather than hold the whole pre-keyframe stream in memory.
            del held[:-2]
        return b""


class HevcPacketRing:
    """The last few seconds of the clear stream, compressed, grouped by GOP."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES, max_gops: int = DEFAULT_MAX_GOPS,
                 clock: Callable[[], float] = monotonic):
        if max_bytes <= 0 or max_gops <= 0:
            raise ValueError("ring bounds must be positive")
        self._max_bytes = max_bytes
        self._max_gops = max_gops
        self._clock = clock
        self._splitter = AnnexBSplitter()
        self._lock = Lock()
        self._gops: list[_Gop] = []
        self._pending_parameter_sets: dict[int, bytes] = {}
        self._current_frame = bytearray()
        self._current_frame_started: float | None = None
        self._frames_seen = 0
        self._dropped_gops = 0

    # -- ingest -------------------------------------------------------------
    def feed(self, chunk: bytes) -> int:
        """Ingest raw stream bytes; returns the number of frames completed."""
        completed = 0
        now = self._clock()
        with self._lock:
            for unit in self._splitter.feed(chunk):
                completed += self._ingest_unit(unit, now)
        return completed

    def _ingest_unit(self, unit: bytes, now: float) -> int:
        kind = nal_type(unit)
        if kind is None:
            return 0
        if kind in (NAL_VPS, NAL_SPS, NAL_PPS):
            self._pending_parameter_sets[kind] = START_CODE + unit
            return 0
        if kind == NAL_AUD or kind >= 36:
            return 0  # delimiters and SEI/reserved units carry nothing we need
        if kind > 31:
            return 0
        completed = 0
        if _first_slice_in_picture(unit):
            completed += self._finish_frame(now)
            if kind in IRAP_TYPES:
                self._start_gop(now)
            self._current_frame_started = now
        if self._current_frame_started is None:
            # Slice data before any picture start: nothing to attach it to.
            return completed
        self._current_frame += START_CODE + unit
        return completed

    def _start_gop(self, now: float) -> None:
        parameter_sets = b"".join(
            self._pending_parameter_sets.get(kind, b"") for kind in (NAL_VPS, NAL_SPS, NAL_PPS)
        )
        if not parameter_sets and self._gops:
            parameter_sets = self._gops[-1].parameter_sets
        self._gops.append(_Gop(parameter_sets=parameter_sets, started_at=now))
        self._trim()

    def _finish_frame(self, now: float) -> int:
        if not self._current_frame or self._current_frame_started is None:
            self._current_frame = bytearray()
            return 0
        if not self._gops:
            # A frame before the first keyframe cannot be decoded on its own.
            self._current_frame = bytearray()
            self._current_frame_started = None
            return 0
        gop = self._gops[-1]
        gop.frames.append(bytes(self._current_frame))
        gop.frame_times.append(self._current_frame_started)
        self._current_frame = bytearray()
        self._current_frame_started = None
        self._frames_seen += 1
        self._trim()
        return 1

    def _trim(self) -> None:
        while len(self._gops) > self._max_gops or (
            len(self._gops) > 1 and sum(gop.size for gop in self._gops) > self._max_bytes
        ):
            self._gops.pop(0)
            self._dropped_gops += 1

    # -- export -------------------------------------------------------------
    def latest_keyframe(self) -> tuple[bytes, float] | None:
        """Parameter sets plus the newest keyframe: the fastest possible decode."""
        with self._lock:
            for gop in reversed(self._gops):
                if gop.frames:
                    return gop.parameter_sets + gop.frames[0], gop.frame_times[0]
        return None

    def latest_gop(self) -> tuple[bytes, list[float]] | None:
        """Parameter sets plus every complete frame of the newest GOP."""
        with self._lock:
            for gop in reversed(self._gops):
                if gop.frames:
                    return gop.parameter_sets + b"".join(gop.frames), list(gop.frame_times)
        return None

    def status(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            newest = None
            for gop in reversed(self._gops):
                if gop.frame_times:
                    newest = gop.frame_times[-1]
                    break
            return {
                "gops": len(self._gops),
                "frames": sum(len(gop.frames) for gop in self._gops),
                "bytes": sum(gop.size for gop in self._gops),
                "newest_age_seconds": None if newest is None else max(0.0, round(now - newest, 2)),
                "frames_seen": self._frames_seen,
                "dropped_gops": self._dropped_gops,
            }


def record_command(source_url: str, ffmpeg: str = "ffmpeg",
                   duration: float | None = None) -> tuple[str, ...]:
    """ffmpeg command that copies the clear stream's packets to stdout, undecoded.

    ``duration`` bounds the copy in stream seconds, so a session that feeds a
    decoder from this stream closes its own RTSP connection when it is done.
    """
    return (
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-analyzeduration", "0", "-probesize", "32",
        "-i", source_url, "-map", "0:v:0", "-an",
        *(("-t", f"{duration:g}") if duration else ()),
        "-c:v", "copy", "-f", "hevc", "pipe:1",
    )


def decode_command(*, ffmpeg: str = "ffmpeg", decoder_arguments: tuple[str, ...] = (),
                   filters: tuple[str, ...] = (), frames: int | None = None,
                   input_framerate: float | None = None) -> tuple[str, ...]:
    """ffmpeg command that decodes an Annex-B HEVC byte stream from stdin to MJPEG on stdout.

    An Annex-B byte stream carries no timestamps, so the raw demuxer invents
    them at its own default of 25 fps. Anything that samples with an ``fps=``
    filter must pass ``input_framerate``, or the ratio between the filter and
    the real stream rate is wrong: at the clear stream's 10 fps, ``fps=5`` off
    a 25 fps assumption samples every fifth picture, that is 2 fps, not 5.

    Probing is skipped as it is on every other clear-stream command: the
    default probe costs about two seconds at 4K, buying nothing the pipe does
    not already state. ``-fpsprobesize 0`` goes with them - the frame rate is
    ``-r``'s to state, not the probe's to guess.
    """
    return (
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-analyzeduration", "0", "-probesize", "32", "-fpsprobesize", "0",
        *decoder_arguments,
        *(("-r", f"{input_framerate:g}") if input_framerate else ()),
        "-f", "hevc", "-i", "pipe:0", "-map", "0:v:0", "-an",
        *(("-frames:v", str(frames)) if frames else ()),
        *(("-vf", ",".join(filters)) if filters else ()),
        "-vsync", "0", "-q:v", "2", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
    )


def decode_frames(data: bytes, command: tuple[str, ...], *, popen=subprocess.Popen,
                  timeout: float = 3.0, max_frame_bytes: int = 8 * 1024 * 1024,
                  max_output_bytes: int = 64 * 1024 * 1024) -> list[bytes]:
    """Run the decoder over ``data`` and return the JPEG frames it produced.

    Bounded by ``timeout`` and ``max_output_bytes``; a stuck or runaway child
    is killed and whatever complete frames arrived are returned.
    """
    from .hot_stream import JpegStreamParser
    parser = JpegStreamParser(max_frame_bytes)
    frames: list[bytes] = []
    try:
        process = popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, ValueError):
        LOGGER.warning("gate_clear_stream decode=failed reason=spawn")
        return frames
    try:
        try:
            output, _ = process.communicate(input=data, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            LOGGER.warning("gate_clear_stream decode=timeout")
        if output:
            frames.extend(parser.feed(output[:max_output_bytes]))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
    return frames
