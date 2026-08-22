# Hot Stream Recognition Implementation Plan

**Goal:** Replace the on-trigger Reolink snapshot fallback with an always-hot
clear-stream frame ring, surface its effective profile, and add a strictly
non-actuating local-recognition shadow seam.

**Spec:** `docs/superpowers/specs/2026-08-22-hot-stream-recognition.md`

## Task 1: Remove the obsolete HTTPS snapshot path

- Revert the progressive snapshot feature and hardening commits while preserving
  later trigger-provenance work.
- Delete snapshot configuration, module, tests and documentation claims.
- Run the focused worker/main/documentation suite.

## Task 2: Add two validated MediaMTX source paths

- Write failing tests for exact fluent/clear environment keys, loopback clear
  read authorization and the `clear` MediaMTX path.
- Extend the strict media configuration, authorization policy, example and
  installer migration.
- Keep `gate` publication and browser playback behaviour unchanged.

## Task 3: Implement the bounded hot-frame ring

- Write failing unit tests for JPEG stream parsing, byte/count bounds, freshness,
  atomic materialisation, restart/backoff and secret-free ffmpeg execution.
- Implement `gate_controller.hot_stream` using a persistent loopback ffmpeg
  child and in-memory ring.
- Fail closed for image acceptance and fail open to FTP/cloud recognition when
  the buffer is unavailable.

## Task 4: Merge buffered frames into the first OCR burst

- Write failing worker tests proving the three newest buffered frames enter the
  first 200 ms burst without a second queue or processing pass.
- Wire the provider into controller startup/shutdown and remove all progressive
  augmentation code.
- Add health/profile fields to the controller heartbeat.

## Task 5: Keep local recognition safely disabled

- Publish an explicit disabled/not-ready capability without installing a model.
- Summarise the private laptop latency and pseudo-label agreement results.
- Hand off a separate laptop-validation prompt with labelled-evaluation and
  promotion gates before any Pi shadow work begins.

## Task 6: Add web UI visibility

- Create an isolated access-gate-ui worktree.
- Write failing Worker/UI tests for the non-secret camera profile/status schema.
- Display the effective clear/fluent profiles, hot-buffer health and explicit
  local-recognition disabled status without exposing credentials or editable
  camera URLs.

## Task 7: Verify, review, publish and deploy

- Run full controller and UI suites, compile/type/lint/build checks, and a
  security-focused diff review.
- Push branches, create PRs, wait for required CI, merge through the protected
  path, and wait for the Pi updater.
- Back up root-managed media configuration, migrate it transactionally, restart
  media/controller services, and verify the loopback clear stream, buffer age,
  resource use and one non-actuating trigger.
