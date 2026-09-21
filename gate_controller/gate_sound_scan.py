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

from .audio_segments import (
    GapLedger, SegmentStore, listening_gaps, segment_coverage, segment_started_at,
)
from .gate_audio_detect import (
    CONFIRMED, FRAME_SAMPLES, UNCONFIRMED, analyse_frames, find_clangs,
    movements_from, summarise,
)
from .sound_model import GateSoundModel, SAMPLE_RATE, motor_runs

LOGGER = logging.getLogger(__name__)

#: Decoding a whole segment at once costs 4.8 M float samples; this board has
#: other work. Sixty seconds at a time keeps the resident set small, and the
#: boundary costs the one 0.96 s patch that straddles it.
CHUNK_SECONDS = 60
#: Stamped on every row, so a movement judged by the model trained on four
#: cycles is never silently compared with one judged by its replacement.
DETECTOR = "yamnet-linear-v2"


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


#: Created here rather than in the store's migration, for the reason the
#: scanner already opens the store first: the scanner can be a newer release
#: than the service beside it, and "no such table" is how that was found out.
LISTENING_SCHEMA = """
    CREATE TABLE IF NOT EXISTS gate_listening (
        segment TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        span_seconds REAL NOT NULL,
        audio_seconds REAL NOT NULL,
        missing_seconds REAL NOT NULL,
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS gate_listening_started_at
        ON gate_listening (started_at DESC);
    CREATE TABLE IF NOT EXISTS gate_listening_gaps (
        started_at TEXT NOT NULL,
        cause TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        missing_seconds REAL NOT NULL,
        detail TEXT,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (started_at, cause)
    );
"""


@dataclass(frozen=True)
class Listening:
    """How much of the scanned span was audio, and the gaps in it."""

    coverage: tuple
    gaps: tuple


def measure_listening(store: SegmentStore, scanned) -> Listening:
    """For the segments just scanned: what was heard, and what was not.

    A movement that is not in ``gate_movements`` has until now meant one
    thing, that the gate did not move. Over 44 measured hours the recorder
    held 95% of the wall clock, and one relay-commanded cycle lost the whole
    of its closing to the other 5%. Silence and deafness have to be told
    apart, and this is where they are: every scanned segment is measured
    against the span it covers, and what is missing is written down with the
    recorder's own reason where it left one.

    Reads each segment's frame headers once more. That is 2.4 MB a segment
    beside a decode and a MobileNet pass over the same file, off the path.
    """
    names = [name for name, _ in scanned]
    if not names:
        return Listening((), ())
    coverage = segment_coverage(store, names)
    if not coverage:
        return Listening((), ())
    since = min(row["started_at"] for row in coverage)
    recorded = GapLedger(store.directory).read(since=since)
    gaps = listening_gaps(coverage, recorded)
    for gap in gaps:
        LOGGER.warning(
            "gate_sound stage=not_listening from=%s to=%s missing_seconds=%.1f cause=%s",
            gap["started_at"].isoformat(), gap["ended_at"].isoformat(),
            gap["missing_seconds"], gap["cause"],
        )
    return Listening(tuple(coverage), tuple(gaps))


