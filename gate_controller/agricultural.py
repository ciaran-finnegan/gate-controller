"""Let farm machinery in by what it is, not by a plate it does not carry.

This is a working farm. A tractor or a telehandler has no plate the camera can
read -- it is caked, bent, behind a loader, or not there -- and a gate that
refuses one costs somebody a climb down from the cab with a fob in the rain.
So a vehicle that is *recognisably agricultural machinery* is admitted whatever
the plate readers made of it.

It is an **appearance credential**, never a plate read. The event it produces
says ``source="appearance"``, ``reason="farm_machinery"`` and names no
authorised plate; nothing here is called OCR and nothing here rewrites an
uncertain plate (gate-controller #96, #63).

What it is careful about, because it opens a physical gate
----------------------------------------------------------
* **A lorry is not a tractor**, and nor is a van, a 4x4 or a car with a
  trailer. Winning the argmax is not enough: the machinery class has to beat
  *every* other class -- each kind of road vehicle, and the empty driveway --
  by ``min_margin``, and to hold ``min_machine`` of the probability outright.
* **More than one frame has to agree** when the burst has more than one. One
  frame is allowed to decide only when it is the only frame there is.
* **Nothing that is leaving.** A departing vehicle comes from behind the camera
  and shows its rear (docs/vehicle-direction.md). A frame the direction prompts
  read as ``exiting`` refuses the burst -- but they are prompts about cars, and
  on a machine they answer ``unknown`` almost every time (25 of the 28 clear
  machinery frames in the stored photos, every clear frame of two departures
  among them). So an
  unknown direction is settled by something a camera *can* see: **a machine
  that is waiting to come in is standing still in front of a closed gate, and
  one that is leaving has the gate behind it and keeps going.** It is admitted
  only when an earlier clear frame of it, between one and fifteen seconds old,
  looks the same as this one. docs/agricultural-admit.md has the measurement.
* **Anything else is the ordinary answer.** Unsure, model missing, a library
  missing, an unreadable frame, an inference that overruns its budget, a second
  assessment while one is still running, more readings in a minute than any
  passage produces, an exception from anywhere: each is a
  verdict of "no", the plate path's own decision stands untouched, and the gate
  does whatever it would have done had this module not existed.

It never stands between a known plate and the gate. A frame the device's own
plate reader decides is never shown to it. For any other frame the reading is
*begun* when the frame arrives, on a thread of its own, and *collected* once
the plate path has declined to open -- so in ``shadow`` no plate decision waits
for it at all. In ``on`` the answer decides whether the frame needs a cloud
lookup, so an undecided frame waits for it: about 140 ms on the Pi, bounded by
``MAX_BUDGET_SECONDS``. What it admits reaches the relay through the same
``ActuationCoordinator`` claim, cooldown and pre-activation checks as a plate
match.

Three states, ``GATE_AGRI_ADMIT=off|shadow|on``, and the code's default is
``off``. In ``shadow`` every assessment is journalled and kept in
``event_appearance`` with its scores and ``would_admit``; the relay is never
asked. Only ``on`` opens the gate.

The arithmetic after the image tower is plain Python on purpose: the unit suite
runs on a CI host with no numpy, and the rule that opens a gate should be
tested everywhere the suite runs. 33 prompts of 512 numbers is 1.3 ms on
the Pi, against 139 ms in the image tower.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .direction_vision import (
    DEFAULT_MODEL_DIR, EXITING, MODEL_FILENAME, UNKNOWN, FrameReading,
)

LOGGER = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

ENV_MODE = "GATE_AGRI_ADMIT"
ENV_MIN_MARGIN = "GATE_AGRI_ADMIT_MIN_MARGIN"
ENV_MIN_MACHINE = "GATE_AGRI_ADMIT_MIN_MACHINE"
ENV_STILL_COSINE = "GATE_AGRI_ADMIT_STILL_COSINE"
ENV_HOURS = "GATE_AGRI_ADMIT_HOURS"
ENV_TIMEZONE = "GATE_AGRI_ADMIT_TIMEZONE"

#: What the access log is told. Free tokens as far as the ingest contract is
#: concerned (``EVENT_TOKEN``), and deliberately not a plate-match reason.
EVENT_SOURCE = "appearance"
EVENT_REASON = "farm_machinery"

SPEC_FILENAME = "agri-clip-v1.json"
DIRECTION_SPEC_FILENAME = "direction-clip-v1.json"
METHOD = "clip-vit-b32-agri-v1"

KIND_MACHINE = "machine"
KIND_ROAD = "road"
KIND_OTHER = "other"

#: Chosen from the stored photos of 2026-09-07..20; see
#: docs/agricultural-admit.md for the table these came from and what they cost.
DEFAULT_MIN_MARGIN = 0.60
DEFAULT_MIN_MACHINE = 0.75
#: "Standing still": this frame against an earlier clear one of the same
#: passage. Stored photos, clear frames one to fifteen seconds apart: a waiting
#: machine scored 0.962-0.977 against itself, a departing one 0.813-0.922.
DEFAULT_STILL_COSINE = 0.95
STILL_MIN_SECONDS = 1.0
STILL_MAX_SECONDS = 15.0
#: Clear frames remembered for that comparison. A passage is a handful.
STILL_MEMORY = 12
#: gate-controller #63's summer window, the wider of its two. Outside it the
#: assessment is still made and still recorded, and never admits: there is not
#: one stored photo of a machine after dark to say what the model does then.
DEFAULT_HOURS = "06:00-22:00"
DEFAULT_TIMEZONE = "Europe/Dublin"

#: Frames of one burst that are read. Each costs about 115 ms on the Pi 5
#: (measured 2026-09-21: 8 ms to decode and crop, 106 ms in the image tower,
#: one thread), and two is what "more than one frame agrees" needs.
MAX_FRAMES = 2
#: Readings begun in any rolling minute. One camera alarm puts at most nine
#: undecided frames into the pipeline as shipped -- five cloud handovers, three
#: authorised injections that then failed to decide, one fallback -- and
#: sixteen at the configuration's ceilings; the sweep's thirty-second waiting
#: phase reads on the device and injects nothing beyond those caps. Thirty is
#: three such alarms a minute, 4.2 s of one core in sixty, and past it a burst
#: is answered ``rate_limited`` so that no flood of frames, from whatever
#: cause, can turn this model into a standing load beside the plate reader.
MAX_READINGS_PER_MINUTE = 30
#: A reading that has not finished is not waited for with less than this left
#: before the relay's own deadline, and never for longer than the ceiling
#: however much budget there is: two frames are 0.3 s at the Pi's p95, and a
#: wedged inference must not hold a decision lane. Once one is wedged, every
#: later burst is answered ``busy`` at once and waits for nothing.
MIN_BUDGET_SECONDS = 0.15
MAX_BUDGET_SECONDS = 0.6

VERDICT_MACHINE = "machine_clear"
VERDICT_UNSURE = "unsure"
VERDICT_ROAD = "road_vehicle"
VERDICT_LEAVING = "leaving"
VERDICT_DISAGREE = "frames_disagree"
VERDICT_NOT_STILL = "not_standing_still"
VERDICT_OUTSIDE_HOURS = "outside_hours"
VERDICT_UNAVAILABLE = "model_unavailable"
VERDICT_UNREADABLE = "unreadable"
VERDICT_TIMEOUT = "timeout"
VERDICT_BUSY = "busy"
VERDICT_RATE_LIMITED = "rate_limited"
VERDICT_NO_BUDGET = "no_budget"
VERDICT_ERROR = "error"


def load_mode(environment=None) -> str:
    """``off`` unless the environment says ``shadow`` or ``on``, exactly.

    A typo must not open a gate, so anything unrecognised is ``off`` and says
    so once at start-up.
    """
    environment = os.environ if environment is None else environment
    raw = (environment.get(ENV_MODE) or "").strip().lower()
    if not raw:
        return MODE_OFF
    if raw in MODES:
        return raw
    LOGGER.warning("gate_agri %s=%r status=rejected using=off", ENV_MODE, raw)
    return MODE_OFF


@dataclass(frozen=True)
class FrameScores:
    """What the model made of one frame."""

    #: Probability on the machinery prompts, together.
    machine: float
    #: The strongest class that is not machinery, of any kind, and its share.
    rival: str
    rival_score: float
    #: The strongest *road vehicle* class and its share.
    road: str
    road_score: float
    #: The car-direction reading of the same frame, by direction_vision's rule.
    direction: str = UNKNOWN
    front: float = 0.0
    rear: float = 0.0

    @property
    def margin(self) -> float:
        """How far machinery is ahead of whatever is closest behind it."""
        return self.machine - self.rival_score

    def to_record(self) -> dict:
        return {
            "machine": round(self.machine, 4), "rival": self.rival,
            "rival_score": round(self.rival_score, 4), "road": self.road,
            "road_score": round(self.road_score, 4), "margin": round(self.margin, 4),
            "direction": self.direction, "front": round(self.front, 3),
            "rear": round(self.rear, 3),
        }


@dataclass(frozen=True)
class Assessment:
    """One burst's answer. ``would_admit`` is the only thing that opens a gate."""

    would_admit: bool
    verdict: str
    frames: tuple[FrameScores, ...] = field(default_factory=tuple)
    elapsed_ms: int = 0
    min_margin: float = DEFAULT_MIN_MARGIN
    min_machine: float = DEFAULT_MIN_MACHINE
    #: How like an earlier clear frame of this passage this one is (cosine),
    #: and how long ago that frame was. None when there was none to compare.
    stillness: float | None = None
    still_seconds: float | None = None

    @property
    def weakest(self) -> FrameScores | None:
        """The frame the rule had most reason to doubt: the one that decided."""
        return min(self.frames, key=lambda frame: frame.margin, default=None)

    def journal(self) -> str:
        frame = self.weakest
        scores = "" if frame is None else (
            f" machine={frame.machine:.3f} margin={frame.margin:+.3f} rival={frame.rival}"
            f":{frame.rival_score:.3f} road={frame.road}:{frame.road_score:.3f}"
            f" direction={frame.direction}"
        )
        still = "" if self.stillness is None else (
            f" stillness={self.stillness:.3f} still_seconds={self.still_seconds:.1f}"
        )
        return (
            f"would_admit={'true' if self.would_admit else 'false'} verdict={self.verdict}"
            f" frames={len(self.frames)} elapsed_ms={self.elapsed_ms}{scores}{still}"
        )

    def to_record(self) -> dict:
        return {
            "would_admit": self.would_admit, "verdict": self.verdict,
            "elapsed_ms": self.elapsed_ms, "min_margin": self.min_margin,
            "min_machine": self.min_machine, "method": METHOD,
            "stillness": None if self.stillness is None else round(self.stillness, 4),
            "still_seconds": None if self.still_seconds is None else round(self.still_seconds, 1),
            "frames": [frame.to_record() for frame in self.frames],
        }


