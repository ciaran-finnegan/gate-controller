"""The clear stream as a frame source that decodes only when asked.

``ClearStreamSource`` replaces the continuously decoding keyframe ring. At
idle it records the compressed stream into an ``HevcPacketRing`` (about 5%
of a core) and decodes one keyframe every ``baseline_seconds`` to keep the
scene baseline current. When a camera event arrives it:

1. decodes the newest buffered keyframe on demand (~0.5 s including ffmpeg
   start-up, still far ahead of a fresh RTSP grab), and
2. starts a live session decoder at ``session_fps`` for up to
   ``session_seconds`` so the capture loop can pick the stillest frame of
   each second instead of hoping a fixed offset lands on a stopped car.

The session decoder is fed by a packet copy of the stream rather than
pointed at RTSP, so it never sees a picture whose reference frames it
missed. Decoding straight from RTSP does: the media server hands a new
reader the middle of a GOP and the SDP already carries the codec parameters,
so ffmpeg decodes those inter pictures against a reference it never
received. Under hardware decode that reference is an uninitialised buffer,
and the frame comes out flat green. ``-c:v copy`` starts the copy at a
keyframe by itself; ``IrapStartGate`` in front of the decoder is the
assertion that it did, journaled as ``session.keyframe_start``.

Measured on the RLC-810A stream with hardware decode, crop and scale: a
5 fps session costs about 60% of one core while it runs; 10 fps about 116%.
The packet copy in front of it adds about 5% of a core.
"""
from collections.abc import Callable
import logging
import subprocess
from threading import Event, Lock, Thread
from time import monotonic

from .clear_stream import (
    HevcPacketRing, IrapStartGate, decode_command, decode_frames, record_command,
)
from .hot_stream import FFMPEG_BINARY, HotFrameRing, JpegStreamParser
from .scene import SceneBaseline, frame_thumbnail, thumbnail_difference

LOGGER = logging.getLogger(__name__)

DEFAULT_SESSION_FPS = 5.0
DEFAULT_SESSION_SECONDS = 45.0
DEFAULT_BASELINE_SECONDS = 30.0
# The clear stream's own frame rate, which must match the camera's main
# stream. An Annex-B pipe carries no timestamps, so the session decoder has
# to be told the rate it is being fed at: ``fps=N`` against a stated rate R
# keeps N/R of the pictures, whatever they really are. Nothing anywhere
# checks the answer, so a camera reconfigured to 15 fps and left stated at
# 10 would run a "5 fps" session at 7.5 (5 x 15/10) and cost half again the
# core it was measured at, while one dropped to 5 fps would run it at 2.5.
# It is settable as GATE_CLEAR_STREAM_SOURCE_FPS for exactly that reason.
DEFAULT_SOURCE_FPS = 10.0
SESSION_RING_FRAMES = 12
KEYFRAME_DECODE_TIMEOUT = 3.0
SESSION_CHUNK_BYTES = 64 * 1024
# Enough of the decoder's complaint to name it in the stop line. A decoder
# that rejects the pipe says so in one line and then exits.
SESSION_STDERR_TAIL = 300


