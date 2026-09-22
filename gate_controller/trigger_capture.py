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

import inspect
import logging
import os
import re
import select
import subprocess
from hashlib import sha256
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
# The shape a `reason=` token on a journal line may take.
_JOURNAL_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
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
DEFAULT_SWEEP_SECONDS = 10.0
MIN_SWEEP_SECONDS, MAX_SWEEP_SECONDS = 1.0, 30.0
DEFAULT_SWEEP_MAX_FPS = 5.0
MIN_SWEEP_FPS, MAX_SWEEP_FPS = 1.0, 10.0
DEFAULT_SWEEP_FALLBACK_FRAMES = 1
MAX_SWEEP_FALLBACK_FRAMES = 3
# Frames of one passage the sweep may hand to the ordinary pipeline purely so
# the cloud reader sees them, while the local reader keeps sweeping. Each is a
# billed lookup, and the service permits one request a second, so this is a
# spend ceiling per passage rather than a rate.
DEFAULT_SWEEP_CLOUD_FRAMES = 5
MAX_SWEEP_CLOUD_FRAMES = 10
DEFAULT_SWEEP_CLOUD_SPACING_SECONDS = 1.0
MIN_SWEEP_CLOUD_SPACING_SECONDS, MAX_SWEEP_CLOUD_SPACING_SECONDS = 0.5, 5.0
# How long the sweep polls for a fresh session frame before asking again.
SWEEP_POLL_SECONDS = 0.05
# The waiting phase: once the full-rate window has closed with the gate still
# shut and a vehicle still in the picture, the on-device reader keeps looking,
# slowly, until the gate opens, the vehicle leaves, the camera raises a new
# alarm, or the cap below runs out.
#
# On 2026-09-20 at 19:15:43 nothing did. The window closed at +10.1 s with the
# plate unread, and for the next 15.5 s -- until the *camera* happened to
# raise a second alarm at +25.9 s -- no frame of a car sitting at the gate was
# read by anything. The read that opened it came at +30.8 s and the driver
# waited 41 s.
#
# 30 s is the cap because of what it has to cover and what it rides on. It
# covers that passage (first authorisable read at +30.8 s; window + waiting
# reaches +40 s). And it stays inside the live decoder session the alarm
# already started (`session_seconds`, 45 s), so waiting never starts a decoder
# of its own: the sweep stops at whichever of the two ends first.
#
# One read a second because the car is not moving: a waiting vehicle gives the
# same picture again, and what changes between reads is sensor noise, which is
# exactly what moves a borderline weakest-character score across its bar. At
# ~0.2 s a read that is a fifth of one of the Pi's four cores for at most
# 30 s (about 6 s of inference a passage), against roughly 90% of a core for
# the 10 s window itself. It spends nothing on the cloud: handovers stay under
# the per-passage ceiling the window already had, and none of them is blind.
DEFAULT_SWEEP_WAITING_SECONDS = 30.0
MAX_SWEEP_WAITING_SECONDS = 60.0
DEFAULT_SWEEP_WAITING_FPS = 1.0
MIN_SWEEP_WAITING_FPS, MAX_SWEEP_WAITING_FPS = 0.2, 2.0
# Consecutive frames showing the idle scene before the waiting phase concludes
# the vehicle has gone. One could be a decoder hiccup; three at one a second
# is three seconds of empty drive.
SWEEP_DEPARTED_FRAMES = 3
# How finely a paced wait is sliced, so a new alarm, an open or a shutdown is
# noticed within this long however slow the cadence.
SWEEP_PACE_SLICE_SECONDS = 0.25
# Frames one sweep may inject on the strength of an authorised on-device read.
# One is the normal case. A second or third exists for the read that was right
# but whose frame was lost on the way -- coalesced out of the queue, timed out
# behind other work, gone stale -- so the vehicle is not left to the camera's
# next alarm. It is a ceiling, not a retry policy: each is a recorded event,
# and a denial that another frame cannot change (`final_*`, `plate_denied`)
# ends the sweep before the ceiling matters. Never more than one is
# outstanding at a time, so one passage cannot put two opens in flight.
MAX_SWEEP_AUTHORISED_INJECTIONS = 3
# An early-origin sweep -- one the early trigger asked for, before any camera
# alarm -- reads on the device and nowhere else. How long it may run with the
# camera still silent: the camera's own alarm has been measured about two
# seconds after a vehicle first shows, so six is that with a wide margin, and a
# would-trigger further ahead of the alarm than this bought the passage nothing.
DEFAULT_EARLY_MAX_SECONDS = 6.0
MAX_EARLY_MAX_SECONDS = 30.0
# ...and how soon it gives up on an empty drive: this many frames in a row with
# no plate box in them and nothing in the picture. The session decoder delivers
# five frames a second and the watched patch overlaps the plate band, so a
# vehicle that really tripped it is in the band within a second; five blank
# frames is that second, and it bounds what any false trigger can cost
# whatever caused it. 0 disables the quick abort.
DEFAULT_EARLY_ABORT_FRAMES = 5
MAX_EARLY_ABORT_FRAMES = 100
ORIGIN_CAMERA, ORIGIN_EARLY = "camera", "early"
EARLY_EVENT_TYPE = "early_trigger"
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
    # "The store already holds an event for these exact bytes." That is a fact
    # about the *file*, not about the vehicle: the earlier copy's own verdict
    # is what speaks for it, and it reaches this session by its own path. On
    # 2026-09-20 at 19:15:54 the sweep's fallback re-injected the very frame
    # it had already handed to the cloud ten seconds earlier, the pipeline
    # answered `duplicate_event`, and because that was not listed here the
    # session settled as `final_duplicate_event` and stopped looking with the
    # car still at the gate.
    "duplicate_event",
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
    # Local sweep: instead of the spaced series, read every live session frame
    # on the device for a bounded window and inject only a frame whose local
    # read already authorises. Off by default; needs GATE_LOCAL_OCR_MODE=active.
    sweep_enabled: bool = False
    sweep_seconds: float = DEFAULT_SWEEP_SECONDS
    sweep_max_fps: float = DEFAULT_SWEEP_MAX_FPS
    # Frames handed to the ordinary pipeline (cloud fallback included) when
    # the sweep window ends with no authorised read: the audit trail and the
    # last-resort cloud read. 0 keeps every sweep frame off the cloud.
    sweep_fallback_frames: int = DEFAULT_SWEEP_FALLBACK_FRAMES
    # The cloud reader runs beside the local one rather than behind it: while
    # the sweep reads frames on the device, it also hands the pipeline a frame
    # at a time for the cloud to read, and whichever answers first opens the
    # gate. 0 keeps the cloud out of the window entirely.
    sweep_cloud_frames: int = DEFAULT_SWEEP_CLOUD_FRAMES
    sweep_cloud_spacing_seconds: float = DEFAULT_SWEEP_CLOUD_SPACING_SECONDS
    # After the window: keep reading on the device, slowly, while a vehicle is
    # still in the picture and the gate has not opened. 0 seconds disables it.
    # See DEFAULT_SWEEP_WAITING_SECONDS for where the numbers come from.
    sweep_waiting_seconds: float = DEFAULT_SWEEP_WAITING_SECONDS
    sweep_waiting_fps: float = DEFAULT_SWEEP_WAITING_FPS
    # An early-origin sweep with the camera still silent: see the constants.
    early_max_seconds: float = DEFAULT_EARLY_MAX_SECONDS
    early_abort_frames: int = DEFAULT_EARLY_ABORT_FRAMES


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
    sweep_enabled = _boolean(environment.get("GATE_LOCAL_SWEEP_ENABLED", "false"))
    sweep_seconds = _number(
        environment.get("GATE_LOCAL_SWEEP_SECONDS", str(DEFAULT_SWEEP_SECONDS)),
        MIN_SWEEP_SECONDS, MAX_SWEEP_SECONDS,
    )
    sweep_max_fps = _number(
        environment.get("GATE_LOCAL_SWEEP_MAX_FPS", str(DEFAULT_SWEEP_MAX_FPS)),
        MIN_SWEEP_FPS, MAX_SWEEP_FPS,
    )
    sweep_fallback_frames = _integer(
        environment.get(
            "GATE_LOCAL_SWEEP_FALLBACK_FRAMES", str(DEFAULT_SWEEP_FALLBACK_FRAMES),
        ),
        0, MAX_SWEEP_FALLBACK_FRAMES,
    )
    sweep_cloud_frames = _integer(
        environment.get(
            "GATE_LOCAL_SWEEP_CLOUD_FRAMES", str(DEFAULT_SWEEP_CLOUD_FRAMES),
        ),
        0, MAX_SWEEP_CLOUD_FRAMES,
    )
    sweep_cloud_spacing = _number(
        environment.get(
            "GATE_LOCAL_SWEEP_CLOUD_SPACING_SECONDS",
            str(DEFAULT_SWEEP_CLOUD_SPACING_SECONDS),
        ),
        MIN_SWEEP_CLOUD_SPACING_SECONDS, MAX_SWEEP_CLOUD_SPACING_SECONDS,
    )
    sweep_waiting_seconds = _number(
        environment.get(
            "GATE_LOCAL_SWEEP_WAITING_SECONDS", str(DEFAULT_SWEEP_WAITING_SECONDS),
        ),
        0.0, MAX_SWEEP_WAITING_SECONDS,
    )
    sweep_waiting_fps = _number(
        environment.get(
            "GATE_LOCAL_SWEEP_WAITING_FPS", str(DEFAULT_SWEEP_WAITING_FPS),
        ),
        MIN_SWEEP_WAITING_FPS, MAX_SWEEP_WAITING_FPS,
    )
    early_max_seconds = _number(
        environment.get("GATE_EARLY_TRIGGER_MAX_SECONDS", str(DEFAULT_EARLY_MAX_SECONDS)),
        1.0, MAX_EARLY_MAX_SECONDS,
    )
    early_abort_frames = _integer(
        environment.get("GATE_EARLY_TRIGGER_ABORT_FRAMES", str(DEFAULT_EARLY_ABORT_FRAMES)),
        0, MAX_EARLY_ABORT_FRAMES,
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
        sweep_enabled=sweep_enabled,
        sweep_seconds=sweep_seconds,
        sweep_max_fps=sweep_max_fps,
        sweep_fallback_frames=sweep_fallback_frames,
        sweep_cloud_frames=sweep_cloud_frames,
        sweep_cloud_spacing_seconds=sweep_cloud_spacing,
        sweep_waiting_seconds=sweep_waiting_seconds,
        sweep_waiting_fps=sweep_waiting_fps,
        early_max_seconds=early_max_seconds,
        early_abort_frames=early_abort_frames,
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


@dataclass(frozen=True)
class EarlyEvent:
    """What stands in for a camera event when the early trigger starts a sweep.

    It is not a camera event and never claims to be one: it has no alarm time,
    and on the wire a frame carrying it is an unverified one.
    """

    features: dict | None = None
    event_type: str = EARLY_EVENT_TYPE
    rule_id: str = EARLY_EVENT_TYPE
    event_at: None = None


def _is_early(event) -> bool:
    return isinstance(event, EarlyEvent)


class SweepPassage:
    """Whose idea a sweep was, and whether the camera has agreed yet.

    **The invariant this carries:** no picture of a passage goes to the cloud
    plate reader unless the *camera* has raised a vehicle event for that
    passage. A sweep the camera started is confirmed from its first instant. A
    sweep the early trigger started is not, until :meth:`confirm` is called
    with the camera's alarm -- and until then it may hand the pipeline only a
    frame its own on-device read already authorises, and that frame carries
    :meth:`cloud_allowed` with it so that nothing downstream can send it on.

    The origin is stated, never inferred from timing.
    """

    def __init__(self, origin: str = ORIGIN_CAMERA):
        self.origin = origin if origin in (ORIGIN_CAMERA, ORIGIN_EARLY) else ORIGIN_EARLY
        self._confirmed = self.origin == ORIGIN_CAMERA
        self.confirmed_at: float | None = None

    @property
    def early(self) -> bool:
        return self.origin == ORIGIN_EARLY

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    def confirm(self, now: float | None = None) -> None:
        """The camera's own vehicle event for this passage has arrived."""
        self._confirmed = True
        self.confirmed_at = now

    def cloud_allowed(self) -> bool:
        return self._confirmed


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
                 activity=NULL_GATE, sweep=None, early_trigger: bool = False,
                 internet_reachable=None):
        self.config = config
        # The controller's network probe (`NetProbeWorker.internet_reachable`),
        # or None. While it answers a definite False the sweep hands no frame
        # to the cloud during its window: the hand-over would only be skipped
        # downstream, and each one is a denied event the passage does not
        # need. The fallback still goes, so the passage is put on record. A
        # probe that is absent, raises or answers anything else changes
        # nothing (see `_cloud_reachable`).
        self._internet_reachable = internet_reachable
        # Whether `on_early_trigger` may start anything at all. False unless
        # GATE_EARLY_TRIGGER=on: in `off` and `shadow` the entry point exists
        # and refuses, whoever calls it.
        self._early_enabled = bool(early_trigger)
        self._early_observer = None
        self._passage = SweepPassage(ORIGIN_CAMERA)
        self._sweep_upgrade: tuple | None = None
        self._early_sweeps = 0
        self._early_upgraded = 0
        self._early_aborted = 0
        # A camera event owns the uplink from the first frame to the end of
        # the presence session. The corpus asks this gate before it sends.
        self._activity = activity or NULL_GATE
        # A LocalSweepReader when the sweep is configured; None otherwise.
        self._sweep = sweep
        self._sweep_runs = 0
        self._sweep_frames = 0
        self._sweep_reads = 0
        self._sweep_busy = 0
        self._sweep_authorised = 0
        self._sweep_injected = 0
        self._sweep_fallbacks = 0
        self._sweep_cloud_handovers = 0
        self._sweep_waiting_reads = 0
        self._sweep_last_read_ms: float | None = None
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
        self._inject_accepts_stillness = False
        self._inject_accepts_sweep_read = False
        self._inject_accepts_cloud_permit = False
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
        # True from the first frame of a camera event to the end of its
        # presence session: the only time another burst's open may end it.
        self._session_active = False
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
        # The worker's injector carries the frame's stillness to the processor
        # (which uses it to decide whether the cloud is worth asking); an
        # injector without the parameter -- every older fake -- is called
        # exactly as before.
        self._inject_accepts_stillness = _accepts_keyword(inject, "stillness")
        # Likewise the sweep's own read of the frame it injects.
        self._inject_accepts_sweep_read = _accepts_keyword(inject, "sweep_read")
        # ...and the permit an early-origin frame must carry. Named exactly:
        # a bare `**kwargs` would swallow it, which is not carrying it.
        self._inject_accepts_cloud_permit = _accepts_keyword(
            inject, "cloud_permit", variadic=False,
        )

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
                        # The one slot may be holding an early trigger nobody
                        # has picked up yet. A camera alarm is never the one
                        # that gives way.
                        outcome = (
                            "scheduled" if self._displace_early((event, now))
                            else "skipped_busy"
                        )
                    else:
                        outcome = "scheduled"
                    if outcome == "scheduled":
                        self._last_scheduled_at = now
        LOGGER.info(
            "gate_trigger_capture outcome=%s event_type=%s",
            outcome, getattr(event, "event_type", "unknown"),
        )
        return outcome

    def _displace_early(self, item) -> bool:
        """Put a camera event in the slot in place of a queued early one. Holds ``_lock``."""
        try:
            queued = self._queue.get_nowait()
        except Empty:
            queued = None
        if queued is not None and not _is_early(queued[0]):
            self._queue.put_nowait(queued)
            return False
        try:
            self._queue.put_nowait(item)
        except Full:
            return False
        return True

    def set_early_observer(self, observer) -> None:
        """Who is told how each early-origin sweep ended (the early trigger's record)."""
        self._early_observer = observer

    def session_active(self) -> bool:
        with self._session_lock:
            return self._session_active

    def on_early_trigger(self, features=None) -> str:
        """Ask for a local-only sweep ahead of the camera's alarm. Never blocks.

        Refused unless the early trigger is ``on``, the local sweep can run
        (there is no early spaced series and no early grab: only the sweep
        knows how to keep a frame off the cloud), and nothing else is in hand.
        It takes no part in the camera's own rate limit, so an early sweep can
        never be the reason a real alarm is `skipped_interval`.
        """
        if not self.config.enabled or not self._early_enabled:
            outcome = "disabled"
        elif not self._sweep_ready():
            outcome = "unavailable"
        elif self.session_active():
            outcome = "skipped_busy"
        else:
            with self._lock:
                try:
                    self._queue.put_nowait((EarlyEvent(features=features), self._clock()))
                except Full:
                    outcome = "skipped_busy"
                else:
                    outcome = "scheduled"
        LOGGER.info(
            "gate_trigger_capture outcome=%s event_type=%s origin=%s",
            outcome, EARLY_EVENT_TYPE, ORIGIN_EARLY,
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
            early = _is_early(event)
            # Every event starts with its own passage, so nothing a previous
            # early sweep left behind can describe this one.
            self._passage = SweepPassage(ORIGIN_EARLY if early else ORIGIN_CAMERA)
            self._sweep_upgrade = None
            if early and not (self._early_enabled and self._sweep_ready()):
                # Only the sweep can keep a frame off the cloud, so an early
                # trigger never falls back to the spaced series.
                LOGGER.info("gate_trigger_capture outcome=early_unavailable")
                continue
            # One span over both halves: a vehicle is at the gate for the
            # whole of it, including the quiet gaps between frames.
            with self._activity.activity(EARLY_EVENT_TYPE if early else "camera_event"):
                try:
                    if self._sweep_ready():
                        self.local_sweep(event, scheduled_at, stop_event)
                    else:
                        self.capture_series(event, scheduled_at, stop_event)
                    upgrade = self._sweep_upgrade if early else None
                    if upgrade is not None:
                        # The camera's alarm arrived during the early sweep:
                        # from here on this is that alarm's passage.
                        event, scheduled_at = upgrade
                    if early and upgrade is None:
                        # The camera never spoke. The presence session hands
                        # frames to the pipeline for the cloud to read, so an
                        # unconfirmed passage does not get one.
                        self._stop_live_session("early_unconfirmed")
                    else:
                        self.presence_session(event, scheduled_at, stop_event)
                finally:
                    with self._session_lock:
                        self._session_active = False

    def _sweep_ready(self) -> bool:
        """Sweep only when configured *and* the local reader can decide.

        A reader that is off, in shadow mode, or not loaded means the sweep
        would read nothing, so the spaced series runs exactly as before.
        """
        if not self.config.sweep_enabled or self._sweep is None:
            return False
        try:
            return bool(self._sweep.available())
        except Exception:
            return False

    def _reset_session(self) -> None:
        with self._session_lock:
            self._session_active = True
            self._session_paths.clear()
            self._session_pending_paths.clear()
            self._session_pending = 0
            self._session_pending_since = None
            self._session_settled = None
            self._session_read_a_plate = False
            self._session_changed.clear()

    def local_sweep(self, event, scheduled_at, stop_event) -> int:
        """Read live session frames, locally and in the cloud at the same time.

        Two readers work the same passage in parallel and the gate opens on
        whichever answers first:

        **On the device.** Every new session frame (newest first, never one
        twice) goes to the local reader at up to ``sweep_max_fps``. When its
        own answer is an authorised plate under the band in force, that frame
        is injected into the ordinary pipeline *with the read*, so the
        pipeline judges the read the sweep took instead of taking another
        (see ``GateProcessor._adopt_sweep_read``), and applies every existing
        safeguard before the relay moves. The sweep **keeps reading** while
        that verdict is outstanding. It used to stop, and on 2026-09-20 that
        cost 5.2 s of a 10 s window looking at nothing while the injected
        frame sat in a queue and was then refused. At most one authorised
        frame is outstanding at a time, and at most
        ``MAX_SWEEP_AUTHORISED_INJECTIONS`` a sweep: if the first is lost or
        refused for a reason another frame can change, the next authorised
        read goes in; an open ends the sweep.

        **In the cloud.** Every ``sweep_cloud_spacing_seconds``, up to
        ``sweep_cloud_frames`` times, the newest frame the local reader could
        not authorise is handed to the same pipeline, again with its read, so
        the pipeline does not spend the on-device reader on a frame already
        read. The sweep does not wait for these. The cap is a spend ceiling,
        because every cloud lookup is billed and the service permits one
        request a second. While the network probe has fresh evidence that the
        internet is down, none of these is handed over at all (journalled
        once, ``stage=cloud_handover_skipped reason=internet_down``): the
        local reads go on exactly as before, and the fallback below still puts
        the passage on record.

        **Waiting.** When the window closes with the gate still shut, the best
        frames seen (up to ``sweep_fallback_frames``, never one already
        injected) are handed on so the passage is recorded, and -- while the
        picture still shows something other than the empty drive -- the local
        reader goes on looking at ``sweep_waiting_fps`` for up to
        ``sweep_waiting_seconds``. A waiting vehicle is the one case where the
        next frame is as good as this one and the read is free. It ends on an
        open (by anything: `note_result` hears every burst), a conclusive
        denial, a new alarm, a shutdown, ``SWEEP_DEPARTED_FRAMES`` empty
        frames in a row, or the cap.

        Returns the frames injected.
        """
        config = self.config
        self._reset_session()
        self._sweep_runs += 1
        # Stated, not inferred: a sweep is early-origin because the early
        # trigger asked for it, and stays unconfirmed until the camera's own
        # alarm is taken off the queue below. See `SweepPassage`.
        passage = SweepPassage(ORIGIN_EARLY if _is_early(event) else ORIGIN_CAMERA)
        self._passage = passage
        self._sweep_upgrade = None
        self._early_sweeps += 1 if passage.early else 0
        sweep_started_at = scheduled_at
        early_deadline = scheduled_at + min(config.early_max_seconds, config.sweep_seconds)
        blank = plate_reads = 0
        first_plate_read: int | None = None
        first_plate_ms: int | None = None
        self._start_live_session()
        window_deadline = scheduled_at + config.sweep_seconds
        final_deadline = window_deadline + max(0.0, config.sweep_waiting_seconds)
        if config.sweep_waiting_seconds > 0 and config.session_seconds > config.sweep_seconds:
            # Never outlive the decoder session the alarm started.
            final_deadline = min(final_deadline, scheduled_at + config.session_seconds)
        window_gap = 1.0 / config.sweep_max_fps if config.sweep_max_fps > 0 else 0.0
        waiting_gap = (
            1.0 / config.sweep_waiting_fps if config.sweep_waiting_fps > 0 else 1.0
        )
        min_gap = window_gap
        trace_id = f"sweep-{self._sweep_runs}-{int(scheduled_at * 1000) & 0xFFFFFFFF:x}"
        after = None
        frames = reads = busy = authorised = injected = handovers = 0
        waiting_reads = 0
        last_read_at: float | None = None
        last_handover_at: float | None = None
        # (score, frame, captured_at, digest, read)
        best: tuple | None = None
        newest: tuple | None = None  # (frame, captured_at, digest, read)
        unread: tuple | None = None  # newest the local reader could not authorise
        # The best frame to spend a paid lookup on: one the on-device detector
        # actually found a plate in. A frame it found none in is a picture
        # problem rather than a reading problem, and the cloud is very likely
        # to answer "no plate found" -- which still costs a lookup and, at one
        # request a second with answers taking five or six, still delays every
        # later answer including the one that opens the gate.
        candidate: tuple | None = None  # (score, frame, captured_at, digest, read)
        handover_blind = 0
        best_plate: str | None = None
        last_frame_digest: bytes | None = None
        duplicates = 0
        reason = "window"
        waiting = False
        fallback = 0
        fallback_done = False
        consecutive_empty = 0
        # Every frame this sweep has handed to the pipeline, by content. The
        # pipeline's identity for a frame *is* its content, so handing the
        # same bytes over twice can only ever come back `duplicate_event`.
        handed: set[bytes] = set()
        # The authorised frame whose verdict is still outstanding, if any, and
        # when it went in.
        authorised_path: Path | None = None
        authorised_since = 0.0
        batch: list[tuple[bytes, float]] = []

        def halted() -> str | None:
            """Why the sweep must stop now, or None to keep going."""
            if stop_event.is_set():
                return "stopping"
            unconfirmed = passage.early and not passage.confirmed
            if not self._queue.empty() and not unconfirmed:
                # (An unconfirmed early sweep takes the camera's alarm off the
                # queue itself, at the top of the loop, and carries on.)
                return "new_event"
            with self._session_lock:
                settled = self._session_settled
            if settled is not None:
                return settled
            if unconfirmed:
                if config.early_abort_frames > 0 and blank >= config.early_abort_frames:
                    return "early_abort"
                if self._clock() >= early_deadline:
                    return "early_unconfirmed"
            if self._clock() >= final_deadline:
                return "wait_cap" if waiting else "window"
            return None

        def hand_over(frame, captured_at, digest, read, *, source) -> Path | None:
            if digest in handed:
                return None
            if source != "sweep" and not passage.cloud_allowed():
                # `sweep_cloud` and `sweep_fallback` exist to be read by the
                # cloud. Until the camera has raised its own event for this
                # passage, neither leaves this function.
                return None
            path = self._inject_bytes(
                frame, captured_at, event, scheduled_at, source=source, read=read,
            )
            if path is not None:
                handed.add(digest)
            return path

        handover_skips_journalled = False

        def cloud_reachable() -> bool:
            """Whether a cloud hand-over is worth making, as the probe sees it."""
            nonlocal handover_skips_journalled
            if self._cloud_reachable():
                return True
            if not handover_skips_journalled:
                handover_skips_journalled = True
                LOGGER.info(
                    "gate_local_sweep stage=cloud_handover_skipped reason=%s of=%d",
                    self._cloud_unavailable_reason(), config.sweep_cloud_frames,
                )
            return False

        def run_fallback() -> None:
            nonlocal fallback, fallback_done
            fallback_done = True
            if injected or config.sweep_fallback_frames <= 0 or not passage.cloud_allowed():
                return
            candidates = []
            if best is not None:
                candidates.append(best[1:])
            if newest is not None and (best is None or newest[1] != best[2]):
                candidates.append(newest)
            # A frame the cloud has already been handed is already on record.
            candidates = [item for item in candidates if item[2] not in handed]
            for frame, captured_at, digest, read in candidates[:config.sweep_fallback_frames]:
                if hand_over(frame, captured_at, digest, read, source="sweep_fallback"):
                    fallback += 1

        while True:
            if passage.early and not passage.confirmed:
                camera = self._take_camera_event()
                if camera is not None:
                    # The camera's alarm for this passage. The sweep is
                    # upgraded where it stands -- same decoder session, same
                    # frames already read -- and the car gets its whole read
                    # window measured from the alarm, as it would have had.
                    lead = max(0.0, camera[1] - sweep_started_at)
                    event, scheduled_at = camera
                    passage.confirm(self._clock())
                    self._sweep_upgrade = camera
                    self._early_upgraded += 1
                    window_deadline = scheduled_at + config.sweep_seconds
                    final_deadline = window_deadline + max(0.0, config.sweep_waiting_seconds)
                    if config.sweep_waiting_seconds > 0 and config.session_seconds > config.sweep_seconds:
                        final_deadline = min(
                            final_deadline, sweep_started_at + config.session_seconds,
                        )
                    LOGGER.info(
                        "gate_local_sweep stage=upgraded origin=early event_type=%s lead_ms=%d "
                        "reads=%d plate_reads=%d",
                        getattr(event, "event_type", "unknown"), round(lead * 1000),
                        reads, plate_reads,
                    )
            reason = halted()
            if reason is not None:
                break
            if not waiting and self._clock() >= window_deadline:
                # The window has closed with the gate still shut. Put the
                # passage on record now rather than when the waiting ends, and
                # carry on only if something is still in the picture.
                run_fallback()
                if consecutive_empty >= SWEEP_DEPARTED_FRAMES:
                    reason = "departed"
                    break
                waiting = True
                min_gap = waiting_gap
                batch = []
                LOGGER.info(
                    "gate_local_sweep stage=waiting event_type=%s fps=%.1f cap_seconds=%d",
                    getattr(event, "event_type", "unknown"), config.sweep_waiting_fps,
                    round(max(0.0, final_deadline - self._clock())),
                )
            if authorised_path is not None:
                with self._session_lock:
                    outstanding = authorised_path in self._session_pending_paths
                if not outstanding or (
                    self._clock() - authorised_since >= self._verdict_deadline_seconds()
                ):
                    # Its verdict is in and the session has not settled, so it
                    # was lost or refused for a reason another frame can
                    # change -- or no verdict is coming at all (the same guard
                    # the presence session keeps). The next authorised read
                    # may go in.
                    authorised_path = None
            if (
                config.sweep_cloud_frames > 0
                and passage.cloud_allowed()
                and handovers < config.sweep_cloud_frames
                # While waiting, only a frame with a plate in it is worth a
                # paid lookup: the fallback has already given the cloud its
                # look at a frame the device found nothing in.
                and (candidate is not None or (unread is not None and not waiting))
                and (
                    last_handover_at is None
                    or self._clock() - last_handover_at >= config.sweep_cloud_spacing_seconds
                )
                # Asked last, so it is asked only when a hand-over would go,
                # and asked again each time: a link that comes back mid-sweep
                # gets the next one.
                and cloud_reachable()
            ):
                # A frame with a plate in it, best read first; otherwise the
                # freshest view there is, which is what this always sent. The
                # fallback stays because the on-device detector missing a plate
                # the cloud would have found is exactly the case a second
                # opinion is being paid for.
                if candidate is not None:
                    _score, frame, captured_at, digest, read = candidate
                    candidate = None
                    blind = False
                else:
                    frame, captured_at, digest, read = unread
                    blind = True
                unread = None
                last_handover_at = self._clock()
                if hand_over(frame, captured_at, digest, read, source="sweep_cloud"):
                    handovers += 1
                    handover_blind += 1 if blind else 0
                    LOGGER.info(
                        "gate_local_sweep stage=cloud_handover frame=%d of=%d plate_seen=%s",
                        handovers, config.sweep_cloud_frames, not blind,
                    )
            if not batch:
                if waiting and last_read_at is not None:
                    # Paced *before* the fetch while waiting, in slices, so
                    # the frame read is the newest there is and a new alarm or
                    # an open is noticed within a slice.
                    gap = min_gap - (self._clock() - last_read_at)
                    if gap > 0:
                        if self._pause(stop_event, min(gap, SWEEP_PACE_SLICE_SECONDS)):
                            reason = "stopping"
                            break
                        continue
                batch = self._unread_frames(after)
                if waiting:
                    batch = batch[-1:]
                if not batch:
                    if self._pause(stop_event, SWEEP_POLL_SECONDS):
                        reason = "stopping"
                        break
                    continue
            frame, captured_at = batch.pop(0)
            after = captured_at
            if last_read_at is not None and self._clock() - last_read_at < min_gap:
                if self._pause(stop_event, min_gap - (self._clock() - last_read_at)):
                    reason = "stopping"
                    break
            frames += 1
            flat_fraction = self._flat_fraction(frame)
            if (
                flat_fraction is not None
                and config.max_flat_fraction > 0
                and flat_fraction > config.max_flat_fraction
            ):
                self._skipped_corrupt += 1
                continue
            scene_difference = self._scene_difference(frame)
            if (
                scene_difference is not None
                and config.empty_scene_threshold > 0
                and scene_difference < config.empty_scene_threshold
            ):
                self._skipped_empty += 1
                consecutive_empty += 1
                blank += 1
                if waiting:
                    # Looked at, even though it was not read: pace the next
                    # look the same way.
                    last_read_at = self._clock()
                    if consecutive_empty >= SWEEP_DEPARTED_FRAMES:
                        reason = "departed"
                        break
                continue
            consecutive_empty = 0
            # The session decoder resamples to a fixed rate. When the camera is
            # delivering less than that -- measured at 4.5 fps against a
            # configured 6 -- the resampler makes the difference up by
            # repeating frames, and reading the same picture twice spends the
            # reader's ~200 ms on an answer already known.
            digest = sha256(frame).digest()
            if digest == last_frame_digest:
                duplicates += 1
                continue
            last_frame_digest = digest
            # Newest *seen*, read or not: a frame the busy reader could not
            # take is still the freshest picture there is to fall back on.
            newest = (frame, captured_at, digest, None)
            last_read_at = self._clock()
            read = self._sweep.read(frame, trace_id=trace_id)
            reads += 1
            waiting_reads += 1 if waiting else 0
            self._sweep_last_read_ms = read.read_ms
            if read.status == "unavailable":
                busy += 1
                continue
            newest = (frame, captured_at, digest, read)
            if read.recognised:
                blank = 0
                plate_reads += 1
                if first_plate_read is None:
                    first_plate_read = reads
                    first_plate_ms = round((self._clock() - sweep_started_at) * 1000)
            elif scene_difference is None:
                # No idle baseline to compare with, so an unread frame is all
                # there is to say the drive is empty.
                blank += 1
            if read.recognised:
                LOGGER.info(
                    "gate_local_sweep stage=read plate=%s score=%.3f authorised=%s "
                    "read_ms=%d frame=%d",
                    read.plate, read.score, read.authorised, round(read.read_ms), frames,
                )
                if best is None or read.score > best[0]:
                    best = (read.score, frame, captured_at, digest, read)
                    best_plate = read.plate
            if not read.authorised:
                # The freshest view the on-device reader could not settle, and
                # so the one worth a paid second opinion.
                unread = (frame, captured_at, digest, read)
                if read.recognised and (candidate is None or read.score > candidate[0]):
                    candidate = (read.score, frame, captured_at, digest, read)
                continue
            authorised += 1
            if authorised_path is not None or injected >= MAX_SWEEP_AUTHORISED_INJECTIONS:
                # One open in flight at a time, and a ceiling on the whole
                # sweep. The read is not lost: it is counted, and if the
                # outstanding frame is refused the next one goes in.
                continue
            path = hand_over(frame, captured_at, digest, read, source="sweep")
            if path is not None:
                injected += 1
                authorised_path = path
                authorised_since = self._clock()
                # The rest of this clump is older than the frame just sent.
                batch = []
        if not fallback_done and reason in ("window", "new_event"):
            run_fallback()
        self._sweep_frames += frames
        self._sweep_reads += reads
        self._sweep_busy += busy
        self._sweep_authorised += authorised
        self._sweep_injected += injected
        self._sweep_fallbacks += fallback
        self._sweep_cloud_handovers += handovers
        self._sweep_waiting_reads += waiting_reads
        unconfirmed = passage.early and not passage.confirmed
        if passage.early:
            self._early_aborted += 1 if reason == "early_abort" else 0
            self._report_early_sweep({
                "reason": reason, "upgraded": passage.confirmed,
                "lead_ms": (
                    None if not passage.confirmed
                    else round(max(0.0, scheduled_at - sweep_started_at) * 1000)
                ),
                "frames": frames, "reads": reads, "plate_reads": plate_reads,
                "first_plate_read": first_plate_read, "first_plate_ms": first_plate_ms,
                "blank_frames": blank, "authorised": authorised, "injected": injected,
                "cloud_handovers": handovers, "fallback": fallback,
                "best_score": None if best is None else round(best[0], 3),
                "elapsed_ms": round(max(0.0, self._clock() - sweep_started_at) * 1000),
            })
        LOGGER.log(
            # An early sweep the camera never confirmed is expected to end with
            # nothing handed on; that is the design, not a fault to warn about.
            logging.INFO if injected or fallback or unconfirmed else logging.WARNING,
            "gate_local_sweep outcome=ended reason=%s event_type=%s frames=%d reads=%d "
            "busy=%d duplicates=%d read_fps=%.1f authorised=%d injected=%d "
            "cloud_handovers=%d blind_handovers=%d fallback=%d "
            "best_plate=%s best_score=%s elapsed_ms=%d waiting_reads=%d",
            reason, getattr(event, "event_type", "unknown"), frames, reads, busy,
            duplicates, reads / max(1e-6, self._clock() - scheduled_at),
            authorised, injected, handovers, handover_blind, fallback, best_plate or "-",
            "-" if best is None else f"{best[0]:.3f}",
            round(max(0.0, self._clock() - scheduled_at) * 1000), waiting_reads,
        )
        return injected + fallback + handovers

    def _cloud_reachable(self) -> bool:
        """False only when the probe answers a definite False; see `__init__`."""
        probe = self._internet_reachable
        if probe is None:
            return True
        try:
            return probe() is not False
        except Exception:
            return True

    def _cloud_unavailable_reason(self) -> str:
        """Why `_cloud_reachable` said no, as the predicate names it, or `internet_down`.

        The probe's bare method has no opinion; the `CloudAvailability`
        `main` hands in says whether it was the probe or the cloud client's
        circuit breaker, so the journal never blames the link for the breaker.
        """
        try:
            reason = getattr(self._internet_reachable, "reason", None)
        except Exception:
            return "internet_down"
        if isinstance(reason, str) and _JOURNAL_TOKEN.fullmatch(reason):
            return reason
        return "internet_down"

    def _take_camera_event(self):
        """The camera alarm waiting in the slot, taken off it; or None."""
        try:
            queued = self._queue.get_nowait()
        except Empty:
            return None
        if _is_early(queued[0]):
            # A second early trigger says nothing the running sweep does not know.
            return None
        return queued

    def _report_early_sweep(self, report: dict) -> None:
        LOGGER.info(
            "gate_local_sweep stage=early_ended reason=%s upgraded=%s lead_ms=%s reads=%d "
            "plate_reads=%d first_plate_ms=%s blank_frames=%d injected=%d cloud_handovers=%d",
            report["reason"], "yes" if report["upgraded"] else "no", report["lead_ms"],
            report["reads"], report["plate_reads"], report["first_plate_ms"],
            report["blank_frames"], report["injected"], report["cloud_handovers"],
        )
        observer = self._early_observer
        if observer is None:
            return
        try:
            observer(report)
        except Exception:
            LOGGER.warning("gate_local_sweep stage=early_observer_failed")

    def _unread_frames(self, after: float | None) -> list[tuple[bytes, float]]:
        """Fresh session frames newer than ``after``, oldest first.

        A source that can hand over a clump (`frames_since`) does; otherwise
        the newest frame stands alone (a keyframe before the session warms).
        """
        source = self._frame_source
        if source is None:
            return []
        frames_since = getattr(source, "frames_since", None)
        try:
            if callable(frames_since):
                picked = list(frames_since(after))
            else:
                latest = source.latest(after=after)
                picked = [] if latest is None else [latest]
        except Exception:
            return []
        if picked:
            self._last_captured_at = picked[-1][1]
        return picked

    def _inject_bytes(self, frame: bytes, captured_at: float, event, scheduled_at,
                      *, source: str, read=None) -> Path | None:
        """Write ``frame`` privately and hand it to the burst pipeline.

        ``read`` is the sweep's own on-device read of this frame. It travels
        with the frame so the pipeline judges that read instead of taking
        another of a re-encoded copy. Returns the injected path, or None.
        """
        started = self._clock()
        try:
            path = write_private_frame(
                _ensure_private_directory(self.output_directory), frame,
            )
        except Exception:
            self._failure_count += 1
            LOGGER.exception("gate_trigger_capture outcome=error source=%s", source)
            return None
        try:
            injected = self._inject_path(path, event, scheduled_at, started, sweep_read=read)
        except Exception:
            self._failure_count += 1
            LOGGER.exception("gate_trigger_capture outcome=error source=%s", source)
            return None
        if not injected:
            return None
        LOGGER.info(
            "gate_trigger_capture outcome=captured event_type=%s capture_ms=%d "
            "source=%s frame_age_ms=%d",
            getattr(event, "event_type", "unknown"),
            round((self._clock() - started) * 1000), source,
            max(0, round((self._clock() - captured_at) * 1000)),
        )
        return path

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
                # Somebody else's frame -- normally the camera's own FTP still
                # of the very same vehicle. It says nothing about this
                # session's frames, with one exception: if it *opened the
                # gate*, the vehicle this session is watching is being let in,
                # and going on reading it (and paying for lookups on frames
                # still queued) buys nothing. On 2026-09-20 that is how the
                # gate did open, at 19:16:25, while the session knew nothing
                # about it.
                if (
                    self._session_active and self._session_settled is None
                    and getattr(result, "opened", False)
                ):
                    self._session_settled = "opened"
                    self._session_changed.set()
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
        self._reset_session()
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
        if not self._inject_path(path, event, scheduled_at, started):
            return ()
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

    def _inject_path(self, path: Path, event, scheduled_at, started, *,
                     sweep_read=None) -> bool:
        """Hand a written frame to the burst pipeline with its trigger telemetry.

        Returns False, having removed the file, when no injector is attached.
        Session bookkeeping is updated before the call and rolled back if the
        injector raises, so a failed hand-off never leaves a frame pending.

        ``sweep_read`` goes to an injector that takes it (the worker's does);
        one that does not -- every older fake -- is called exactly as before
        and the pipeline simply reads the frame for itself.
        """
        captured_at = self._wall_clock()
        origin = started if scheduled_at is None else scheduled_at
        delta_ms = max(0.0, (self._clock() - origin) * 1000.0)
        passage = self._passage
        unconfirmed = _is_early(event) or (passage.early and not passage.confirmed)
        if unconfirmed:
            # Not a camera event, and it does not say it is one: on the wire
            # this is an unverified frame, exactly like an FTP still.
            trigger = TriggerTelemetry(
                source=EARLY_EVENT_TYPE, event_type="unverified", correlation="unverified",
            )
        else:
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
            return False
        if passage.early and not self._inject_accepts_cloud_permit:
            # An injector that cannot carry the permit would hand this frame
            # on with nothing to stop it reaching the cloud. It does not go.
            path.unlink(missing_ok=True)
            LOGGER.warning("gate_trigger_capture outcome=early_refused reason=no_cloud_permit")
            return False
        with self._session_lock:
            self._session_paths.add(path)
            self._session_pending_paths.add(path)
            self._session_pending += 1
            if self._session_pending_since is None:
                # The overdue guard is timed from the oldest frame still
                # outstanding, not from the presence loop first noticing it.
                self._session_pending_since = self._clock()
        extra = (
            {"stillness": self._last_stillness}
            if self._inject_accepts_stillness and self._last_stillness is not None
            else {}
        )
        if (
            sweep_read is not None and self._inject_accepts_sweep_read
            and getattr(sweep_read, "carried", False)
        ):
            extra["sweep_read"] = sweep_read
        if passage.early:
            # Carried with the frame for the whole of its life in the
            # pipeline, and asked at the moment a request would go out: the
            # answer is no until the camera's own alarm has arrived.
            extra["origin"] = passage.origin
            extra["cloud_permit"] = passage.cloud_allowed
        try:
            inject((path,), captured_at, trigger, **extra)
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
        return True

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
            "sweep": {
                "enabled": self.config.sweep_enabled,
                "ready": self._sweep_ready(),
                "seconds": self.config.sweep_seconds,
                "max_fps": self.config.sweep_max_fps,
                "fallback_frames": self.config.sweep_fallback_frames,
                "runs": self._sweep_runs,
                "frames": self._sweep_frames,
                "reads": self._sweep_reads,
                "busy": self._sweep_busy,
                "authorised": self._sweep_authorised,
                "injected": self._sweep_injected,
                "fallbacks": self._sweep_fallbacks,
                "cloud_frames": self.config.sweep_cloud_frames,
                "cloud_handovers": self._sweep_cloud_handovers,
                "waiting_seconds": self.config.sweep_waiting_seconds,
                "waiting_fps": self.config.sweep_waiting_fps,
                "waiting_reads": self._sweep_waiting_reads,
                "last_read_ms": (
                    None if self._sweep_last_read_ms is None
                    else round(self._sweep_last_read_ms)
                ),
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


def _accepts_keyword(callable_object, keyword: str, *, variadic: bool = True) -> bool:
    """Whether *callable_object* takes this keyword (a ``**kwargs`` counts unless told not to)."""
    try:
        parameters = inspect.signature(callable_object).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword
        or (variadic and parameter.kind == inspect.Parameter.VAR_KEYWORD)
        for parameter in parameters
    )


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
