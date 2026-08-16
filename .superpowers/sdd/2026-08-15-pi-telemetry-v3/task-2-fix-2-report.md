# Pi Telemetry V3 Task 2 Fix 2 Report

## Review Finding Addressed

- `ProcessingTrace` now anchors the wall-clock `received_at` boundary to its monotonic creation point and accepts the worker's monotonic `decision_started_at` boundary.
- `capture_to_burst_ms` includes the upstream quiet window. `burst_to_ocr_ms` and `end_to_end_ms` include ranking, hashing, and frame-quality preprocessing.
- Upstream intervals are bounded by the existing 600,000 ms duration limit. Only cross-clock skew up to 100 ms is clamped; invalid, non-finite, too-old, or inconsistent timestamps leave affected durations unavailable.
- Upstream seeding runs through the existing best-effort trace wrapper. A seed failure disables telemetry without changing OCR, authorization, relay, or outbox behavior.
- Duplicate checks still occur before trace creation. Direct nonduplicate skip paths still produce one terminal trace.

## Strict TDD Evidence

Focused upstream tests were written before implementation. After correcting a test-only missing `timedelta` import, the clean RED run was:

```sh
python3.12 -m unittest tests.test_telemetry tests.test_processor
```

Result: 62 tests ran with 4 expected failures and 4 expected errors. The trace did not yet expose `seed_upstream`; allow, timeout, and stale traces omitted the quiet window/preprocessing; and an injected seed exception did not yet suppress telemetry.

The focused GREEN run covered eight new tests:

```sh
python3.12 -m unittest \
  tests.test_telemetry.ProcessingTraceTests.test_upstream_boundaries_anchor_wall_capture_to_monotonic_burst \
  tests.test_telemetry.ProcessingTraceTests.test_upstream_capture_clamps_only_small_cross_clock_skew \
  tests.test_telemetry.ProcessingTraceTests.test_inconsistent_upstream_capture_is_unavailable_not_fabricated \
  tests.test_telemetry.ProcessingTraceTests.test_invalid_upstream_timestamps_leave_all_derived_stages_unavailable \
  tests.test_processor.GateProcessorTests.test_upstream_quiet_window_and_preprocessing_are_in_stage_durations \
  tests.test_processor.GateProcessorTests.test_upstream_seed_failure_is_best_effort \
  tests.test_processor.GateProcessorTests.test_preprocessing_timeout_retains_upstream_terminal_durations \
  tests.test_processor.GateProcessorTests.test_preprocessing_stale_path_retains_upstream_terminal_durations
```

Result: 8 passed, 0 failed.

The allow-path clock test advances through 500 ms of quiet-window time, 200 ms before processor entry, 300 ms of hashing, 100 ms of quality work, and 50 ms of OCR. It asserts exactly 500 ms capture-to-burst, 600 ms burst-to-OCR, 50 ms OCR, and 1,150 ms end-to-end, with one recognizer call and one relay call. Timeout and stale tests assert 4,600 ms and 6,200 ms terminal durations respectively, with no recognizer or relay calls.

## Verification

Broader focused regression commands passed:

```sh
python3.12 -m unittest tests.test_telemetry tests.test_worker tests.test_actuation tests.test_relay -v
```

Result: 68 passed, 0 failed.

The processor suite, excluding only two concurrently added uncommitted Task 3 persistence tests, passed 47 tests. This covered existing allow, deny/no-match, OCR exception, timeout/stale, duplicate, direct-skip, relay, and outbox call-count behavior.

The exact committed `d4f2a67` snapshot was exported to a temporary directory and given a clean full-suite run:

```sh
python3.12 -m unittest discover -s tests
```

Result: 283 passed, 0 failed, 1 platform-specific skip.

The active worktree also contains unrelated, uncommitted Task 3 RED tests in `tests/test_main.py`, `tests/test_outbox.py`, `tests/test_processor.py`, `tests/test_store.py`, and `tests/test_telemetry_export.py`. Full discovery there ran 294 tests and failed only those not-yet-implemented Task 3 contracts (1 failure and 9 errors). Those Task 3 changes were not edited, staged, or committed for this fix; `tests/test_processor.py` contains separate committed round 2 hunks.

Static verification:

```sh
python3.12 -m compileall -q gate_controller tests
git show --check d4f2a67
git diff --check
```

Result: all clean.

## Self-review

- Trace construction remains after both duplicate guards, preserving trace-free duplicate behavior.
- The compatibility fallback still calls `mark_burst()` when no upstream decision boundary is supplied.
- The direct-skip path seeds `received_at` when available and still emits one terminal trace.
- No relay pulse, hold-open, actuation, authorization, OCR limit, stale inhibition, result, or wire-schema behavior changed.
- No hardware relay tests or network/dependency installation were performed.

The reproducible enabled-versus-disabled latency benchmark remains a Task 4 acceptance item. These deterministic tests prove accounting boundaries but do not establish the under-25 ms p95 overhead target.

## Commit And Files

Implementation commit: `d4f2a676c3ba926040dbd2948028b7376f7c4071` (`fix: seed telemetry from upstream timing`)

- `gate_controller/processor.py`
- `gate_controller/telemetry.py`
- `tests/test_processor.py`
- `tests/test_telemetry.py`
