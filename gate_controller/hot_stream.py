"""Bounded continuously decoded fluent-stream frames for OCR fallback."""

import hashlib
import logging
import os
import secrets
import stat
import subprocess
import warnings
from collections import deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from time import monotonic

from PIL import Image


LOGGER = logging.getLogger(__name__)
FFMPEG_BINARY = "/usr/bin/ffmpeg"
LOOPBACK_FLUENT_STREAM = "rtsp://127.0.0.1:8554/camera"
MAX_FRAMES = 16
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class HotStreamConfig:
    enabled: bool
    output_directory: Path
    source_url: str = LOOPBACK_FLUENT_STREAM
    sample_fps: float = 5.0
    frame_count: int = 8
    selection_count: int = 2
    max_frame_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 48 * 1024 * 1024
    max_age_seconds: float = 1.0
    restart_seconds: float = 1.0


def load_hot_stream_config(environment, upload_root: Path) -> HotStreamConfig:
    enabled = _boolean(environment.get("GATE_HOT_STREAM_ENABLED", "false"))
    sample_fps = _number(environment.get("GATE_HOT_STREAM_SAMPLE_FPS", "5"), 1, 10)
    frame_count = _integer(environment.get("GATE_HOT_STREAM_FRAME_COUNT", "8"), 1, MAX_FRAMES)
    requested_selection_count = _integer(
        environment.get("GATE_HOT_STREAM_SELECTION_COUNT", "2"), 1, 3,
    )
    # Release 5f2359f documented three buffered selections. Keep that persisted
    # value upgrade-compatible while reserving the first OCR request for FTP.
    selection_count = min(requested_selection_count, 2)
    if selection_count > frame_count:
        raise ValueError("hot stream selection count exceeds frame count")
    max_frame_bytes = _integer(
        environment.get("GATE_HOT_STREAM_MAX_FRAME_BYTES", str(8 * 1024 * 1024)),
        1, MAX_FRAME_BYTES,
    )
    max_total_bytes = _integer(
        environment.get("GATE_HOT_STREAM_MAX_TOTAL_BYTES", str(48 * 1024 * 1024)),
        max_frame_bytes, MAX_TOTAL_BYTES,
    )
    max_age = _number(environment.get("GATE_HOT_STREAM_MAX_AGE_SECONDS", "1"), 0.1, 2)
    return HotStreamConfig(
        enabled=enabled,
        output_directory=Path(upload_root) / ".hot-stream",
        sample_fps=sample_fps,
        frame_count=frame_count,
        selection_count=selection_count,
        max_frame_bytes=max_frame_bytes,
        max_total_bytes=max_total_bytes,
        max_age_seconds=max_age,
    )


class JpegStreamParser:
    def __init__(self, max_frame_bytes: int):
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        if not isinstance(chunk, bytes) or not chunk:
            return []
        self._buffer.extend(chunk)
        frames = []
        while self._buffer:
            start = self._buffer.find(b"\xff\xd8\xff")
            if start < 0:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            end = self._buffer.find(b"\xff\xd9", 3)
            if end < 0:
                if len(self._buffer) > self._max_frame_bytes:
                    self._buffer.clear()
                break
            end += 2
            frame = bytes(self._buffer[:end])
            del self._buffer[:end]
            if len(frame) <= self._max_frame_bytes:
                frames.append(frame)
        return frames


class HotFrameRing:
    def __init__(self, *, max_frames: int, max_frame_bytes: int, max_total_bytes: int):
        self._max_frames = max_frames
        self._max_frame_bytes = max_frame_bytes
        self._max_total_bytes = max_total_bytes
        self._frames = deque()
        self._total_bytes = 0
        self._lock = Lock()

    def add(self, frame: bytes, *, captured_at: float | None = None) -> bool:
        if (
            not isinstance(frame, bytes)
            or not 0 < len(frame) <= self._max_frame_bytes
            or not _is_decodable_jpeg(frame)
        ):
            return False
        digest = hashlib.sha256(frame).digest()
        captured_at = monotonic() if captured_at is None else captured_at
        with self._lock:
            for index, candidate in enumerate(self._frames):
                if candidate[1] == digest:
                    _old_timestamp, _digest, existing = candidate
                    del self._frames[index]
                    self._frames.append((captured_at, digest, existing))
                    return False
            self._frames.append((captured_at, digest, frame))
            self._total_bytes += len(frame)
            while (
                len(self._frames) > self._max_frames
                or self._total_bytes > self._max_total_bytes
            ):
                _timestamp, _digest, removed = self._frames.popleft()
                self._total_bytes -= len(removed)
        return True

    def select(self, count: int, *, now: float | None = None, max_age: float) -> list[bytes]:
        now = monotonic() if now is None else now
        with self._lock:
            return [
                frame for captured_at, _digest, frame in reversed(self._frames)
                if 0 <= now - captured_at <= max_age
            ][:count]

    def materialise(
        self, output_directory: Path, count: int, *,
        now: float | None = None, max_age: float,
    ) -> tuple[Path, ...]:
        frames = self.select(count, now=now, max_age=max_age)
        if not frames:
            return ()
        directory = _ensure_private_directory(output_directory)
        paths = []
        try:
            for frame in frames:
                paths.append(write_private_frame(directory, frame))
            return tuple(paths)
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise

    def status(self, *, now: float | None = None, max_age: float = 1.0) -> dict:
        now = monotonic() if now is None else now
        with self._lock:
            latest = self._frames[-1][0] if self._frames else None
            count = len(self._frames)
        age_ms = None if latest is None else max(0, round((now - latest) * 1000))
        return {
            "ready": age_ms is not None and age_ms <= round(max_age * 1000),
            "buffered_frames": count,
            "latest_frame_age_ms": age_ms,
        }


