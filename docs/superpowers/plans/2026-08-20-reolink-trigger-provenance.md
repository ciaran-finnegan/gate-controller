# Reolink Trigger Provenance Implementation Plan

## Goal

Implement gate-controller issue #55 so every recognition event records whether it
originated from a Reolink line-crossing alert, a generic vehicle alert, another
camera rule, or the existing unverified FTP fallback. Surface that provenance in
Gate Mate without allowing camera metadata to authorize or actuate the gate.

## Controller

1. Add a bounded, thread-safe camera-event correlator with duplicate and stale
   event rejection.
2. Add a small authenticated Reolink webhook endpoint on the Pi. It accepts only
   bounded JSON fields and has no relay or authorization dependency.
3. Correlate webhook metadata with the nearest FTP image burst without delaying
   recognition. Preserve `camera_ftp/unverified` when no match exists.
4. Extend event telemetry with a sanitized trigger summary and retain it through
   SQLite/outbox delivery.
5. Wire the worker through environment settings, systemd, and Reolink deployment
   documentation. No nginx or ONVIF listener is required.

## Cloud And UI

1. Extend the strict controller-ingest contract and fingerprint with the trigger
   summary, storing it in the existing telemetry JSON column.
2. Return trigger details in access-log review context.
3. Show camera event type, rule identifier, source, and correlation state in the
   vehicle detail panel. Legacy events must be labelled as unavailable rather
   than inferred.

## TDD And Safety

- Write failing tests before implementation in each repository.
- Cover valid, malformed, unauthorized, oversized, duplicate, stale, matched,
  and FTP-fallback cases.
- Assert that trigger-only input makes zero relay calls.
- Never include the webhook secret or raw camera payload in telemetry or logs.
- Do not actuate the physical gate during verification.

## Verification And Rollout

1. Run focused tests, then each repository's full test/lint/build suite.
2. Review the final diffs and security boundaries.
3. Merge and deploy both repositories.
4. Verify the active Pi release, configure the camera webhook, and passively
   correlate the next drive event against the moved-line experiment.
