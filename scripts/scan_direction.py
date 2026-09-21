#!/usr/bin/env python3
"""Judge which way each vehicle was going, after the passage, from everything known.

Runs on a timer, off the path that opens the gate. Two steps:

1. For each recent event not yet read it fetches the photo back from the
   dashboard -- the controller deletes its own copy once that has it -- and
   reads front or rear with the vision model.
2. It groups events into passages and has ``direction_signals`` judge each one
   from the photo readings, the gate's own movements and the live width fit
   together. That combined verdict is what is kept per passage and what every
   event of the passage is labelled with on the dashboard.

Step 2 is here, and not beside the relay, because this is the first moment the
evidence exists: a photo is read after the fact, and the gate-sound scan writes
a movement up to twenty minutes after it happened. A passage is therefore
judged again on every pass, and the dashboard is told only when the answer
changes. Nothing here can open, delay or refuse a gate.

    sudo -u gate-controller python3 scripts/scan_direction.py            # recent
    sudo -u gate-controller python3 scripts/scan_direction.py --days 30  # backfill
    sudo -u gate-controller python3 scripts/scan_direction.py --no-ship  # judge, send nothing
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gate_controller import direction_passages  # noqa: E402
from gate_controller.direction_signals import METHOD as COMBINED_METHOD  # noqa: E402
from gate_controller.direction_vision import VisionDirection, UNKNOWN  # noqa: E402
from gate_controller.store import LocalStore  # noqa: E402

LOGGER = logging.getLogger("gate_direction_vision")
DEFAULT_DATABASE = Path("/var/lib/gate-controller/gate-controller.db")
METHOD = "vision-clip-v1"
#: Cloudflare refuses Python's default agent with 403, error 1010.
USER_AGENT = "gate-controller-direction/1"
SHIP_BATCH = 200
TIMEOUT_SECONDS = 60
#: A confidence that moved by less than this is not news.
SCORE_CHANGE = 0.05
#: A passage this fresh may still be arriving -- more frames to come, and its
#: events perhaps not yet on the dashboard, where an update for a row that does
#: not exist yet changes nothing and is not reported. It waits for the next pass.
SETTLE_SECONDS = 120.0


class Dashboard:
    """The controller's own service token, from its environment. Never logged."""

    def __init__(self, environment=None):
        environment = os.environ if environment is None else environment
        self.base = (environment.get("GATE_CLOUDFLARE_API_URL") or "").strip().rstrip("/")
        self.controller_id = (environment.get("GATE_CONTROLLER_ID") or "primary").strip()
        self.headers = {
            "CF-Access-Client-Id": (environment.get("GATE_CLOUDFLARE_ACCESS_CLIENT_ID") or "").strip(),
            "CF-Access-Client-Secret": (environment.get("GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET") or "").strip(),
            "User-Agent": USER_AGENT,
        }

    @property
    def configured(self) -> bool:
        return bool(self.base and all(self.headers.values()))

    def image(self, event_id: int, idempotency_key: str | None) -> bytes | None:
        query = {"event_id": str(event_id), "controller_id": self.controller_id}
        if idempotency_key:
            query["idempotency_key"] = idempotency_key
        try:
            with request.urlopen(request.Request(
                f"{self.base}/api/controller/events/image?{parse.urlencode(query)}", headers=self.headers,
            ), timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except error.HTTPError as failure:
            if failure.code == 404:
                return None
            raise

    def send(self, directions: list[dict]) -> dict:
        """Post verdicts; returns the dashboard's own account of what it did."""
        body = json.dumps({"controller_id": self.controller_id, "directions": directions}).encode()
        with request.urlopen(request.Request(
            f"{self.base}/api/controller/directions", data=body, method="POST",
            headers={**self.headers, "Content-Type": "application/json"},
        ), timeout=TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read())
        return answer if isinstance(answer, dict) else {}


def pending(connection, since: str, limit: int):
    return connection.execute(
        "SELECT e.id, e.idempotency_key FROM events e"
        " LEFT JOIN event_directions d ON d.event_id = e.id"
        " WHERE d.event_id IS NULL AND e.received_at >= ?"
        " ORDER BY e.received_at LIMIT ?", (since, limit),
    ).fetchall()


def record(connection, event_id: int, *, status: str, reading=None, now: str) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO event_directions"
            " (event_id, status, verdict, score, front, rear, top, method, classified_at, shipped_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (event_id, status,
             reading.direction if reading else UNKNOWN,
             round(reading.score, 3) if reading else 0.0,
             None if reading is None else round(reading.front, 3),
             None if reading is None else round(reading.rear, 3),
             None if reading is None else reading.top,
             METHOD, now),
        )


