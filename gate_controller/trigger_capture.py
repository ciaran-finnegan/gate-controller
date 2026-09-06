"""Webhook-triggered clear-stream frame capture for early recognition.

An accepted camera webhook arrives well before the camera's FTP JPEG. This
module waits for the vehicle to stop at the closed gate, then grabs a short
series of full-resolution frames from the loopback MediaMTX clear path and
hands each to the normal burst pipeline, so a sharp plate at rest reaches OCR
without waiting for the upload or relying on a moving-vehicle snapshot. The webhook never authorises or actuates anything by itself:
the captured frame goes through the same recognition, authorisation, claim,
and relay code as an FTP upload. The FTP path is unchanged and remains the
fallback when capture fails.
"""

from __future__ import annotations

import logging
import os
import select
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic
from urllib.parse import urlsplit

from .hot_stream import (
    FFMPEG_BINARY, MAX_FRAME_BYTES, HotStreamBuffer, HotStreamConfig,
    _ensure_private_directory, _is_decodable_jpeg, write_private_frame,
)
from .images import measure_frame_quality
from .plate_region import PlateRegion, parse_plate_region
from .scene import SceneBaseline
from .telemetry import TriggerTelemetry


LOGGER = logging.getLogger(__name__)
LOOPBACK_CLEAR_STREAM = "rtsp://127.0.0.1:8554/clear"
MIN_CAPTURE_TIMEOUT_SECONDS = 0.5
MAX_CAPTURE_TIMEOUT_SECONDS = 4.0
MIN_CAPTURE_INTERVAL_SECONDS = 0.5
MAX_CAPTURE_INTERVAL_SECONDS = 30.0
MAX_CAPTURE_DELAY_SECONDS = 5.0
MAX_CAPTURE_COUNT = 3
MIN_CAPTURE_SPACING_SECONDS = 0.5
MAX_CAPTURE_SPACING_SECONDS = 3.0
SKIPPED_EVENT_TYPES = frozenset({"manual_test"})
# One keyframe per second at the camera's 1x interval; the ring only has to
# outlive the next keyframe plus its decode.
KEYFRAME_RING_FRAMES = 4
KEYFRAME_MAX_AGE_SECONDS = 1.6
# How the clear stream is held while idle: "compressed" keeps packets and
# decodes on demand; "decoded" is the older continuously decoding ring.
CLEAR_STREAM_MODES = frozenset({"compressed", "decoded"})
MIN_SESSION_FPS, MAX_SESSION_FPS = 1.0, 10.0
MIN_SESSION_SECONDS, MAX_SESSION_SECONDS = 5.0, 300.0
# Presence session: after the series, keep offering fresh frames while the
# vehicle is still at the gate and nothing has read its plate yet.
MAX_PRESENCE_WINDOW_SECONDS = 120.0
MIN_PRESENCE_SPACING_SECONDS = 1.0
MAX_PRESENCE_SPACING_SECONDS = 15.0
MAX_PRESENCE_FRAMES = 10
# Outcomes that mean the plate was never actually read, so another frame can
# still change the answer. A plate that was read but not authorised is final.
# Mean thumbnail difference from the idle scene below which a frame shows an
# empty drive. Empty-vs-empty drift over 30 s measures around 0.01; a vehicle
# in the plate band measures well above 0.08.
DEFAULT_EMPTY_SCENE_THRESHOLD = 0.03
MAX_EMPTY_SCENE_THRESHOLD = 0.5
PRESENCE_RETRY_REASONS = frozenset({
    "ocr_error", "ocr_busy", "decision_timeout", "stale_burst", "no_match",
    "processing_error", "queue_coalesced", "upload_incomplete",
})
# Hardware decode through the Pi 5's HEVC block via the DRM render node. The
# software path is the default so a host without the node keeps working.
HWACCEL_CHOICES = frozenset({"", "drm"})
DRM_RENDER_NODE = "/dev/dri/renderD128"
MIN_FRAME_WIDTH = 640
MAX_FRAME_WIDTH = 3840