def judge(frames, *, min_margin: float = DEFAULT_MIN_MARGIN,
          min_machine: float = DEFAULT_MIN_MACHINE) -> tuple[bool, str]:
    """The rule. ``(would_admit, verdict)`` for the frames of one burst.

    Order matters and is the order of caution: leaving refuses first, a road
    vehicle anywhere in the burst refuses next, and only then is "is it a
    machine" asked -- of *every* frame that was read, so two frames that
    disagree refuse as well.
    """
    frames = tuple(frames)
    if not frames:
        return False, VERDICT_UNREADABLE
    if any(frame.direction == EXITING for frame in frames):
        return False, VERDICT_LEAVING
    if any(
        frame.rival == frame.road and frame.road_score >= frame.machine for frame in frames
    ):
        return False, VERDICT_ROAD
    clear = [
        frame.machine >= min_machine and frame.margin >= min_margin for frame in frames
    ]
    if all(clear):
        return True, VERDICT_MACHINE
    if any(clear):
        return False, VERDICT_DISAGREE
    return False, VERDICT_UNSURE


class PromptScorer:
    """The frozen text side: an image embedding in, :class:`FrameScores` out.

    No model and no numpy. The prompts were embedded once, on a laptop, and
    ship as numbers in ``models/agri-clip-v1.json``; the car-direction prompts
    are direction_vision's own file, read here so that one image embedding
    answers both questions and the direction *rule* stays where it lives
    (:class:`~gate_controller.direction_vision.FrameReading`).
    """

    def __init__(self, spec_path: Path | None = None, direction_spec_path: Path | None = None):
        models = Path(__file__).with_name("models")
        spec = json.loads(Path(spec_path or models / SPEC_FILENAME).read_text(encoding="utf-8"))
        self.input_size = int(spec["input_size"])
        self.mean = tuple(float(value) for value in spec["mean"])
        self.std = tuple(float(value) for value in spec["std"])
        self._scale = float(spec["logit_scale"])
        self._labels = tuple(str(label) for label in spec["labels"])
        self._kinds = {str(label): str(kind) for label, kind in spec["kinds"].items()}
        self._text = tuple(tuple(float(v) for v in row) for row in spec["text_embeddings"])
        if len(self._labels) != len(self._text) or not self._text:
            raise ValueError("labels and text embeddings do not line up")
        if set(self._labels) != set(self._kinds):
            raise ValueError("every label needs a kind")
        kinds = set(self._kinds.values())
        if KIND_MACHINE not in kinds or KIND_ROAD not in kinds:
            raise ValueError("the prompts name no machinery, or no road vehicle")
        direction = json.loads(
            Path(direction_spec_path or models / DIRECTION_SPEC_FILENAME).read_text(encoding="utf-8")
        )
        self._direction_scale = float(direction["logit_scale"])
        self._direction_labels = tuple(str(label) for label in direction["labels"])
        self._direction_text = tuple(
            tuple(float(v) for v in row) for row in direction["text_embeddings"]
        )
        if len(self._direction_labels) != len(self._direction_text):
            raise ValueError("direction labels and embeddings do not line up")

    def score(self, embedding) -> FrameScores:
        embedding = tuple(float(value) for value in embedding)
        if len(embedding) != len(self._text[0]) or not all(map(math.isfinite, embedding)):
            raise ValueError("not an image embedding")
        per = _softmax_by_label(self._text, self._labels, embedding, self._scale)
        machine = sum(
            share for label, share in per.items() if self._kinds[label] == KIND_MACHINE
        )
        others = {
            label: share for label, share in per.items()
            if self._kinds[label] != KIND_MACHINE
        }
        roads = {
            label: share for label, share in others.items()
            if self._kinds[label] == KIND_ROAD
        }
        rival = max(others, key=others.get)
        road = max(roads, key=roads.get)
        ends = _softmax_by_label(
            self._direction_text, self._direction_labels, embedding, self._direction_scale,
        )
        reading = FrameReading(
            front=ends.get("front", 0.0), rear=ends.get("rear", 0.0),
            top=max(ends, key=ends.get),
        )
        return FrameScores(
            machine=machine, rival=rival, rival_score=others[rival],
            road=road, road_score=roads[road],
            direction=reading.direction, front=reading.front, rear=reading.rear,
        )


    def shares(self, embedding) -> dict[str, float]:
        """Every label's share for one embedding, ``empty`` and the road kinds included."""
        embedding = tuple(float(value) for value in embedding)
        if len(embedding) != len(self._text[0]) or not all(map(math.isfinite, embedding)):
            raise ValueError("not an image embedding")
        return _softmax_by_label(self._text, self._labels, embedding, self._scale)


