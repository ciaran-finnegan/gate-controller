"""Hear the gate, with a model that was trained rather than a threshold.

The first detector measured band *shares* -- how much of a 32 ms frame's
energy sat between 1.2 and 3 kHz. It was written from four cycles recorded in
one afternoon, was correct on all four, and on the first overnight recording
reported a 629-second motor run and called 62% of a quiet half hour a gate
moving. Share has a denominator: in daylight, low-frequency wind held it down,
and overnight that wind is gone and the ratio saturates on whatever is left.

Two more objections were not fixable by tuning it. Ireland does not divide into
"day" and "night", so a threshold set against two samples of weather is set
against nothing. And a tractor is a motor: agricultural traffic, or a diesel
idling at the gate waiting for it to open, is loud, sustained and harmonic in
exactly the band the rule called "gate".

What replaced it: YAMNet -- a MobileNet trained on AudioSet -- used only for
its 1024-dimensional **embedding**, with a linear classifier on top. Its own
class outputs are weak on this audio, which is band-limited to 16 kHz, quiet,
and recorded at distance; the gate motor reads to AudioSet as "Silence,
Animal, Snake". The embedding is another matter. Leave-one-cycle-out over the
four commanded cycles: 80-100% recall of the motor, and under half a percent
of frames flagged on the same overnight audio the threshold rule called 62%.

The classifier is 1024 weights and an intercept. It lives in this repository
as JSON -- diffable, readable without this code, and small enough that
deploying a retrained one is a text change. YAMNet itself is 16 MB, never
changes, and is not in the repository: it is installed beside the plate models
and this module reports itself unavailable without it, rather than failing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: YAMNet consumes 16 kHz mono and answers once per 0.48 s hop.
SAMPLE_RATE = 16000
HOP_SECONDS = 0.48

DEFAULT_MODEL_DIR = Path("/var/lib/gate-controller/models")
YAMNET_FILENAME = "yamnet.onnx"
#: Shipped in this package; see ``models/gate-motor-v2.json``.
#:
#: v1 was trained on four cycles from one afternoon and reported about a
#: hundred gate movements a day at a site that sees ten passages. v2 is
#: trained on 49.2 hours of retained recording -- rain, wind, night, daylight
#: and three quarters of an hour of farm machinery -- and is scored by held-out
#: *day*, never by held-out cycle. Measured, with the day in question excluded
#: from training entirely:
#:
#: ====================================  ==========  ==========
#: held-out day                          2026-09-19  2026-09-20
#: ====================================  ==========  ==========
#: motor runs per day, v1                      79.4       107.5
#: motor runs per day, v2                      25.0        19.3
#: uncorroborated runs per day, v1             25.0        58.8
#: uncorroborated runs per day, v2               0.0         0.0
#: relay openings found, v1                      4/4         6/6
#: relay openings found, v2                      4/4         5/6
#: runs confirmed by eye, v1                     8/8       15/15
#: runs confirmed by eye, v2                     8/8       13/15
#: ====================================  ==========  ==========
#:
#: "Uncorroborated" means a run with no relay firing, no latch clang and no
#: camera event within four minutes either side. Thirty-six of those were
#: rendered as spectrograms and looked at: every one was wind, a road vehicle,
#: rain or farm machinery, and none was overturned.
#:
#: v1 is kept beside it. A model is evidence about a moment in a site's life,
#: and the previous one is what the movements already in the database were
#: judged by; deleting it would make those rows unreadable.
CLASSIFIER_FILENAME = "gate-motor-v2.json"

#: Above this a frame is the motor. 0.5 is where a logistic classifier's own
#: decision boundary sits; it is named rather than inlined because it is the
#: one number here that a future retraining might want to move.
MOTOR_PROBABILITY = 0.5
#: Shorter than this is not a gate.
#:
#: The measured travel is 15-27 seconds. At a three-second floor a scan of
#: forty-eight segments found 88 openings against 7 closings -- and a gate
#: that opens 88 times shuts 88 times, so most of those were transients the
#: classifier fired on for a frame or two. Eight seconds is well under the
#: shortest real travel, with room for a run clipped by a segment boundary or
#: broken by a dropped frame, and well over anything that is not the motor.
MIN_RUN_SECONDS = 8.0
#: A dropout shorter than this does not end a run. The classifier misses the
#: odd frame mid-travel, and splitting a single 24-second movement into five
#: would make the duration -- which is how a stall is told from a completed
#: journey -- meaningless.
MAX_GAP_SECONDS = 1.5


@dataclass(frozen=True)
class SoundWindow:
    """One classified stretch of recorded audio."""

    started_at: datetime
    probabilities: list[float]

    def at(self, index: int) -> datetime:
        return self.started_at + timedelta(seconds=index * HOP_SECONDS)


def _sigmoid(value: float) -> float:
    # Written out rather than via exp() alone so a large negative logit cannot
    # overflow: this runs over a whole night of audio unattended.
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    scaled = math.exp(value)
    return scaled / (1.0 + scaled)


class GateSoundModel:
    """YAMNet's embedding plus the linear layer that knows this gate.

    Never raises on a missing model: ``available`` is False and every caller
    treats that as "no opinion", which is the same thing the recorder does
    when the stream is down. A gate controller must not stop opening gates
    because an analysis model is missing.
    """

    def __init__(self, model_dir: Path | None = None, classifier_path: Path | None = None):
        self._model_dir = Path(model_dir or os.environ.get("GATE_MODEL_DIR", DEFAULT_MODEL_DIR))
        self._classifier_path = Path(
            classifier_path or Path(__file__).with_name("models") / CLASSIFIER_FILENAME
        )
        self._session = None
        self._weights: list[float] = []
        self._intercept = 0.0
        self._reason: str | None = None
        self._load()

    @property
    def available(self) -> bool:
        return self._session is not None and bool(self._weights)

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def _load(self) -> None:
        try:
            document = json.loads(self._classifier_path.read_text(encoding="utf-8"))
            self._weights = [float(value) for value in document["weights"]]
            self._intercept = float(document["intercept"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._reason = f"classifier unreadable: {type(error).__name__}"
            return
        path = self._model_dir / YAMNET_FILENAME
        if not path.exists():
            # Expected on a board where the model has not been installed yet.
            self._reason = f"{path} is not installed"
            return
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            # One thread: this runs beside plate recognition on four cores and
            # must never be the reason a read is late.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(path), options, providers=["CPUExecutionProvider"],
            )
        except Exception as error:  # onnxruntime raises broadly
            self._reason = f"yamnet would not load: {type(error).__name__}"
            self._session = None

    def classify(self, samples, started_at: datetime) -> SoundWindow:
        """Motor probability per 0.48 s frame of ``samples`` (float32, 16 kHz)."""
        if not self.available:
            return SoundWindow(started_at, [])
        _classes, embeddings, _spectrogram = self._session.run(None, {"waveform": samples})
        probabilities = []
        for row in embeddings:
            total = self._intercept
            for weight, value in zip(self._weights, row):
                total += weight * float(value)
            probabilities.append(_sigmoid(total))
        return SoundWindow(started_at, probabilities)


def motor_runs(window: SoundWindow, *, threshold: float = MOTOR_PROBABILITY,
               min_seconds: float = MIN_RUN_SECONDS,
               max_gap_seconds: float = MAX_GAP_SECONDS):
    """Sustained stretches above ``threshold``, as ``MotorRun``s.

    Duration is the whole point of joining across a short dropout: a completed
    travel is fifteen to twenty-seven seconds and a stall against an obstruction
    is not, and that distinction disappears if one movement is reported as five.
    """
    from .gate_audio_detect import MotorRun

    runs = []
    start: int | None = None
    last_over: int | None = None
    for index, probability in enumerate(window.probabilities):
        if probability >= threshold:
            if start is None:
                start = index
            last_over = index
            continue
        if start is None or last_over is None:
            continue
        if (index - last_over) * HOP_SECONDS <= max_gap_seconds:
            continue
        runs.append((start, last_over))
        start = last_over = None
    if start is not None and last_over is not None:
        runs.append((start, last_over))

    return [
        MotorRun(start=window.at(first), end=window.at(last + 1))
        for first, last in runs
        if (last + 1 - first) * HOP_SECONDS >= min_seconds
    ]
