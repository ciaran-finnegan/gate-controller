#!/usr/bin/env python3
"""Judge which way each vehicle was going, from its photo, after the passage.

Runs on a timer, off the path that opens the gate. For each recent event not
yet judged it fetches the photo back from the dashboard -- the controller
deletes its own copy once that has it -- reads front or rear with the vision
model, keeps the verdict locally, and posts the decisive ones back.

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

from gate_controller.direction_vision import VisionDirection, UNKNOWN  # noqa: E402
from gate_controller.store import LocalStore  # noqa: E402

LOGGER = logging.getLogger("gate_direction_vision")
DEFAULT_DATABASE = Path("/var/lib/gate-controller/gate-controller.db")
METHOD = "vision-clip-v1"
#: Cloudflare refuses Python's default agent with 403, error 1010.
USER_AGENT = "gate-controller-direction/1"
SHIP_BATCH = 200
TIMEOUT_SECONDS = 60


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

    def send(self, directions: list[dict]) -> int:
        body = json.dumps({"controller_id": self.controller_id, "directions": directions}).encode()
        with request.urlopen(request.Request(
            f"{self.base}/api/controller/directions", data=body, method="POST",
            headers={**self.headers, "Content-Type": "application/json"},
        ), timeout=TIMEOUT_SECONDS) as response:
            return int(json.loads(response.read()).get("updated", 0))


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


def unshipped(connection):
    return connection.execute(
        "SELECT d.event_id, e.idempotency_key, d.verdict, d.score FROM event_directions d"
        " JOIN events e ON e.id = d.event_id"
        " WHERE d.shipped_at IS NULL AND d.verdict IN ('entering', 'exiting')"
        " ORDER BY d.event_id",
    ).fetchall()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--no-ship", action="store_true", help="judge and keep, send nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    model = VisionDirection()
    if not model.available:
        LOGGER.warning("gate_direction_vision stage=unavailable detail=%s", model.unavailable_reason)
        return 0
    dashboard = Dashboard()
    if not dashboard.configured:
        LOGGER.warning("gate_direction_vision stage=unavailable detail=no dashboard credentials")
        return 0

    LocalStore(Path(args.database))  # creates event_directions on an older release
    connection = sqlite3.connect(args.database)
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        counts = {"read": 0, "no_image": 0, "unreadable": 0, "failed": 0}
        for event_id, key in pending(connection, since, args.limit):
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

        if args.no_ship:
            return 0
        rows = unshipped(connection)
        sent = 0
        for start in range(0, len(rows), SHIP_BATCH):
            batch = rows[start:start + SHIP_BATCH]
            try:
                dashboard.send([{"event_id": event_id, "idempotency_key": key, "direction": verdict,
                                 "score": score, "method": METHOD} for event_id, key, verdict, score in batch])
            except Exception as failure:
                LOGGER.warning("gate_direction_vision stage=ship_failed detail=%s", type(failure).__name__)
                break
            stamp = datetime.now(timezone.utc).isoformat()
            with connection:
                connection.executemany("UPDATE event_directions SET shipped_at = ? WHERE event_id = ?",
                                       [(stamp, row[0]) for row in batch])
            sent += len(batch)
        LOGGER.info("gate_direction_vision stage=shipped verdicts=%d", sent)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