@dataclass(frozen=True)
class TriggerCaptureConfig:
    enabled: bool
    output_directory: Path
    source_url: str = LOOPBACK_CLEAR_STREAM
    timeout_seconds: float = 2.5
    min_interval_seconds: float = 5.0
    max_frame_bytes: int = 8 * 1024 * 1024
    # Vehicles stop at the closed gate. Wait for that before the first grab,
    # then take a short series so at least one frame sees the plate at rest.
    delay_seconds: float = 1.5
    capture_count: int = 3
    spacing_seconds: float = 1.0
    # Keep the clear stream's keyframes decoded continuously so the first
    # frame of a series is ready the instant the webhook arrives, instead of
    # a fresh RTSP grab that costs about two seconds.
    hot_keyframes: bool = True
    # "drm" decodes through the Pi 5's hardware HEVC block instead of the CPU.
    hwaccel: str = ""
    # Scale decoded frames down to this width before the JPEG encode; 0 keeps
    # the native size. OCR uploads are downscaled to 1920 anyway, so encoding
    # 4K frames only costs CPU, heat, and ring memory.
    frame_width: int = 0
    # Crop to the band of the frame where plates appear, as frame fractions,
    # before any scaling. A crop narrower than frame_width is never upscaled.
    plate_region: PlateRegion | None = None
    # A vehicle that triggered the camera is still there after the series.
    # While nothing has read its plate, keep offering one fresh keyframe at a
    # time, spaced out, for a bounded window and a bounded number of frames.
    # This is what turns a five-second network blip into a delayed open
    # instead of a closed gate. 0 frames disables the session.
    presence_window_seconds: float = 20.0
    presence_spacing_seconds: float = 3.0
    presence_max_frames: int = 4
    # Hold the clear stream compressed and decode only for events (the
    # default), or keep the older continuously decoded keyframe ring.
    clear_stream_mode: str = "compressed"
    # Live decode rate and length of the per-event session in compressed mode.
    session_fps: float = 5.0
    session_seconds: float = 45.0
    # Skip frames that barely differ from the idle scene (no vehicle in the
    # plate band yet, or it has left). 0 disables the check.
    empty_scene_threshold: float = DEFAULT_EMPTY_SCENE_THRESHOLD
    # Skip frames whose bright pixels exceed this fraction (headlight or IR
    # blaze washing out the plate). 0 disables; the value is always journaled
    # so a threshold can be chosen from real captures.
    max_highlight_clipping: float = 0.0


