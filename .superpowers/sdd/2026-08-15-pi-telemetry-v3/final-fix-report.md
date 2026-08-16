# Pi Telemetry V3 Final Fix Report

## Status

Final review findings are fixed on `codex/telemetry-v3-producer` without changing camera-arrival timing, authorization, OCR, relay pulse, actuation, hold-open, credential exclusion, or offline retry behavior.

- Malformed telemetry and telemetry row/outbox/commit rewrite failures return the recovered original V2 outbox payload. Delivery continues and logs only bounded audit tokens: `status=fallback_v2` with `reason=malformed` or `reason=rewrite_failed`.
- Export rejects the live database, `-wal`, `-shm`, and `-journal` sidecars, resolved path aliases, symlinks, hardlinks, and aliases introduced while the temporary export is being written. The guard runs again immediately before `os.replace`.
- Export uses a private SQLite backup in 16-page steps, aborts after 250 ms of continuous busy status, reads only the snapshot, and streams stable `(received_at, event_id)` keyset pages capped at 100 rows.
- `frames[].status`, including `quality_unavailable`, now survives telemetry persistence, pending outbox promotion, attempt metadata updates, and JSON export.
- Retention strips embedded telemetry and restores schema version 2 on aged completed outbox payloads in the same transaction that removes detailed telemetry. Decision/evidence fields are unchanged; pending and retry payloads are untouched.

## TDD Evidence

Baseline before test changes:

```sh
python3.12 -m unittest discover -s tests
```

Result: 297 passed, 1 skipped.

The initial focused RED run exercised nine review cases and produced the expected missing-behavior failures: 12 failures and 3 errors, including suppressed delivery, dropped frame status, missing keyset API, retained completed telemetry, unsafe replacement, and live-database export. The bounded-busy test separately errored with `sqlite3.OperationalError: database is locked`; the commit-failure test separately errored instead of returning V2.

Focused GREEN:

```sh
python3.12 -m unittest \
  tests.test_outbox.OutboxWorkerTests.test_malformed_telemetry_falls_back_to_the_original_v2_delivery \
  tests.test_outbox.OutboxWorkerTests.test_telemetry_rewrite_failure_falls_back_without_blocking_delivery \
  tests.test_outbox.OutboxWorkerTests.test_telemetry_commit_failure_returns_the_recovered_v2_payload \
  tests.test_store.LocalStoreTests.test_frame_quality_status_survives_persistence_and_outbox_attachment \
  tests.test_store.LocalStoreTests.test_telemetry_pages_use_received_at_and_event_id_as_a_stable_keyset \
  tests.test_store.LocalStoreTests.test_retention_removes_only_old_telemetry_with_completed_delivery \
  tests.test_telemetry_export.TelemetryExportTests.test_json_export_is_bounded_by_since_and_redacts_unsafe_stored_fields \
  tests.test_telemetry_export.TelemetryExportTests.test_export_rejects_database_sidecars_and_same_file_aliases \
  tests.test_telemetry_export.TelemetryExportTests.test_output_is_rechecked_for_database_aliases_before_replace \
  tests.test_telemetry_export.TelemetryExportTests.test_concurrent_snapshot_export_does_not_delay_an_actuation_claim \
  tests.test_telemetry_export.TelemetryExportTests.test_snapshot_busy_wait_is_bounded
```

Result: 11 passed, 0 failed. The concurrent export is deliberately paused during snapshot-row serialization; a real actuation claim succeeds in under 250 ms while export remains paused. A continuously exclusive database lock makes snapshot creation fail in under one second with the bounded snapshot timeout.

Broader store/outbox/export/processor/telemetry/actuation/relay regression run: 134 passed, 0 failed.

Final verification:

```sh
python3.12 -m unittest discover -s tests
python3.12 -m compileall -q gate_controller tests
git diff --check
```

Result: 306 passed, 1 expected platform skip; compile and diff checks clean.

## Commit And Files

Implementation commit: `67e1cdb238f83a676ca0626481d7c8d9882cdd9e` (`fix: harden telemetry delivery and export`)

- `gate_controller/store.py`
- `gate_controller/telemetry_export.py`
- `tests/test_outbox.py`
- `tests/test_store.py`
- `tests/test_telemetry_export.py`

No network access, dependency installation, deployment, push, hardware relay tests, or subagents were used.

## Follow-up And Concern

The matching web consumer contract must accept and preserve the bounded `frames[].status` token, including `quality_unavailable`. The web repository was not edited in this task.

Snapshot export intentionally favors gate availability: after 250 ms of continuous SQLite busy status it raises a retryable `TimeoutError` instead of waiting seconds behind pre-relay writes. The deterministic concurrency test proves lock isolation and the claim-time bound; it is not a Pi hardware throughput benchmark.
