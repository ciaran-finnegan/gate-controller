"""Cut yesterday's event windows out of the recorded segments, for the corpus.

Run on a timer. Reads the controller's ``events`` table read-only, cuts a
window around each passage out of the segments the recorder wrote, and leaves
the pairs in the corpus for the existing uploader to ship to R2.

Nothing here touches the recognition path: the database is opened read-only,
the output is a directory the uploader polls, and a failure leaves the
segments exactly where they were.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.audio_segments import SegmentStore
from gate_controller.audio_windows import (
    DEFAULT_AFTER_SECONDS, DEFAULT_BEFORE_SECONDS, extract_windows,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path,
                        default=Path("/var/lib/gate-controller/gate-controller.db"))
    parser.add_argument("--segments", type=Path,
                        default=Path("/var/lib/gate-controller/audio-segments"))
    parser.add_argument("--corpus", type=Path,
                        default=Path("/var/lib/gate-controller/training-corpus/audio"))
    parser.add_argument("--hours", type=float, default=24.0,
                        help="how far back to look (default: the last day)")
    parser.add_argument("--before-seconds", type=float, default=DEFAULT_BEFORE_SECONDS)
    parser.add_argument("--after-seconds", type=float, default=DEFAULT_AFTER_SECONDS)
    parser.add_argument("--source-url", default="rtsp://127.0.0.1:8554/clear")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=arguments.hours)
    store = SegmentStore(arguments.segments)
    report = extract_windows(
        database=arguments.database, store=store, corpus_directory=arguments.corpus,
        start=start, end=end, before_seconds=arguments.before_seconds,
        after_seconds=arguments.after_seconds, source_url=arguments.source_url,
        dry_run=arguments.dry_run,
    )
    print(f"events={report['events']} windows={report['windows']} "
          f"written={report['written']} empty={report['empty']} bytes={report['bytes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