def _softmax_by_label(text, labels, embedding, scale: float) -> dict[str, float]:
    logits = [
        scale * sum(weight * value for weight, value in zip(row, embedding))
        for row in text
    ]
    peak = max(logits)
    weights = [math.exp(logit - peak) for logit in logits]
    total = sum(weights)
    per: dict[str, float] = {}
    for label, weight in zip(labels, weights):
        per[label] = per.get(label, 0.0) + weight / total
    return per


class ClipImageTower:
    """The model boundary: a JPEG in, an image embedding out, or None.

    The same ``clip-vit-b32-visual-int8.onnx`` direction_vision uses, installed
    beside the plate models and not kept in the repository. Never raises, on a
    missing file, a missing library or a frame that is not a picture.
    """

    def __init__(self, scorer: PromptScorer, model_dir: Path | None = None):
        self._size = scorer.input_size
        self._mean = scorer.mean
        self._std = scorer.std
        self._session = None
        self._reason: str | None = None
        path = Path(model_dir or os.environ.get("GATE_MODEL_DIR", DEFAULT_MODEL_DIR)) / MODEL_FILENAME
        if not path.exists():
            self._reason = f"{path} is not installed"
            return
        try:
            import numpy  # noqa: F401
            import onnxruntime as ort
        except ImportError as error:
            self._reason = f"{error.name or 'a dependency'} is not available"
            return
        try:
            options = ort.SessionOptions()
            # One thread, like every other model on this board: a plate read
            # must never wait behind this.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(path), options, providers=["CPUExecutionProvider"],
            )
        except Exception as error:  # onnxruntime raises broadly
            self._reason = f"model would not load: {type(error).__name__}"
            self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def embed(self, jpeg: bytes):
        if self._session is None:
            return None
        try:
            return [float(v) for v in self._session.run(None, {"image": self._prepare(jpeg)})[0][0]]
        except Exception:
            LOGGER.warning("gate_agri stage=embed_failed", exc_info=False)
            return None

    def _prepare(self, jpeg: bytes):
        """Shortest side to the model's size, then the centre square.

        The preprocessing direction_vision measured: a letterbox of the whole
        wide frame shrinks the vehicle until the model stops seeing it.
        """
        import numpy as np
        from PIL import Image

        with Image.open(BytesIO(jpeg)) as source:
            image = source.convert("RGB")
        width, height = image.size
        scale = self._size / min(width, height)
        image = image.resize((round(width * scale), round(height * scale)), Image.BICUBIC)
        width, height = image.size
        left, top = (width - self._size) // 2, (height - self._size) // 2
        image = image.crop((left, top, left + self._size, top + self._size))
        mean = np.array(self._mean, dtype=np.float32)
        std = np.array(self._std, dtype=np.float32)
        pixels = (np.asarray(image, dtype=np.float32) / 255.0 - mean) / std
        return pixels.transpose(2, 0, 1)[None]


