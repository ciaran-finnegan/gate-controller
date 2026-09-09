"""On-device plate recognition beside (and, in active mode, before) the cloud.

Two pretrained ONNX graphs read the same plate-band crop the controller
already uploads to Plate Recognizer: a YOLOv9-tiny detector from
``open-image-models`` and the ``cct-xs-v2-global-model`` recogniser from
``fast-plate-ocr`` (both MIT). Measured on this gate's own frames they give
90.2% exact reads and 98.2% correct gate decisions with zero wrong-plate
accepts, and OCR confidence separates right from wrong cleanly: 0.998 mean on
correct reads against 0.772 on wrong ones. See
``docs/local-recognition.md`` and ``docs/reviews/2026-09-06-*`` for the
measurements this module is built on.

Three modes, chosen by ``GATE_LOCAL_OCR_MODE``:

``off``
    Nothing is imported, loaded or run. The controller behaves exactly as it
    did before this module existed. This is the default in code.
``shadow``
    Every frame sent to the cloud is also read locally, on one background
    worker, and the two answers are journalled. The local answer can never
    reach the relay.
``active``
    The local read runs first on the same crop. A read at or above
    ``GATE_LOCAL_OCR_MIN_CONFIDENCE`` is fed, plate and confidence, into the
    controller's own :func:`~gate_controller.matching.decide_access` -- the
    same function, the same thresholds, exact and fuzzy alike, under the
    same time-of-day matching policy the processor will apply -- and when
    that authorises *this frame's own read*, the observation goes to the
    processor with ``source="local"`` and the cloud request is skipped.
    Recogniser confidence is the only local-specific gate; everything else
    falls through to the cloud exactly as before.

**Which statistic the threshold uses.** ``GATE_LOCAL_OCR_MIN_CONFIDENCE`` is
compared against the **minimum** per-character probability of the read, not
against their mean. The recogniser emits one softmax maximum per character
slot; on a seven-character Irish plate, six characters at 1.00 and one at 0.65
average 0.96 and would clear a 0.95 mean gate, and that one weak character is
precisely the one deciding whether the read is the authorised plate. The
measured separation (0.998 on correct reads against 0.772 on wrong ones) means
a per-character minimum costs very little. The mean is kept beside it as
``mean_score`` -- journal, corpus sidecar, telemetry -- and gates nothing.

**What the active path may spend.** A frame waits at most
``LOCAL_DECISION_TIMEOUT_SECONDS``, never more than the decision budget it was
handed minus ``LOCAL_DECISION_CLOUD_RESERVE_SECONDS`` kept back for the cloud
request, and never more than ``LOCAL_EVENT_WAIT_BUDGET_SECONDS`` in total
across one event's frames. A stuck engine therefore costs one bounded wait per
event, not one per frame.

The models are thermally expensive on a fanless Pi 5 (154 ms mean / 177 ms
p95 per frame single-threaded, ~0.35 C/s of heating), so inference is
single-threaded, the sessions are loaded once at start-up, and a frame is
only ever read on an event. One worker thread means two inferences can never
run at once.

Every dependency is imported lazily. When onnxruntime, open-image-models or
fast-plate-ocr are missing, or the models cannot be fetched, the recogniser
reports ``unavailable`` once and the controller runs exactly as today.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter

from .match_policy import MIN_CONFIGURABLE_CONFIDENCE
from .matching import decide_access, normalise_plate
from .models import PlateObservation


LOGGER = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ACTIVE)

CLOUD_FALLBACK = "fallback"
CLOUD_ALWAYS = "always"
CLOUD_MODES = (CLOUD_FALLBACK, CLOUD_ALWAYS)

#: The 384 detector is the knee of the curve on this hardware: 154 ms mean on
#: one Pi core against the 640 variant's 422 ms, with no accuracy loss
#: observed on the Pi's own frames.
DEFAULT_DETECTOR = "yolo-v9-t-384-license-plate-end2end"
DEFAULT_RECOGNISER = "cct-xs-v2-global-model"
#: The admission gate for a local read, and the *floor under every other
#: local bar*: a read below this never reaches :func:`decide_access` at all,
#: as an observation or as a corroboration.
#:
#: 0.95 was chosen for the shadow-mode measurement (it keeps 415 of 458
#: labelled baseline frames at 98.1% exact) and it is far too high to be the
#: admission gate. ``agreement_min_local_confidence`` ships at 0.50 for
#: ``standard``, and with a 0.95 gate in front of it the agreement rule could
#: never run: the 11:13:48 case on 2026-09-08 -- local ``10CE1990`` at 0.566,
#: cloud the same plate at 0.806 -- was excluded from the corroboration pool
#: before any bar it was written for could see it.
#:
#: 0.5 is what the live Pi has run since 2026-09-08 (owner decision). The
#: authorised-plate match is the real gate, not the recogniser's own score;
#: the 0.75-snapped baseline measured zero wrong-plate accepts. A site that
#: wants the stricter shadow-mode gate sets
#: ``GATE_LOCAL_OCR_MIN_CONFIDENCE`` back to 0.95.
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_THREADS = 1
MAX_THREADS = 4
#: ``ProtectHome=true`` hides ``~/.cache`` from the service, and the state
#: directory is the only place the gate-controller user may write.
DEFAULT_MODEL_DIR = "/var/lib/gate-controller/models"

#: Crops are padded before the recogniser, matching the measured baseline.
CROP_PAD = 0.08
#: Never read more than a handful of boxes from one frame.
MAX_BOXES = 5
#: Bounded latency history behind ``status()``.
MAX_LATENCY_SAMPLES = 256
#: How long the active path will wait for a local read before giving the frame
#: to the cloud. The measured cost is 154 ms mean / 177 ms p95 on one Pi core,
#: so this is a stuck-engine guard, not a normal outcome. It is a ceiling, not
#: an entitlement: the wait is also clamped to the decision budget left and to
#: the event's own budget below.
LOCAL_DECISION_TIMEOUT_SECONDS = 2.0
#: What the local wait must leave behind for the cloud request it may still
#: need to make. A cold handshake to Plate Recognizer costs 0.4-0.8 s over this
#: uplink and the read itself another second or so, so a guard that eats into
#: this would time out the very burst it was meant to accelerate.
LOCAL_DECISION_CLOUD_RESERVE_SECONDS = 2.0
#: Total local waiting one event may pay, across all of its frames. Admission
#: is bounded per frame, but without this a three-frame burst could pay the
#: stuck-engine guard three times over inside one decision budget.
LOCAL_EVENT_WAIT_BUDGET_SECONDS = 2.0
#: How many events' summaries are retained for the telemetry block.
MAX_ITEM_SUMMARIES = 64
#: Local observations kept per event, mirroring the telemetry item bound.
MAX_EVENT_OBSERVATIONS = 8

STATUS_RECOGNIZED = "recognized"
STATUS_NO_PLATE = "no_plate"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
#: Lifecycle state only, never a frame's status: it is reported by
#: :meth:`LocalRecognizer.status` and keeps ``available`` false after a close.
STATUS_CLOSED = "closed"

AGREEMENT_MATCH = "match"
AGREEMENT_LOCAL_ONLY = "local_only"
AGREEMENT_CLOUD_ONLY = "cloud_only"
AGREEMENT_BOTH_NONE = "both_none"
AGREEMENT_MISMATCH = "mismatch"

AUTHORISED_LOCAL = "local_match"
AUTHORISED_CLOUD = "cloud_match"
AUTHORISED_BOTH = "both"
AUTHORISED_NONE = "none"

DECISION_LOCAL = "local"
DECISION_CLOUD = "cloud"
DECISION_NONE = "none"


class LocalRecognizerUnavailable(RuntimeError):
    """The local models could not be loaded, so nothing may run locally."""


@dataclass(frozen=True)
class LocalRecognizerConfig:
    mode: str = MODE_OFF
    detector: str = DEFAULT_DETECTOR
    recogniser: str = DEFAULT_RECOGNISER
    threads: int = DEFAULT_THREADS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    model_dir: Path = Path(DEFAULT_MODEL_DIR)
    cloud: str = CLOUD_FALLBACK

    @property
    def enabled(self) -> bool:
        return self.mode in (MODE_SHADOW, MODE_ACTIVE)

    @property
    def active(self) -> bool:
        return self.mode == MODE_ACTIVE


def load_local_recognizer_config(environment=None) -> LocalRecognizerConfig:
    """Read ``GATE_LOCAL_OCR_*``; an unset environment means ``off``."""
    environment = os.environ if environment is None else environment
    mode = (environment.get("GATE_LOCAL_OCR_MODE") or MODE_OFF).strip().lower()
    if mode not in MODES:
        raise ValueError("GATE_LOCAL_OCR_MODE must be one of off, shadow, active")
    cloud = (environment.get("GATE_LOCAL_OCR_CLOUD") or CLOUD_FALLBACK).strip().lower()
    if cloud not in CLOUD_MODES:
        raise ValueError("GATE_LOCAL_OCR_CLOUD must be fallback or always")
    detector = (environment.get("GATE_LOCAL_OCR_DETECTOR") or DEFAULT_DETECTOR).strip()
    recogniser = (environment.get("GATE_LOCAL_OCR_RECOGNISER") or DEFAULT_RECOGNISER).strip()
    if not detector or not recogniser:
        raise ValueError("GATE_LOCAL_OCR_DETECTOR and GATE_LOCAL_OCR_RECOGNISER must be non-empty")
    raw_threads = str(environment.get("GATE_LOCAL_OCR_THREADS", "") or "").strip()
    try:
        threads = int(raw_threads) if raw_threads else DEFAULT_THREADS
    except ValueError as error:
        raise ValueError("GATE_LOCAL_OCR_THREADS must be an integer") from error
    if not 1 <= threads <= MAX_THREADS:
        raise ValueError(f"GATE_LOCAL_OCR_THREADS must be between 1 and {MAX_THREADS}")
    raw_confidence = str(environment.get("GATE_LOCAL_OCR_MIN_CONFIDENCE", "") or "").strip()
    try:
        min_confidence = float(raw_confidence) if raw_confidence else DEFAULT_MIN_CONFIDENCE
    except ValueError as error:
        raise ValueError("GATE_LOCAL_OCR_MIN_CONFIDENCE must be a number") from error
    if not isfinite(min_confidence) or not 0 < min_confidence <= 1:
        raise ValueError("GATE_LOCAL_OCR_MIN_CONFIDENCE must be above 0 and at most 1")
    if min_confidence < MIN_CONFIGURABLE_CONFIDENCE:
        # The same floor the match bars use, for the same reason: a bar of
        # 1e-9 is a bar no read can fail, which is a typo or a disabled
        # threshold rather than a posture. Fail closed to the shipped
        # default, which is stricter than the floor, not to the value asked
        # for.
        LOGGER.warning(
            "gate_local_ocr key=GATE_LOCAL_OCR_MIN_CONFIDENCE status=rejected "
            "reason=below_floor floor=%.2f using=%.2f",
            MIN_CONFIGURABLE_CONFIDENCE, DEFAULT_MIN_CONFIDENCE,
        )
        min_confidence = DEFAULT_MIN_CONFIDENCE
    directory = (environment.get("GATE_LOCAL_OCR_MODEL_DIR") or DEFAULT_MODEL_DIR).strip()
    model_dir = Path(directory or DEFAULT_MODEL_DIR)
    if not model_dir.is_absolute():
        raise ValueError("GATE_LOCAL_OCR_MODEL_DIR must be an absolute path")
    return LocalRecognizerConfig(
        mode=mode, detector=detector, recogniser=recogniser, threads=threads,
        min_confidence=min_confidence, model_dir=model_dir, cloud=cloud,
    )


@dataclass(frozen=True)
class EngineRead:
    """One candidate read: the text, its confidences and its pixel box.

    ``confidence`` is the weakest character of the read -- the statistic the
    threshold is applied to. ``mean_confidence`` is the arithmetic mean of the
    same characters, kept for telemetry only.
    """

    plate: str
    confidence: float
    detection_confidence: float
    box: tuple[int, int, int, int]
    mean_confidence: float = 0.0


@dataclass(frozen=True)
class EngineResult:
    reads: tuple[EngineRead, ...] = ()
    width: int = 0
    height: int = 0
    decode_ms: float = 0.0
    detect_ms: float = 0.0
    ocr_ms: float = 0.0


@dataclass(frozen=True)
class LocalRecognition:
    """The local answer, shaped like the cloud answer the processor consumes."""

    plate: str | None = None
    #: The weakest character of the read: the statistic the threshold gates on.
    score: float = 0.0
    #: The mean character confidence of the same read: telemetry, never a gate.
    mean_score: float = 0.0
    box: tuple[float, float, float, float] | None = None
    candidates: tuple[dict, ...] = ()
    status: str = STATUS_NO_PLATE
    decode_ms: float = 0.0
    detect_ms: float = 0.0
    ocr_ms: float = 0.0
    total_ms: float = 0.0
    source: str = "local"

    @property
    def recognised(self) -> bool:
        return bool(self.plate)

    def observation(self) -> PlateObservation:
        # `cloud_lookup=False` is the read as the recogniser produced it: no
        # request left the Pi for this plate. The OCR client overwrites it when
        # the frame's cloud request went out anyway, which is what `always`
        # mode does.
        return PlateObservation(
            plate=self.plate, confidence=self.score, source="local",
            cloud_lookup=False,
        )

    def to_sidecar(self) -> dict:
        """The bounded ``local`` block written beside every corpus frame."""
        payload: dict[str, object] = {
            "status": self.status,
            "plate": self.plate,
            "score": round(float(self.score), 6),
            "mean_score": round(float(self.mean_score), 6),
            "latency_ms": {
                "decode": round(float(self.decode_ms), 3),
                "detect": round(float(self.detect_ms), 3),
                "ocr": round(float(self.ocr_ms), 3),
                "total": round(float(self.total_ms), 3),
            },
            "candidates": [dict(candidate) for candidate in self.candidates[:MAX_BOXES]],
        }
        if self.box is not None:
            payload["box"] = [round(float(value), 6) for value in self.box]
        return payload


def unavailable_recognition(total_ms: float = 0.0) -> LocalRecognition:
    return LocalRecognition(status=STATUS_UNAVAILABLE, total_ms=total_ms)


class OnnxPlateReadEngine:
    """Detector + recogniser on onnxruntime CPU. Every import is lazy."""

    def __init__(self, config: LocalRecognizerConfig) -> None:
        self._config = config
        self._detector = None
        self._recogniser = None
        self._grayscale = False
        self._cv2 = None
        self._numpy = None

    def load(self) -> None:
        try:
            import cv2
            import numpy
            import onnxruntime
            from fast_plate_ocr.inference import hub as ocr_hub
            from fast_plate_ocr import LicensePlateRecognizer
            from open_image_models.detection.core.hub import (
                DETECTION_MODELS, download_model as download_detector,
            )
            from open_image_models.detection.factory import create_detector
        except Exception as error:  # pragma: no cover - depends on the host
            raise LocalRecognizerUnavailable("import_failed") from error

        model_dir = Path(self._config.model_dir)
        try:
            model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise LocalRecognizerUnavailable("model_dir_unwritable") from error

        options = onnxruntime.SessionOptions()
        # One core, one graph at a time: the board is thermally limited, not
        # compute limited, and four threads heat it ten times faster.
        options.intra_op_num_threads = self._config.threads
        options.inter_op_num_threads = 1
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL

        try:
            spec = DETECTION_MODELS[self._config.detector]
        except KeyError as error:
            raise LocalRecognizerUnavailable("unknown_detector") from error
        try:
            detector_path = download_detector(
                self._config.detector, save_directory=model_dir / self._config.detector,
            )
            self._detector = create_detector(
                detector_path, backend=spec.backend, class_labels=spec.class_labels,
                providers=["CPUExecutionProvider"], sess_options=options,
            )
        except Exception as error:
            raise LocalRecognizerUnavailable("detector_load_failed") from error

        try:
            onnx_path, config_path = ocr_hub.download_model(
                model_name=self._config.recogniser,
                save_directory=model_dir / self._config.recogniser,
            )
            self._recogniser = LicensePlateRecognizer(
                onnx_model_path=onnx_path, plate_config_path=config_path,
                device="cpu", sess_options=options,
            )
        except Exception as error:
            raise LocalRecognizerUnavailable("recogniser_load_failed") from error

        self._grayscale = getattr(self._recogniser.config, "image_color_mode", "") == "grayscale"
        self._cv2 = cv2
        self._numpy = numpy

    def warmup(self) -> None:
        """One inference on a synthetic frame, so the first real frame is not slow."""
        numpy = self._numpy
        blank = numpy.zeros((256, 512, 3), dtype=numpy.uint8)
        self._detector.predict(blank)
        config = self._recogniser.config
        shape = (
            (config.img_height, config.img_width)
            if self._grayscale
            else (config.img_height, config.img_width, 3)
        )
        self._recogniser.run_one(numpy.zeros(shape, dtype=numpy.uint8))

    def read(self, image: bytes) -> EngineResult:
        cv2, numpy = self._cv2, self._numpy
        started = perf_counter()
        frame = cv2.imdecode(numpy.frombuffer(image, dtype=numpy.uint8), cv2.IMREAD_COLOR)
        decode_ms = (perf_counter() - started) * 1000
        if frame is None:
            raise ValueError("local recogniser could not decode the frame")
        height, width = frame.shape[:2]

        started = perf_counter()
        detections = self._detector.predict(frame)
        detect_ms = (perf_counter() - started) * 1000
        detections = sorted(
            detections, key=lambda item: float(item.confidence), reverse=True
        )[:MAX_BOXES]

        reads: list[EngineRead] = []
        ocr_ms = 0.0
        for detection in detections:
            box = detection.bounding_box
            crop, coordinates = _crop_with_pad(frame, box, width, height)
            if crop is None:
                continue
            prepared = cv2.cvtColor(
                crop, cv2.COLOR_BGR2GRAY if self._grayscale else cv2.COLOR_BGR2RGB
            )
            started = perf_counter()
            prediction = self._recogniser.run_one(prepared, return_confidence=True)
            ocr_ms += (perf_counter() - started) * 1000
            raw = str(prediction.plate or "")
            text = normalise_plate(raw)
            scores = character_scores(raw, getattr(prediction, "char_probs", None))
            reads.append(EngineRead(
                plate=text,
                confidence=min(scores) if scores else 0.0,
                mean_confidence=sum(scores) / len(scores) if scores else 0.0,
                detection_confidence=float(detection.confidence), box=coordinates,
            ))
        return EngineResult(
            reads=tuple(reads), width=width, height=height,
            decode_ms=decode_ms, detect_ms=detect_ms, ocr_ms=ocr_ms,
        )


def character_scores(raw_text: str, probabilities) -> tuple[float, ...]:
    """The per-character probabilities of the characters actually read.

    ``char_probs`` carries one softmax maximum per character *slot*, including
    the recogniser's padding slots. The library removes only *trailing*
    padding (``rstrip(pad_char)``), so slicing the probabilities by the length
    of the normalised plate would silently shift by one for a read with an
    interior pad or separator. Pairing them positionally instead, and keeping
    only the slots whose character survives :func:`normalise_plate`, cannot
    misalign whatever the recogniser emits.

    A non-finite probability is reported as 0.0 rather than dropped: a slot
    the recogniser could not score must fail the confidence gate, not vanish
    from it.
    """
    if probabilities is None:
        return ()
    ravel = getattr(probabilities, "ravel", None)
    if callable(ravel):
        # One image, one row: flatten so a (1, slots) array pairs with the
        # text character by character rather than row by row.
        try:
            probabilities = ravel()
        except Exception:
            pass
    scores: list[float] = []
    for character, probability in zip(raw_text, probabilities):
        if not normalise_plate(character):
            continue
        try:
            value = float(probability)
        except (TypeError, ValueError):
            value = 0.0
        scores.append(value if isfinite(value) else 0.0)
    return tuple(scores)


def _crop_with_pad(frame, box, width: int, height: int):
    box_width = box.x2 - box.x1
    box_height = box.y2 - box.y1
    left = max(0, int(box.x1 - CROP_PAD * box_width))
    top = max(0, int(box.y1 - CROP_PAD * box_height))
    right = min(width, int(box.x2 + CROP_PAD * box_width))
    bottom = min(height, int(box.y2 + CROP_PAD * box_height))
    if right <= left or bottom <= top:
        left, top = max(0, int(box.x1)), max(0, int(box.y1))
        right, bottom = min(width, int(box.x2)), min(height, int(box.y2))
    if right <= left or bottom <= top:
        return None, (left, top, right, bottom)
    crop = frame[top:bottom, left:right]
    if crop is None or getattr(crop, "size", 0) == 0:
        return None, (left, top, right, bottom)
    return crop, (left, top, right, bottom)


def box_to_frame(box, geometry, plate_region=None, width: int = 0, height: int = 0):
    """Map a pixel box on the uploaded crop to fractions of the whole frame.

    Mirrors ``PlateRecognizerClient._log_plate_box`` so a local box and a
    cloud box are directly comparable in the journal and the corpus.
    """
    try:
        x1, y1, x2, y2 = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    upload_width = float(getattr(geometry, "upload_width", 0) or width or 0)
    upload_height = float(getattr(geometry, "upload_height", 0) or height or 0)
    if upload_width <= 0 or upload_height <= 0:
        return None
    frame_width = float(getattr(geometry, "frame_width", 0) or upload_width)
    frame_height = float(getattr(geometry, "frame_height", 0) or upload_height)
    crop_left = float(getattr(geometry, "crop_left", 0) or 0)
    crop_top = float(getattr(geometry, "crop_top", 0) or 0)
    crop_width = float(getattr(geometry, "crop_width", 0) or upload_width)
    crop_height = float(getattr(geometry, "crop_height", 0) or upload_height)
    if frame_width <= 0 or frame_height <= 0 or crop_width <= 0 or crop_height <= 0:
        return None
    x = (crop_left + x1 / upload_width * crop_width) / frame_width
    y = (crop_top + y1 / upload_height * crop_height) / frame_height
    w = (x2 - x1) / upload_width * crop_width / frame_width
    h = (y2 - y1) / upload_height * crop_height / frame_height
    if getattr(geometry, "precropped", False) and plate_region is not None:
        x, y, w, h = plate_region.to_frame((x, y, w, h))
    return (x, y, w, h)


def _authorised_set(authorised) -> set[str]:
    if authorised is None:
        return set()
    try:
        values = authorised() if callable(authorised) else authorised
        plates = {normalise_plate(str(plate)) for plate in values}
    except Exception:
        return set()
    plates.discard("")
    return plates


def resolve_policy(policy):
    """The matching policy in force, from a provider, an object, or nothing.

    Resolved once per frame and then held, so the band the local gate applies
    is the band in force at the instant the frame was read -- the same instant
    the processor resolves for the very same frame. This is exactly what
    :meth:`GateProcessor._current_match_policy` does with the same provider.
    """
    if policy is None:
        return None
    try:
        return policy() if callable(policy) else policy
    except Exception:
        # A policy that cannot be read must not be silently replaced with a
        # laxer one. Returning None hands `decide_access` its own fallback,
        # which is the same one the processor takes when the provider throws,
        # so the two sides still agree about this frame.
        return None


def authorise(observations, authorised: set[str], *, policy=None, now=None):
    """The controller's own matching, unchanged, over local observations.

    Exactly :func:`~gate_controller.matching.decide_access`: same thresholds,
    same exact-first rule, same two-frame fuzzy rule, and the same policy band
    the processor will apply to this very frame. ``policy`` and ``now`` are
    passed positionally and by keyword just as the processor passes them, so
    an overnight ``strict`` band denies a local fuzzy read exactly as it
    denies a cloud one. Nothing about the local path relaxes it.
    """
    if not authorised:
        return None
    try:
        return decide_access(observations, authorised, policy, now=now)
    except Exception:
        return None


def authorises_plate(observations, authorised: set[str], plate, *,
                     policy=None, now=None) -> bool:
    """Does the shared matching authorise, *on the strength of this plate*?

    ``decide_access`` weighs every observation of the event, so asking only
    whether it allows would let one frame answer on another frame's credit:
    a frame that read an unrelated plate would "decide", spend the frame, and
    then be journalled as the local match. Requiring the decision to rest on
    this frame's own read keeps ``source="local"`` and the shadow journal's
    ``authorised=`` field -- the evidence the promotion decision rests on --
    saying what they claim to say.
    """
    plate = normalise_plate(str(plate or ""))
    if not plate:
        return False
    decision = authorise(observations, authorised, policy=policy, now=now)
    if decision is None or not decision.allowed:
        return False
    return normalise_plate(str(decision.observed_plate or "")) == plate


def is_confident(score, minimum: float) -> bool:
    """Is ``score`` a real number at or above ``minimum``?

    Written as a positive assertion on purpose. ``nan < 0.95`` is ``False``,
    so a NaN confidence walks straight through a ``<`` gate; ``nan >= 0.95``
    is ``False`` too, so this one fails closed. An empty probability slice is
    enough to produce that NaN, and it would go on to reach the event's
    ``ocr_confidence`` and the outbox as a bare ``NaN`` literal that no strict
    JSON reader accepts.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return False
    if not isfinite(value):
        return False
    return value >= minimum


