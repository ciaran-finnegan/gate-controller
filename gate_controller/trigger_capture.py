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
from math import isfinite
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic
from urllib.parse import urlsplit

from .backpressure import NULL_GATE
from .hot_stream import (
    FFMPEG_BINARY, MAX_FRAME_BYTES, HotStreamBuffer, HotStreamConfig,
    _ensure_private_directory, _is_decodable_jpeg, write_private_frame,
)
from .images import measure_flat_fraction, measure_frame_quality
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
# The camera's own main-stream frame rate. The session decoder reads a raw
# HEVC pipe, which carries no timestamps at all, so it has to be told this,
# and nothing verifies the answer: the sampling filter keeps a fixed
# fraction of the pictures, so a real rate that is not the stated one moves
# the live session's rate in proportion, silently.
MIN_SOURCE_FPS, MAX_SOURCE_FPS = 1.0, 60.0
# Presence session: after the series, keep offering fresh frames while the
# vehicle is still at the gate and nothing has read its plate yet.
MAX_PRESENCE_WINDOW_SECONDS = 120.0
MIN_PRESENCE_SPACING_SECONDS = 1.0
MAX_PRESENCE_SPACING_SECONDS = 15.0
MAX_PRESENCE_FRAMES = 10
# Mean thumbnail difference from the idle scene below which a frame shows an
# empty drive. Empty-vs-empty drift over 30 s measures around 0.01; a vehicle
# in the plate band measures well above 0.08.
DEFAULT_EMPTY_SCENE_THRESHOLD = 0.03
MAX_EMPTY_SCENE_THRESHOLD = 0.5
# Fraction of the plate band that may be one single flat colour before the
# frame is treated as a broken decode rather than a picture. The two
# distributions this sits between were measured over 4,469 real frames and
# the broken frames from the incident folders:
#
#   real     367 frames from the camera as it is configured now reach 0.632
#            at the top, and 20 of them - infrared night, empty drive, IR
#            bloom and mist - land between 0.5 and 0.6.
#   broken   0.913, 0.969, 0.994 and 1.000.
#
# 0.6 is inside the real distribution, not above it: it would have thrown
# away those 20 night frames, which are exactly the frames night recognition
# cannot spare. 0.8 clears the real maximum by 0.17 and stays 0.11 below the
# least flat broken frame. The measure is deliberately unhurried about real
# texture - it averages a 480x270 draft down to 64x36 before comparing, and
# that resample smooths genuine detail toward flatness - so the headroom
# above the real frames is what has to be generous, not the margin below the
# broken ones.
DEFAULT_MAX_FLAT_FRACTION = 0.8
# Reasons that leave the plate genuinely unread, so another frame can still
# change the answer. `no_match` is here because most of them are not a
# different vehicle at all: they are the authorised plate read a shade under
# the confidence bar, and the very next frame often reads it cleanly. A read
# that really is another vehicle is settled by `_is_another_vehicle` instead,
# which asks the decision rather than the reason string.
PRESENCE_RETRY_REASONS = frozenset({
    "ocr_error", "ocr_busy", "decision_timeout", "stale_burst", "no_match",
    "processing_error", "queue_coalesced", "upload_incomplete",
    # A frame lost to shutdown never read the plate either; the loop leaves
    # on its own stop_event rather than treating this as a final answer.
    "service_stopping",
})
# Belt and braces behind note_dropped(): if a verdict never arrives at all the
# session must not sit out the whole window with a vehicle at the gate. The
# guard has to be generous enough that it never fires on a merely busy
# pipeline: a frame can wait behind every other burst the bounded queue holds
# (max_pending_bursts, 2 by default) plus the one being decided, each of which
# may take the whole decision timeout, and an open then holds the relay for
# its pulse. Anything past that means the verdict is not coming.
VERDICT_QUEUE_DEPTH = 3
# The bar a read must clear before "this is a different vehicle" is a
# conclusion rather than a guess. Deliberately *not* the band's own exact bar:
# that one decides whether to open the gate, and overnight it is 0.90, so a
# 0.806 read of a visitor's plate could never be conclusive and every denied
# night passage ran to the frame budget - 6 lookups and 11.5 s against 3 and
# 2.5 s by day, and a `stage=unresolved` warning although a plate had been
# read. This decision only ever *stops spending*; it can never open the gate,
# so it takes the standard exact bar around the clock. The distance test
# beside it is what keeps a misread of an authorised plate out.
DEFAULT_CONCLUSIVE_READ_CONFIDENCE = 0.75
# Below this a "conclusive read" is not conclusive, so the session would stop
# retrying on noise. The same reasoning as the matching bars' own floor.
MIN_CONCLUSIVE_READ_CONFIDENCE = 0.10
RELAY_PULSE_ALLOWANCE_SECONDS = 5.0
MIN_DECISION_TIMEOUT_SECONDS = 0.5
MAX_DECISION_TIMEOUT_SECONDS = 30.0
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
    # The band of the frame where plates appear, as frame fractions. The OCR
    # upload is always cropped to it; captured frames are only cropped when
    # crop_capture is set, because a cropped capture loses the camera's
    # timestamp overlay and the operator's context in every evidence image.
    plate_region: PlateRegion | None = None
    crop_capture: bool = False
    # A vehicle that triggered the camera is still there after the series.
    # While nothing has read its plate, keep offering one fresh keyframe at a
    # time, spaced out, for a bounded window and a bounded number of frames.
    # This is what turns a five-second network blip into a delayed open
    # instead of a closed gate. 0 frames disables the session.
    presence_window_seconds: float = 20.0
    presence_spacing_seconds: float = 3.0
    presence_max_frames: int = 4
    # The processor's decision timeout, mirrored here only to bound how long
    # the presence loop waits for a verdict that may never come.
    decision_timeout_seconds: float = 4.0
    # Hold the clear stream compressed and decode only for events (the
    # default), or keep the older continuously decoded keyframe ring.
    clear_stream_mode: str = "compressed"
    # Live decode rate and length of the per-event session in compressed mode.
    session_fps: float = 5.0
    session_seconds: float = 45.0
    # What the camera's main stream actually runs at, so the session decoder
    # can time an Annex-B pipe that carries no timestamps of its own.
    source_fps: float = 10.0
    # Skip frames that barely differ from the idle scene (no vehicle in the
    # plate band yet, or it has left). 0 disables the check.
    empty_scene_threshold: float = DEFAULT_EMPTY_SCENE_THRESHOLD
    # Skip frames whose bright pixels exceed this fraction (headlight or IR
    # blaze washing out the plate). 0 disables; the value is always journaled
    # so a threshold can be chosen from real captures.
    max_highlight_clipping: float = 0.0
    # Skip frames whose plate band is mostly one flat colour: a picture the
    # decoder could not finish, not a scene. 0 disables.
    max_flat_fraction: float = DEFAULT_MAX_FLAT_FRACTION
    # What a read must carry before the presence session concludes it is
    # looking at a different vehicle and stops offering frames. It gates no
    # actuation: see DEFAULT_CONCLUSIVE_READ_CONFIDENCE.
    conclusive_read_confidence: float = DEFAULT_CONCLUSIVE_READ_CONFIDENCE


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
    crop_capture = _boolean(environment.get("GATE_TRIGGER_CAPTURE_CROP", "false"))
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
    # Clamped rather than validated: the processor owns this setting, and a
    # value it accepts must never stop capture from starting.
    decision_timeout = _clamped(
        environment.get("GATE_DECISION_TIMEOUT_SECONDS", "4"),
        MIN_DECISION_TIMEOUT_SECONDS, MAX_DECISION_TIMEOUT_SECONDS, 4.0,
    )
    empty_scene = _number(
        environment.get("GATE_EMPTY_SCENE_THRESHOLD", str(DEFAULT_EMPTY_SCENE_THRESHOLD)),
        0.0, MAX_EMPTY_SCENE_THRESHOLD,
    )
    max_clipping = _number(environment.get("GATE_MAX_HIGHLIGHT_CLIPPING", "0"), 0.0, 1.0)
    max_flat_fraction = _number(
        environment.get(
            "GATE_TRIGGER_CAPTURE_MAX_FLAT_FRACTION", str(DEFAULT_MAX_FLAT_FRACTION),
        ),
        0.0, 1.0,
    )
    clear_stream_mode = str(environment.get("GATE_CLEAR_STREAM_MODE", "compressed")).strip().lower()
    if clear_stream_mode not in CLEAR_STREAM_MODES:
        raise ValueError("GATE_CLEAR_STREAM_MODE must be 'compressed' or 'decoded'")
    session_fps = _number(environment.get("GATE_SESSION_FPS", "5"), MIN_SESSION_FPS, MAX_SESSION_FPS)
    session_seconds = _number(
        environment.get("GATE_SESSION_SECONDS", "45"), MIN_SESSION_SECONDS, MAX_SESSION_SECONDS,
    )
    source_fps = _number(
        environment.get("GATE_CLEAR_STREAM_SOURCE_FPS", "10"), MIN_SOURCE_FPS, MAX_SOURCE_FPS,
    )
    # Clamped, not validated: a bad value here must not stop capture from
    # starting, and the fallback is the shipped bar.
    conclusive_confidence = _clamped(
        environment.get(
            "GATE_PRESENCE_CONCLUSIVE_CONFIDENCE",
            str(DEFAULT_CONCLUSIVE_READ_CONFIDENCE),
        ),
        MIN_CONCLUSIVE_READ_CONFIDENCE, 1.0, DEFAULT_CONCLUSIVE_READ_CONFIDENCE,
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
        crop_capture=crop_capture,
        presence_window_seconds=presence_window,
        presence_spacing_seconds=presence_spacing,
        presence_max_frames=presence_frames,
        decision_timeout_seconds=decision_timeout,
        empty_scene_threshold=empty_scene,
        max_highlight_clipping=max_clipping,
        max_flat_fraction=max_flat_fraction,
        clear_stream_mode=clear_stream_mode,
        session_fps=session_fps,
        session_seconds=session_seconds,
        source_fps=source_fps,
        conclusive_read_confidence=conclusive_confidence,
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
    if config.plate_region is not None and config.crop_capture:
        filters.append(config.plate_region.ffmpeg_crop_filter())
    if config.frame_width > 0:
        filters.append(f"scale=w='min(iw,{config.frame_width})':h=-2")
    return tuple(filters)


@dataclass(frozen=True)
class LostFrame:
    """The verdict of a frame that vanished before anything could decide it.

    A burst can be coalesced out of the bounded queue or lost to a processing
    error, in which case no ``ProcessingResult`` is ever produced. This stands
    in for one so the presence session's pending count still comes back down.
    """

    reason: str
    opened: bool = False
    decision: None = None


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
                capture_config.plate_region.as_env()
                if capture_config.plate_region and capture_config.crop_capture else "full"
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
                 clock=monotonic, wall_clock=None, frame_source=None,
                 activity=NULL_GATE):
        self.config = config
        # A camera event owns the uplink from the first frame to the end of
        # the presence session. The corpus asks this gate before it sends.
        self._activity = activity or NULL_GATE
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
        self._dropped_frames = 0
        self._lost_verdicts = 0
        self._skipped_empty = 0
        self._skipped_clipped = 0
        self._skipped_corrupt = 0
        self._unresolved_sessions = 0
        self._below_bar_sessions = 0
        self._last_skip: str | None = None
        self._live_session = False
        self._last_stillness: float | None = None
        # Presence-session bookkeeping, shared with the worker's result hook.
        self._session_lock = Lock()
        # Every frame injected this session, kept so a late verdict can still
        # settle it, and the subset still counted as outstanding.
        self._session_paths: set[Path] = set()
        self._session_pending_paths: set[Path] = set()
        self._session_pending = 0
        self._session_pending_since: float | None = None
        self._session_settled: str | None = None
        # Whether any frame of this session put characters on the vehicle.
        self._session_read_a_plate = False
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
            # One span over both halves: a vehicle is at the gate for the
            # whole of it, including the quiet gaps between frames.
            with self._activity.activity("camera_event"):
                self.capture_series(event, scheduled_at, stop_event)
                self.presence_session(event, scheduled_at, stop_event)

    def note_result(self, paths, result) -> bool:
        """Learn how a frame this capture injected was decided.

        Called by the worker for every processed burst; frames from other
        sources are ignored. Returns True when the paths belonged to this
        session.

        A session ends on a *conclusive* answer, and reading characters is not
        one. Until 2026-09-08 any decision carrying an ``observed_plate``
        settled the session as ``plate_read``, so a read that was denied for
        being a shade under the confidence bar stopped the retries with the
        vehicle still at the gate and the gate still shut -- twice on
        2026-09-08, once on a frame both readers had read correctly. Three
        outcomes are conclusive now:

        ``opened``
            The gate opened. Nothing further is owed.
        ``plate_denied``
            A confident read of a plate that is not near anything authorised:
            a different vehicle, not a misread of an authorised one. Retrying
            only spends paid lookups on a car that is not getting in.
        ``final_<reason>``
            The pipeline gave a final answer that another frame cannot change
            (a revoked authorisation, an ambiguous fuzzy match).

        Everything else -- an uncertain read included -- keeps the session
        offering frames for the rest of its window.
        """
        with self._session_lock:
            matched = [Path(path) for path in paths if Path(path) in self._session_paths]
            if not matched:
                return False
            outstanding = [path for path in matched if path in self._session_pending_paths]
            for path in matched:
                self._session_paths.discard(path)
                self._session_pending_paths.discard(path)
            if outstanding:
                # A verdict for a frame already written off still settles the
                # session, but must not count against the frame now pending.
                self._session_pending = max(0, self._session_pending - 1)
                if self._session_pending == 0:
                    self._session_pending_since = None
            decision = getattr(result, "decision", None)
            if _read_a_plate(decision):
                # Read and refused is not "nothing could read the plate".
                self._session_read_a_plate = True
            if self._session_settled is None:
                if getattr(result, "opened", False):
                    self._session_settled = "opened"
                elif _is_another_vehicle(
                    decision, self.config.conclusive_read_confidence,
                ):
                    self._session_settled = "plate_denied"
                elif getattr(result, "reason", None) not in PRESENCE_RETRY_REASONS:
                    self._session_settled = f"final_{getattr(result, 'reason', 'unknown')}"
            self._session_changed.set()
            return True

    def superseded(self, paths) -> bool:
        """Has this frame's own session already opened the gate?

        The burst processor asks before it spends a paid lookup on a frame
        that has been sitting in the queue. A passage that has already opened
        cannot be improved by reading another of its frames, and the relay
        cooldown would refuse the second open anyway; the only thing left to
        decide is whether to pay for it.

        Deliberately narrow: only an *opened* session supersedes, and only for
        a frame this session actually injected. A denied session keeps reading
        its frames, because one of them may still be the one that opens.
        """
        try:
            candidates = [Path(path) for path in paths]
        except (TypeError, ValueError):
            return False
        with self._session_lock:
            if self._session_settled != "opened":
                return False
            return any(path in self._session_paths for path in candidates)

    def note_dropped(self, paths, reason: str) -> bool:
        """Account for an injected frame that never reached a decision.

        Without this the frame stays pending for the rest of the window and
        the presence loop waits for a verdict that is never coming, with the
        vehicle still at the gate.
        """
        if not self.note_result(paths, LostFrame(reason)):
            return False
        with self._session_lock:
            self._dropped_frames += 1
        LOGGER.info("gate_presence stage=frame_dropped reason=%s", reason)
        return True

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
        warned_overdue = False
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
                pending_since = self._session_pending_since
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
                # A frame is still being decided; wait for its verdict, but
                # not past the point where it cannot still be coming. The
                # clock runs from the injection, not from this loop noticing.
                waited = 0.0 if pending_since is None else self._clock() - pending_since
                if waited >= self._verdict_deadline_seconds():
                    self._abandon_verdict(pending, warned=warned_overdue)
                    warned_overdue = True
                    continue
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
            with self._session_lock:
                read_a_plate = self._session_read_a_plate
            if read_a_plate:
                # A plate *was* read; it simply did not clear its bar, or it
                # sat too close to an authorised one to be conclusive. That is
                # a matching question, not a camera one, so it is journalled
                # apart from the sessions where nothing could be read at all -
                # `stage=unresolved` is the line an operator greps for when
                # the gate did not open and nobody knows why.
                self._below_bar_sessions += 1
                LOGGER.warning(
                    "gate_presence stage=plate_read_below_bar reason=%s "
                    "event_type=%s extra_frames=%d",
                    reason, getattr(event, "event_type", "unknown"), extra,
                )
            else:
                # A vehicle was here and nothing read its plate: the one line
                # an operator should be looking for when the gate did not
                # open.
                self._unresolved_sessions += 1
                LOGGER.warning(
                    "gate_presence stage=unresolved reason=%s event_type=%s extra_frames=%d",
                    reason, getattr(event, "event_type", "unknown"), extra,
                )
        return extra

    def _verdict_deadline_seconds(self) -> float:
        """How long an outstanding frame may stay undecided before it is lost.

        Deliberately generous: every burst ahead of this one in the bounded
        queue may spend the whole decision timeout, and an open holds the
        relay for its pulse on top of that.
        """
        return max(
            self.config.decision_timeout_seconds * VERDICT_QUEUE_DEPTH
            + RELAY_PULSE_ALLOWANCE_SECONDS,
            self.config.presence_spacing_seconds * 2,
        )

    def _abandon_verdict(self, pending: int, *, warned: bool) -> None:
        """Write off verdicts that never arrived so the session can continue.

        note_dropped() should have accounted for every lost frame already, so
        this line means a frame vanished somewhere that does not report back.
        Only the outstanding set is reset: the paths stay, so a verdict that
        does arrive late still settles the session (an open must never be
        thrown away) without counting against whatever is pending by then.
        Warned once per session; the write-off itself happens every time.
        """
        with self._session_lock:
            self._lost_verdicts += self._session_pending
            self._session_pending_paths.clear()
            self._session_pending = 0
            self._session_pending_since = None
        if not warned:
            LOGGER.warning(
                "gate_presence stage=verdict_overdue pending=%d waited_seconds=%.1f",
                pending, self._verdict_deadline_seconds(),
            )

    def capture_series(self, event, scheduled_at, stop_event) -> int:
        """Take a short bounded series: with hot keyframes the frame decoded
        moments before the alarm goes first, immediately; the rest wait for
        the vehicle to stop (the delay, then the spacing between frames)."""
        injected = 0
        after = None
        slots = self.config.capture_count
        with self._session_lock:
            self._session_paths.clear()
            self._session_pending_paths.clear()
            self._session_pending = 0
            self._session_pending_since = None
            self._session_settled = None
            self._session_read_a_plate = False
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
        flat_fraction = self._flat_fraction(frame)
        if (
            flat_fraction is not None
            and self.config.max_flat_fraction > 0
            and flat_fraction > self.config.max_flat_fraction
        ):
            # Checked before the scene comparison: a frame the decoder never
            # finished is not an observation of the drive, and its flat
            # colour reads as a large scene difference, not a small one.
            self._skipped_corrupt += 1
            self._last_skip = "corrupt"
            LOGGER.info(
                "gate_trigger_capture outcome=skipped_corrupt event_type=%s source=%s "
                "flat_fraction=%.3f",
                event.event_type, source, flat_fraction,
            )
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
            self._session_pending_paths.add(path)
            self._session_pending += 1
            if self._session_pending_since is None:
                # The overdue guard is timed from the oldest frame still
                # outstanding, not from the presence loop first noticing it.
                self._session_pending_since = self._clock()
        try:
            inject((path,), captured_at, trigger)
        except Exception:
            with self._session_lock:
                self._session_paths.discard(path)
                self._session_pending_paths.discard(path)
                self._session_pending = max(0, self._session_pending - 1)
                if self._session_pending == 0:
                    self._session_pending_since = None
            path.unlink(missing_ok=True)
            raise
        self._capture_count += 1
        LOGGER.info(
            "gate_trigger_capture outcome=captured event_type=%s capture_ms=%d "
            "source=%s frame_age_ms=%d scene_difference=%s clipping=%s flat_fraction=%s "
            "stillness=%s",
            event.event_type, round((self._clock() - started) * 1000),
            source, max(0, round((self._clock() - frame_captured_at) * 1000)),
            "unavailable" if scene_difference is None else f"{scene_difference:.3f}",
            "unavailable" if clipping is None else f"{clipping:.2f}",
            "unavailable" if flat_fraction is None else f"{flat_fraction:.3f}",
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

    def _flat_fraction(self, frame: bytes) -> float | None:
        """How much of the plate band is one flat colour, or None if unmeasurable.

        The band is cropped here unless the capture is already cropped to it.
        """
        region = None if self.config.crop_capture else self.config.plate_region
        try:
            return measure_flat_fraction(frame, region)
        except Exception:
            return None

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
                "plate_read_below_bar": self._below_bar_sessions,
                "conclusive_confidence": self.config.conclusive_read_confidence,
                "dropped_frames": self._dropped_frames,
                "lost_verdicts": self._lost_verdicts,
            },
            "skipped": {
                "empty_scene": self._skipped_empty,
                "clipped": self._skipped_clipped,
                "corrupt": self._skipped_corrupt,
                "empty_scene_threshold": self.config.empty_scene_threshold,
                "max_highlight_clipping": self.config.max_highlight_clipping,
                "max_flat_fraction": self.config.max_flat_fraction,
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


def _is_another_vehicle(decision, bar: float = DEFAULT_CONCLUSIVE_READ_CONFIDENCE) -> bool:
    """Is this denial about a *different car*, rather than a doubtful read?

    Two things have to hold together, and either one missing means the session
    keeps trying:

    * a plate was read at or above ``bar``, so the characters are not a guess;
    * no authorised plate sits within :data:`MAX_NEAR_MISS_DISTANCE` of it.
      ``near_miss_distance`` is already computed on every denial for review;
      its absence is precisely "nothing authorised looks like this".

    ``bar`` is a *conclusive-read* bar, not the band's own exact bar. The two
    answer different questions: the band's bar decides whether to open the
    gate, and overnight it is 0.90. Asking it here meant a perfectly legible
    0.806 read of a visitor's plate could never be conclusive, so every denied
    night passage ran to the frame budget - 6 paid lookups over 11.5 s against
    3 over 2.5 s by day - and then warned `stage=unresolved` although the
    plate had been read. Nothing decided here can open the gate; it can only
    stop the session spending, so it is not the bar that guards the relay.

    A read that is close to an authorised plate is the case another frame
    fixes, so it never ends the session. Being wrong here costs paid lookups,
    never an open, so it is written to keep trying when it cannot be sure.
    """
    if not _read_a_plate(decision):
        return False
    if getattr(decision, "near_miss_distance", None) is not None:
        return False
    try:
        confidence = float(getattr(decision, "confidence", 0.0))
    except (TypeError, ValueError):
        return False
    return isfinite(confidence) and confidence >= bar


def _read_a_plate(decision) -> bool:
    """Did this denial actually put characters on the vehicle?

    A denial carrying an ``observed_plate`` read the plate and refused it -
    under its bar, ambiguous, close to something authorised. That is a
    different thing from a passage nothing could read at all, and only the
    second one is what `stage=unresolved` was written to find.
    """
    if decision is None or getattr(decision, "allowed", False):
        return False
    return bool(getattr(decision, "observed_plate", None))


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


def _clamped(value, minimum: float, maximum: float, default: float) -> float:
    """Read a number owned by another component: never raise, always bound."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return min(max(parsed, minimum), maximum)


def _number(value, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("trigger capture numeric configuration is invalid") from error
    if not minimum <= parsed <= maximum:
        raise ValueError("trigger capture numeric configuration is outside the safe range")
    return parsed
