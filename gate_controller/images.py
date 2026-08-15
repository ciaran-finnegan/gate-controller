import hashlib
import warnings
from pathlib import Path
from time import monotonic, sleep

from PIL import Image, ImageFilter, ImageStat


MAX_IMAGE_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = min(Image.MAX_IMAGE_PIXELS or MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)

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