def load_trigger_capture_config(
    environment, state_root: Path, *, webhook_enabled: bool,
) -> TriggerCaptureConfig:
    """Capture is on by default whenever the webhook listener is enabled.

    Frames are written under the controller's own state directory, never
    under the FTP upload tree: the installer re-owns that tree to the FTP
    user and adds group permissions, which would break the owner-only check
    on the next start.
    """
    enabled = _boolean(environment.get("GATE_TRIGGER_CAPTURE_ENABLED", "true"))
    configured_directory = environment.get("GATE_TRIGGER_CAPTURE_DIRECTORY")
    output_directory = (
        Path(configured_directory) if configured_directory
        else Path(state_root) / "trigger-capture"
    )
    if not output_directory.is_absolute():
        raise ValueError("GATE_TRIGGER_CAPTURE_DIRECTORY must be an absolute path")
    source_url = environment.get("GATE_TRIGGER_CAPTURE_SOURCE", LOOPBACK_CLEAR_STREAM)
    _validate_loopback_rtsp(source_url)
    timeout = _number(
        environment.get("GATE_TRIGGER_CAPTURE_TIMEOUT_SECONDS", "2.5"),
        MIN_CAPTURE_TIMEOUT_SECONDS, MAX_CAPTURE_TIMEOUT_SECONDS,
    )
    min_interval = _number(
        environment.get("GATE_TRIGGER_CAPTURE_MIN_INTERVAL_SECONDS", "5"),
        MIN_CAPTURE_INTERVAL_SECONDS, MAX_CAPTURE_INTERVAL_SECONDS,
    )
    delay = _number(
        environment.get("GATE_TRIGGER_CAPTURE_DELAY_SECONDS", "1.5"),
        0.0, MAX_CAPTURE_DELAY_SECONDS,
    )
    capture_count = _integer(
        environment.get("GATE_TRIGGER_CAPTURE_COUNT", "3"), 1, MAX_CAPTURE_COUNT,
    )
    spacing = _number(
        environment.get("GATE_TRIGGER_CAPTURE_SPACING_SECONDS", "1"),
        MIN_CAPTURE_SPACING_SECONDS, MAX_CAPTURE_SPACING_SECONDS,
    )
    max_frame_bytes = _integer(
        environment.get("GATE_TRIGGER_CAPTURE_MAX_FRAME_BYTES", str(8 * 1024 * 1024)),
        1, MAX_FRAME_BYTES,
    )
    hot_keyframes = _boolean(environment.get("GATE_TRIGGER_CAPTURE_HOT_KEYFRAMES", "true"))
    hwaccel = str(environment.get("GATE_TRIGGER_CAPTURE_HWACCEL", "")).strip().lower()
    if hwaccel not in HWACCEL_CHOICES:
        raise ValueError("GATE_TRIGGER_CAPTURE_HWACCEL must be empty or 'drm'")
    frame_width_raw = str(environment.get("GATE_TRIGGER_CAPTURE_FRAME_WIDTH", "0")).strip()
    frame_width = 0 if frame_width_raw in ("", "0") else _integer(
        frame_width_raw, MIN_FRAME_WIDTH, MAX_FRAME_WIDTH,
    )
    plate_region = parse_plate_region(environment.get("GATE_PLATE_REGION"))
    presence_window = _number(
        environment.get("GATE_PRESENCE_WINDOW_SECONDS", "20"), 0.0, MAX_PRESENCE_WINDOW_SECONDS,
    )
    presence_spacing = _number(
        environment.get("GATE_PRESENCE_SPACING_SECONDS", "3"),
        MIN_PRESENCE_SPACING_SECONDS, MAX_PRESENCE_SPACING_SECONDS,
    )
    presence_frames = _integer(
        environment.get("GATE_PRESENCE_MAX_FRAMES", "4"), 0, MAX_PRESENCE_FRAMES,
    )
    empty_scene = _number(
        environment.get("GATE_EMPTY_SCENE_THRESHOLD", str(DEFAULT_EMPTY_SCENE_THRESHOLD)),
        0.0, MAX_EMPTY_SCENE_THRESHOLD,
    )
    max_clipping = _number(environment.get("GATE_MAX_HIGHLIGHT_CLIPPING", "0"), 0.0, 1.0)
    clear_stream_mode = str(environment.get("GATE_CLEAR_STREAM_MODE", "compressed")).strip().lower()
    if clear_stream_mode not in CLEAR_STREAM_MODES:
        raise ValueError("GATE_CLEAR_STREAM_MODE must be 'compressed' or 'decoded'")
    session_fps = _number(environment.get("GATE_SESSION_FPS", "5"), MIN_SESSION_FPS, MAX_SESSION_FPS)
    session_seconds = _number(
        environment.get("GATE_SESSION_SECONDS", "45"), MIN_SESSION_SECONDS, MAX_SESSION_SECONDS,
    )
    return TriggerCaptureConfig(
        enabled=enabled and webhook_enabled,
        output_directory=output_directory,
        source_url=source_url,
        timeout_seconds=timeout,
        min_interval_seconds=min_interval,
        max_frame_bytes=max_frame_bytes,
        delay_seconds=delay,
        capture_count=capture_count,
        spacing_seconds=spacing,
        hot_keyframes=hot_keyframes,
        hwaccel=hwaccel,
        frame_width=frame_width,
        plate_region=plate_region,
        presence_window_seconds=presence_window,
        presence_spacing_seconds=presence_spacing,
        presence_max_frames=presence_frames,
        empty_scene_threshold=empty_scene,
        max_highlight_clipping=max_clipping,
        clear_stream_mode=clear_stream_mode,
        session_fps=session_fps,
        session_seconds=session_seconds,
    )


