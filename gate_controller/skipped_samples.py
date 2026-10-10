"""One skipped frame of each kind per sweep, kept for a person to look at.

The sweep skips a frame without reading it for two reasons: the picture shows
the idle drive (``empty``) or its plate band is one flat colour, a frame the
decoder could not finish (``corrupt``). Both were only ever counted, so when a
sweep logged ``reads=0`` nobody could tell which test had thrown the frames
away, or whether the test was right. This keeps the first frame of each kind
from each sweep, named by when the sweep started (UTC) and the reason, e.g.
``20261008T092415123Z-empty.jpg``.

Bounded and private: at most ``max_files`` files and ``max_bytes`` in total,
oldest pruned first (the name sorts by time, so the order is the name's, never
the wall clock's), in an owner-only directory, files 0600. It is called from
the sweep loop and must never raise into it or slow it: one small write, in a
try, debug-logged when it fails.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .hot_stream import _ensure_private_directory, write_private_frame

LOGGER = logging.getLogger(__name__)

DEFAULT_SKIPPED_SAMPLE_FILES = 50
MAX_SKIPPED_SAMPLE_FILES = 1000
# 50 frames at the 1920 px the sweep decodes is about 10-15 MB.
DEFAULT_SKIPPED_SAMPLE_BYTES = 24 * 1024 * 1024
SAMPLE_REASONS = ("empty", "corrupt")


class SkippedFrameSamples:
    def __init__(
        self, directory: Path | None, *, max_files: int = DEFAULT_SKIPPED_SAMPLE_FILES,
        max_bytes: int = DEFAULT_SKIPPED_SAMPLE_BYTES,
    ) -> None:
        self.directory = None if directory is None else Path(directory)
        self.max_files = max_files
        self.max_bytes = max_bytes

    @property
    def enabled(self) -> bool:
        return self.directory is not None and self.max_files > 0 and self.max_bytes > 0

    def trim(self) -> None:
        """Bring what an earlier run left into the current caps (0 files removes all).

        Called once at start-up, so lowering or disabling the cap takes effect
        on the samples already on disk. Creates nothing. Never raises.
        """
        try:
            if self.directory is not None and self.directory.is_dir():
                self._prune(self.directory, keep=None)
        except Exception:
            LOGGER.debug("gate_local_sweep stage=skipped_sample_trim_failed", exc_info=True)

    def keep(self, frame: bytes, started_at: datetime, reason: str) -> str | None:
        """Write ``frame`` as the sweep's sample for ``reason``; its file name, or None.

        Never raises.
        """
        if not self.enabled:
            return None
        try:
            if len(frame) > self.max_bytes:
                return None
            stamp = started_at.astimezone(timezone.utc)
            name = f"{stamp:%Y%m%dT%H%M%S}{stamp.microsecond // 1000:03d}Z-{reason}.jpg"
            directory = _ensure_private_directory(self.directory)
            write_private_frame(directory, frame, name=name)
            self._prune(directory, keep=name)
            return name
        except Exception:
            LOGGER.debug("gate_local_sweep stage=skipped_sample_failed reason=%s", reason,
                         exc_info=True)
            return None

    def _prune(self, directory: Path, *, keep: str | None) -> None:
        sizes: list[tuple[str, int]] = []
        for path in directory.glob("*.jpg"):
            try:
                sizes.append((path.name, path.stat().st_size))
            except OSError:
                continue
        sizes.sort()
        total = sum(size for _name, size in sizes)
        count = len(sizes)
        for name, size in sizes:
            if count <= max(0, self.max_files) and total <= max(0, self.max_bytes):
                break
            if name == keep:
                continue
            try:
                os.unlink(directory / name)
            except OSError:
                continue
            count -= 1
            total -= size
