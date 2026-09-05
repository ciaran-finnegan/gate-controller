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
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from .hot_stream import (
    FFMPEG_BINARY, MAX_FRAME_BYTES, HotStreamBuffer, HotStreamConfig,
    _ensure_private_directory, _is_decodable_jpeg, write_private_frame,
)
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
    # Scale decoded frames to this width before the JPEG encode; 0 keeps the
    # native size. OCR uploads are downscaled to 1920 anyway, so encoding 4K
    # frames only costs CPU, heat, and ring memory.
    frame_width: int = 0


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
    """The -vf chain: pull hardware frames back, optionally sample, then scale.

    Sampling before scaling means dropped keyframes are never scaled.
    """
    filters = []
    if config.hwaccel == "drm":
        filters.extend(["hwdownload", "format=nv12"])
    if sample:
        filters.append("fps=1")
    if config.frame_width > 0:
        filters.append(f"scale={config.frame_width}:-2")
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
        self._decode = {"hwaccel": capture_config.hwaccel or "software",
                        "frame_width": capture_config.frame_width or 3840}

    def latest(self, *, after: float | None = None) -> tuple[bytes, float] | None:
        return self._ring.latest(
            now=self._clock(), max_age=self.config.max_age_seconds, after=after,
        )

    def status(self) -> dict:
        status = super().status()
        status.update({
            "stream": "clear",
            "keyframes_only": True,
            "source_profile": {
                "codec": "h265", "width": 3840, "height": 2160, "fps": 10,
            },
            "decode": dict(self._decode),
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

    def capture_series(self, event, scheduled_at, stop_event) -> int:
        """Take a short bounded series: with hot keyframes the frame decoded
        moments before the alarm goes first, immediately; the rest wait for
        the vehicle to stop (the delay, then the spacing between frames)."""
        injected = 0
        after = None
        slots = self.config.capture_count
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
        frame, source, frame_captured_at = self._acquire(after)
        self._last_captured_at = frame_captured_at
        if frame is None:
            self._failure_count += 1
            return ()
        path = write_private_frame(
            _ensure_private_directory(self.output_directory), frame,
        )
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
        try:
            inject((path,), captured_at, trigger)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._capture_count += 1
        LOGGER.info(
            "gate_trigger_capture outcome=captured event_type=%s capture_ms=%d "
            "source=%s frame_age_ms=%d",
            event.event_type, round((self._clock() - started) * 1000),
            source, max(0, round((self._clock() - frame_captured_at) * 1000)),
        )
        return (path,)

    def _acquire(self, after: float | None) -> tuple[bytes | None, str, float | None]:
        """Prefer a fresh buffered keyframe; otherwise grab from the stream."""
        if self._frame_source is not None:
            try:
                latest = self._frame_source.latest(after=after)
            except Exception:
                latest = None
            if latest is not None:
                frame, captured_at = latest
                return frame, "keyframe", captured_at
            LOGGER.info("gate_trigger_capture keyframe=unavailable fallback=grab")
        frame = self._grab()
        return frame, "grab", (self._clock() if frame is not None else None)

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "stream": "clear",
            "captures": self._capture_count,
            "failures": self._failure_count,
            "timeout_seconds": self.config.timeout_seconds,
            "delay_seconds": self.config.delay_seconds,
            "capture_count": self.config.capture_count,
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
