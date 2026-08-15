# Pi Telemetry V3 Task 2 Fix 1 Report

## Review Findings Addressed

- `decision_to_relay_ms` is marked from the real relay activation callback, before pulse sleep and SQLite finalization.
- Trace creation, frame quality, trace marks, relay-boundary marks, outcome marks, and finish are best effort and cannot suppress a successful gate result.
- Frame quality has its own serialized bounded `status`; OCR attempt status reports OCR only.
- Direct nonduplicate skip/error paths create one terminal trace, while duplicate processing and duplicate direct skips create no second trace.
- Processor content digests are reused by frame-quality measurement instead of hashing each frame twice.

## TDD Follow-up

The independent review added the missing duplicate-direct-skip regression first:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 \
  python -m unittest \
  tests.test_processor.GateProcessorTests.test_duplicate_direct_skip_does_not_create_a_second_trace
```

RED result: 1 test failed because the duplicate returned `queue_coalesced` with a second trace instead of `duplicate_event` without telemetry.

GREEN focused result:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 \
  python -m unittest \
  tests.test_processor tests.test_telemetry tests.test_images tests.test_actuation tests.test_relay
```

Result: 89 passed, 0 failed.

Full verification:

```sh
uv run --with pillow==12.3.0 --with requests==2.34.2 --with watchdog==6.0.0 \
  python -m unittest discover -s tests
```

Result: 275 passed, 0 failed, 1 platform-specific skip.

Static verification:

```sh
git diff --check
```

Result: clean.

## Commits

- Main review-fix implementation: `f8715f6`
- Duplicate direct-skip follow-up: `1e60428`

## Changed Files

- `gate_controller/actuation.py`
- `gate_controller/images.py`
- `gate_controller/processor.py`
- `gate_controller/relay.py`
- `gate_controller/telemetry.py`
- `tests/test_actuation.py`
- `tests/test_images.py`
- `tests/test_processor.py`
- `tests/test_relay.py`
- `tests/test_telemetry.py`

The reproducible enabled-versus-disabled 100-event latency benchmark remains a Task 4 acceptance item; it is not claimed by this Task 2 report.