def record(connection, moves, scanned, *, now=None, listening: Listening | None = None) -> int:
    """Write movements and mark the segments, in one transaction.

    A movement is keyed on when it started, so re-scanning a segment after a
    restart updates the row rather than reporting the gate opening twice.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    written = 0
    if listening is not None:
        connection.executescript(LISTENING_SCHEMA)
    with connection:
        for row in (listening.coverage if listening is not None else ()):
            connection.execute(
                "INSERT INTO gate_listening (segment, started_at, span_seconds,"
                " audio_seconds, missing_seconds, recorded_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(segment) DO UPDATE SET span_seconds=excluded.span_seconds,"
                " audio_seconds=excluded.audio_seconds,"
                " missing_seconds=excluded.missing_seconds, recorded_at=excluded.recorded_at",
                (row["segment"], row["started_at"].isoformat(), round(row["span_seconds"], 2),
                 round(row["audio_seconds"], 2), round(row["missing_seconds"], 2), stamp),
            )
        for gap in (listening.gaps if listening is not None else ()):
            connection.execute(
                "INSERT INTO gate_listening_gaps (started_at, cause, ended_at,"
                " missing_seconds, detail, recorded_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(started_at, cause) DO UPDATE SET ended_at=excluded.ended_at,"
                " missing_seconds=excluded.missing_seconds, detail=excluded.detail",
                (gap["started_at"].isoformat(), gap["cause"], gap["ended_at"].isoformat(),
                 round(gap["missing_seconds"], 2), gap["detail"], stamp),
            )
        for move in moves:
            connection.execute(
                "INSERT INTO gate_movements (started_at, ended_at, seconds, outcome,"
                " uncommanded, clang_at, clang_peak_dbfs, detector, created_at,"
                " confirmation, latch_at, latch_peak_dbfs)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(started_at) DO UPDATE SET"
                " ended_at=excluded.ended_at, seconds=excluded.seconds,"
                " outcome=excluded.outcome, uncommanded=excluded.uncommanded,"
                " clang_at=excluded.clang_at, clang_peak_dbfs=excluded.clang_peak_dbfs,"
                " detector=excluded.detector, confirmation=excluded.confirmation,"
                " latch_at=excluded.latch_at, latch_peak_dbfs=excluded.latch_peak_dbfs",
                (
                    move.start.isoformat(), move.end.isoformat(), round(move.seconds, 2),
                    move.outcome, 1 if move.uncommanded else 0,
                    None if move.clang is None else move.clang.at.isoformat(),
                    None if move.clang is None else round(move.clang.peak_dbfs, 1),
                    DETECTOR, stamp, move.confirmation,
                    None if move.latch is None else move.latch.at.isoformat(),
                    None if move.latch is None else round(move.latch.peak_dbfs, 1),
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


def gate_state(connection, *, now=None, recent_hours: int = 24) -> dict | None:
    """What the microphone says the gate is doing, for the heartbeat.

    ``state`` is what the last movement left behind, and ``open_for_seconds``
    is how long it has been that way. A gate standing open is the only moment
    the property is not secured, and until this it was a thing nobody could
    know from anywhere but the driveway.

    ``uncommanded`` over the window is the denominator recognition has never
    had: the rate is relay openings over *all* openings, and 84% of passages
    at this site never fire the relay.

    Returns None when nothing has been scanned, which the dashboard must show
    as "unknown" rather than as a shut gate.
    """
    moment = now or datetime.now(timezone.utc)
    try:
        last = connection.execute(
            "SELECT started_at, ended_at, outcome, confirmation FROM gate_movements"
            " ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        since = (moment - _seconds(recent_hours * 3600)).isoformat()
        counts = dict(connection.execute(
            "SELECT outcome, COUNT(*) FROM gate_movements WHERE started_at >= ?"
            " GROUP BY outcome", (since,),
        ))
        uncommanded = connection.execute(
            "SELECT COUNT(*) FROM gate_movements WHERE started_at >= ? AND uncommanded = 1",
            (since,),
        ).fetchone()[0]
        confirmed = connection.execute(
            "SELECT COUNT(*) FROM gate_movements WHERE started_at >= ?"
            " AND confirmation = ?", (since, CONFIRMED),
        ).fetchone()[0]
        unconfirmed = connection.execute(
            "SELECT COUNT(*) FROM gate_movements WHERE started_at >= ?"
            " AND confirmation = ?", (since, UNCONFIRMED),
        ).fetchone()[0]
        latches = connection.execute(
            "SELECT COUNT(*) FROM gate_movements WHERE started_at >= ?"
            " AND latch_at IS NOT NULL", (since,),
        ).fetchone()[0]
        scanned = connection.execute(
            "SELECT COUNT(*), MAX(scanned_at) FROM gate_sound_scans"
        ).fetchone()
    except Exception:
        # An older database, or one mid-migration. The heartbeat says nothing
        # rather than reporting a gate state it cannot support.
        return None
    if not last:
        return None

    state = "shut" if last[2] == "shut" else "open"
    try:
        ended = datetime.fromisoformat(last[1])
    except (TypeError, ValueError):
        ended = moment
    report = {
        "state": state,
        "state_confirmation": last[3] or UNCONFIRMED,
        "since": last[1],
        "open_for_seconds": round((moment - ended).total_seconds()) if state == "open" else 0,
        "movements_24h": sum(counts.values()),
        # The three numbers a dashboard should be reading instead of
        # `movements_24h`. Over 49.2 hours of recording the previous model put
        # 25-59 movements a day into that total that a person looking at the
        # spectrogram identified as wind, rain, a car on the road or a farm
        # machine. `movements_24h` is kept because removing it would break
        # anything already reading it, but it is a ceiling, not a count.
        "confirmed_24h": confirmed,
        "unconfirmed_24h": unconfirmed,
        "latches_heard_24h": latches,
        "closed_24h": counts.get("shut", 0),
        "uncommanded_24h": uncommanded,
        "segments_scanned": scanned[0] if scanned else 0,
        "last_scan_at": scanned[1] if scanned else None,
        "detector": DETECTOR,
    }
    # Additive, and absent until the scanner has measured something.
    report.update(listening_state(connection, now=moment, recent_hours=recent_hours))
    return report


def listening_state(connection, *, now=None, recent_hours: int = 24) -> dict:
    """How much of the window the microphone was actually heard, for the heartbeat.

    Every other number in the gate block is a count of things heard, and none
    of them can be read without this one: "no closing heard" from a recorder
    that was listening is a gate standing open, and from one that was not it is
    nothing at all.

    ``listening_24h_seconds`` is audio on the card, measured from its frames,
    not time a process was up. ``not_listening_24h_seconds`` is the span those
    segments cover less that audio, so the two add up to what was scanned
    rather than to 86400 -- a scanner two hours old must not read as 92% deaf.

    Empty when nothing has been measured, which a reader must show as unknown.
    """
    moment = now or datetime.now(timezone.utc)
    since = (moment - _seconds(recent_hours * 3600)).isoformat()
    try:
        heard = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(audio_seconds), 0), COALESCE(SUM(missing_seconds), 0)"
            " FROM gate_listening WHERE started_at >= ?", (since,),
        ).fetchone()
        gaps = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(missing_seconds), 0) FROM gate_listening_gaps"
            " WHERE started_at >= ?", (since,),
        ).fetchone()
        latest = connection.execute(
            "SELECT started_at, ended_at, cause FROM gate_listening_gaps"
            " WHERE started_at >= ? ORDER BY started_at DESC LIMIT 1", (since,),
        ).fetchone()
    except Exception:
        # A database the newer scanner has not written to yet.
        return {}
    if not heard or not heard[0]:
        return {}
    return {
        "listening_24h_seconds": round(heard[1]),
        "not_listening_24h_seconds": round(heard[2]),
        "listening_gaps_24h": gaps[0],
        "longest_gap_24h_seconds": round(gaps[1]),
        "last_gap_at": latest[0] if latest else None,
        "last_gap_until": latest[1] if latest else None,
        "last_gap_cause": latest[2] if latest else None,
    }
