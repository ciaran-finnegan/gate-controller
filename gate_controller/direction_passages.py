"""Gather what is known about each passage, and have ``direction_signals`` judge it.

A verdict belongs to a *passage* -- one vehicle going through -- and the
controller's unit of work is an *event*, of which one vehicle usually produces
several. Nothing the controller knows while a car is at the gate is enough to
say which way it was going: the photo's verdict is read afterwards, by
``scripts/scan_direction.py``, and the gate's own movements are written by the
gate-sound scan up to twenty minutes later. So passages are assembled and
judged here, after the fact, from the rows those two leave behind, and judged
*again* on every pass until the evidence stops arriving.

This module reads ``events``, ``event_telemetry``, ``event_directions``,
``gate_movements`` and ``gate_sound_scans``; it writes only the two tables it
creates. It is called from the direction scan and from nowhere near the relay.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

from .direction_signals import (
    HELD_OPEN_SECONDS, METHOD, SAME_ARRIVAL_SECONDS, SAME_PASSAGE_SECONDS, DirectionVerdict,
    Movement, PassageEvidence, combine, judge, unseen_departures,
)

#: Events closer together than this are one vehicle. Not guessed: scored against
#: the camera's own alarm identity over the week to 2026-09-21, 10 s reproduces
#: it exactly -- no alarm split across two passages and no passage holding two
#: alarms -- and from 20 s upward passages begin to swallow a second alarm.
PASSAGE_GAP_SECONDS = 10.0
#: How far outside the judged window to read, so that a passage at its edge is
#: grouped, and sees the gate, exactly as it would in the middle.
EDGE_SECONDS = 600.0
#: The gate-sound scan writes one row per segment it has read; YAMNet answers
#: once per this many seconds, which turns its frame count into a duration.
SCAN_FRAME_SECONDS = 0.48
#: A five-minute segment holds 620 frames, which is 297.6 s: the last patch is
#: not a whole one. Without this a passage that falls in the seam between two
#: segments reads as "the gate was not being listened to".
SCAN_SLACK_SECONDS = 5.0
_SEGMENT = re.compile(r"(\d{8}T\d{6})Z")

KIND_VEHICLE = "vehicle"
KIND_UNSEEN = "gate_only"
REMOTE_COMMAND = "remote_command"
#: ``direction-clip-v1``'s label for "a yellow tractor or farm machine".
MACHINE_LABEL = "machine"

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS passage_directions (
        passage_key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        first_event_id INTEGER,
        last_event_id INTEGER,
        events INTEGER NOT NULL DEFAULT 0,
        verdict TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0,
        conflict INTEGER NOT NULL DEFAULT 0,
        contributing TEXT NOT NULL DEFAULT '',
        signals TEXT NOT NULL,
        method TEXT NOT NULL,
        judged_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS passage_directions_first_seen ON passage_directions (first_seen_at)",
    """
    CREATE TABLE IF NOT EXISTS event_direction_shipments (
        event_id INTEGER PRIMARY KEY,
        verdict TEXT NOT NULL,
        score REAL NOT NULL DEFAULT 0,
        method TEXT NOT NULL,
        shipped_at TEXT NOT NULL
    )
    """,
)


def ensure_schema(connection) -> None:
    with connection:
        for statement in SCHEMA:
            connection.execute(statement)


def _moment(text) -> float | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


@dataclass(frozen=True)
class PassageEvent:
    event_id: int
    idempotency_key: str | None
    at: float
    source: str


@dataclass(frozen=True)
class Passage:
    """One vehicle going through, or one gate cycle nobody was seen at."""

    key: str
    kind: str
    started_at: float
    ended_at: float
    events: tuple
    verdict: DirectionVerdict


def _group(rows):
    passages, current, previous = [], [], None
    for row in rows:
        if previous is not None and row["at"] - previous > PASSAGE_GAP_SECONDS:
            passages.append(current)
            current = []
        current.append(row)
        previous = row["at"]
    if current:
        passages.append(current)
    return passages


def _box_width(payloads):
    """The live estimator's best answer across the passage's events."""
    best = None
    for payload in payloads:
        try:
            block = json.loads(payload).get("direction") if payload else None
        except (TypeError, ValueError, AttributeError):
            block = None
        if not isinstance(block, dict) or block.get("verdict") not in ("entering", "exiting"):
            continue
        if best is None or (block.get("score") or 0) > (best.get("score") or 0):
            best = block
    return best


def _heard(scans, moment: float) -> bool:
    return any(start <= moment <= end for start, end in scans)


def judge_passages(connection, since: str) -> list:
    """Every passage that began at or after ``since``, judged on what is there now.

    Vehicle passages first, then the gate cycles no vehicle accounts for.
    """
    since_at = _moment(since) or 0.0
    floor = _iso(since_at - EDGE_SECONDS)
    rows = [
        {"id": r[0], "key": r[1], "at": _moment(r[2]), "source": r[3], "relay_at": _moment(r[4]),
         "payload": r[5], "vision": r[6], "score": r[7], "top": r[8]}
        for r in connection.execute(
            "SELECT e.id, e.idempotency_key, e.received_at, e.source, e.relay_activated_at,"
            " t.payload, d.verdict, d.score, d.top FROM events e"
            " LEFT JOIN event_telemetry t ON t.event_id = e.id"
            " LEFT JOIN event_directions d ON d.event_id = e.id"
            " WHERE e.received_at >= ? ORDER BY e.received_at, e.id", (floor,))
    ]
    rows = [row for row in rows if row["at"] is not None]
    movements = tuple(
        Movement(started, ended, clang=bool(clang))
        for started, ended, clang in (
            (_moment(r[0]), _moment(r[1]), r[2])
            for r in connection.execute(
                "SELECT started_at, ended_at, clang_at FROM gate_movements WHERE started_at >= ?"
                " ORDER BY started_at", (floor,))
        ) if started is not None and ended is not None
    )
    scans = []
    for name, frames in connection.execute("SELECT segment, frames FROM gate_sound_scans"):
        found = _SEGMENT.search(name or "")
        if not found:
            continue
        start = datetime.strptime(found.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        if start >= since_at - EDGE_SECONDS - 3600:
            scans.append((start, start + (frames or 0) * SCAN_FRAME_SECONDS + SCAN_SLACK_SECONDS))
    commands = tuple(row["relay_at"] for row in rows if row["relay_at"] is not None)

    grouped = _group(rows)
    seen = []
    for group in grouped:
        camera = [row["at"] for row in group if row["source"] != REMOTE_COMMAND]
        seen.append((min(camera), max(camera)) if camera else None)

    evidence_of = {}
    for index, group in enumerate(grouped):
        if group[-1]["at"] < since_at - SAME_ARRIVAL_SECONDS:
            continue
        span = seen[index]
        first = span[0] if span else None
        evidence_of[index] = PassageEvidence(
            first_seen_at=first,
            last_seen_at=span[1] if span else None,
            frames=tuple((row["vision"], row["score"]) for row in group if row["vision"]),
            box_width=_box_width(row["payload"] for row in group),
            movements=tuple(
                m for m in movements
                if first is not None and abs(m.started_at - first) <= SAME_PASSAGE_SECONDS * 2.5),
            commands_at=commands,
            others_seen=tuple(other for position, other in enumerate(seen)
                              if other is not None and position != index),
            machine=any(row["top"] == MACHINE_LABEL for row in group),
            gate_heard=first is not None and all(
                _heard(scans, first - offset) for offset in (HELD_OPEN_SECONDS, 10.0, 0.0)),
        )
    # Twice: once on each passage's own evidence, and once more with the
    # nearest passage's *own* verdict beside it. Never a third time -- a
    # verdict is lent by the passage that earned it, not passed down a queue.
    alone = {index: judge(evidence) for index, evidence in evidence_of.items()}
    # Whether the gate really opened for each passage, rather than merely
    # having been asked to: our relay fired on one of its own events. A
    # neighbour that got in is what makes the flank filling this frame the
    # same car rather than a second vehicle.
    opened = [any(row["relay_at"] is not None for row in group) for group in grouped]

    judged = []
    for index, group in enumerate(grouped):
        if group[0]["at"] < since_at or index not in evidence_of:
            continue
        neighbour, gap, neighbour_opened = None, None, False
        for other in (index - 1, index + 1):
            if other not in alone or seen[index] is None or seen[other] is None:
                continue
            distance = max(seen[index][0] - seen[other][1], seen[other][0] - seen[index][1])
            if alone[other].decisive and (gap is None or distance < gap):
                neighbour, gap, neighbour_opened = alone[other], distance, opened[other]
        judged.append(Passage(
            key=f"e{group[0]['id']}", kind=KIND_VEHICLE,
            started_at=group[0]["at"], ended_at=group[-1]["at"],
            events=tuple(PassageEvent(row["id"], row["key"], row["at"], row["source"]) for row in group),
            verdict=judge(evidence_of[index], neighbour=neighbour, neighbour_gap=gap,
                          neighbour_opened=neighbour_opened),
        ))

    every_seen = tuple(span for span in seen if span is not None)
    for movement, opinion in unseen_departures(
            [m for m in movements if m.started_at >= since_at], commands, every_seen):
        judged.append(Passage(
            key=f"m{_iso(movement.started_at)}", kind=KIND_UNSEEN,
            started_at=movement.started_at, ended_at=movement.ended_at,
            events=(), verdict=combine(opinion),
        ))
    return judged


def record(connection, passages, since: str, now: str) -> None:
    """Replace what was believed about this window with what is believed now.

    Replaced rather than updated: a late event can join two passages into one,
    and a row for a passage that no longer exists would be a verdict about
    nothing.
    """
    since_at = _moment(since) or 0.0
    with connection:
        connection.execute("DELETE FROM passage_directions WHERE first_seen_at >= ?", (_iso(since_at),))
        for passage in passages:
            workings = passage.verdict.as_dict()
            connection.execute(
                "INSERT OR REPLACE INTO passage_directions (passage_key, kind, first_seen_at, last_seen_at,"
                " first_event_id, last_event_id, events, verdict, confidence, conflict, contributing,"
                " signals, method, judged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (passage.key, passage.kind, _iso(passage.started_at), _iso(passage.ended_at),
                 passage.events[0].event_id if passage.events else None,
                 passage.events[-1].event_id if passage.events else None,
                 len(passage.events), workings["verdict"], workings["confidence"],
                 1 if workings["conflict"] else 0, ",".join(workings["contributing"]),
                 json.dumps(workings["signals"], separators=(",", ":")), METHOD, now),
            )


def window_start(now: datetime, days: float) -> str:
    return (now - timedelta(days=days)).isoformat()
