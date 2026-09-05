"""The part of the camera frame where number plates can appear.

Plates only ever occupy a band of the picture: the drive in front of the
gate. Cropping to that band before OCR keeps the plate at native detail while
shrinking the upload, and cropping in the keyframe decoder cuts encode cost.
The region is expressed as fractions of the frame so it survives a camera or
resolution change unchanged.
"""
from dataclasses import dataclass
from math import isfinite

MIN_REGION_FRACTION = 0.1


@dataclass(frozen=True)
class PlateRegion:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in values):
            raise ValueError("plate region values must be finite numbers")
        if not (0 <= self.x < 1 and 0 <= self.y < 1):
            raise ValueError("plate region origin must lie inside the frame")
        if not (MIN_REGION_FRACTION <= self.width <= 1 - self.x + 1e-9):
            raise ValueError("plate region width must be at least 0.1 and fit inside the frame")
        if not (MIN_REGION_FRACTION <= self.height <= 1 - self.y + 1e-9):
            raise ValueError("plate region height must be at least 0.1 and fit inside the frame")

    @property
    def is_full_frame(self) -> bool:
        return self.x == 0 and self.y == 0 and self.width == 1 and self.height == 1

    def pixel_box(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) in pixels, even-aligned for chroma subsampling."""
        left = _even(frame_width * self.x)
        top = _even(frame_height * self.y)
        right = min(frame_width, max(left + 2, _even(frame_width * (self.x + self.width))))
        bottom = min(frame_height, max(top + 2, _even(frame_height * (self.y + self.height))))
        return left, top, right, bottom

    def ffmpeg_crop_filter(self) -> str:
        """A crop filter in input-size expressions, so it applies at any resolution."""
        return (
            f"crop=trunc(iw*{self.width:.4f}/2)*2:trunc(ih*{self.height:.4f}/2)*2"
            f":trunc(iw*{self.x:.4f}/2)*2:trunc(ih*{self.y:.4f}/2)*2"
        )

    def to_frame(self, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """Map (x, y, w, h) fractions of this region to fractions of the whole frame."""
        x, y, w, h = box
        return (self.x + x * self.width, self.y + y * self.height, w * self.width, h * self.height)

    def as_env(self) -> str:
        return f"{self.x:g},{self.y:g},{self.width:g},{self.height:g}"


FULL_FRAME = PlateRegion(0.0, 0.0, 1.0, 1.0)


def _even(value: float) -> int:
    return int(value // 2) * 2


def parse_plate_region(value) -> PlateRegion | None:
    """Parse ``GATE_PLATE_REGION`` as ``x,y,w,h`` frame fractions; empty means none."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError("GATE_PLATE_REGION must be four comma-separated fractions: x,y,w,h")
    try:
        numbers = tuple(float(part) for part in parts)
    except ValueError as error:
        raise ValueError("GATE_PLATE_REGION values must be numbers") from error
    try:
        region = PlateRegion(*numbers)
    except ValueError as error:
        raise ValueError(f"GATE_PLATE_REGION: {error}") from error
    return None if region.is_full_frame else region
