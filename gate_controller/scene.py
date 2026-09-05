"""A low-resolution memory of what the empty drive looks like.

The keyframe decoder sees the scene every second. While no camera event has
happened for a while, a small grayscale thumbnail of the latest frame is kept
as the baseline. A candidate frame that barely differs from that baseline
shows an empty drive: either the alarm fired before the vehicle entered the
picture (the pre-alarm ring frame at 19:33 on 2026-09-05) or the vehicle has
already left. Either way it is not worth an OCR request, and in the presence
session it is the departure signal the review asked for.
"""
from collections.abc import Callable
from io import BytesIO
from time import monotonic
import warnings

from PIL import Image

THUMBNAIL_SIZE = (96, 54)
DEFAULT_IDLE_SECONDS = 60.0
DEFAULT_REFRESH_SECONDS = 30.0


def frame_thumbnail(frame: bytes) -> list[int] | None:
    """Grayscale pixels of a small thumbnail, or None for an undecodable frame."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(frame)) as image:
                if image.format != "JPEG":
                    return None
                image.draft("L", THUMBNAIL_SIZE)
                image.load()
                image = image.convert("L")
                image = image.resize(THUMBNAIL_SIZE, Image.Resampling.BILINEAR)
                return list(image.getdata())
    except Exception:
        return None


def thumbnail_difference(left: list[int], right: list[int]) -> float:
    """Mean absolute pixel difference, normalised to 0..1."""
    if not left or len(left) != len(right):
        return 1.0
    return sum(abs(a - b) for a, b in zip(left, right)) / (255.0 * len(left))


class SceneBaseline:
    """Remember the idle scene and score how much a frame departs from it."""

    def __init__(self, *, idle_seconds: float = DEFAULT_IDLE_SECONDS,
                 refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
                 clock: Callable[[], float] = monotonic):
        if not idle_seconds >= 0 or not refresh_seconds > 0:
            raise ValueError("scene baseline timings must be non-negative and refresh positive")
        self._idle_seconds = idle_seconds
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._baseline: list[int] | None = None
        self._baseline_at: float | None = None
        self._last_activity: float | None = None
        self._refreshes = 0

    def note_activity(self, now: float | None = None) -> None:
        """A camera event: the scene is busy, so stop refreshing the baseline."""
        self._last_activity = self._clock() if now is None else now

    def observe(self, frame: bytes, now: float | None = None) -> bool:
        """Offer a decoded keyframe; it becomes the baseline when the scene is idle."""
        now = self._clock() if now is None else now
        if self._last_activity is not None and now - self._last_activity < self._idle_seconds:
            return False
        if self._baseline_at is not None and now - self._baseline_at < self._refresh_seconds:
            return False
        thumbnail = frame_thumbnail(frame)
        if thumbnail is None:
            return False
        self._baseline = thumbnail
        self._baseline_at = now
        self._refreshes += 1
        return True

    def difference(self, frame: bytes) -> float | None:
        """How far a frame is from the idle scene, 0..1, or None without a baseline."""
        if self._baseline is None:
            return None
        thumbnail = frame_thumbnail(frame)
        if thumbnail is None:
            return None
        return thumbnail_difference(self._baseline, thumbnail)

    def status(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        return {
            "available": self._baseline is not None,
            "age_seconds": (
                None if self._baseline_at is None else max(0.0, round(now - self._baseline_at, 1))
            ),
            "refreshes": self._refreshes,
        }