class HotStreamBuffer:
    def __init__(self, config: HotStreamConfig, *, popen=subprocess.Popen, clock=monotonic):
        self.config = config
        self.output_directory = config.output_directory
        self._popen = popen
        self._clock = clock
        self._ring = HotFrameRing(
            max_frames=config.frame_count,
            max_frame_bytes=config.max_frame_bytes,
            max_total_bytes=config.max_total_bytes,
        )
        self._restart_count = 0
        self._process = None
        fps = f"{config.sample_fps:g}"
        self.command = (
            FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", config.source_url,
            "-map", "0:v:0", "-an", "-vf", f"fps={fps}",
            "-q:v", "3", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
        )
        self.child_environment = {"LANG": "C", "LC_ALL": "C"}

    def select(self, _received_at=None) -> tuple[Path, ...]:
        selected = self._ring.materialise(
            self.output_directory, self.config.selection_count,
            now=self._clock(), max_age=self.config.max_age_seconds,
        )
        LOGGER.info(
            "gate_hot_stream selection_count=%d ready=%s",
            len(selected), bool(selected),
        )
        return selected

    def status(self) -> dict:
        ring = self._ring.status(now=self._clock(), max_age=self.config.max_age_seconds)
        return {
            "enabled": self.config.enabled,
            **ring,
            "stream": "fluent",
            "sample_fps": self.config.sample_fps,
            "source_profile": {
                "codec": "h264", "width": 640, "height": 360, "fps": 10,
            },
            "restart_count": self._restart_count,
        }

    def run_forever(self, stop_event) -> None:
        if not self.config.enabled:
            stop_event.wait()
            return
        _ensure_private_directory(self.output_directory)
        while not stop_event.is_set():
            parser = JpegStreamParser(self.config.max_frame_bytes)
            try:
                process = self._popen(
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=self.child_environment,
                    close_fds=True,
                )
                self._process = process
                while not stop_event.is_set():
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    for frame in parser.feed(chunk):
                        self._ring.add(frame, captured_at=self._clock())
            except (OSError, ValueError):
                LOGGER.warning("gate_hot_stream outcome=restart reason=stream_error")
            finally:
                self._stop_child()
            if not stop_event.is_set():
                self._restart_count += 1
                stop_event.wait(self.config.restart_seconds)

    def close(self) -> None:
        self._stop_child()

    def _stop_child(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def write_private_frame(directory: Path, frame: bytes) -> Path:
    """Write one JPEG into an owner-only directory with a fresh random name."""
    target = Path(directory) / f"frame-{secrets.token_hex(12)}.jpg"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    completed = False
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(frame)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("frame write failed")
            view = view[written:]
        completed = True
    finally:
        try:
            os.close(descriptor)
        finally:
            if not completed:
                target.unlink(missing_ok=True)
    return target


def _ensure_private_directory(path: Path) -> Path:
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    except FileNotFoundError as error:
        raise ValueError("hot stream output parent is missing") from error
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("hot stream output directory must be owner-only")
    return path


def _is_decodable_jpeg(data: bytes) -> bool:
    if data[:3] != b"\xff\xd8\xff":
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG":
                    return False
                image.verify()
        return True
    except (
        OSError, ValueError, Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        return False


def _boolean(value) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("GATE_HOT_STREAM_ENABLED must be true or false")


def _integer(value, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("hot stream integer configuration is invalid") from error
    if isinstance(value, bool) or not minimum <= parsed <= maximum:
        raise ValueError("hot stream integer configuration is outside the safe range")
    return parsed


def _number(value, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("hot stream numeric configuration is invalid") from error
    if not minimum <= parsed <= maximum:
        raise ValueError("hot stream numeric configuration is outside the safe range")
    return parsed