@dataclass(frozen=True)
class Hours:
    """The local hours in which an appearance may admit. May wrap midnight."""

    start: int
    end: int
    timezone_name: str = DEFAULT_TIMEZONE

    @classmethod
    def parse(cls, text: str, timezone_name: str = DEFAULT_TIMEZONE) -> "Hours":
        first, _, second = text.strip().partition("-")
        start, end = _minutes(first), _minutes(second)
        ZoneInfo(timezone_name)
        return cls(start, end, timezone_name)

    def open_at(self, moment: datetime) -> bool:
        """False whenever the local time cannot be worked out."""
        try:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            local = moment.astimezone(ZoneInfo(self.timezone_name))
        except (ZoneInfoNotFoundError, ValueError, OSError, OverflowError, AttributeError):
            return False
        minute = local.hour * 60 + local.minute
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end


def _minutes(text: str) -> int:
    hours, separator, minutes = text.strip().partition(":")
    if not separator:
        raise ValueError("expected HH:MM")
    value = int(hours) * 60 + int(minutes)
    if not (0 <= int(minutes) < 60 and 0 <= value <= 24 * 60):
        raise ValueError("expected HH:MM between 00:00 and 24:00")
    return value


def _bounded_share(environment, name: str, default: float) -> float:
    raw = environment.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or not 0.5 <= value <= 1.0:
        # Below a half, "machinery" need not even be the likeliest answer.
        LOGGER.warning("gate_agri %s=%r status=rejected using=%.2f", name, raw, default)
        return default
    return value


