"""Read every live clear-stream frame on the device until one opens the gate.

The capture series takes three frames seconds apart and hands each to the
processor, where a local miss costs a paid cloud lookup. With the on-device
reader answering in about 175 ms, the session decoder's 5 fps output can be
read frame by frame instead: the *sweep* runs the local reader over each new
session frame for a bounded window, spends nothing on the cloud, and injects
a frame into the ordinary burst pipeline only when the reader's own answer is
already an authorised plate under the policy band in force. The read travels
with the frame (:class:`SweepRead` carries the recogniser's own answer and the
digest of the frame it was taken from), so the processor judges *that* read
rather than taking another: it applies the confidence gate, the shared
matching under the band in force at that moment, and every existing safeguard
(freshness, authorisation re-check under the relay lock, cooldown,
idempotency) exactly once, in the ordinary pipeline. The sweep decides nothing
itself.

Until 2026-09-21 the processor read the injected frame a second time. That is
not a second opinion. The sweep encodes the plate band at JPEG quality 90 and
the pipeline's upload at 85, and the recogniser's weakest-character score is
sensitive enough to that alone to cross the bar: on 2026-09-20 at 19:16:14 the
sweep read ``10CE1990`` at 0.877, the pipeline read the same frame at 0.723,
and the driver waited another eleven seconds. See ``docs/local-recognition.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from hashlib import sha256
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
    #: What travels with the frame when it is injected, so the pipeline judges
    #: this read instead of taking another. ``recognition`` is the recogniser's
    #: own answer, unaltered; ``frame_digest`` is the SHA-256 of the *whole
    #: frame* it was taken from, which is the identity the pipeline gives the
    #: file, so a read can only ever be adopted for the pixels it came from;
    #: ``image`` and ``geometry`` are the exact bytes the model saw and how
    #: they map onto the frame, for the corpus and the plate box. All four are
    #: None on a read that produced no answer.
    recognition: object | None = field(default=None, repr=False, compare=False)
    frame_digest: str | None = field(default=None, compare=False)
    image: bytes | None = field(default=None, repr=False, compare=False)
    geometry: object | None = field(default=None, repr=False, compare=False)

    @property
    def recognised(self) -> bool:
        return bool(self.plate)

    @property
    def carried(self) -> bool:
        """Whether there is a completed read here for the pipeline to adopt."""
        return self.recognition is not None and bool(self.frame_digest)


@dataclass(frozen=True)
class SweepGeometry:
    """How the band the sweep read maps back onto the camera frame.

    The attribute names are the ones :func:`local_recognizer.box_to_frame`
    reads, so a sweep read's plate box lands in whole-frame fractions exactly
    as a pipeline read's does. Without it the box was in fractions of the
    *band*, which is a different scale from every other box in the journal.
    """

    frame_width: int
    frame_height: int
    crop_left: int
    crop_top: int
    crop_width: int
    crop_height: int
    upload_width: int
    upload_height: int
    precropped: bool = False
    cropped: bool = True


def crop_to_region(frame: bytes, region: PlateRegion | None) -> bytes:
    """The plate band of ``frame`` as JPEG bytes; the frame itself without a region.

    Any decode problem returns the frame unchanged so a sweep read still
    happens on something rather than nothing.
    """
    return crop_with_geometry(frame, region)[0]


def crop_with_geometry(frame: bytes, region: PlateRegion | None):
    """``(band bytes, SweepGeometry or None)`` for one frame. Never raises."""
    if region is None:
        return frame, None
    try:
        from PIL import Image

        with Image.open(BytesIO(frame)) as image:
            width, height = image.size
            left, top, right, bottom = region.pixel_box(width, height)
            if left == 0 and top == 0 and right == width and bottom == height:
                return frame, None
            cropped = image.convert("RGB").crop((left, top, right, bottom))
            output = BytesIO()
            cropped.save(output, format="JPEG", quality=SWEEP_JPEG_QUALITY)
            geometry = SweepGeometry(
                width, height, left, top, right - left, bottom - top,
                right - left, bottom - top,
            )
            return output.getvalue(), geometry
    except Exception:
        return frame, None


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
            image, geometry = crop_with_geometry(frame, self._plate_region)
            handle = self._recognizer.begin(
                image, trace_id=trace_id, geometry=geometry,
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
        # Only a read that actually completed is worth carrying: a busy or
        # timed-out reader said nothing about the frame, and the pipeline
        # should read it for itself.
        completed = str(status) in ("recognized", "no_plate")
        return SweepRead(
            status=str(status), plate=plate, score=score, authorised=authorised,
            read_ms=(self._clock() - started) * 1000.0,
            recognition=recognition if completed else None,
            frame_digest=sha256(frame).hexdigest() if completed else None,
            image=image if completed else None,
            geometry=geometry if completed else None,
        )
