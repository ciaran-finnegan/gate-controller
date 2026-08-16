"""Private, atomic export of bounded local processing telemetry."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .store import LocalStore


_TOP_LEVEL_KEYS = (
    "trace_id", "taxonomy_version", "stage_durations", "frames", "ocr_attempts",
    "decision", "actuation", "delivery",
)
_NESTED_KEYS = {
    "stage_durations": (
        "capture_to_burst_ms", "burst_to_ocr_ms", "ocr_ms", "decision_ms",
        "decision_to_relay_ms", "end_to_end_ms", "delivery_lag_ms",
    ),
    "frames": (
        "sequence", "digest", "width", "height", "sharpness", "brightness",
        "darkness", "highlight_clipping", "status",
    ),
    "ocr_attempts": (
        "frame_sequence", "duration_ms", "status", "plate", "confidence", "make",
        "colour",
    ),
    "decision": ("outcome", "reason"),
    "actuation": ("claim", "attempted", "relay_outcome"),
    "delivery": ("outbox_attempt", "state"),
}
_CSV_FIELDS = (
    "event_id", "received_at", "source", "reason", "opened", "observed_plate",
    "authorised_plate", "ocr_confidence", "telemetry_created_at", "trace_id",
    "taxonomy_version", "capture_to_burst_ms", "burst_to_ocr_ms", "ocr_ms",
    "decision_ms", "decision_to_relay_ms", "end_to_end_ms", "delivery_lag_ms",
    "frame_count", "ocr_attempt_count", "decision_outcome", "decision_reason",
    "actuation_claim", "actuation_attempted", "relay_outcome", "outbox_attempt",
    "delivery_state",
)
_EXPORT_PAGE_SIZE = 100
_DATABASE_SIDECARS = ("", "-wal", "-shm", "-journal")


def export_telemetry(store, *, format: str, since: datetime,
                     output: Path | None) -> int:
    if output is None:
        raise ValueError("an explicit telemetry export output path is required")
    if format not in {"json", "csv"}:
        raise ValueError("telemetry export format must be json or csv")
    if since.tzinfo is None:
        raise ValueError("telemetry export since timestamp must include a timezone")
    output = Path(output)
    protected_paths = _database_paths(store.path)
    _validate_output_path(output, protected_paths)
    with tempfile.TemporaryDirectory(
        prefix=".telemetry-snapshot-", dir=output.parent
    ) as snapshot_directory:
        snapshot_path = Path(snapshot_directory) / "telemetry.db"
        store.create_telemetry_snapshot(snapshot_path)
        snapshot = LocalStore(snapshot_path)
        rows = _snapshot_rows(snapshot, since)
        return _atomic_write(
            output,
            lambda destination: _write(destination, format, rows),
            protected_paths,
        )


def _snapshot_rows(store: LocalStore, since: datetime):
    after = None
    while True:
        page = store.event_telemetry_page(
            since, after=after, limit=_EXPORT_PAGE_SIZE
        )
        if not page:
            return
        yield from page
        after = (page[-1]["received_at"], page[-1]["event_id"])


def _safe_row(row: dict) -> dict:
    return {
        "event_id": row["event_id"],
        "received_at": row["received_at"],
        "decision_at": row["decision_at"],
        "relay_activated_at": row["relay_activated_at"],
        "source": row["source"],
        "reason": row["reason"],
        "opened": bool(row["opened"]),
        "authorised_plate": row["authorised_plate"],
        "observed_plate": row["observed_plate"],
        "ocr_confidence": row["ocr_confidence"],
        "telemetry_created_at": row["telemetry_created_at"],
        "telemetry": _safe_telemetry(row["telemetry"]),
    }


def _safe_telemetry(value: object) -> dict:
    telemetry = value if isinstance(value, dict) else {}
    safe = {
        key: telemetry[key]
        for key in ("trace_id", "taxonomy_version")
        if key in telemetry and _scalar(telemetry[key])
    }
    for key in ("stage_durations", "decision", "actuation", "delivery"):
        nested = telemetry.get(key)
        safe[key] = _allowlisted_dict(nested, _NESTED_KEYS[key])
    for key in ("frames", "ocr_attempts"):
        items = telemetry.get(key)
        safe[key] = [
            _allowlisted_dict(item, _NESTED_KEYS[key])
            for item in items[:8]
            if isinstance(item, dict)
        ] if isinstance(items, list) else []
    return {key: safe[key] for key in _TOP_LEVEL_KEYS if key in safe}


def _allowlisted_dict(value: object, allowed: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in allowed if key in value and _scalar(value[key])}


def _scalar(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _write(destination, format: str, rows) -> int:
    if format == "json":
        count = 0
        destination.write("[")
        for raw_row in rows:
            destination.write("," if count else "")
            destination.write("\n  ")
            json.dump(_safe_row(raw_row), destination, sort_keys=True)
            count += 1
        destination.write("\n]\n" if count else "]\n")
        return count
    writer = csv.DictWriter(destination, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    count = 0
    for raw_row in rows:
        writer.writerow(_csv_row(_safe_row(raw_row)))
        count += 1
    return count


def _csv_row(row: dict) -> dict:
    telemetry = row["telemetry"]
    durations = telemetry.get("stage_durations", {})
    decision = telemetry.get("decision", {})
    actuation = telemetry.get("actuation", {})
    delivery = telemetry.get("delivery", {})
    flattened = {key: row.get(key) for key in _CSV_FIELDS if key in row}
    flattened.update({key: durations.get(key) for key in _NESTED_KEYS["stage_durations"]})
    flattened.update({
        "trace_id": telemetry.get("trace_id"),
        "taxonomy_version": telemetry.get("taxonomy_version"),
        "frame_count": len(telemetry.get("frames", [])),
        "ocr_attempt_count": len(telemetry.get("ocr_attempts", [])),
        "decision_outcome": decision.get("outcome"),
        "decision_reason": decision.get("reason"),
        "actuation_claim": actuation.get("claim"),
        "actuation_attempted": actuation.get("attempted"),
        "relay_outcome": actuation.get("relay_outcome"),
        "outbox_attempt": delivery.get("outbox_attempt"),
        "delivery_state": delivery.get("state"),
    })
    return flattened


def _atomic_write(output: Path, write, protected_paths: tuple[Path, ...]) -> int:
    parent = output.parent
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".telemetry-", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as destination:
            descriptor = -1
            count = write(destination)
            destination.flush()
            os.fsync(destination.fileno())
        _validate_output_path(output, protected_paths)
        os.replace(temporary, output)
        output.chmod(0o600)
        _fsync_directory(parent)
        return count
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _database_paths(database: Path) -> tuple[Path, ...]:
    absolute = Path(os.path.abspath(os.fspath(database)))
    resolved = absolute.resolve(strict=False)
    protected = {
        Path(f"{base}{suffix}")
        for base in (absolute, resolved)
        for suffix in _DATABASE_SIDECARS
    }
    return tuple(protected)


def _validate_output_path(output: Path, protected_paths: tuple[Path, ...]) -> None:
    candidate = Path(os.path.abspath(os.fspath(output)))
    resolved_candidate = candidate.resolve(strict=False)
    for protected in protected_paths:
        resolved_protected = protected.resolve(strict=False)
        if candidate == protected or resolved_candidate == resolved_protected:
            raise ValueError("telemetry output must not target the live database or sidecars")
        try:
            same_file = candidate.samefile(protected)
        except OSError:
            same_file = False
        if same_file:
            raise ValueError("telemetry output must not alias the live database or sidecars")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