class ClearStreamSource:
    """Record compressed video continuously; decode keyframes and sessions on demand."""

    def __init__(self, source_url: str, *, decoder_arguments: tuple[str, ...] = (),
                 filters: tuple[str, ...] = (), max_frame_bytes: int = 8 * 1024 * 1024,
                 session_fps: float = DEFAULT_SESSION_FPS,
                 session_seconds: float = DEFAULT_SESSION_SECONDS,
                 baseline_seconds: float = DEFAULT_BASELINE_SECONDS,
                 source_fps: float = DEFAULT_SOURCE_FPS,
                 popen=subprocess.Popen, clock: Callable[[], float] = monotonic,
                 ring: HevcPacketRing | None = None, scene: SceneBaseline | None = None):
        if not (0 < session_fps <= 10):
            raise ValueError("session_fps must be between 0 and 10")
        if not (0 < session_seconds <= 300):
            raise ValueError("session_seconds must be between 0 and 300")
        if not (0 < source_fps <= 60):
            raise ValueError("source_fps must be between 0 and 60")
        self.source_url = source_url
        self._decoder_arguments = tuple(decoder_arguments)
        self._filters = tuple(filters)
        self._max_frame_bytes = max_frame_bytes
        self.session_fps = session_fps
        self.session_seconds = session_seconds
        self.baseline_seconds = baseline_seconds
        self.source_fps = source_fps
        self._popen = popen
        self._clock = clock
        self.ring = ring or HevcPacketRing(clock=clock)
        self.scene = scene or SceneBaseline(clock=clock)
        self.command = record_command(source_url, FFMPEG_BINARY)
        self.child_environment = {"LANG": "C", "LC_ALL": "C"}
        self._process = None
        self._restart_count = 0
        self._decode_count = 0
        self._decode_failures = 0
        self._session_lock = Lock()
        self._session_ring: HotFrameRing | None = None
        self._session_process = None
        self._session_source_process = None
        self._session_thread: Thread | None = None
        self._session_stop = Event()
        self._session_started_at: float | None = None
        self._session_frames = 0
        self._session_gate_open = False
        self._session_stderr = ""
        self._last_baseline_at: float | None = None
        self._closed = False

    # -- recording loop -----------------------------------------------------
    def run_forever(self, stop_event) -> None:
        while not stop_event.is_set():
            try:
                process = self._popen(
                    self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=self.child_environment, close_fds=True,
                )
                self._process = process
                while not stop_event.is_set():
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self.ring.feed(chunk)
                    self._maybe_refresh_baseline()
            except (OSError, ValueError):
                LOGGER.warning("gate_clear_stream outcome=restart reason=stream_error")
            finally:
                self._stop_child()
            if not stop_event.is_set():
                self._restart_count += 1
                stop_event.wait(1.0)

    def _maybe_refresh_baseline(self) -> None:
        now = self._clock()
        if self._last_baseline_at is not None and now - self._last_baseline_at < self.baseline_seconds:
            return
        if self.session_active() or self.ring.latest_keyframe() is None:
            return
        self._last_baseline_at = now
        frame = self.decode_latest_keyframe()
        if frame is not None:
            self.scene.observe(frame[0], now)

    # -- on-demand keyframe ------------------------------------------------
    def decode_latest_keyframe(self) -> tuple[bytes, float] | None:
        """Decode the newest buffered keyframe now. Returns (jpeg, captured_at)."""
        latest = self.ring.latest_keyframe()
        if latest is None:
            return None
        data, captured_at = latest
        command = decode_command(
            ffmpeg=FFMPEG_BINARY, decoder_arguments=self._decoder_arguments,
            filters=self._filters, frames=1,
        )
        frames = decode_frames(
            data, command, popen=self._popen, timeout=KEYFRAME_DECODE_TIMEOUT,
            max_frame_bytes=self._max_frame_bytes,
        )
        if not frames:
            self._decode_failures += 1
            return None
        self._decode_count += 1
        return frames[-1], captured_at

    # -- frame source protocol used by TriggerFrameCapture -----------------
    def note_activity(self) -> None:
        self.scene.note_activity(self._clock())

    def scene_difference(self, frame: bytes) -> float | None:
        return self.scene.difference(frame)

    def latest(self, *, after: float | None = None) -> tuple[bytes, float] | None:
        """A live session frame newer than ``after``; otherwise the newest keyframe."""
        with self._session_lock:
            ring = self._session_ring
        if ring is not None:
            live = ring.latest(now=self._clock(), max_age=2.0 / self.session_fps + 0.5, after=after)
            if live is not None:
                return live
            # The live decoder takes about a second to deliver its first
            # frame; until then the buffered keyframe is the instant answer.
        keyframe = self.decode_latest_keyframe()
        if keyframe is None:
            return None
        if after is not None and keyframe[1] <= after:
            return None
        return keyframe

    def stillest(self, *, after: float | None = None,
                 window_seconds: float = 1.0) -> tuple[bytes, float, float | None] | None:
        """The stillest live frame of the last ``window_seconds``.

        Among session frames newer than ``after``, return the one that differs
        least from its predecessor, with its capture time and that difference
        (None when only one frame exists). A stopped vehicle produces a run of
        near-identical frames; a moving one does not. Falls back to the
        newest frame when there is no session.
        """
        with self._session_lock:
            ring = self._session_ring
        if ring is None:
            latest = self.latest(after=after)
            return None if latest is None else (latest[0], latest[1], None)
        now = self._clock()
        candidates = ring.since(after, now=now, max_age=window_seconds + 1.0 / self.session_fps)
        if not candidates:
            keyframe = self.latest(after=after)
            return None if keyframe is None else (keyframe[0], keyframe[1], None)
        if len(candidates) == 1:
            frame, captured_at = candidates[0]
            return frame, captured_at, None
        previous = frame_thumbnail(candidates[0][0])
        best = None
        for frame, captured_at in candidates[1:]:
            current = frame_thumbnail(frame)
            if previous is not None and current is not None:
                stillness = thumbnail_difference(previous, current)
                if best is None or stillness <= best[2]:
                    best = (frame, captured_at, stillness)
            previous = current
        if best is None:
            frame, captured_at = candidates[-1]
            return frame, captured_at, None
        return best

    # -- live session decoder ---------------------------------------------
    def session_active(self) -> bool:
        with self._session_lock:
            return self._session_ring is not None

    def start_session(self) -> bool:
        """Start decoding the live stream at session_fps for session_seconds."""
        with self._session_lock:
            if self._closed or self._session_ring is not None:
                return False
            self._session_ring = HotFrameRing(
                max_frames=SESSION_RING_FRAMES, max_frame_bytes=self._max_frame_bytes,
                max_total_bytes=self._max_frame_bytes * SESSION_RING_FRAMES,
            )
            self._session_stop = Event()
            self._session_started_at = self._clock()
            self._session_frames = 0
            self._session_gate_open = False
            self._session_stderr = ""
            stop = self._session_stop
            ring = self._session_ring
        thread = Thread(target=self._run_session, args=(ring, stop), name="gate-clear-session", daemon=True)
        with self._session_lock:
            self._session_thread = thread
        thread.start()
        LOGGER.info("gate_clear_stream session=started fps=%g seconds=%g", self.session_fps, self.session_seconds)
        return True

    def stop_session(self, reason: str = "ended") -> None:
        with self._session_lock:
            stop = self._session_stop
            processes = (self._session_source_process, self._session_process)
            ring = self._session_ring
            frames = self._session_frames
            keyframe_start = self._session_gate_open
            decoder_error = self._session_stderr
            self._session_ring = None
            self._session_process = None
            self._session_source_process = None
        stop.set()
        for process in processes:
            if process is not None:
                _terminate(process)
        if ring is not None:
            # A session that never reached a keyframe produced nothing the
            # decoder could turn into a picture, so it is a fault however
            # tidily it ended - a decoder that rejected the pipe outright
            # otherwise reads as an ordinary `stream_ended`.
            LOGGER.log(
                logging.INFO if keyframe_start else logging.WARNING,
                "gate_clear_stream session=stopped reason=%s frames=%d keyframe_start=%s "
                "decoder_error=%s",
                reason, frames, keyframe_start, decoder_error or "none",
            )

    def _session_commands(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The packet copy that feeds the session, and the decoder behind it."""
        # Sample before the hardware download so a dropped frame never costs
        # the 4K copy out of the decoder, let alone a crop or scale.
        filters = (f"fps={self.session_fps:g}",) + tuple(
            f for f in self._filters if not f.startswith("fps=")
        )
        return (
            record_command(self.source_url, FFMPEG_BINARY, duration=self.session_seconds),
            decode_command(
                ffmpeg=FFMPEG_BINARY, decoder_arguments=self._decoder_arguments,
                filters=filters, input_framerate=self.source_fps,
            ),
        )

    def _run_session(self, ring: HotFrameRing, stop: Event) -> None:
        source_command, decode = self._session_commands()
        try:
            source = self._popen(
                source_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.child_environment, close_fds=True,
            )
        except (OSError, ValueError):
            LOGGER.warning("gate_clear_stream session=failed reason=spawn stage=source")
            self.stop_session("spawn_failed")
            return
        try:
            # The decoder's stderr is read, not discarded: this is the only
            # place that says why a decoder rejected the pipe, and without it
            # that failure is indistinguishable from an ordinary end of stream.
            decoder = self._popen(
                decode, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.child_environment, close_fds=True,
            )
        except (OSError, ValueError):
            _terminate(source)
            LOGGER.warning("gate_clear_stream session=failed reason=spawn stage=decoder")
            self.stop_session("spawn_failed")
            return
        with self._session_lock:
            if self._session_ring is not ring:
                _terminate(source)
                _terminate(decoder)
                return
            self._session_source_process = source
            self._session_process = decoder
        feeder = Thread(
            target=self._feed_session, args=(source, decoder, stop),
            name="gate-clear-session-feed", daemon=True,
        )
        feeder.start()
        Thread(
            target=self._collect_session_errors, args=(decoder,),
            name="gate-clear-session-stderr", daemon=True,
        ).start()
        parser = JpegStreamParser(self._max_frame_bytes)
        try:
            while not stop.is_set():
                chunk = decoder.stdout.read(SESSION_CHUNK_BYTES)
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    ring.add(frame, captured_at=self._clock())
                    self._session_frames += 1
        except (OSError, ValueError):
            pass
        finally:
            _terminate(source)
            _terminate(decoder)
            with self._session_lock:
                still_current = self._session_ring is ring
            if still_current and not stop.is_set():
                self.stop_session("stream_ended")

    def _feed_session(self, source, decoder, stop: Event) -> None:
        """Copy packets into the decoder, starting at the first keyframe.

        ``-c:v copy`` already drops the leading non-keyframe packets, so the
        gate normally opens on the copy's very first bytes and spends the
        rest of the session handing chunks straight through. What it adds is
        the assertion, journaled as ``session.keyframe_start``: if the
        decoder is ever fed the middle of a GOP again it produces a flat
        frame that passes every downstream check and costs a plate lookup.
        """
        gate = IrapStartGate()
        try:
            while not stop.is_set():
                chunk = source.stdout.read(SESSION_CHUNK_BYTES)
                if not chunk:
                    break
                payload = gate.feed(chunk)
                if not payload:
                    continue
                if not stop.is_set():
                    with self._session_lock:
                        self._session_gate_open = True
                decoder.stdin.write(payload)
                decoder.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                decoder.stdin.close()
            except (OSError, ValueError):
                pass
            _terminate(source)

    def _collect_session_errors(self, decoder) -> None:
        """Keep the tail of the decoder's stderr for the session's stop line."""
        stderr = getattr(decoder, "stderr", None)
        if stderr is None:
            return
        try:
            for line in iter(stderr.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                with self._session_lock:
                    joined = f"{self._session_stderr} {text}".strip()
                    self._session_stderr = joined[-SESSION_STDERR_TAIL:]
        except (OSError, ValueError):
            pass
        finally:
            try:
                stderr.close()
            except (OSError, ValueError):
                pass

    # -- status / shutdown ------------------------------------------------
    def status(self) -> dict:
        now = self._clock()
        with self._session_lock:
            active = self._session_ring is not None
            started = self._session_started_at
            keyframe_start = self._session_gate_open
        return {
            "enabled": True,
            "stream": "clear",
            "mode": "compressed_ring",
            "ring": self.ring.status(now),
            "restart_count": self._restart_count,
            "decodes": self._decode_count,
            "decode_failures": self._decode_failures,
            "session": {
                "active": active,
                "fps": self.session_fps,
                "seconds": self.session_seconds,
                "age_seconds": None if not active or started is None else round(now - started, 1),
                "frames": self._session_frames,
                # False while the session is still waiting for the keyframe
                # that lets its decoder produce a picture at all.
                "keyframe_start": keyframe_start,
            },
            "scene": self.scene.status(now),
        }

    def close(self) -> None:
        with self._session_lock:
            self._closed = True
        self.stop_session("closing")
        self._stop_child()

    def _stop_child(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            _terminate(process)


def _terminate(process) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    except Exception:
        pass
