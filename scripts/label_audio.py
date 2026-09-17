"""Work the labelling queue: propose clips, hear them, say what they are.

Four verbs, and the only one that needs a person is `review`:

    propose   build a queue from relay actuations and detector output,
              cutting the audio for each candidate so it can be heard
    status    what the corpus holds
    record    write one verdict
    export    the labelled set to train on

Everything a person decides lands in an append-only JSON Lines file that
outlives the session and ships to R2 with the rest of the corpus. Nothing here
runs a model or owns a threshold; candidates arrive from outside with whatever
confidence their source claims, so replacing the detector next month does not
invalidate a corpus built with this one.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.audio_labels import (
    LABELS, LabelStore, Verdict, candidates_from_actuations,
    candidates_from_detections, training_rows, unreviewed, write_manifest,
)
from gate_controller.audio_segments import SegmentStore, extract_window

DEFAULT_SEGMENTS = Path("/var/lib/gate-controller/training-corpus/audio-segments")
DEFAULT_DATABASE = Path("/var/lib/gate-controller/gate-controller.db")
DEFAULT_LABELS = Path("/var/lib/gate-controller/audio-labels.jsonl")


def _utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def cmd_propose(arguments) -> int:
    store = LabelStore(arguments.labels)
    candidates = []

    if arguments.database.exists():
        since = (datetime.now(timezone.utc) - timedelta(hours=arguments.hours)).isoformat()
        connection = sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            rows = [dict(r) for r in connection.execute(
                "SELECT id, relay_activated_at, source FROM events"
                " WHERE relay_activated_at IS NOT NULL AND relay_activated_at >= ?", (since,))]
        finally:
            connection.close()
        candidates += candidates_from_actuations(rows)

    if arguments.detections and arguments.detections.exists():
        document = json.loads(arguments.detections.read_text(encoding="utf-8"))
        items = document.get("detections", document) if isinstance(document, dict) else document
        candidates += candidates_from_detections(
            [d for d in items
             if float(d.get("confidence") or 0) >= arguments.min_confidence
             and float(d.get("seconds") or 0) >= arguments.min_seconds])

    queue = unreviewed(candidates, store)[:arguments.limit]
    segments = SegmentStore(arguments.segments)
    arguments.out.mkdir(parents=True, exist_ok=True)

    cut = missing = 0
    for candidate in queue:
        target = arguments.out / f"{candidate.clip_id}.aac"
        if target.exists():
            cut += 1
            continue
        audio = extract_window(segments, candidate.at,
                              candidate.at + timedelta(seconds=candidate.seconds))
        if not audio:
            # The segments are gone or were never recorded. Counted, so a hole
            # in the queue is visible rather than looking like a quiet day.
            missing += 1
            continue
        target.write_bytes(audio)
        cut += 1

    document = write_manifest(arguments.out / "queue.json", queue, store)
    print(f"candidates {len(candidates)}  queued {len(queue)}  clips cut {cut}  no audio {missing}")
    print(f"queue: {arguments.out / 'queue.json'}")
    if document["counts"]:
        print(f"corpus so far: {document['counts']}")
    return 0


def cmd_status(arguments) -> int:
    store = LabelStore(arguments.labels)
    counts = store.counts()
    standing = store.current()
    print(f"labels file: {store.path}")
    print(f"clips labelled: {len(standing)}   verdicts recorded: {len(store.rows())}")
    if not counts:
        print("nothing labelled yet")
        return 0
    width = max(len(name) for name in counts)
    for name, count in counts.items():
        print(f"  {name:<{width}}  {count}")
    by_source: dict[str, int] = {}
    for row in standing.values():
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1
    print(f"by source: {by_source}")
    disputed = store.disputed()
    if disputed:
        print(f"disputed clips (worth a second listen): {len(disputed)}")
    return 0


def cmd_record(arguments) -> int:
    store = LabelStore(arguments.labels)
    store.record(Verdict(
        clip_id=arguments.clip_id, label=arguments.label, source=arguments.source,
        at=datetime.now(timezone.utc), by=arguments.by, note=arguments.note,
    ))
    print(f"recorded {arguments.clip_id} = {arguments.label}")
    return 0


def cmd_import(arguments) -> int:
    """Take verdicts in bulk, as a review tool would produce them."""
    document = json.loads(arguments.file.read_text(encoding="utf-8"))
    items = document.get("verdicts", document) if isinstance(document, dict) else document
    store = LabelStore(arguments.labels)
    now = datetime.now(timezone.utc)
    written = skipped = 0
    for item in items:
        label = item.get("label")
        clip = item.get("clip_id")
        if not clip or label not in LABELS:
            skipped += 1
            continue
        store.record(Verdict(clip_id=clip, label=label,
                             source=item.get("source") or "human", at=now,
                             by=item.get("by"), note=item.get("note")))
        written += 1
    print(f"imported {written} verdicts, skipped {skipped}")
    return 0


def cmd_export(arguments) -> int:
    store = LabelStore(arguments.labels)
    rows = training_rows(store, sources=tuple(arguments.sources))
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "sources": list(arguments.sources), "rows": rows}
    if arguments.out:
        arguments.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"{len(rows)} labelled clips -> {arguments.out}")
    else:
        print(json.dumps(payload, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="build a review queue and cut its audio")
    p.add_argument("--segments", type=Path, default=DEFAULT_SEGMENTS)
    p.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    p.add_argument("--detections", type=Path, default=None,
                   help="JSON from a detector run")
    p.add_argument("--out", type=Path, default=Path("./review-queue"))
    p.add_argument("--hours", type=float, default=48.0)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--min-confidence", type=float, default=0.5)
    p.add_argument("--min-seconds", type=float, default=5.0)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("status", help="what the corpus holds")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("record", help="write one verdict")
    p.add_argument("clip_id")
    p.add_argument("label", choices=LABELS)
    p.add_argument("--source", default="human", choices=("human", "detector", "relay", "import"))
    p.add_argument("--by", default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("import", help="take verdicts in bulk from a review tool")
    p.add_argument("file", type=Path)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="the labelled set to train on")
    p.add_argument("--sources", nargs="+", default=["human"])
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=cmd_export)

    arguments = parser.parse_args(argv)
    return arguments.func(arguments)


if __name__ == "__main__":
    sys.exit(main())