def decoder_input_arguments(config: TriggerCaptureConfig) -> tuple[str, ...]:
    """ffmpeg input options that select the decoder for the clear stream."""
    if config.hwaccel == "drm":
        return (
            "-hwaccel", "drm", "-hwaccel_device", DRM_RENDER_NODE,
            "-hwaccel_output_format", "drm_prime",
        )
    return ()


def decoder_filters(config: TriggerCaptureConfig, *, sample: bool) -> tuple[str, ...]:
    """The -vf chain: pull hardware frames back, sample, crop, then scale down.

    Sampling first means dropped keyframes are never cropped or scaled; the
    crop runs at native resolution so the plate keeps its detail; the scale
    only ever shrinks (``min(iw, width)``), so a narrow crop is not blown up.
    """
    filters = []
    if sample:
        # Before the hardware download: a dropped keyframe must not cost the
        # 4K copy out of the decoder.
        filters.append("fps=1")
    if config.hwaccel == "drm":
        filters.extend(["hwdownload", "format=nv12"])
    if config.plate_region is not None:
        filters.append(config.plate_region.ffmpeg_crop_filter())
    if config.frame_width > 0:
        filters.append(f"scale=w='min(iw,{config.frame_width})':h=-2")
    return tuple(filters)


class ClearKeyframeBuffer(HotStreamBuffer):
    """Continuously decode only the clear stream's keyframes.

    At the camera's 1x keyframe interval this is one 4K decode per second, so
    a frame taken moments before the alarm is already in memory when the
    webhook arrives. Decoded in software and encoded at 4K that costs most of
    one Pi 5 core; with ``hwaccel="drm"`` and ``frame_width=1920`` it is about
    a fifth of a core. The on-demand grab remains the fallback when the ring
    is stale.
    """

    def __init__(self, capture_config: TriggerCaptureConfig, **kwargs) -> None:
        super().__init__(
            HotStreamConfig(
                enabled=True,
                output_directory=capture_config.output_directory,
                source_url=capture_config.source_url,
                sample_fps=1.0,
                frame_count=KEYFRAME_RING_FRAMES,
                selection_count=1,
                max_frame_bytes=capture_config.max_frame_bytes,
                max_total_bytes=capture_config.max_frame_bytes * KEYFRAME_RING_FRAMES,
                max_age_seconds=KEYFRAME_MAX_AGE_SECONDS,
            ),
            **kwargs,
        )
        self.command = (
            FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-analyzeduration", "0", "-probesize", "32",
            *decoder_input_arguments(capture_config),
            "-skip_frame", "nokey",
            "-i", capture_config.source_url,
            "-map", "0:v:0", "-an",
            "-vf", ",".join(decoder_filters(capture_config, sample=True)),
            "-q:v", "2", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
        )
        self.scene = SceneBaseline(clock=self._clock)
        self._decode = {
            "hwaccel": capture_config.hwaccel or "software",
            "frame_width": capture_config.frame_width or 3840,
            "plate_region": (
                capture_config.plate_region.as_env() if capture_config.plate_region else "full"
            ),
        }

    def latest(self, *, after: float | None = None) -> tuple[bytes, float] | None:
        return self._ring.latest(
            now=self._clock(), max_age=self.config.max_age_seconds, after=after,
        )

    def _on_frame(self, frame: bytes, now: float) -> None:
        self.scene.observe(frame, now)

    def note_activity(self) -> None:
        self.scene.note_activity(self._clock())

    def scene_difference(self, frame: bytes) -> float | None:
        return self.scene.difference(frame)

    def status(self) -> dict:
        status = super().status()
        status.update({
            "stream": "clear",
            "keyframes_only": True,
            "source_profile": {
                "codec": "h265", "width": 3840, "height": 2160, "fps": 10,
            },
            "decode": dict(self._decode),
            "scene": self.scene.status(self._clock()),
        })
        return status


