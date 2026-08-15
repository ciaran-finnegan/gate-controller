import hashlib
import warnings
from pathlib import Path
from time import monotonic, sleep

from PIL import Image, ImageFilter, ImageStat

from .telemetry import FrameTelemetry


MAX_IMAGE_PIXELS = 16_000_000
QUALITY_SIZE = (320, 180)
QUALITY_UNAVAILABLE_DIGEST = hashlib.sha256(b"quality_unavailable").hexdigest()
Image.MAX_IMAGE_PIXELS = min(Image.MAX_IMAGE_PIXELS or MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)


def measure_frame_quality(path: Path) -> FrameTelemetry:
    """Return bounded, downsampled quality proxies without exposing image paths."""
    path = Path(path)
    try:
        digest = _content_digest(path)
    except OSError:
        digest = QUALITY_UNAVAILABLE_DIGEST

    try:
        if not _has_jpeg_signature(path):
            raise ValueError("invalid image signature")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != "JPEG":
                    raise ValueError("invalid image format")
                width, height = image.size
                image.draft("L", QUALITY_SIZE)
                image.load()
                image.thumbnail(QUALITY_SIZE, Image.Resampling.BILINEAR)
                grayscale = image.convert("L")

        histogram = grayscale.histogram()
        pixel_count = max(sum(histogram), 1)
        brightness = sum(value * count for value, count in enumerate(histogram)) / (
            255 * pixel_count
        )
        darkness = sum(histogram[:33]) / pixel_count
        highlight_clipping = sum(histogram[240:]) / pixel_count
        sharpness = _sharpness_proxy(grayscale)
        return FrameTelemetry(
            sequence=0,
            digest=digest,
            width=width,
            height=height,
            sharpness=sharpness,
            brightness=brightness,
            darkness=darkness,
            highlight_clipping=highlight_clipping,
        )
    except (
        OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError,
    ):
        return FrameTelemetry(
            sequence=0,
            digest=digest,
            width=1,
            height=1,
            sharpness=0.0,
            brightness=0.0,
            darkness=0.0,
            highlight_clipping=0.0,
            status="quality_unavailable",
        )


def _sharpness_proxy(grayscale: Image.Image) -> float:
    if grayscale.width < 3 or grayscale.height < 3:
        return 0.0
    laplacian = grayscale.filter(ImageFilter.Kernel(
        (3, 3),
        (0, 1, 0, 1, -4, 1, 0, 1, 0),
        scale=1,
        offset=128,
    ))
    interior = laplacian.crop((1, 1, laplacian.width - 1, laplacian.height - 1))
    histogram = interior.histogram()
    pixel_count = max(sum(histogram), 1)
    mean_deviation = sum(
        abs(value - 128) * count for value, count in enumerate(histogram)
    ) / pixel_count
    return min(mean_deviation / 128, 1.0)


def wait_until_readable(path: Path, timeout: float, poll_interval: float = 0.1) -> bool:
    """Wait for a complete, decodable image without treating partial uploads as valid."""
    path = Path(path)
    deadline = monotonic() + max(timeout, 0)
    while True:
        if _is_valid_image(path):
            return True
        if monotonic() >= deadline:
            return False
        sleep(poll_interval)


def rank_images(paths, *, max_bytes: int | None = None) -> list[Path]:
    """Return valid images ordered from sharpest to least sharp."""
    scored_paths = []
    for path in paths:
        path = Path(path)
        try:
            if max_bytes is not None and path.stat().st_size > max_bytes:
                continue
            if not _has_jpeg_signature(path):
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format != "JPEG":
                        continue
                    image.load()
                    grayscale = image.convert("L")
                    edges = grayscale.filter(ImageFilter.FIND_EDGES)
                    sharpness = ImageStat.Stat(edges).var[0]
        except (
            OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError,
        ):
            continue
        scored_paths.append((sharpness, path))
    return [path for _, path in sorted(scored_paths, key=lambda item: (-item[0], _content_digest(item[1])))]


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_valid_image(path: Path) -> bool:
    if not _has_jpeg_signature(path):
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != "JPEG":
                    return False
                image.verify()
        return True
    except (
        OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError,
    ):
        return False


def _has_jpeg_signature(path: Path) -> bool:
    try:
        with Path(path).open("rb") as source:
            return source.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False