def classify_agreement(local_plate, cloud_plate) -> str:
    local_plate = normalise_plate(local_plate or "")
    cloud_plate = normalise_plate(cloud_plate or "")
    if not local_plate and not cloud_plate:
        return AGREEMENT_BOTH_NONE
    if local_plate and not cloud_plate:
        return AGREEMENT_LOCAL_ONLY
    if cloud_plate and not local_plate:
        return AGREEMENT_CLOUD_ONLY
    return AGREEMENT_MATCH if local_plate == cloud_plate else AGREEMENT_MISMATCH


def classify_authorised(local_match: bool, cloud_match: bool) -> str:
    if local_match and cloud_match:
        return AUTHORISED_BOTH
    if local_match:
        return AUTHORISED_LOCAL
    if cloud_match:
        return AUTHORISED_CLOUD
    return AUTHORISED_NONE


@dataclass
class _Summary:
    """What one event's frames did locally, for the telemetry block."""

    mode: str
    frames: int = 0
    plate: str | None = None
    score: float = 0.0
    latency_ms: float = 0.0
    agreement: str = AGREEMENT_BOTH_NONE
    authorised: str = AUTHORISED_NONE
    decision_source: str = DECISION_NONE
    status: str = STATUS_NO_PLATE

    def to_block(self) -> dict:
        return {
            "mode": self.mode,
            "frames": self.frames,
            "plate": self.plate,
            "score": self.score,
            "latency_ms": self.latency_ms,
            "agreement": self.agreement,
            "authorised": self.authorised,
            "decision_source": self.decision_source,
            "status": self.status,
        }


