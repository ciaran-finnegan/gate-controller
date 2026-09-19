"""Which way a vehicle was going, from what the camera actually saw.

An arriving car shows the camera its **front**: it comes up the approach from
the road, headlights and grille toward the lens. A departing car comes from
inside the property, from behind the camera, so it appears suddenly and
side-on right beside it and then shows its **rear** as it drives away. A person
glancing at the photo can tell which in an instant, and so, it turns out, can a
general image model that was never trained on this gate.

Measured 2026-09-20 on passages labelled by eye: CLIP (ViT-B/32, image tower
only, quantised to 8 bits, 84 MB) called 6 of 6 correctly from single frames --
rear 0.95/0.80/0.69/0.76 on four departures, front 1.00 on an arrival. The
estimator this complements fits a slope of box width over three frames spanning
two seconds, which a passage here rarely supplies: it answered on 18 of 991.

Two things the measurement had to settle:

* **Centre-crop, don't letterbox.** Shrinking the whole wide frame into the
  square the model takes made it call every departure "arriving", confidently.
  The standard centre crop keeps the vehicle large enough to show the features
  that matter.
* **Side-on counts.** The close side view of a departing car reads as "rear"
  more than "front", which is the right way round for this purpose.

The text side never changes, so the prompts are embedded once and shipped as
numbers in ``models/direction-clip-v1.json``. Only the image tower runs here,
and it is installed beside the plate models rather than kept in the repository.
Without it this reports itself unavailable and does nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path("/var/lib/gate-controller/models")
MODEL_FILENAME = "clip-vit-b32-visual-int8.onnx"
SPEC_FILENAME = "direction-clip-v1.json"

#: Below this much front-or-rear between them, the frame is not of a vehicle
#: seen end-on -- an empty driveway, a farm machine, a door panel -- and says
#: nothing about direction.
MIN_END_ON_MASS = 0.3

ENTERING = "entering"
EXITING = "exiting"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class FrameReading:
    """What the model saw in one frame."""

    front: float
    rear: float
    top: str

    @property
    def direction(self) -> str:
        if self.front + self.rear < MIN_END_ON_MASS:
            return UNKNOWN
        return ENTERING if self.front > self.rear else EXITING

    @property
    def score(self) -> float:
        """How far one end outweighs the other; 0 when it says nothing."""
        if self.direction == UNKNOWN:
            return 0.0
        return abs(self.front - self.rear)


class VisionDirection:
    """The image tower and the frozen prompts. Never raises on a missing model."""

    def __init__(self, model_dir: Path | None = None, spec_path: Path | None = None):
        self._model_dir = Path(model_dir or os.environ.get("GATE_MODEL_DIR", DEFAULT_MODEL_DIR))
        self._spec_path = Path(spec_path or Path(__file__).with_name("models") / SPEC_FILENAME)
        self._session = None
        self._reason: str | None = None
        self._load()

    @property
    def available(self) -> bool:
        return self._session is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def _load(self) -> None:
        """Say why it cannot run, most useful reason first.

        The model file is checked before anything is imported: on a board
        without it, "not installed" is the answer that tells someone what to
        do, and it should not be masked by a missing library on a machine --
        CI, say -- that was never going to run it anyway.
        """
        try:
            spec = json.loads(self._spec_path.read_text(encoding="utf-8"))
            labels = list(spec["labels"])
            text = spec["text_embeddings"]
            mean, std = spec["mean"], spec["std"]
            self._size = int(spec["input_size"])
            self._scale = float(spec["logit_scale"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._reason = f"prompt embeddings unreadable: {type(error).__name__}"
            return
        path = self._model_dir / MODEL_FILENAME
        if not path.exists():
            self._reason = f"{path} is not installed"
            return
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as error:
            self._reason = f"{error.name or 'a dependency'} is not available"
            return
        try:
            self._labels = labels
            self._text = np.array(text, dtype=np.float32)
            self._mean = np.array(mean, dtype=np.float32)
            self._std = np.array(std, dtype=np.float32)
            options = ort.SessionOptions()
            # One thread, like every other model on this board: plate
            # recognition must never wait behind a direction verdict.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        except Exception as error:  # onnxruntime raises broadly
            self._reason = f"model would not load: {type(error).__name__}"
            self._session = None

    def _prepare(self, jpeg: bytes):
        """Shortest side to the model's size, then the centre square.

        Not a letterbox: shrinking the whole wide frame to fit made the model
        call every departure "arriving". See the module docstring.
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
        pixels = (np.asarray(image, dtype=np.float32) / 255.0 - self._mean) / self._std
        return pixels.transpose(2, 0, 1)[None]

    def read(self, jpeg: bytes) -> FrameReading | None:
        """One frame's reading, or None if the model is missing or the image is not one."""
        if not self.available:
            return None
        try:
            import numpy as np

            embedding = self._session.run(None, {"image": self._prepare(jpeg)})[0][0]
        except Exception:
            LOGGER.warning("gate_direction_vision stage=read_failed", exc_info=False)
            return None
        logits = self._scale * (self._text @ embedding)
        weights = np.exp(logits - logits.max())
        weights /= weights.sum()
        per: dict[str, float] = {}
        for label, weight in zip(self._labels, weights):
            per[label] = per.get(label, 0.0) + float(weight)
        return FrameReading(
            front=per.get("front", 0.0), rear=per.get("rear", 0.0),
            top=max(per, key=per.get),
        )
