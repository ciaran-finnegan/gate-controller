"""Read every live clear-stream frame on the device until one opens the gate.

The capture series takes three frames seconds apart and hands each to the
processor, where a local miss costs a paid cloud lookup. With the on-device
reader answering in about 175 ms, the session decoder's 5 fps output can be
read frame by frame instead: the *sweep* runs the local reader over each new
session frame for a bounded window, spends nothing on the cloud, and injects
a frame into the ordinary burst pipeline only when the reader's own answer is
already an authorised plate under the policy band in force. The processor
then reads that frame again through its normal local pass and every existing
safeguard (freshness, authorisation re-check under the relay lock, cooldown,
idempotency) applies unchanged. The sweep decides nothing itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from time import monotonic

from .plate_region import PlateRegion


LOGGER = logging.getLogger(__name__)
SWEEP_JPEG_QUALITY = 90
DEFAULT_READ_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class SweepRead:
    """What the local reader said about one sweep frame."""

    status: str
    plate: str | None = None
    score: float = 0.0
    authorised: bool = False
    read_ms: float = 0.0

    @property
    def recognised(self) -> bool:
        return bool(self.plate)


def crop_to_region(frame: bytes, region: PlateRegion | None) -> bytes:
    """The plate band of ``frame`` as JPEG bytes; the frame itself without a region.

    Any decode problem returns the frame unchanged so a sweep read still
    happens on something rather than nothing.
    """
    if region is None:
        return frame
    try:
        from PIL import Image

        with Image.open(BytesIO(frame)) as image:
            width, height = image.size
            left, top, right, bottom = region.pixel_box(width, height)
            if left == 0 and top == 0 and right == width and bottom == height:
                return frame
            cropped = image.convert("RGB").crop((left, top, right, bottom))
            output = BytesIO()
            cropped.save(output, format="JPEG", quality=SWEEP_JPEG_QUALITY)
            return output.getvalue()
    except Exception:
        return frame


class LocalSweepReader:
    """The local reader plus the providers the processor uses, for sweep frames.

    ``authorised`` and ``match_policy`` are the very same providers handed to
    the processor and the OCR client, so a sweep frame is admitted under the
    same band and the same plate list the processor will re-check it against.
    """

    def __init__(self, recognizer, *, authorised=None, match_policy=None,
                 plate_region: PlateRegion | None = None,
                 read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
                 clock=monotonic):
        self._recognizer = recognizer
        self._authorised = authorised
        self._match_policy = match_policy
        self._plate_region = plate_region
        self._read_timeout = read_timeout_seconds
        self._clock = clock

    def available(self) -> bool:
        """Only an active, loaded recogniser may sweep; shadow mode never does."""
        recognizer = self._recognizer
        if recognizer is None:
            return False
        try:
            # `active` is the configured mode *and* a loaded, ready engine.
            return bool(recognizer.active)
        except Exception:
            return False

    def read(self, frame: bytes, *, trace_id: str | None = None) -> SweepRead:
        """Read one frame locally and say whether it alone authorises.

        Never raises. A reader that is busy with another frame, unavailable,
        or out of time answers ``unavailable`` and the sweep simply moves on
        to the next frame.
        """
        started = self._clock()
        try:
            image = crop_to_region(frame, self._plate_region)
            handle = self._recognizer.begin(
                image, trace_id=trace_id,
                authorised=self._authorised, policy=self._match_policy,
            )
            recognition = handle.result(self._read_timeout)
            authorised = bool(self._recognizer.decides(handle, recognition))
        except Exception:
            LOGGER.warning("gate_local_sweep stage=read_failed")
            return SweepRead(status="error", read_ms=(self._clock() - started) * 1000.0)
        status = getattr(recognition, "status", "unavailable")
        plate = getattr(recognition, "plate", None) or None
        score = getattr(recognition, "score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        return SweepRead(
            status=str(status), plate=plate, score=score, authorised=authorised,
            read_ms=(self._clock() - started) * 1000.0,
        )
