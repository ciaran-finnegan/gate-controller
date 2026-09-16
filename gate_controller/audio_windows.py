"""Cut the audio around each vehicle event out of the recorded segments.

The recorder keeps the whole day; this decides what was worth keeping. It runs
once a day, reads the controller's own ``events`` table for the period, cuts a
window around each one out of the segments, and writes each window into the
training corpus as an ``.aac`` payload plus a ``.json`` sidecar. From there the
existing uploader ships it to R2 as ``kind=audio`` with no changes anywhere:
``.aac`` is already in its media-type table and the corpus already walks the
``audio`` directory.

Where the labels come from, for nothing
---------------------------------------
Every row in ``events`` already carries what the window needs to be a training
example, produced by the pipeline for its own reasons:

* ``relay_activated_at`` -- the instant the gate was *commanded* to move, so
  the motor run and the closing clang sit at a known offset from it. This is
  the free label the whole approach rests on.
* ``opened`` and ``actuation_outcome`` -- whether the gate moved at all, which
  separates a passage that opened the gate from one that did not.
* ``source`` and ``reason`` -- a webhook vehicle detection against an FTP
  still, so a window can be filtered by what noticed the vehicle.
* ``observed_plate`` -- the same vehicle recurring, which is how a per-vehicle
  acoustic signature could ever be checked.

The direction estimate ``direction.py`` already produces in shadow mode is the
fourth, and it is what makes *entering* against *exiting* trainable without
anyone labelling a recording by hand.

Windows, and why they overlap on purpose
-----------------------------------------
A window runs from ``before_seconds`` ahead of the event to ``after_seconds``
past it. The pre-roll is the point: the vehicle approaching and the motor
starting both happen before the relay fires, and the per-event recorder could
never capture either. The tail is long enough to hold a gate that opens, waits
and closes.

Two events close together produce one merged window rather than two clips of
mostly the same audio, and the sidecar names every event inside it. Duplicated
audio in a training set is not neutral: it inflates whichever class happens to
arrive in bursts.

Nothing here is on the recognition path. It reads a read-only copy of the
database, writes into the corpus buffer, and runs on a timer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

from .audio_segments import SegmentStore, extract_window, iter_adts_frames

LOGGER = logging.getLogger(__name__)

#: Far enough ahead of the event to hold the approach and the motor starting.
#: The relay fires a second or two after the camera notices, and a vehicle is
#: audible well before that.
DEFAULT_BEFORE_SECONDS = 60.0
#: Far enough past it to hold open, dwell and close. The gate's auto-close
#: timer lives in the motor controller and is not known here, which is exactly
#: why this is generous and why it can be changed without redeploying: the
#: segments are still on the card and the window can simply be re-cut.
DEFAULT_AFTER_SECONDS = 180.0
MIN_WINDOW_SECONDS = 5.0
MAX_WINDOW_SECONDS = 900.0

SIDECAR_SCHEMA_VERSION = 2
WINDOW_KIND = "audio"
WINDOW_MEDIA_TYPE = "audio/aac"
#: The corpus orders artefacts by stem, so the stem is the capture instant in
#: the same shape ``audio_capture`` uses. A window sorts among the frames of
#: the same passage rather than after all of them.
STEM_FORMAT = "%Y%m%dT%H%M%S%fZ"

EVENT_COLUMNS = (
    "id", "received_at", "decision_at", "relay_activated_at", "source", "reason",
    "opened", "observed_plate", "authorised_plate", "ocr_confidence", "actuation_outcome",
)


@dataclass
class Window:
    """One span of audio to cut, and every event that asked for it."""

    start: datetime
    end: datetime
    events: list[dict] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def overlaps(self, other: "Window") -> bool:
        return other.start <= self.end and self.start <= other.end


def read_events(database: Path, start: datetime, end: datetime) -> list[dict]:
    """The events in the period, oldest first, from a read-only connection.

    Read-only by URI rather than by convention: this runs while the controller
    is live and must not be able to take a write lock on the database a gate
    decision is about to commit to.
    """
    columns = ", ".join(EVENT_COLUMNS)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT {columns} FROM events WHERE received_at >= ? AND received_at < ?"
            " ORDER BY received_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def plan_windows(events, *, before_seconds: float = DEFAULT_BEFORE_SECONDS,
                 after_seconds: float = DEFAULT_AFTER_SECONDS) -> list[Window]:
    """One window per event, with overlapping ones merged into one cut."""
    before = _bounded(before_seconds)
    after = _bounded(after_seconds)
    planned: list[Window] = []
    for event in events:
        moment = _parse(event.get("received_at"))
        if moment is None:
            continue
        window = Window(moment - timedelta(seconds=before),
                        moment + timedelta(seconds=after), [event])
        if planned and planned[-1].overlaps(window):
            merged = planned[-1]
            merged.end = max(merged.end, window.end)
            merged.events.extend(window.events)
        else:
            planned.append(window)
    return planned


def build_sidecar(window: Window, *, captured_at: datetime, audio_seconds: float,
                  requested_seconds: float, source_url: str) -> dict:
    """What this clip is and what the controller was doing while it recorded.

    ``label`` is the coarse free label -- whether the gate was commanded to
    move inside this window -- and it is deliberately the same vocabulary
    ``audio_capture`` used, so clips from both paths answer the same question.
    The per-event detail below it is what a finer label would be built from.
    """
    actuations = [
        {
            "event_id": event.get("id"),
            "relay_activated_at": event.get("relay_activated_at"),
            "offset_seconds": _offset(event.get("relay_activated_at"), captured_at),
            "outcome": event.get("actuation_outcome"),
        }
        for event in window.events
        if event.get("relay_activated_at")
    ]
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "kind": "gate_audio_window",
        "captured_at": captured_at.isoformat(),
        "source": "audio_segments",
        "artefact": {"kind": WINDOW_KIND, "media_type": WINDOW_MEDIA_TYPE},
        "ocr": None,
        "label": "actuated" if actuations else "no_actuation",
        "window": {
            "requested_start": window.start.isoformat(),
            "requested_end": window.end.isoformat(),
            "requested_seconds": round(requested_seconds, 2),
            # What was actually recovered. A window spanning a recorder restart
            # is short, and a training set needs to know that rather than
            # assume every clip is the length that was asked for.
            "recovered_seconds": round(audio_seconds, 2),
            "complete": abs(audio_seconds - requested_seconds) < 1.0,
        },
        "audio": {
            "container": "adts",
            "codec": "aac_lc",
            "sample_rate_hz": 16000,
            "channels": 1,
            "raw_copy": True,
            "decoded": False,
            "source": source_url,
        },
        "actuations": actuations or None,
        "events": [
            {key: event.get(key) for key in EVENT_COLUMNS}
            for event in window.events
        ],
    }


def extract_windows(*, database: Path, store: SegmentStore, corpus_directory: Path,
                    start: datetime, end: datetime,
                    before_seconds: float = DEFAULT_BEFORE_SECONDS,
                    after_seconds: float = DEFAULT_AFTER_SECONDS,
                    source_url: str = "", dry_run: bool = False) -> dict:
    """Cut every window in the period into the corpus. Returns what it did."""
    events = read_events(database, start - timedelta(seconds=after_seconds),
                         end + timedelta(seconds=before_seconds))
    windows = plan_windows(events, before_seconds=before_seconds, after_seconds=after_seconds)
    written = empty = 0
    written_bytes = 0
    for window in windows:
        audio = extract_window(store, window.start, window.end)
        if not audio:
            # The segments for this window are gone or were never recorded.
            # Counted and journalled, so a hole in the corpus is visible rather
            # than looking like a quiet day.
            empty += 1
            continue
        seconds = sum(length for _, _, length in iter_adts_frames(audio))
        sidecar = build_sidecar(window, captured_at=window.start, audio_seconds=seconds,
                                requested_seconds=window.seconds, source_url=source_url)
        if not dry_run:
            _write_pair(corpus_directory, window.start, audio, sidecar)
        written += 1
        written_bytes += len(audio)
    LOGGER.info(
        "gate_audio_windows stage=extracted events=%d windows=%d written=%d empty=%d bytes=%d",
        len(events), len(windows), written, empty, written_bytes,
    )
    return {
        "events": len(events),
        "windows": len(windows),
        "written": written,
        "empty": empty,
        "bytes": written_bytes,
    }


def _write_pair(directory: Path, captured_at: datetime, audio: bytes, sidecar: dict) -> Path:
    """Write payload and sidecar as a matching pair, privately and atomically.

    The same convention and permissions as the rest of the corpus: 0700
    directory, 0600 files, one stem shared by both halves. The payload lands
    before the sidecar, because the uploader offers only complete pairs and a
    sidecar without its audio would be the half it cannot ship.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = captured_at.strftime(STEM_FORMAT)
    payload_path = directory / f"{stem}.aac"
    sidecar_path = directory / f"{stem}.json"
    _write_private(payload_path, audio)
    _write_private(sidecar_path, json.dumps(sidecar, separators=(",", ":")).encode("utf-8"))
    return payload_path


def _write_private(path: Path, data: bytes) -> None:
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


def _bounded(seconds: float) -> float:
    value = float(seconds)
    if not MIN_WINDOW_SECONDS <= value <= MAX_WINDOW_SECONDS:
        raise ValueError(
            f"a window bound must be between {MIN_WINDOW_SECONDS} and {MAX_WINDOW_SECONDS} seconds"
        )
    return value


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _offset(value, origin: datetime) -> float | None:
    moment = _parse(value)
    if moment is None:
        return None
    return round((moment - origin).total_seconds(), 3)
