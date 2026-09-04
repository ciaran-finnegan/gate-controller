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
    FFMPEG_BINARY, MAX_FRAME_BYTES, _ensure_private_directory,
    _is_decodable_jpeg, write_private_frame,
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
    capture_count: int = 2
    spacing_seconds: float = 1.0


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
        environment.get("GATE_TRIGGER_CAPTURE_COUNT", "2"), 1, MAX_CAPTURE_COUNT,
    )
    spacing = _number(
        environment.get("GATE_TRIGGER_CAPTURE_SPACING_SECONDS", "1"),
        MIN_CAPTURE_SPACING_SECONDS, MAX_CAPTURE_SPACING_SECONDS,
    )
    max_frame_bytes = _integer(
        environment.get("GATE_TRIGGER_CAPTURE_MAX_FRAME_BYTES", str(8 * 1024 * 1024)),
        1, MAX_FRAME_BYTES,
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
    )


class TriggerFrameCapture:
    """Grab one clear-stream frame per accepted camera event, bounded and serial."""

    def __init__(self, config: TriggerCaptureConfig, *, popen=subprocess.Popen,
                 clock=monotonic, wall_clock=None):
        self.config = config
        self.output_directory = config.output_directory
        self._popen = popen
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._queue: Queue = Queue(maxsize=1)
        self._inject = None
        self._lock = Lock()
        self._process = None
        self._last_scheduled_at: float | None = None
        self._capture_count = 0
        self._failure_count = 0
        self.command = (
            FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", config.source_url,
            "-map", "0:v:0", "-an", "-frames:v", "1",
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
        """Wait for the vehicle to stop, then grab a short bounded series."""
        injected = 0
        if self._pause(stop_event, self.config.delay_seconds):
            return injected
        for index in range(self.config.capture_count):
            if index and self._pause(stop_event, self.config.spacing_seconds):
                break
            try:
                if self.capture_once(event, scheduled_at):
                    injected += 1
            except Exception:
                self._failure_count += 1
                LOGGER.exception("gate_trigger_capture outcome=error")
        return injected

    @staticmethod
    def _pause(stop_event, seconds: float) -> bool:
        """Sleep unless stopping. Returns True when the service is stopping."""
        if seconds <= 0:
            return stop_event.is_set()
        return stop_event.wait(seconds)

    def capture_once(self, event, scheduled_at: float | None = None) -> tuple[Path, ...]:
        """Grab, validate, and inject one frame. Returns the injected paths."""
        started = self._clock()
        frame = self._grab()
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
            "gate_trigger_capture outcome=captured event_type=%s capture_ms=%d",
            event.event_type, round((self._clock() - started) * 1000),
        )
        return (path,)

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
        """Kill and reap any ffmpeg child still running at shutdown."""
        process = self._process
        self._process = None
        if process is None:
            return
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
        self._process = process
        try:
            output, reason = self._read_bounded(process)
        finally:
            _terminate(process)
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
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            if len(buffer) + len(chunk) > self.config.max_frame_bytes:
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