def _told(connection, first_event_id: int) -> dict:
    """What the dashboard currently believes about each event, as far as we know.

    In order of authority: what this pass last sent; failing that, a photo
    verdict an earlier release sent on its own; failing that, the live width
    fit that travelled inside the event itself.
    """
    told = {}
    for event_id, payload in connection.execute(
            "SELECT event_id, payload FROM event_telemetry WHERE event_id >= ?"
            " AND payload LIKE '%\"direction\"%'", (first_event_id,)):
        try:
            block = json.loads(payload).get("direction") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        if block.get("verdict") in ("entering", "exiting"):
            told[event_id] = (block["verdict"], float(block.get("score") or 0.0))
    for event_id, verdict, score in connection.execute(
            "SELECT event_id, verdict, score FROM event_directions WHERE event_id >= ?"
            " AND shipped_at IS NOT NULL AND verdict IN ('entering', 'exiting')", (first_event_id,)):
        told[event_id] = (verdict, float(score or 0.0))
    for event_id, verdict, score in connection.execute(
            "SELECT event_id, verdict, score FROM event_direction_shipments WHERE event_id >= ?",
            (first_event_id,)):
        told[event_id] = (verdict, float(score or 0.0))
    return told


def outstanding(connection, passages, now: float | None = None):
    """What the dashboard has not been told: ``(verdicts, retractions)``.

    Every event of a passage carries the passage's verdict -- including the
    frames that showed nothing by themselves, which is most of them. A verdict
    is sent when it is new or has changed. A passage that has *become* unknown,
    because a later signal contradicted an earlier one, is a retraction.
    """
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    passages = [p for p in passages if p.events and now - p.ended_at >= SETTLE_SECONDS]
    if not passages:
        return [], []
    told = _told(connection, min(event.event_id for p in passages for event in p.events))
    verdicts, retractions = [], []
    for passage in passages:
        answer = passage.verdict
        for event in passage.events:
            before = told.get(event.event_id)
            entry = {"event_id": event.event_id, "idempotency_key": event.idempotency_key,
                     "method": COMBINED_METHOD}
            if answer.decisive:
                score = round(answer.confidence, 3)
                if before and before[0] == answer.verdict and abs(before[1] - score) < SCORE_CHANGE:
                    continue
                verdicts.append({**entry, "direction": answer.verdict, "score": score,
                                 "signals": list(answer.contributing)})
            elif before and before[0] in ("entering", "exiting"):
                retractions.append({**entry, "direction": UNKNOWN, "score": 0.0, "retract": True})
    return verdicts, retractions


