# Pi Telemetry V3 Task 3 Report

## Scope

Implemented best-effort post-terminal SQLite persistence, pending outbox V3 promotion,
delivery-attempt metadata, completed-delivery retention, and private JSON/CSV export.
No authorization, matching, cooldown, relay pulse, stale-image, or hold-open behavior was
changed.

## RED

Command:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 --with watchdog==6.0.0 \
  python -m unittest tests.test_store tests.test_outbox tests.test_processor tests.test_main -v
```

Result: 105 tests run; 1 failure and 8 errors. The failures proved the missing SQLite
table/API, V3 attachment and retention behavior, outbox attempt handling, processor
attachment, and CLI dispatch.

Command:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 --with watchdog==6.0.0 \
  python -m unittest tests.test_telemetry_export -v
```

Result: 1 loader error because `gate_controller.telemetry_export` did not exist.

## GREEN

Focused command:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 --with watchdog==6.0.0 \
  python -m unittest tests.test_store tests.test_outbox tests.test_telemetry_export \
  tests.test_processor tests.test_main -v
```

Result: 109 tests passed.

Full command:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 --with watchdog==6.0.0 \
  python -m unittest discover -s tests -v
```

Result: 297 tests passed; 1 platform-specific `flock` test skipped.

Additional verification:

```sh
git diff --check
python3 -m compileall -q gate_controller tests
```

Both commands exited successfully.

## Commit

Implementation commit: `df76075 feat: persist and export event telemetry`

Changed files:

- `gate_controller/__main__.py`
- `gate_controller/outbox.py`
- `gate_controller/processor.py`
- `gate_controller/store.py`
- `gate_controller/telemetry_export.py`
- `tests/test_main.py`
- `tests/test_outbox.py`
- `tests/test_processor.py`
- `tests/test_store.py`
- `tests/test_telemetry_export.py`

## Contract Notes

- `event_telemetry` has one row per event and one owner per trace ID.
- Identical attachment is idempotent; event or trace conflicts are rejected.
- Telemetry attachment and pending outbox V3 promotion share one separate transaction
  after the terminal gate event has committed.
- Completed outbox payloads are never rewritten by attachment or retry operations.
- Retry attempt/state is persisted immediately before send; successful completion and
  local `delivered` state are committed together.
- Retention runs at most hourly and deletes only telemetry older than 30 days whose
  outbox delivery completed.
- Export uses an explicit path, an allowlisted redacted contract, mode `0600`, fsync,
  and atomic `os.replace`.