class LocalFrame:
    """One frame's local read, paired with the cloud answer for one journal line.

    The decision never waits on this. Whichever answer lands second emits the
    single ``gate_local_ocr`` line for the frame.
    """

    def __init__(self, recognizer: "LocalRecognizer", trace_id, future,
                 authorised: set[str], policy=None, now=None):
        self._recognizer = recognizer
        self._trace_id = trace_id
        self._future = future
        self._authorised = authorised
        # Resolved once, at admission, and reused for the decision and for the
        # journal line, so both are computed under the band that was in force
        # when the frame was read.
        self._policy = policy
        self._now = now
        self._lock = Lock()
        self._accumulated = False
        self._local: LocalRecognition | None = None
        self._cloud_settled = False
        self._cloud_plate: str | None = None
        self._cloud_score = 0.0
        self._decision_source = DECISION_NONE
        self._logged = False
        if future is not None:
            future.add_done_callback(self._local_finished)

    # -- local side -------------------------------------------------------
    def result(self, timeout: float | None = None) -> LocalRecognition:
        """Block for the local read. Only the active path ever calls this.

        A wait that runs out is reported as ``unavailable``, exactly like a
        frame the reader could not take: there is no local answer for this
        frame in time, and the cloud has it. It is not an ``error`` -- nothing
        failed, and the read may still land and be journalled behind us.
        """
        if self._future is None:
            return unavailable_recognition()
        if timeout is not None and timeout <= 0:
            settled = self.settled()
            return settled if settled is not None else unavailable_recognition()
        try:
            return self._future.result(timeout=timeout)
        except (FutureTimeoutError, TimeoutError):
            return unavailable_recognition()
        except Exception:
            return LocalRecognition(status=STATUS_ERROR)

    def settled(self) -> LocalRecognition | None:
        """The local read if it has already finished, else None. Never blocks."""
        with self._lock:
            return self._local

    def local_observations(self) -> tuple:
        """Every confident local read of this event, this frame included.

        The event's frames accumulate exactly as the processor accumulates
        cloud observations, so the shared decision function sees the same
        shape of input and its two-frame fuzzy rule behaves identically.
        """
        with self._lock:
            local = self._local
            append = not self._accumulated
            self._accumulated = True
        return self._recognizer._accumulate(self._trace_id, local, append=append)

    def answers_for(self, recognition: LocalRecognition) -> bool:
        """Does the shared matching authorise *on this frame's own read*?"""
        return authorises_plate(
            self.local_observations(), self._authorised, recognition.plate,
            policy=self._policy, now=self._now,
        )

    def _local_finished(self, future) -> None:
        try:
            recognition = future.result()
        except Exception:
            recognition = LocalRecognition(status=STATUS_ERROR)
        with self._lock:
            self._local = recognition
        self._maybe_log()

    # -- cloud side -------------------------------------------------------
    def complete_cloud(self, plate=None, score=0.0, *, decided: bool = True) -> None:
        with self._lock:
            if self._cloud_settled:
                return
            self._cloud_settled = True
            self._cloud_plate = plate
            try:
                self._cloud_score = float(score or 0.0)
            except (TypeError, ValueError):
                self._cloud_score = 0.0
            if decided and plate:
                self._decision_source = DECISION_CLOUD
        self._maybe_log()

    def decided_locally(self) -> None:
        """The local read opened the gate; the cloud is skipped or advisory."""
        with self._lock:
            self._decision_source = DECISION_LOCAL
        self._maybe_log()

    def abandon_cloud(self) -> None:
        """The cloud attempt ended without an answer for this frame."""
        self.complete_cloud(None, 0.0, decided=False)

    def cloud_skipped(self) -> None:
        with self._lock:
            if self._cloud_settled:
                return
            self._cloud_settled = True
        self._maybe_log()

    # -- journal ----------------------------------------------------------
    def _maybe_log(self) -> None:
        with self._lock:
            if self._logged or self._local is None or not self._cloud_settled:
                return
            self._logged = True
            local = self._local
            cloud_plate = self._cloud_plate
            cloud_score = self._cloud_score
            decision_source = self._decision_source
        self._recognizer._journal(
            trace_id=self._trace_id, local=local, cloud_plate=cloud_plate,
            cloud_score=cloud_score, decision_source=decision_source,
            authorised=self._authorised, observations=self.local_observations(),
            policy=self._policy, now=self._now,
        )


