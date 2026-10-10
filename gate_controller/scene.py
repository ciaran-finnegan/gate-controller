"""A low-resolution memory of what the empty drive looks like.

The keyframe decoder sees the scene every second. While no camera event has
happened for a while, a small grayscale thumbnail of the latest frame is kept
as the baseline. A candidate frame that barely differs from that baseline
shows an empty drive: either the alarm fired before the vehicle entered the
picture (the pre-alarm ring frame at 19:33 on 2026-09-05) or the vehicle has
already left. Either way it is not worth an OCR request, and in the presence
session it is the departure signal the review asked for.

The baseline is a picture of the drive *under the light it had when it was
taken*, and at night the light is the camera's own spotlight, which comes on
with motion a minute before the alarm and goes off some 20 s after the last
of it. On 2026-10-09 at 21:42 the baseline had been refreshed under that
spotlight; when the departing Audi took the light with it the drive went
black (thumbnail mean 7.4 against the lit baseline's 106, difference 0.385),
nothing matched the baseline again, and the sweep read 152 black frames to
its cap. Black against black measures 0.0013. So the newest *dark* idle
frame is kept as well, and :meth:`SceneBaseline.dark_difference` scores a
dark frame against it. It is deliberately a separate question from
:meth:`SceneBaseline.difference`: a plate lamp lights about nine pixels of
the 5,184 in a thumbnail, which is below the noise between two black frames,
so a dark frame that matches the dark baseline may still carry a readable
plate and must be read, not skipped. The sweep's waiting phase asks the
reader as well before it concludes anything from a dark match.
"""
from collections.abc import Callable
from io import BytesIO
from time import monotonic
import warnings

from PIL import Image

THUMBNAIL_SIZE = (96, 54)
DEFAULT_IDLE_SECONDS = 60.0
DEFAULT_REFRESH_SECONDS = 30.0
# Mean thumbnail luma (0..255) below which a frame is dark: the spotlight and
# the IR illuminator are both off and nothing in the picture is lit. The black
# stills of 2026-10-09 measure 7.4-7.5 with the camera's white overlay text
# included; the same drive under the spotlight measures 71-106, and the early
# trigger's luma sat at about 6 before the light came on and 41 after. Any
# headlight in the picture, pointing anywhere, puts the mean well above this.
DARK_THUMBNAIL_LUMA = 20.0


def thumbnail_mean(thumbnail: list[int]) -> float:
    """Mean luma of a thumbnail, 0..255; 0.0 for an empty one."""
    return sum(thumbnail) / len(thumbnail) if thumbnail else 0.0


def is_dark(thumbnail: list[int]) -> bool:
    return thumbnail_mean(thumbnail) < DARK_THUMBNAIL_LUMA


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
        # The newest idle frame that was itself dark. Kept alongside the
        # baseline, not instead of it, so a lit refresh (the spotlight came on)
        # does not forget what the dark drive looks like.
        self._dark_baseline: list[int] | None = None
        self._dark_baseline_at: float | None = None
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
        if is_dark(thumbnail):
            self._dark_baseline = thumbnail
            self._dark_baseline_at = now
        return True

    def difference(self, frame: bytes) -> float | None:
        """How far a frame is from the idle scene, 0..1, or None without a baseline."""
        if self._baseline is None:
            return None
        thumbnail = frame_thumbnail(frame)
        if thumbnail is None:
            return None
        return thumbnail_difference(self._baseline, thumbnail)

    def dark_difference(self, frame: bytes) -> float | None:
        """How far a *dark* frame is from the dark idle scene, or None.

        None whenever the question does not apply: the frame is not dark, no
        dark idle frame has been seen, or the frame cannot be decoded. A small
        value says the picture is the unlit drive as it was last seen idle,
        and nothing more -- see the module docstring for why that alone does
        not mean there is nothing to read.
        """
        if self._dark_baseline is None:
            return None
        thumbnail = frame_thumbnail(frame)
        if thumbnail is None or not is_dark(thumbnail):
            return None
        return thumbnail_difference(self._dark_baseline, thumbnail)

    def status(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        return {
            "available": self._baseline is not None,
            "age_seconds": (
                None if self._baseline_at is None else max(0.0, round(now - self._baseline_at, 1))
            ),
            "refreshes": self._refreshes,
            "dark_available": self._dark_baseline is not None,
            "dark_age_seconds": (
                None if self._dark_baseline_at is None
                else max(0.0, round(now - self._dark_baseline_at, 1))
            ),
        }