def _load_hours(environment) -> Hours:
    default = Hours.parse(DEFAULT_HOURS, DEFAULT_TIMEZONE)
    raw = (environment.get(ENV_HOURS) or "").strip()
    zone = (environment.get(ENV_TIMEZONE) or "").strip() or DEFAULT_TIMEZONE
    if not raw and zone == DEFAULT_TIMEZONE:
        return default
    try:
        return Hours.parse(raw or DEFAULT_HOURS, zone)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        LOGGER.warning(
            "gate_agri %s=%r %s=%r status=rejected using=%s", ENV_HOURS, raw,
            ENV_TIMEZONE, zone, DEFAULT_HOURS,
        )
        return default


class PendingAssessment:
    """A reading that has been started. ``result`` waits for it, within a budget."""

    def __init__(self, settled: Assessment | None, answer: Queue | None = None, refusal=None):
        self._settled = settled
        self._answer = answer
        self._refusal = refusal
        self._lock = Lock()

    def result(self, budget_seconds: float) -> Assessment:
        """The assessment, or a refusal if it is not ready within the budget.

        The answer is settled the first time this returns, so asking twice
        gives the same one. A budget too small to be worth starting on is
        ``no_budget`` unless the reading has already finished.
        """
        with self._lock:
            if self._settled is not None:
                return self._settled
            try:
                try:
                    self._settled = self._answer.get_nowait()
                except Empty:
                    if not math.isfinite(budget_seconds) or budget_seconds < MIN_BUDGET_SECONDS:
                        self._settled = self._refusal(VERDICT_NO_BUDGET)
                    else:
                        self._settled = self._answer.get(
                            timeout=min(budget_seconds, MAX_BUDGET_SECONDS),
                        )
            except Empty:
                self._settled = self._refusal(VERDICT_TIMEOUT)
            except Exception:
                self._settled = self._refusal(VERDICT_ERROR)
            return self._settled