def _mark(connection, entries, now: str) -> None:
    with connection:
        connection.executemany(
            "INSERT OR REPLACE INTO event_direction_shipments (event_id, verdict, score, method, shipped_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(e["event_id"], e["direction"], e["score"], e["method"], now) for e in entries])
        # The photo reading rode along inside the combined verdict; it is not
        # still waiting to be sent.
        connection.executemany(
            "UPDATE event_directions SET shipped_at = ? WHERE event_id = ? AND shipped_at IS NULL",
            [(now, e["event_id"]) for e in entries])


def ship(connection, dashboard, passages) -> dict:
    """Tell the dashboard what changed. Returns counts for the journal.

    Retractions go separately, because the dashboard deployed today skips an
    ``unknown`` outright -- it would neither apply one nor say so. One that
    understands ``retract`` answers with a ``retracted`` count; until it does,
    a retraction is left unsent and tried again while its passage is still
    inside the window, which bounds it to a few small requests.
    """
    verdicts, retractions = outstanding(connection, passages)
    counts = {"verdicts": 0, "retracted": 0, "retractions_waiting": 0}
    for start in range(0, len(verdicts), SHIP_BATCH):
        batch = verdicts[start:start + SHIP_BATCH]
        try:
            dashboard.send(batch)
        except Exception as failure:
            LOGGER.warning("gate_direction stage=ship_failed detail=%s", type(failure).__name__)
            return counts
        _mark(connection, batch, datetime.now(timezone.utc).isoformat())
        counts["verdicts"] += len(batch)
    for start in range(0, len(retractions), SHIP_BATCH):
        batch = retractions[start:start + SHIP_BATCH]
        try:
            answer = dashboard.send(batch)
        except Exception as failure:
            LOGGER.warning("gate_direction stage=ship_failed detail=%s", type(failure).__name__)
            break
        if isinstance(answer.get("retracted"), int):
            _mark(connection, batch, datetime.now(timezone.utc).isoformat())
            counts["retracted"] += len(batch)
        else:
            counts["retractions_waiting"] += len(batch)
    return counts


def read_photos(connection, model, dashboard, since: str, limit: int) -> dict:
    """Read front or rear off every recent event that has not been read yet."""
    counts = {"read": 0, "no_image": 0, "unreadable": 0, "failed": 0}
    for event_id, key in pending(connection, since, limit):
        now = datetime.now(timezone.utc).isoformat()
        try:
            jpeg = dashboard.image(event_id, key)
        except Exception as failure:
            # Left unjudged, so the next pass tries again.
            counts["failed"] += 1
            LOGGER.warning("gate_direction_vision stage=fetch_failed event_id=%s detail=%s",
                           event_id, type(failure).__name__)
            continue
        if jpeg is None:
            record(connection, event_id, status="no_image", now=now)
            counts["no_image"] += 1
            continue
        reading = model.read(jpeg)
        record(connection, event_id, status="read" if reading else "unreadable", reading=reading, now=now)
        counts["read" if reading else "unreadable"] += 1
        if reading:
            LOGGER.info("gate_direction_vision event_id=%s verdict=%s score=%.2f front=%.2f rear=%.2f top=%s",
                        event_id, reading.direction, reading.score, reading.front, reading.rear, reading.top)
    LOGGER.info("gate_direction_vision stage=judged %s", counts)
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--no-ship", action="store_true", help="judge and keep, send nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Neither of these stops the pass. Without the model or the dashboard no
    # photo can be read, but the gate's own movements still say something, and
    # what they say is still worth keeping.
    model = VisionDirection()
    if not model.available:
        LOGGER.warning("gate_direction_vision stage=unavailable detail=%s", model.unavailable_reason)
    dashboard = Dashboard()
    if not dashboard.configured:
        LOGGER.warning("gate_direction_vision stage=unavailable detail=no dashboard credentials")

    LocalStore(Path(args.database))  # creates event_directions on an older release
    connection = sqlite3.connect(args.database)
    try:
        direction_passages.ensure_schema(connection)
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        if model.available and dashboard.configured:
            read_photos(connection, model, dashboard, since, args.limit)

        # Judged again on every pass: the gate-sound scan writes a movement up
        # to twenty minutes after it happened, and that can change the answer.
        passages = direction_passages.judge_passages(connection, since)
        direction_passages.record(connection, passages, since, datetime.now(timezone.utc).isoformat())
        resolved = sum(1 for passage in passages if passage.verdict.decisive)
        LOGGER.info(
            "gate_direction stage=combined passages=%d resolved=%d conflicts=%d gate_only=%d",
            len(passages), resolved, sum(1 for passage in passages if passage.verdict.conflict),
            sum(1 for passage in passages if passage.kind == direction_passages.KIND_UNSEEN))

        if args.no_ship or not dashboard.configured:
            return 0
        LOGGER.info("gate_direction stage=shipped %s", ship(connection, dashboard, passages))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