class _NullFrame(LocalFrame):
    """Stand-in used when nothing local ran, so callers need no branches."""

    def __init__(self) -> None:  # noqa: D107 - deliberately not calling super
        self._logged = True

    def result(self, timeout: float | None = None) -> LocalRecognition:
        return unavailable_recognition()

    def settled(self) -> LocalRecognition | None:
        return None

    def local_observations(self) -> tuple:
        return ()

    def answers_for(self, recognition: LocalRecognition) -> bool:
        return False

    def complete_cloud(self, plate=None, score=0.0, *, decided: bool = True) -> None:
        return

    def abandon_cloud(self) -> None:
        return

    def decided_locally(self) -> None:
        return

    def cloud_skipped(self) -> None:
        return


NULL_FRAME = _NullFrame()


class LocalRecognizer:
    """Loads the models once, reads frames one at a time, journals agreement."""

    def __init__(self, config: LocalRecognizerConfig, *, engine_factory=None,
                 plate_region=None, logger=None, clock=perf_counter,
                 wall_clock=None) -> None:
        self._config = config
        self._engine_factory = engine_factory or OnnxPlateReadEngine
        self._plate_region = plate_region
        self._logger = logger or LOGGER
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._engine = None
        self._lock = Lock()
        self._state = "loading" if config.enabled else MODE_OFF
        self._unavailable_reason: str | None = None
        self._unavailable_logged = False
        self._load_ms = 0.0
        self._warmup_ms = 0.0
        self._closed = False
        self._pool = (
            # One worker: two inferences must never share the board.
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="gate-local-ocr")
            if config.enabled else None
        )
        self._warmup_thread: Thread | None = None
        self._summaries: dict[str, _Summary] = {}
        self._summary_order: list[str] = []
        self._observations: dict[str, list] = {}
        self._observation_order: list[str] = []
        #: Seconds of local waiting each event has already paid, so a burst
        #: cannot pay the stuck-engine guard once per frame. Bounded exactly
        #: like the summaries and the observations.
        self._event_waits: dict[str, float] = {}
        self._wait_order: list[str] = []
        self._inflight = 0
        self._counts = {
            "frames": 0, "recognised": 0, "errors": 0, "unavailable": 0,
            "busy": 0,
            AGREEMENT_MATCH: 0, AGREEMENT_MISMATCH: 0, AGREEMENT_LOCAL_ONLY: 0,
            AGREEMENT_CLOUD_ONLY: 0, AGREEMENT_BOTH_NONE: 0,
            "local_decisions": 0,
        }
        self._latencies: list[float] = []

    # -- lifecycle --------------------------------------------------------
    @property
    def config(self) -> LocalRecognizerConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def active(self) -> bool:
        return self._config.active and self.available

    @property
    def available(self) -> bool:
        with self._lock:
            return self._state == "ready"

    def start(self) -> None:
        """Load and warm the models on a background thread."""
        if not self._config.enabled or self._warmup_thread is not None:
            return
        thread = Thread(target=self._load_and_warm, name="gate-local-ocr-warmup", daemon=True)
        self._warmup_thread = thread
        thread.start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        thread = self._warmup_thread
        if thread is not None:
            thread.join(timeout)
        return self.available

    def _load_and_warm(self) -> None:
        started = self._clock()
        try:
            engine = self._engine_factory(self._config)
            engine.load()
        except LocalRecognizerUnavailable as error:
            self._mark_unavailable(str(error) or "load_failed")
            return
        except Exception:
            self._mark_unavailable("load_failed")
            return
        load_ms = (self._clock() - started) * 1000
        started = self._clock()
        try:
            engine.warmup()
        except Exception:
            self._mark_unavailable("warmup_failed")
            return
        warmup_ms = (self._clock() - started) * 1000
        with self._lock:
            if self._closed:
                return
            self._engine = engine
            self._state = "ready"
            self._load_ms = load_ms
            self._warmup_ms = warmup_ms
        self._log(
            "gate_local_ocr stage=ready mode=%s detector=%s recogniser=%s "
            "threads=%d load_ms=%d warmup_ms=%d model_dir=%s",
            self._config.mode, self._config.detector, self._config.recogniser,
            self._config.threads, round(load_ms), round(warmup_ms),
            self._config.model_dir,
        )

    def _mark_unavailable(self, reason: str) -> None:
        with self._lock:
            self._state = STATUS_UNAVAILABLE
            self._unavailable_reason = reason
            already = self._unavailable_logged
            self._unavailable_logged = True
        if not already:
            self._log("gate_local_ocr stage=unavailable reason=%s", reason)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # The engine is gone, so the state must stop saying `ready`:
            # `available` is what the active path asks before trusting a read,
            # and a closed recogniser has nothing to offer.
            self._state = STATUS_CLOSED
            pool = self._pool
            self._pool = None
            self._engine = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # -- inference --------------------------------------------------------
    def begin(self, image: bytes, *, trace_id=None, geometry=None,
              authorised=None, policy=None) -> LocalFrame:
        """Queue one frame for local reading and return its pairing handle.

        Returns immediately. The single worker serialises inferences, so a
        second frame simply waits its turn behind the first. ``policy`` is
        resolved here, once, and the frame carries the answer: the decision
        and the journal line then both speak about the band that was in force
        when the frame was read.
        """
        if not self._config.enabled or self._closed:
            return NULL_FRAME
        plates = _authorised_set(authorised)
        resolved, now = resolve_policy(policy), self._wall_clock()
        with self._lock:
            pool, state = self._pool, self._state
        if pool is None:
            return NULL_FRAME
        if state != "ready":
            return self._declined(trace_id, plates, resolved, now)
        with self._lock:
            # One outstanding read, never a queue. A stalled inference must
            # not make every later frame wait its turn before falling back to
            # the cloud, and queued frames would each pin a JPEG in memory.
            if self._inflight:
                busy = True
            else:
                busy = False
                self._inflight += 1
        if busy:
            # Counted once, as `busy`. A frame refused because another read is
            # in flight is not evidence that the recogniser is unavailable, and
            # counting it as both made the two counters uncomparable.
            self._count("busy")
            return self._declined(trace_id, plates, resolved, now, count=None)
        try:
            future = pool.submit(self._read, image, geometry)
        except RuntimeError:
            with self._lock:
                self._inflight -= 1
            return NULL_FRAME
        return LocalFrame(self, trace_id, future, plates, resolved, now)

    def _declined(self, trace_id, plates, policy=None, now=None, *,
                  count: str | None = "unavailable") -> LocalFrame:
        """A frame the local reader could not take, answered immediately."""
        if count is not None:
            self._count(count)
        future: Future = Future()
        future.set_result(unavailable_recognition())
        return LocalFrame(self, trace_id, future, plates, policy, now)

    def recognise(self, image: bytes, *, trace_id=None, geometry=None,
                  authorised=None, policy=None,
                  timeout: float | None = None) -> LocalRecognition:
        """Blocking convenience wrapper, used by the active path."""
        return self.begin(
            image, trace_id=trace_id, geometry=geometry, authorised=authorised,
            policy=policy,
        ).result(timeout)

    def wait_seconds(self, trace_id, remaining: float | None, *,
                     reserve: float = LOCAL_DECISION_CLOUD_RESERVE_SECONDS) -> float:
        """How long this frame may block on its local read.

        The smallest of three bounds: the stuck-engine ceiling, whatever the
        decision budget can spare once the cloud request's share is set aside,
        and what is left of this event's own waiting budget. Returns 0.0 when
        there is nothing to spend, in which case the caller takes the read
        only if it has already landed.

        ``reserve`` is that cloud request's share. It defaults to the whole of
        it, which is right whenever ``remaining`` is the decision's own budget.
        A caller whose budget has already been reserved from -- the off-slot
        local pass, bounded by `GateProcessor._local_pass_deadline` -- passes
        0.0 rather than holding the same seconds back twice.
        """
        seconds = LOCAL_DECISION_TIMEOUT_SECONDS
        if remaining is not None:
            try:
                spare = float(remaining) - reserve
            except (TypeError, ValueError):
                spare = 0.0
            seconds = min(seconds, spare if isfinite(spare) else 0.0)
        seconds = min(seconds, self.event_wait_remaining(trace_id))
        return max(0.0, seconds)

    def event_wait_remaining(self, trace_id) -> float:
        """What is left of one event's total local-waiting budget."""
        if not trace_id:
            # No trace id, no event identity: the frame stands alone, exactly
            # as its observations do.
            return LOCAL_EVENT_WAIT_BUDGET_SECONDS
        with self._lock:
            spent = self._event_waits.get(trace_id, 0.0)
        return max(0.0, LOCAL_EVENT_WAIT_BUDGET_SECONDS - spent)

    def record_event_wait(self, trace_id, seconds: float) -> None:
        """Charge ``seconds`` of local waiting to this event."""
        if not trace_id:
            return
        try:
            spent = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        if not isfinite(spent):
            return
        with self._lock:
            if trace_id not in self._event_waits:
                self._wait_order.append(trace_id)
                while len(self._wait_order) > MAX_ITEM_SUMMARIES:
                    self._event_waits.pop(self._wait_order.pop(0), None)
            self._event_waits[trace_id] = self._event_waits.get(trace_id, 0.0) + spent

    def decides(self, frame: "LocalFrame", recognition: LocalRecognition) -> bool:
        """May this local read answer for the frame instead of the cloud?

        Three conditions, and no fourth: the recogniser is confident enough
        (the only local-specific gate, applied to the weakest character of the
        read), the controller's own
        :func:`~gate_controller.matching.decide_access` authorises the local
        observations of this event -- exact or fuzzy, unchanged thresholds,
        under the policy band in force -- and the plate that decision rests on
        is *this frame's own read*, not a sibling frame's.
        """
        if not self._config.active or not recognition.recognised:
            return False
        if not is_confident(recognition.score, self._config.min_confidence):
            return False
        return frame.answers_for(recognition)

    def _accumulate(self, trace_id, recognition, *, append: bool) -> tuple:
        """Confident local reads for one event, bounded and ordered."""
        confident = (
            recognition is not None and recognition.recognised
            and is_confident(recognition.score, self._config.min_confidence)
        )
        if not trace_id:
            # No trace id means no event identity. A shared bucket would let
            # two unrelated frames satisfy the two-frame fuzzy rule between
            # them, so a frame without a trace stands alone.
            return (recognition.observation(),) if confident else ()
        key = trace_id
        with self._lock:
            observations = self._observations.setdefault(key, [])
            if key not in self._observation_order:
                self._observation_order.append(key)
                while len(self._observation_order) > MAX_ITEM_SUMMARIES:
                    self._observations.pop(self._observation_order.pop(0), None)
            if append and confident and len(observations) < MAX_EVENT_OBSERVATIONS:
                observations.append(recognition.observation())
            return tuple(observations)

    def _read(self, image: bytes, geometry) -> LocalRecognition:
        try:
            return self._read_once(image, geometry)
        finally:
            with self._lock:
                self._inflight = max(0, self._inflight - 1)

    def _read_once(self, image: bytes, geometry) -> LocalRecognition:
        started = self._clock()
        with self._lock:
            engine, state = self._engine, self._state
        if engine is None or state != "ready":
            self._count("unavailable")
            return unavailable_recognition((self._clock() - started) * 1000)
        try:
            result = engine.read(image)
        except Exception:
            self._count("errors")
            self._log_debug("gate_local_ocr stage=read_failed")
            return LocalRecognition(
                status=STATUS_ERROR, total_ms=(self._clock() - started) * 1000,
            )
        total_ms = (self._clock() - started) * 1000
        candidates = tuple(
            {
                "plate": read.plate,
                "score": round(float(read.confidence), 6),
                "mean_score": round(float(read.mean_confidence), 6),
                "detection_score": round(float(read.detection_confidence), 6),
            }
            for read in result.reads[:MAX_BOXES]
        )
        best = result.reads[0] if result.reads else None
        box = (
            box_to_frame(
                best.box, geometry, self._plate_region, result.width, result.height,
            )
            if best is not None else None
        )
        recognition = LocalRecognition(
            plate=best.plate or None if best is not None else None,
            score=float(best.confidence) if best is not None else 0.0,
            mean_score=float(best.mean_confidence) if best is not None else 0.0,
            box=box, candidates=candidates,
            status=STATUS_RECOGNIZED if (best and best.plate) else STATUS_NO_PLATE,
            decode_ms=result.decode_ms, detect_ms=result.detect_ms,
            ocr_ms=result.ocr_ms, total_ms=total_ms,
        )
        self._record_latency(total_ms, recognition.recognised)
        return recognition

    # -- journal and telemetry -------------------------------------------
    def _journal(self, *, trace_id, local: LocalRecognition, cloud_plate,
                 cloud_score, decision_source, authorised: set[str],
                 observations=(), policy=None, now=None) -> None:
        local_plate = normalise_plate(local.plate or "")
        cloud_normalised = normalise_plate(str(cloud_plate or ""))
        agreement = classify_agreement(local_plate, cloud_normalised)
        # Both sides are labelled the way the decision is taken: on this
        # frame's own read, and under the policy band that was in force when
        # the frame was read. A label computed any other way is not the
        # evidence the promotion decision needs.
        local_match = (
            is_confident(local.score, self._config.min_confidence)
            and authorises_plate(
                observations, authorised, local_plate, policy=policy, now=now,
            )
        )
        cloud_match = authorises_plate(
            [PlateObservation(plate=cloud_normalised, confidence=float(cloud_score or 0.0))]
            if cloud_normalised else (),
            authorised, cloud_normalised, policy=policy, now=now,
        )
        authorised_label = classify_authorised(local_match, cloud_match)
        self._count(agreement)
        self._count("frames")
        if local.recognised:
            self._count("recognised")
        if decision_source == DECISION_LOCAL:
            self._count("local_decisions")
        self._remember(
            trace_id, local=local, agreement=agreement, authorised=authorised_label,
            decision_source=decision_source,
        )
        self._log(
            "gate_local_ocr stage=%s trace_id=%s local_plate=%s local_score=%.3f "
            "local_mean=%.3f local_ms=%d cloud_plate=%s cloud_score=%.3f "
            "agreement=%s authorised=%s decision_source=%s",
            self._config.mode, trace_id or "-", local_plate or "-", local.score,
            local.mean_score, round(local.total_ms), cloud_normalised or "-",
            cloud_score, agreement, authorised_label, decision_source,
        )

    def _remember(self, trace_id, *, local, agreement, authorised, decision_source) -> None:
        if not trace_id:
            return
        with self._lock:
            summary = self._summaries.get(trace_id)
            if summary is None:
                summary = _Summary(mode=self._config.mode)
                self._summaries[trace_id] = summary
                self._summary_order.append(trace_id)
                while len(self._summary_order) > MAX_ITEM_SUMMARIES:
                    self._summaries.pop(self._summary_order.pop(0), None)
            summary.frames += 1
            summary.latency_ms = round(float(local.total_ms))
            summary.status = local.status
            summary.agreement = agreement
            summary.authorised = authorised
            if decision_source != DECISION_NONE:
                summary.decision_source = decision_source
            if local.recognised and local.score >= summary.score:
                summary.plate = local.plate
                summary.score = round(float(local.score), 3)

    def observations(self, trace_id) -> tuple:
        """This event's confident local reads, for the processor to weigh.

        Read-only: nothing is accumulated here. The processor uses them only
        as ``corroborations`` for the agreement rule, keyed by trace id so one
        event's reads can never speak for another's.
        """
        if not trace_id:
            return ()
        with self._lock:
            return tuple(self._observations.get(trace_id, ()))

    def summary(self, trace_id) -> dict | None:
        """The compact per-event block, or None when nothing local ran."""
        if not trace_id:
            return None
        with self._lock:
            summary = self._summaries.get(trace_id)
            return summary.to_block() if summary is not None else None

    def forget(self, trace_id) -> None:
        with self._lock:
            if self._summaries.pop(trace_id, None) is not None:
                try:
                    self._summary_order.remove(trace_id)
                except ValueError:
                    pass
            if self._observations.pop(trace_id, None) is not None:
                try:
                    self._observation_order.remove(trace_id)
                except ValueError:
                    pass
            if self._event_waits.pop(trace_id, None) is not None:
                try:
                    self._wait_order.remove(trace_id)
                except ValueError:
                    pass

    def status(self) -> dict:
        with self._lock:
            counts = dict(self._counts)
            latencies = sorted(self._latencies)
            state = self._state
            reason = self._unavailable_reason
            load_ms = self._load_ms
            warmup_ms = self._warmup_ms
        mean = sum(latencies) / len(latencies) if latencies else 0.0
        return {
            "mode": self._config.mode,
            "cloud": self._config.cloud,
            "state": state,
            "unavailable_reason": reason,
            "detector": self._config.detector,
            "recogniser": self._config.recogniser,
            "threads": self._config.threads,
            "min_confidence": self._config.min_confidence,
            "model_dir": str(self._config.model_dir),
            "load_ms": round(load_ms, 1),
            "warmup_ms": round(warmup_ms, 1),
            "frames": counts["frames"],
            "recognised": counts["recognised"],
            "agreements": counts[AGREEMENT_MATCH],
            "mismatches": counts[AGREEMENT_MISMATCH],
            "local_only": counts[AGREEMENT_LOCAL_ONLY],
            "cloud_only": counts[AGREEMENT_CLOUD_ONLY],
            "both_none": counts[AGREEMENT_BOTH_NONE],
            "local_decisions": counts["local_decisions"],
            "errors": counts["errors"],
            "unavailable": counts["unavailable"],
            "busy": counts["busy"],
            "latency_ms": {
                "samples": len(latencies),
                "mean": round(mean, 1),
                "p95": round(_percentile(latencies, 95), 1),
            },
        }

    # -- internals --------------------------------------------------------
    def _count(self, key: str, amount: int = 1) -> None:
        with self._lock:
            if key in self._counts:
                self._counts[key] += amount

    def _record_latency(self, milliseconds: float, recognised: bool) -> None:
        with self._lock:
            self._latencies.append(float(milliseconds))
            if len(self._latencies) > MAX_LATENCY_SAMPLES:
                del self._latencies[0]

    def _log(self, message: str, *args) -> None:
        try:
            self._logger.info(message, *args)
        except Exception:
            return

    def _log_debug(self, message: str, *args) -> None:
        try:
            self._logger.warning(message, *args)
        except Exception:
            return


def _percentile(ordered: list[float], percent: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percent / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def build_local_recognizer(environment=None, *, plate_region=None,
                           engine_factory=None) -> LocalRecognizer | None:
    """The configured recogniser, or None when the mode is ``off``."""
    config = load_local_recognizer_config(environment)
    if not config.enabled:
        return None
    return LocalRecognizer(
        config, plate_region=plate_region, engine_factory=engine_factory,
    )