class FarmMachineryPolicy:
    """What the processor consults about a burst no plate has opened the gate for.

    ``embed`` is the model boundary -- ``ClipImageTower.embed`` in production,
    anything that turns JPEG bytes into an embedding in a test. Everything from
    there to ``would_admit`` is this class and :func:`judge`.

    It remembers one thing between bursts: the last few *clear* machinery
    frames and when they were taken, which is what "standing still" is judged
    against. Nothing else carries over, and nothing is remembered about a
    frame that was not clearly a machine.
    """

    def __init__(self, mode: str, embed, scorer: PromptScorer, *,
                 min_margin: float = DEFAULT_MIN_MARGIN,
                 min_machine: float = DEFAULT_MIN_MACHINE,
                 still_cosine: float = DEFAULT_STILL_COSINE,
                 hours: Hours | None = None, unavailable_reason: str | None = None,
                 clock=None):
        self._mode = mode if mode in MODES else MODE_OFF
        self._embed = embed
        self._scorer = scorer
        self._min_margin = min_margin
        self._min_machine = min_machine
        self._still_cosine = still_cosine
        self._hours = hours or Hours.parse(DEFAULT_HOURS, DEFAULT_TIMEZONE)
        self._unavailable_reason = unavailable_reason
        self._clock = clock or monotonic
        self._running = Lock()
        # The early trigger's shadow look, kept apart from `_running` so that
        # it can never be the reason a reading is refused.
        self._looking = Lock()
        self._memory_lock = Lock()
        self._begun: deque = deque()
        self._clear_frames: deque = deque(maxlen=STILL_MEMORY)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def consulted(self) -> bool:
        """Whether the processor should ask at all."""
        return self._mode in (MODE_SHADOW, MODE_ON)

    @property
    def admits(self) -> bool:
        """Whether a ``would_admit`` may reach the relay. Only ``on``."""
        return self._mode == MODE_ON

    def describe(self) -> str:
        return (
            f"mode={self._mode} min_margin={self._min_margin:.2f} "
            f"min_machine={self._min_machine:.2f} still_cosine={self._still_cosine:.2f} "
            f"hours={self._hours.start // 60:02d}:{self._hours.start % 60:02d}-"
            f"{self._hours.end // 60:02d}:{self._hours.end % 60:02d} "
            f"model={'ready' if self._unavailable_reason is None else self._unavailable_reason}"
        )

    def assess(self, paths, *, budget_seconds: float, at: datetime | None = None) -> Assessment:
        """One burst's answer, waited for. Never raises, never outstays its budget."""
        return self.begin(paths, at=at).result(budget_seconds)

    def begin(self, paths, *, at: datetime | None = None) -> "PendingAssessment":
        """Start reading a burst and return at once. Never raises.

        ``at`` is when the burst was captured. The reading runs on its own
        thread for two reasons. The wait for it can be bounded -- the image
        tower cannot be interrupted, but a decision lane need not stand behind
        it. And it can be started when a frame arrives and collected when the
        plate path has finished with it, so that in the ordinary case nobody
        waits at all. Readings are strictly one at a time, in the order they
        were begun, which is what keeps the standing-still memory in capture
        order; a burst that arrives while one is running is answered ``busy``.
        """
        paths = tuple(paths)
        at = at or datetime.now(timezone.utc)
        if self._unavailable_reason is not None or self._embed is None:
            return PendingAssessment(self._refusal(VERDICT_UNAVAILABLE))
        if not paths:
            return PendingAssessment(self._refusal(VERDICT_UNREADABLE))
        if not self._running.acquire(blocking=False):
            return PendingAssessment(self._refusal(VERDICT_BUSY))
        if not self._within_rate():
            self._running.release()
            return PendingAssessment(self._refusal(VERDICT_RATE_LIMITED))
        answer: Queue = Queue(maxsize=1)

        def read():
            try:
                answer.put(self._assess_now(paths, at))
            finally:
                self._running.release()

        try:
            Thread(target=read, name="gate-agri-assess", daemon=True).start()
        except Exception:
            self._running.release()
            return PendingAssessment(self._refusal(VERDICT_ERROR))
        return PendingAssessment(None, answer, self._refusal)

    def look(self, jpeg: bytes) -> dict:
        """What is in one picture, for a caller that decides nothing. Never raises.

        The early trigger's shadow record asks this of the frame that made it
        fire: label shares, ``empty`` against every kind of vehicle. It has a
        lock of its own -- one look at a time -- and never takes the readings'
        lock, so a look in progress can never make :meth:`begin` answer
        ``busy`` to a real burst: for the tenth of a second they overlap the
        tower simply runs twice, which it is safe to do. It touches neither
        the rate the readings are capped at nor the standing-still memory.
        """
        if self._unavailable_reason is not None or self._embed is None:
            return {"status": "unavailable"}
        if not self._looking.acquire(blocking=False):
            return {"status": "skipped_busy"}
        try:
            embedding = self._embed(jpeg)
            if embedding is None:
                return {"status": "unreadable"}
            shares = self._scorer.shares(embedding)
            return {
                "status": "ok",
                "shares": {label: round(share, 4) for label, share in shares.items()},
                "top": max(shares, key=shares.get),
                "empty": round(shares.get("empty", 0.0), 4),
            }
        except Exception:
            return {"status": "error"}
        finally:
            self._looking.release()

    def _within_rate(self) -> bool:
        """Whether another reading may start this minute. Holds ``_running``."""
        now = self._clock()
        while self._begun and now - self._begun[0] >= 60.0:
            self._begun.popleft()
        if len(self._begun) >= MAX_READINGS_PER_MINUTE:
            return False
        self._begun.append(now)
        return True

    def _refusal(self, verdict: str) -> Assessment:
        return Assessment(
            would_admit=False, verdict=verdict,
            min_margin=self._min_margin, min_machine=self._min_machine,
        )

    def _assess_now(self, paths, at: datetime) -> Assessment:
        started = self._clock()
        stillness = still_seconds = None
        frames = ()
        try:
            read = self._read_frames(paths)
            frames = tuple(scores for scores, _embedding in read)
            would_admit, verdict = judge(
                frames, min_margin=self._min_margin, min_machine=self._min_machine,
            )
            if would_admit:
                embeddings = [embedding for _scores, embedding in read]
                stillness, still_seconds = self._standing_still(embeddings, at)
                if stillness is None or stillness < self._still_cosine:
                    would_admit, verdict = False, VERDICT_NOT_STILL
                elif not self._hours.open_at(at):
                    would_admit, verdict = False, VERDICT_OUTSIDE_HOURS
        except Exception:
            LOGGER.warning("gate_agri stage=assess_failed", exc_info=True)
            would_admit, verdict, frames = False, VERDICT_ERROR, ()
        return Assessment(
            would_admit=would_admit, verdict=verdict, frames=tuple(frames),
            elapsed_ms=max(0, round((self._clock() - started) * 1000)),
            min_margin=self._min_margin, min_machine=self._min_machine,
            stillness=stillness, still_seconds=still_seconds,
        )

    def _standing_still(self, embeddings, at: datetime):
        """How like an earlier clear frame this burst is, then remember it.

        Only ever called with a burst every frame of which is clearly a
        machine. The answer is the best match among clear frames taken between
        ``STILL_MIN_SECONDS`` and ``STILL_MAX_SECONDS`` before this one: closer
        than that and two frames of a moving machine still look alike (0.964 at
        0.7 s, on a departing tractor); further and it is another passage.
        """
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        best = None
        with self._memory_lock:
            for earlier_at, earlier in self._clear_frames:
                age = (at - earlier_at).total_seconds()
                if not STILL_MIN_SECONDS <= age <= STILL_MAX_SECONDS:
                    continue
                for embedding in embeddings:
                    likeness = _cosine(embedding, earlier)
                    if best is None or likeness > best[0]:
                        best = (likeness, age)
            for embedding in embeddings:
                self._clear_frames.append((at, embedding))
        return best if best is not None else (None, None)

    def _read_frames(self, paths):
        read = []
        for path in paths[:MAX_FRAMES]:
            embedding = self._embed(Path(path).read_bytes())
            if embedding is None:
                # A frame that cannot be read is not a frame that agreed.
                return ()
            embedding = tuple(float(value) for value in embedding)
            read.append((self._scorer.score(embedding), embedding))
        return tuple(read)