class TriggerFrameCapture:
    """Grab one clear-stream frame per accepted camera event, bounded and serial."""

    def __init__(self, config: TriggerCaptureConfig, *, popen=subprocess.Popen,
                 clock=monotonic, wall_clock=None, frame_source=None):
        self.config = config
        self.output_directory = config.output_directory
        self._popen = popen
        # An object with latest(after=...) -> (jpeg_bytes, captured_at) or
        # None, normally the ClearKeyframeBuffer.
        self._frame_source = frame_source
        self._last_captured_at: float | None = None
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._queue: Queue = Queue(maxsize=1)
        self._inject = None
        self._lock = Lock()
        self._process = None
        self._closed = False
        self._last_scheduled_at: float | None = None
        self._capture_count = 0
        self._failure_count = 0
        self._presence_retries = 0
        self._skipped_empty = 0
        self._skipped_clipped = 0
        self._unresolved_sessions = 0
        self._last_skip: str | None = None
        self._live_session = False
        self._last_stillness: float | None = None
        # Presence-session bookkeeping, shared with the worker's result hook.
        self._session_lock = Lock()
        self._session_paths: set[Path] = set()
        self._session_pending = 0
        self._session_settled: str | None = None
        self._session_changed = Event()
        # The SDP already carries the codec parameters, so probing is skipped
        # (the default probe alone costs about two seconds at 4K), and the
        # decoder ignores everything before the first keyframe: a P-frame
        # decoded against a synthetic grey reference is a grey frame, which
        # would be a valid JPEG and a wasted OCR request.
        grab_filters = decoder_filters(config, sample=False)
        self.command = (
            FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-analyzeduration", "0", "-probesize", "32",
            *decoder_input_arguments(config),
            "-skip_frame", "nokey",
            "-i", config.source_url,
            "-map", "0:v:0", "-an", "-frames:v", "1",
            *(("-vf", ",".join(grab_filters)) if grab_filters else ()),
            "-q:v", "2", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
        )
        self.child_environment = {"LANG": "C", "LC_ALL": "C"}

    def attach(self, inject) -> None:
        """Receive the burst injector from the worker before capture starts."""
        self._inject = inject

    def on_camera_event(self, event) -> str:
        """Schedule a capture from the webhook thread without blocking it."""
        if not self.config.enabled:
            return "disabled"
        if getattr(event, "event_type", None) in SKIPPED_EVENT_TYPES:
            outcome = "skipped_type"
        else:
            now = self._clock()
            note_activity = getattr(self._frame_source, "note_activity", None)
            if callable(note_activity):
                try:
                    note_activity()
                except Exception:
                    pass
            with self._lock:
                last = self._last_scheduled_at
                if last is not None and now - last < self.config.min_interval_seconds:
                    outcome = "skipped_interval"
                else:
                    try:
                        self._queue.put_nowait((event, now))
                    except Full:
                        outcome = "skipped_busy"
                    else:
                        self._last_scheduled_at = now
                        outcome = "scheduled"
        LOGGER.info(
            "gate_trigger_capture outcome=%s event_type=%s",
            outcome, getattr(event, "event_type", "unknown"),
        )
        return outcome

    def run_forever(self, stop_event) -> None:
        if not self.config.enabled:
            stop_event.wait()
            return
        _ensure_private_directory(self.output_directory)
        while not stop_event.is_set():
            try:
                event, scheduled_at = self._queue.get(timeout=0.25)
            except Empty:
                continue
            self.capture_series(event, scheduled_at, stop_event)
            self.presence_session(event, scheduled_at, stop_event)

    def note_result(self, paths, result) -> None:
        """Learn how a frame this capture injected was decided.

        Called by the worker for every processed burst; frames from other
        sources are ignored. An open or a read plate settles the session;
        anything that never read the plate leaves it open to another frame.
        """
        with self._session_lock:
            matched = [Path(path) for path in paths if Path(path) in self._session_paths]
            if not matched:
                return
            for path in matched:
                self._session_paths.discard(path)
            self._session_pending = max(0, self._session_pending - 1)
            if self._session_settled is None:
                if getattr(result, "opened", False):
                    self._session_settled = "opened"
                else:
                    decision = getattr(result, "decision", None)
                    if decision is not None and getattr(decision, "observed_plate", None):
                        self._session_settled = "plate_read"
                    elif getattr(result, "reason", None) not in PRESENCE_RETRY_REASONS:
                        self._session_settled = f"final_{getattr(result, 'reason', 'unknown')}"
            self._session_changed.set()

    def presence_session(self, event, scheduled_at, stop_event) -> int:
        """Keep offering fresh frames while the vehicle is present and unread.

        One frame is outstanding at a time so a retry can never push a sharp
        frame out of the bounded burst queue. The session ends when the gate
        opens, a plate is read, the window closes, the frame budget is spent,
        a newer camera event is waiting, or the service stops.
        """
        config = self.config
        if config.presence_max_frames <= 0 or config.presence_window_seconds <= 0:
            self._stop_live_session("presence_disabled")
            return 0
        deadline = scheduled_at + config.presence_window_seconds
        extra = 0
        reason = "window"
        after = self._last_captured_at
        while True:
            if stop_event.is_set():
                reason = "stopping"
                break
            if not self._queue.empty():
                reason = "new_event"
                break
            with self._session_lock:
                settled = self._session_settled
                pending = self._session_pending
            if settled is not None:
                reason = settled
                break
            if extra >= config.presence_max_frames:
                reason = "budget"
                break
            if self._clock() >= deadline:
                reason = "window"
                break
            if pending > 0:
                # A frame is still being decided; wait for its verdict.
                self._session_changed.clear()
                self._session_changed.wait(0.25)
                continue
            if self._pause(stop_event, config.presence_spacing_seconds):
                reason = "stopping"
                break
            if self._clock() >= deadline:
                reason = "window"
                break
            after, count = self._capture_slot(event, scheduled_at, after=after)
            if count:
                extra += count
                self._presence_retries += 1
                LOGGER.info(
                    "gate_trigger_capture outcome=presence_retry frame=%d event_type=%s",
                    extra, getattr(event, "event_type", "unknown"),
                )
            elif self._last_skip == "empty_scene":
                reason = "departed"
                break
        LOGGER.info(
            "gate_trigger_capture outcome=presence_ended reason=%s extra_frames=%d",
            reason, extra,
        )
        self._stop_live_session(reason)
        if reason in ("window", "budget", "departed"):
            # A vehicle was here and nothing read its plate: the one line an
            # operator should be looking for when the gate did not open.
            self._unresolved_sessions += 1
            LOGGER.warning(
                "gate_presence stage=unresolved reason=%s event_type=%s extra_frames=%d",
                reason, getattr(event, "event_type", "unknown"), extra,
            )
        return extra

    def capture_series(self, event, scheduled_at, stop_event) -> int:
        """Take a short bounded series: with hot keyframes the frame decoded
        moments before the alarm goes first, immediately; the rest wait for
        the vehicle to stop (the delay, then the spacing between frames)."""
        injected = 0
        after = None
        slots = self.config.capture_count
        with self._session_lock:
            self._session_paths.clear()
            self._session_pending = 0
            self._session_settled = None
            self._session_changed.clear()
        self._start_live_session()
        if self._frame_source is not None:
            after, count = self._capture_slot(event, scheduled_at, after=after)
            injected += count
            slots -= 1
        for index in range(slots):
            wait = self.config.delay_seconds if index == 0 else self.config.spacing_seconds
            if self._pause(stop_event, wait):
                break
            after, count = self._capture_slot(event, scheduled_at, after=after)
            injected += count
        return injected

    def _capture_slot(self, event, scheduled_at, *, after):
        try:
            paths = self.capture_once(event, scheduled_at, after=after)
        except Exception:
            self._failure_count += 1
            LOGGER.exception("gate_trigger_capture outcome=error")
            return after, 0
        captured_at = self._last_captured_at
        return (captured_at if captured_at is not None else after), (1 if paths else 0)

    @staticmethod
    def _pause(stop_event, seconds: float) -> bool:
        """Sleep unless stopping. Returns True when the service is stopping."""
        if seconds <= 0:
            return stop_event.is_set()
        return stop_event.wait(seconds)

    def capture_once(self, event, scheduled_at: float | None = None, *,
                     after: float | None = None) -> tuple[Path, ...]:
        """Acquire, validate, and inject one frame. Returns the injected paths."""
        started = self._clock()
        self._last_skip = None
        frame, source, frame_captured_at = self._acquire(after)
        self._last_captured_at = frame_captured_at
        if frame is None:
            self._failure_count += 1
            return ()
        scene_difference = self._scene_difference(frame)
        if (
            scene_difference is not None
            and self.config.empty_scene_threshold > 0
            and scene_difference < self.config.empty_scene_threshold
        ):
            self._skipped_empty += 1
            self._last_skip = "empty_scene"
            LOGGER.info(
                "gate_trigger_capture outcome=skipped_empty_scene event_type=%s "
                "source=%s scene_difference=%.3f",
                event.event_type, source, scene_difference,
            )
            return ()
        path = write_private_frame(
            _ensure_private_directory(self.output_directory), frame,
        )
        clipping = self._highlight_clipping(path)
        if (
            clipping is not None
            and self.config.max_highlight_clipping > 0
            and clipping > self.config.max_highlight_clipping
        ):
            path.unlink(missing_ok=True)
            self._skipped_clipped += 1
            self._last_skip = "clipped"
            LOGGER.info(
                "gate_trigger_capture outcome=skipped_clipped event_type=%s source=%s "
                "clipping=%.2f",
                event.event_type, source, clipping,
            )
            return ()
        captured_at = self._wall_clock()
        origin = started if scheduled_at is None else scheduled_at
        delta_ms = max(0.0, (self._clock() - origin) * 1000.0)
        trigger = TriggerTelemetry(
            source="reolink_webhook",
            event_type=event.event_type,
            rule_id=event.rule_id,
            correlation="matched",
            event_at=event.event_at,
            delta_ms=delta_ms,
        )
        inject = self._inject
        if inject is None:
            path.unlink(missing_ok=True)
            LOGGER.warning("gate_trigger_capture outcome=unattached")
            return ()
        with self._session_lock:
            self._session_paths.add(path)
            self._session_pending += 1
        try:
            inject((path,), captured_at, trigger)
        except Exception:
            with self._session_lock:
                self._session_paths.discard(path)
                self._session_pending = max(0, self._session_pending - 1)
            path.unlink(missing_ok=True)
            raise
        self._capture_count += 1
        LOGGER.info(
            "gate_trigger_capture outcome=captured event_type=%s capture_ms=%d "
            "source=%s frame_age_ms=%d scene_difference=%s clipping=%s stillness=%s",
            event.event_type, round((self._clock() - started) * 1000),
            source, max(0, round((self._clock() - frame_captured_at) * 1000)),
            "unavailable" if scene_difference is None else f"{scene_difference:.3f}",
            "unavailable" if clipping is None else f"{clipping:.2f}",
            "unavailable" if self._last_stillness is None else f"{self._last_stillness:.3f}",
        )
        return (path,)

    def _scene_difference(self, frame: bytes) -> float | None:
        difference = getattr(self._frame_source, "scene_difference", None)
        if not callable(difference):
            return None
        try:
            value = difference(frame)
        except Exception:
            return None
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @staticmethod
    def _highlight_clipping(path: Path) -> float | None:
        try:
            return float(measure_frame_quality(path).highlight_clipping)
        except Exception:
            return None

    def _acquire(self, after: float | None) -> tuple[bytes | None, str, float | None]:
        """Prefer the stillest live frame, then a buffered keyframe, then a grab."""
        if self._frame_source is not None:
            stillest = getattr(self._frame_source, "stillest", None)
            if callable(stillest) and self._live_session:
                try:
                    picked = stillest(after=after)
                except Exception:
                    picked = None
                if picked is not None:
                    frame, captured_at, stillness = picked
                    self._last_stillness = stillness
                    return frame, "session", captured_at
            try:
                latest = self._frame_source.latest(after=after)
            except Exception:
                latest = None
            if latest is not None:
                frame, captured_at = latest
                self._last_stillness = None
                return frame, "keyframe", captured_at
            LOGGER.info("gate_trigger_capture keyframe=unavailable fallback=grab")
        self._last_stillness = None
        frame = self._grab()
        return frame, "grab", (self._clock() if frame is not None else None)

    def _start_live_session(self) -> None:
        start = getattr(self._frame_source, "start_session", None)
        if not callable(start):
            return
        try:
            self._live_session = bool(start())
        except Exception:
            self._live_session = False

    def _stop_live_session(self, reason: str) -> None:
        if not self._live_session:
            return
        self._live_session = False
        stop = getattr(self._frame_source, "stop_session", None)
        if callable(stop):
            try:
                stop(reason)
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "stream": "clear",
            "captures": self._capture_count,
            "failures": self._failure_count,
            "timeout_seconds": self.config.timeout_seconds,
            "delay_seconds": self.config.delay_seconds,
            "capture_count": self.config.capture_count,
            "presence": {
                "window_seconds": self.config.presence_window_seconds,
                "spacing_seconds": self.config.presence_spacing_seconds,
                "max_frames": self.config.presence_max_frames,
                "retries": self._presence_retries,
                "unresolved": self._unresolved_sessions,
            },
            "skipped": {
                "empty_scene": self._skipped_empty,
                "clipped": self._skipped_clipped,
                "empty_scene_threshold": self.config.empty_scene_threshold,
                "max_highlight_clipping": self.config.max_highlight_clipping,
            },
        }

    def close(self) -> None:
        """Kill and reap any ffmpeg child still running at shutdown, and
        refuse to track one that is spawned afterwards."""
        with self._lock:
            self._closed = True
            process = self._process
            self._process = None
        if process is not None:
            _terminate(process)

    def _grab(self) -> bytes | None:
        try:
            process = self._popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.child_environment,
                close_fds=True,
            )
        except (OSError, ValueError):
            LOGGER.warning("gate_trigger_capture outcome=failed reason=spawn")
            return None
        with self._lock:
            # Publish under the lock so close() either sees this child or
            # has already marked us closed, in which case it dies here.
            if self._closed:
                stopping = True
            else:
                stopping = False
                self._process = process
        if stopping:
            _terminate(process)
            LOGGER.warning("gate_trigger_capture outcome=failed reason=stopping")
            return None
        try:
            output, reason = self._read_bounded(process)
        finally:
            _terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
        if reason is not None:
            LOGGER.warning("gate_trigger_capture outcome=failed reason=%s", reason)
            return None
        if process.returncode != 0:
            LOGGER.warning("gate_trigger_capture outcome=failed reason=exit_status")
            return None
        if not output or not _is_decodable_jpeg(output):
            LOGGER.warning("gate_trigger_capture outcome=failed reason=invalid_frame")
            return None
        return output

    def _read_bounded(self, process) -> tuple[bytes, str | None]:
        """Read stdout until EOF, never holding more than max_frame_bytes and
        never waiting past the capture timeout. Returns (bytes, failure)."""
        deadline = self._clock() + self.config.timeout_seconds
        buffer = bytearray()
        descriptor = process.stdout.fileno()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return bytes(buffer), "timeout"
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                continue
            # Never request more than the remaining capacity plus one byte,
            # so an oversized frame is detected without ever being held.
            capacity = self.config.max_frame_bytes - len(buffer)
            chunk = os.read(descriptor, min(64 * 1024, capacity + 1))
            if not chunk:
                break
            if len(chunk) > capacity:
                return bytes(buffer), "frame_too_large"
            buffer.extend(chunk)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return bytes(buffer), "exit_wait"
        return bytes(buffer), None


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


def _validate_loopback_rtsp(value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme != "rtsp" or parts.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("GATE_TRIGGER_CAPTURE_SOURCE must be a loopback rtsp:// URL")
    if parts.username or parts.password:
        raise ValueError("GATE_TRIGGER_CAPTURE_SOURCE must not embed credentials")


def _boolean(value) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("GATE_TRIGGER_CAPTURE_ENABLED must be true or false")


def _integer(value, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("trigger capture integer configuration is invalid") from error
    if isinstance(value, bool) or not minimum <= parsed <= maximum:
        raise ValueError("trigger capture integer configuration is outside the safe range")
    return parsed


def _number(value, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("trigger capture numeric configuration is invalid") from error
    if not minimum <= parsed <= maximum:
        raise ValueError("trigger capture numeric configuration is outside the safe range")
    return parsed
