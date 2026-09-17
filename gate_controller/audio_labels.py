"""Turn recorded audio into labelled examples, durably.

Recording is solved: every five-minute segment now reaches R2. Labelling is
not. The classifier that found seven uncommanded gate openings was trained on
four spans I typed into a dictionary by hand, and when that session ended the
labels went with it. Eight weeks of recording without this module is eight
weeks of *unlabelled* audio.

The loop this closes
--------------------
1. **Propose.** Candidates come from three places, in descending order of how
   much they cost: the relay, which labels its own actuations for free; a
   detector run over the segments, which proposes and is often wrong; and a
   person who heard something.
2. **Review.** A human confirms, corrects or rejects. There is no way around
   this: "is that the gate or a tractor" is exactly the judgement the model
   does not yet have, and inventing labels to avoid asking is how a training
   set quietly becomes fiction.
3. **Keep.** Verdicts append to a store that outlives the session, ships to R2
   with everything else, and can be replayed into a training set months later.

Why append-only
---------------
A label is an observation, not a setting. Somebody deciding in November that a
clip they called `gate_motor` in September was really a tractor is *new
information about both*, and overwriting the September row would destroy the
evidence that the two are confusable -- which is the single most useful thing
that disagreement could tell us. So rows are appended and the latest verdict
for a clip wins, with the history intact behind it.

The store is JSON Lines for the same reason the segment filenames are
timestamps: a format that survives a truncated write, is readable without this
code, and appends without rewriting.

What this module does not do
----------------------------
It runs no model and owns no thresholds. Candidates arrive from outside with
whatever confidence their source claims. That keeps the labels independent of
the detector that proposed them, so a detector replaced next month does not
invalidate a corpus built with this one.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: The vocabulary, fixed deliberately. A free-text label field would fill with
#: `gate`, `Gate`, `gate motor` and `motor?` inside a week, and no training set
#: could be built from it without a cleanup pass that loses information.
#:
#: The distinctions here are the ones that were argued for on evidence:
#: opening and closing are separate because a run that ends without a clang is
#: the gate standing open; the clang is separate because it is the only
#: positive confirmation of closure; and `tractor` exists as its own label
#: because agricultural traffic is loud, sustained and harmonic in the same
#: band as the motor, which makes it the confusion most likely to matter.
LABELS = (
    "gate_motor_opening",
    "gate_motor_closing",
    "gate_clang",
    "gate_motor_unknown_direction",
    "vehicle_entering",
    "vehicle_exiting",
    "vehicle_passing_road",
    "tractor",
    "rain",
    "wind",
    "birds",
    "aircraft",
    "voices",
    "other",
    #: Explicitly "there is nothing here", which is not the same as an unlabelled
    #: clip. A confirmed negative is worth as much as a positive and there is no
    #: other way to record that somebody listened and heard nothing.
    "nothing",
)

#: Who said so. A relay actuation is not evidence of the same kind as a person
#: who listened, and a training run should be able to weight them differently
#: or exclude the cheap ones entirely.
SOURCES = ("relay", "detector", "human", "import")

#: A verdict from a person supersedes anything a machine proposed, whenever it
#: was made. Without this a detector re-run would silently overwrite review.
HUMAN_WINS = True

MAX_NOTE_BYTES = 500


@dataclass(frozen=True)
class Candidate:
    """Something worth listening to, before anyone has said what it is."""

    #: The instant the clip starts, UTC.
    at: datetime
    seconds: float
    source: str
    #: What proposed it, and how sure it was. A relay actuation has no
    #: confidence because it is not a guess.
    confidence: float | None = None
    detail: str | None = None

    @property
    def clip_id(self) -> str:
        """A stable name for this span of audio.

        Derived from the instant and the length rather than assigned, so the
        same span proposed twice by two different detectors is one clip with
        two opinions, not two clips.
        """
        key = f"{self.at.astimezone(timezone.utc).isoformat()}|{round(self.seconds, 1)}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "at": self.at.astimezone(timezone.utc).isoformat(),
            "seconds": round(float(self.seconds), 2),
            "source": self.source,
            "confidence": None if self.confidence is None else round(float(self.confidence), 4),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Verdict:
    """One opinion about one clip, at one moment."""

    clip_id: str
    label: str
    source: str
    at: datetime
    by: str | None = None
    note: str | None = None
    confidence: float | None = None

    def as_row(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "clip_id": self.clip_id,
            "label": self.label,
            "source": self.source,
            "recorded_at": self.at.astimezone(timezone.utc).isoformat(),
            "by": self.by,
            "note": self.note,
            "confidence": None if self.confidence is None else round(float(self.confidence), 4),
        }


class LabelStore:
    """An append-only record of what people and machines said about clips."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- writing ----------------------------------------------------------
    def record(self, verdict: Verdict) -> None:
        """Append one verdict. Never rewrites, never reorders."""
        if verdict.label not in LABELS:
            raise ValueError(f"{verdict.label!r} is not one of {len(LABELS)} known labels")
        if verdict.source not in SOURCES:
            raise ValueError(f"{verdict.source!r} is not one of {SOURCES}")
        note = verdict.note
        if note is not None and len(note.encode("utf-8")) > MAX_NOTE_BYTES:
            raise ValueError("note is too long")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(verdict.as_row(), separators=(",", ":"), sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def record_many(self, verdicts) -> int:
        count = 0
        for verdict in verdicts:
            self.record(verdict)
            count += 1
        return count

    # -- reading ----------------------------------------------------------
    def rows(self) -> list[dict]:
        """Every verdict ever recorded, in the order it was recorded.

        A line that will not parse is skipped rather than raising: the file is
        appended to under power loss, so a truncated final line is expected and
        is not a reason to lose every label before it.
        """
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    LOGGER.warning("gate_audio_labels stage=unparsable_row")
                    continue
                if isinstance(row, dict) and row.get("clip_id") and row.get("label"):
                    out.append(row)
        return out

    def current(self) -> dict:
        """The label that stands for each clip, and what it is based on.

        Latest verdict wins, except that a human verdict is never superseded by
        a machine one however recent -- a detector re-run must not quietly undo
        review.
        """
        standing: dict[str, dict] = {}
        for row in self.rows():
            clip = row["clip_id"]
            held = standing.get(clip)
            if held is None:
                standing[clip] = row
                continue
            if HUMAN_WINS and held.get("source") == "human" and row.get("source") != "human":
                continue
            standing[clip] = row
        return standing

    def history(self, clip_id: str) -> list[dict]:
        """Everything ever said about one clip, oldest first.

        Disagreement is evidence. A clip called `gate_motor` in September and
        `tractor` in November says those two are confusable, which is more
        useful than either verdict alone.
        """
        return [row for row in self.rows() if row["clip_id"] == clip_id]

    def disputed(self) -> dict:
        """Clips that have been given more than one distinct label."""
        seen: dict[str, set] = {}
        for row in self.rows():
            seen.setdefault(row["clip_id"], set()).add(row["label"])
        return {clip: labels for clip, labels in seen.items() if len(labels) > 1}

    def counts(self) -> dict:
        """How many clips stand at each label. The corpus, at a glance."""
        tally: dict[str, int] = {}
        for row in self.current().values():
            tally[row["label"]] = tally.get(row["label"], 0) + 1
        return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))