def _cosine(first, second) -> float:
    dot = sum(a * b for a, b in zip(first, second))
    norm = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return dot / norm if norm > 0 else 0.0


def build_policy(environment=None, *, model_dir: Path | None = None) -> FarmMachineryPolicy | None:
    """The policy the controller runs with, or None when it is ``off``.

    ``off`` builds nothing: no prompts are parsed, no model is loaded and the
    processor is handed no policy at all. In ``shadow`` and ``on`` the image
    tower is loaded here, at start-up, so the three quarters of a second that
    takes is never spent inside a decision. A model that is not installed
    leaves a policy that answers ``model_unavailable`` to everything.
    """
    environment = os.environ if environment is None else environment
    mode = load_mode(environment)
    if mode == MODE_OFF:
        return None
    embed, reason = None, None
    try:
        scorer = PromptScorer()
    except Exception as error:
        LOGGER.warning("gate_agri stage=unavailable detail=prompts unreadable: %s",
                       type(error).__name__)
        return None
    try:
        tower = ClipImageTower(scorer, model_dir=model_dir)
        embed, reason = tower.embed, tower.unavailable_reason
    except Exception as error:
        reason = f"model would not load: {type(error).__name__}"
    policy = FarmMachineryPolicy(
        mode, embed, scorer,
        min_margin=_bounded_share(environment, ENV_MIN_MARGIN, DEFAULT_MIN_MARGIN),
        min_machine=_bounded_share(environment, ENV_MIN_MACHINE, DEFAULT_MIN_MACHINE),
        still_cosine=_bounded_share(environment, ENV_STILL_COSINE, DEFAULT_STILL_COSINE),
        hours=_load_hours(environment), unavailable_reason=reason,
    )
    LOGGER.info("gate_agri stage=configured %s", policy.describe())
    return policy
