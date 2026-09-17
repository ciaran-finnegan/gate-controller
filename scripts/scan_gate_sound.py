#!/usr/bin/env python3
"""Read what the gate did from the audio already on the card.

Runs on a timer, after the fact, over finished segments. Nothing here is in the
path of opening a gate and nothing here can delay one: it decodes recordings,
scores them, and writes rows.

    sudo python3 scripts/scan_gate_sound.py            # what has not been read
    sudo python3 scripts/scan_gate_sound.py --dry-run  # say, write nothing
    sudo python3 scripts/scan_gate_sound.py --all      # re-read everything
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gate_controller.audio_segments import SegmentStore, load_segment_config  # noqa: E402
from gate_controller.gate_sound_scan import (  # noqa: E402
    already_scanned, record, scan,
)
from gate_controller.sound_model import GateSoundModel  # noqa: E402
from gate_controller.store import LocalStore  # noqa: E402

LOGGER = logging.getLogger("gate_sound")

DEFAULT_DATABASE = Path("/var/lib/gate-controller/gate-controller.db")


def relay_firings(connection, since: datetime) -> list[datetime]:
    """When the controller itself commanded the gate, so a fob can be told apart.

    An opening with one of these just before it is ours. An opening without is
    a key fob, a keypad or a hand -- 84% of passages at this site, and the
    denominator every question about recognition has been missing.
    """
    moments = []
    for (stamp,) in connection.execute(
        "SELECT relay_activated_at FROM events WHERE relay_activated_at IS NOT NULL"
        " AND relay_activated_at >= ?", (since.isoformat(),),
    ):
        try:
            moments.append(datetime.fromisoformat(stamp))
        except (TypeError, ValueError):
            continue
    return moments


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--limit", type=int, default=24,
                        help="segments per run; 24 is two hours at five minutes each")
    parser.add_argument("--all", action="store_true", help="re-read segments already scanned")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_segment_config(os.environ)
    if not config.get("directory"):
        LOGGER.error("gate_sound stage=refused reason=no_segment_directory")
        return 2
    store = SegmentStore(
        config["directory"],
        retention_hours=config["retention_hours"],
        min_free_bytes=config["min_free_bytes"],
    )

    model = GateSoundModel()
    if not model.available:
        # Not an error: a board without the model simply has nothing to say.
        LOGGER.warning("gate_sound stage=unavailable detail=%s", model.unavailable_reason)
        return 0

    # Through the store, so the tables this writes to exist even when the
    # controller running beside it is an older release that has never created
    # them. Opening the database directly failed with "no such table" on a
    # board where the scanner was newer than the service.
    LocalStore(Path(args.database))
    connection = sqlite3.connect(args.database)
    try:
        seen = set() if args.all else already_scanned(connection)
        since = datetime.now(timezone.utc) - timedelta(days=3)
        result, moves, scanned = scan(
            store, model,
            already_scanned=seen,
            commanded_at=relay_firings(connection, since),
            initial_state=last_state(connection),
            limit=args.limit,
        )
        for move in moves:
            LOGGER.info("gate_sound movement %s", move.as_dict())
        if args.dry_run:
            LOGGER.info("gate_sound stage=dry_run %s", result.as_dict())
            return 0
        written = record(connection, moves, scanned)
        LOGGER.info("gate_sound stage=recorded movements=%d %s", written, result.as_dict())
    finally:
        connection.close()
    return 0


def last_state(connection) -> str:
    """Where the gate was left, so the next scan's alternation starts right."""
    try:
        row = connection.execute(
            "SELECT outcome FROM gate_movements ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return "shut"
    return row[0] if row and row[0] in {"shut", "open"} else "shut"


if __name__ == "__main__":
    sys.exit(main())
