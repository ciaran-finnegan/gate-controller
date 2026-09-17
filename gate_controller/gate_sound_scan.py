"""Turn recorded audio into a record of what the gate actually did.

The controller fires the relay and then assumes the gate moved. Nothing
anywhere confirms it, and at this site most gate movements are not the
controller's doing at all: over thirty days, 296 passages produced 46 relay
firings. The other 250 were key fobs, keypads and hands, and every one of them
was invisible to every part of this system.

This reads the segments the recorder already keeps and writes down three things
the system could not previously know:

* **the gate moved**, and for how long -- duration is what would separate a
  completed travel from a stall against an obstruction;
* **the gate shut**, from the clang of the leaves meeting. A motor run that
  ends without one is the gate standing open, which is the only moment the
  property is not secured and the only positive confirmation of closure
  available;
* **somebody opened it who was not us**, which is the denominator every
  question about recognition has been missing. Recognition rate is relay
  openings over *all* openings, and until now there was no all.

It runs after the fact, over finished segments, on a timer. Nothing here is in
the path of opening a gate, and nothing here can delay one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import subprocess

from .audio_segments import SegmentStore, segment_started_at
from .gate_audio_detect import (
    FRAME_SAMPLES, analyse_frames, find_clangs, movements_from, summarise,
)
from .sound_model import GateSoundModel, SAMPLE_RATE, motor_runs

LOGGER = logging.getLogger(__name__)

#: Decoding a whole segment at once costs 4.8 M float samples; this board has
#: other work. Sixty seconds at a time keeps the resident set small, and the
#: boundary costs the one 0.96 s patch that straddles it.
CHUNK_SECONDS = 60
DETECTOR = "yamnet-linear-v1"


@dataclass(frozen=True)
class ScanResult:
    segments: int
    frames: int
    movements: int
    skipped: int
    unavailable: str | None = None

    def as_dict(self) -> dict:
        return {
            "segments": self.segments, "frames": self.frames,
            "movements": self.movements, "skipped": self.skipped,
            "unavailable": self.unavailable,
        }


def decode(path, *, run=subprocess.run):
    """A segment as signed 16-bit mono at 16 kHz, or None if ffmpeg refuses.

    The segments are AAC the camera encoded; nothing else in the controller
    decodes them, which is the point -- the recorder remuxes without ever
    running a decoder, and the cost of one is paid here, later, off the path.

    Returned as *integers*, deliberately. ``analyse_frames`` takes 16-bit PCM
    and scales it itself; handing it samples already scaled to +/-1 divides by
    32768 a second time, which put every frame 90 dB down and meant no clang
    ever cleared the -38 dBFS floor. With no clangs the gate never reads as
    shut, so every movement after the first is taken for a closing run and no
    fob opening is ever flagged. YAMNet wants the scaled form, and gets it at
    the one place it is used.
    """
    import numpy as np

    completed = run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout:
        return None
    return np.frombuffer(completed.stdout, dtype=np.int16)


#: How far either side of a motor run's end to look for the clang. The four
#: measured closing clangs land 1.1-2.5 s before the motor stops, and the
#: attribution window in the state machine is wider than that; four seconds
#: covers both with room for a late one.
CLANG_SEARCH_SECONDS = 4.0


def scan_segment(model: GateSoundModel, path, started_at: datetime):
    """Every motor run and clang in one segment, in wall-clock time.

    The two detectors cost wildly different amounts. YAMNet is a MobileNet and
    runs at a fraction of a core. ``analyse_frames`` is a pure-Python FFT over
    a 1024-sample window hopped by half, which is 9,375 windows for a
    five-minute segment and took six and a half minutes for twenty minutes of
    audio -- slower than the timer that drives it, on a board that is also
    reading number plates.

    So the band analysis is not run over the segment at all. A clang is only
    ever *interpreted* next to the end of a motor run -- that is the whole of
    what the state machine does with it -- so only those few seconds are
    analysed, and the rest of the night costs nothing.
    """
    import numpy as np

    samples = decode(path)
    if samples is None or len(samples) < SAMPLE_RATE:
        return [], [], 0

    runs, frames = [], 0
    step = CHUNK_SECONDS * SAMPLE_RATE
    for offset in range(0, len(samples), step):
        chunk = samples[offset:offset + step]
        if len(chunk) < SAMPLE_RATE:
            break
        at = started_at + _seconds(offset / SAMPLE_RATE)
        scaled = np.ascontiguousarray(chunk, dtype=np.float32) / 32768.0
        window = model.classify(scaled, at)
        frames += len(window.probabilities)
        runs.extend(motor_runs(window))

    clangs = []
    for run in runs:
        ends_at = (run.end - started_at).total_seconds()
        first = max(0, int((ends_at - CLANG_SEARCH_SECONDS) * SAMPLE_RATE))
        last = min(len(samples), int((ends_at + CLANG_SEARCH_SECONDS) * SAMPLE_RATE))
        if last - first < FRAME_SAMPLES:
            continue
        clangs.extend(find_clangs(analyse_frames(
            samples[first:last], SAMPLE_RATE, started_at + _seconds(first / SAMPLE_RATE),
        )))
    return runs, clangs, frames


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)


def scan(store: SegmentStore, model: GateSoundModel, *, already_scanned=(),
         commanded_at=(), initial_state: str = "shut", limit: int = 24):
    """Scan the segments not yet looked at, oldest first.

    Returns the counts, the movements, and which segments were read -- the
    last so the caller can mark them without working it out again and risking
    a different answer.

    Oldest first because the state machine alternates: a movement only means
    "shut" if the gate was open, and reading last night before this morning
    would get every outcome backwards.

    The newest segment is skipped: ffmpeg still has it open, and half a motor
    run is a stall that never happened.
    """
    if not model.available:
        return ScanResult(0, 0, 0, 0, unavailable=model.unavailable_reason), [], []

    seen = set(already_scanned)
    segments = store.segments()[:-1]
    pending = [segment for segment in segments if segment.path.name not in seen][:limit]

    runs, clangs, frames, scanned = [], [], 0, []
    for segment in pending:
        started_at = segment_started_at(segment.path)
        if started_at is None:
            continue
        found, heard, counted = scan_segment(model, segment.path, started_at)
        runs.extend(found)
        clangs.extend(heard)
        frames += counted
        scanned.append((segment.path.name, counted))

    moves = movements_from(
        runs, clangs, commanded_at=commanded_at, initial_state=initial_state,
    )
    LOGGER.info(
        "gate_sound stage=scanned segments=%d frames=%d movements=%d uncommanded=%d final_state=%s",
        len(scanned), frames, len(moves),
        sum(1 for move in moves if move.uncommanded), summarise(moves).get("final_state"),
    )
    return ScanResult(len(scanned), frames, len(moves), len(segments) - len(pending)), moves, scanned


def record(connection, moves, scanned, *, now=None) -> int:
    """Write movements and mark the segments, in one transaction.

    A movement is keyed on when it started, so re-scanning a segment after a
    restart updates the row rather than reporting the gate opening twice.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    written = 0
    with connection:
        for move in moves:
            connection.execute(
                "INSERT INTO gate_movements (started_at, ended_at, seconds, outcome,"
                " uncommanded, clang_at, clang_peak_dbfs, detector, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(started_at) DO UPDATE SET"
                " ended_at=excluded.ended_at, seconds=excluded.seconds,"
                " outcome=excluded.outcome, uncommanded=excluded.uncommanded,"
                " clang_at=excluded.clang_at, clang_peak_dbfs=excluded.clang_peak_dbfs,"
                " detector=excluded.detector",
                (
                    move.start.isoformat(), move.end.isoformat(), round(move.seconds, 2),
                    move.outcome, 1 if move.uncommanded else 0,
                    None if move.clang is None else move.clang.at.isoformat(),
                    None if move.clang is None else round(move.clang.peak_dbfs, 1),
                    DETECTOR, stamp,
                ),
            )
            written += 1
        for name, frames in scanned:
            connection.execute(
                "INSERT INTO gate_sound_scans (segment, scanned_at, frames, movements)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(segment) DO UPDATE SET"
                " scanned_at=excluded.scanned_at, frames=excluded.frames",
                (name, stamp, frames, len(moves)),
            )
    return written


def already_scanned(connection) -> set[str]:
    try:
        return {row[0] for row in connection.execute("SELECT segment FROM gate_sound_scans")}
    except Exception:
        # A database without the table is one the migration has not touched
        # yet; scanning everything again is correct and merely slow.
        return set()