def candidates_from_actuations(rows, *, before_seconds: float = 5.0,
                               seconds: float = 90.0) -> list[Candidate]:
    """Free candidates: every relay actuation is a gate cycle somebody paid for.

    These are the cheapest labels in the system and the only ones that need no
    review at all to be *useful* -- the gate certainly moved. What they cannot
    say is whether it opened or closed, or whether it finished, so they are
    proposed as ``gate_motor_unknown_direction`` and a reviewer refines them.
    """
    found = []
    for row in rows:
        moment = _parse(row.get("relay_activated_at"))
        if moment is None:
            continue
        found.append(Candidate(
            at=moment - timedelta(seconds=before_seconds),
            seconds=seconds,
            source="relay",
            detail=f"event {row.get('id')} {row.get('source') or ''}".strip(),
        ))
    return found


def candidates_from_detections(detections) -> list[Candidate]:
    """Candidates a detector proposed. Often wrong, which is the point."""
    found = []
    for item in detections:
        moment = _parse(item.get("at"))
        if moment is None:
            continue
        found.append(Candidate(
            at=moment,
            seconds=float(item.get("seconds") or 0.0),
            source="detector",
            confidence=item.get("confidence"),
            detail=item.get("detail"),
        ))
    return [c for c in found if c.seconds > 0]


def unreviewed(candidates, store: LabelStore) -> list[Candidate]:
    """Candidates nobody has judged yet, newest first.

    Newest first because a reviewer with ten minutes should spend them on
    audio whose conditions they might still remember, not on the oldest thing
    in the queue.
    """
    standing = store.current()
    pending = [c for c in candidates
               if standing.get(c.clip_id, {}).get("source") != "human"]
    return sorted(pending, key=lambda c: c.at, reverse=True)


def training_rows(store: LabelStore, *, sources=("human",), labels=None) -> list[dict]:
    """The labelled set to train on.

    Defaults to human verdicts only. A model trained on its own detector's
    proposals would learn to agree with itself, which is the failure mode this
    whole loop exists to avoid; including the cheap sources has to be a
    deliberate argument, not the default.
    """
    wanted = set(labels) if labels else None
    out = []
    for row in store.current().values():
        if row.get("source") not in sources:
            continue
        if wanted is not None and row["label"] not in wanted:
            continue
        out.append(row)
    return sorted(out, key=lambda row: row["clip_id"])


def write_manifest(path: Path, candidates, store: LabelStore) -> dict:
    """A review queue somebody can open: candidates plus what is known so far."""
    standing = store.current()
    items = []
    for candidate in candidates:
        row = standing.get(candidate.clip_id)
        entry = candidate.as_dict()
        entry["label"] = row["label"] if row else None
        entry["label_source"] = row["source"] if row else None
        items.append(entry)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "labels": list(LABELS),
        "counts": store.counts(),
        "items": items,
    }
    _write_private(Path(path), json.dumps(document, indent=1).encode("utf-8"))
    return document


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
