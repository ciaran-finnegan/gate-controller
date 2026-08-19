# Whole-Branch Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three final review findings with deterministic regression tests, transactional media deployment rollback, and complete branch verification.

**Architecture:** Relay activation and shutdown share one re-entrant boundary primitive that linearizes the last inhibition check, GPIO HIGH, shutdown latch, and forced GPIO LOW while the controller lock remains the in-flight-work terminalization barrier. TURN refresh distinguishes one safe bootstrap state, an empty trusted runtime file, from strict rotation of a complete runtime environment. The media installer snapshots every stable artifact and prior unit state under the existing install/TURN lock, then restores artifacts, reloads managers, and only restores service state after all rollback prerequisites succeed.

**Tech Stack:** Python 3.11+, `unittest`/`pytest`, Bash, systemd command boundaries, atomic filesystem replacement.

**Spec:** User-provided whole-branch review findings in this task; related architecture is documented in `docs/superpowers/specs/2026-08-17-cloudflare-replatform-design.md`.

## Global Constraints

- Work only in `/Users/ciaranfinnegan/dev/gate-controller/.worktrees/codex-cloudflare-replatform`.
- Use strict RED/GREEN TDD for each finding and preserve expiry priority, bounded shutdown, interruptible pulse behavior, generic relay backends, strict existing TURN rotation, secret redaction, and persistent locking.
- Do not touch Pi hardware or external systems.
- Hold the install/TURN lock through media rollback and fail closed if rollback cannot complete.
- Commit all fixes together after the complete verification matrix succeeds.

---

### Task 1: Atomic relay activation boundary

**Files:**
- Modify: `PiRelay.py`
- Modify: `gate_controller/relay.py`
- Test: `tests/test_relay.py`
- Test: `tests/test_main.py` if shutdown integration expectations require adjustment

**Interfaces:**
- Consumes: relay backends exposing `on()`, `off()`, and optionally an `activation_boundary` context-manager lock.
- Produces: `PiRelay.Relay.activation_boundary`, forwarded by `PiRelayAdapter`, and `RelayController.begin_shutdown()` that latches and forces LOW before waiting for in-flight trigger terminalization.

- [ ] Add a deterministic production-adapter test whose GPIO HIGH implementation pauses after the inhibition callback and records whether the shutdown latch was already established.
- [ ] Run `.venv/bin/python -m unittest tests.test_relay.RelayControllerTests.<focused_test> -v`; expect HIGH to be recorded after `_shutdown_requested` is set on the unfixed code.
- [ ] Add a shared `threading.RLock` to the Pi relay library/adapter and make callback plus GPIO HIGH one critical section.
- [ ] Make the controller use that boundary, and have `begin_shutdown()` set the event/latch and force LOW inside it before waiting on the controller lock.
- [ ] Re-run the focused test and the relay/main/actuation/command/processor suite; expect all to pass.

### Task 2: Safe initial TURN credential bootstrap

**Files:**
- Modify: `deployment/gate_media_turn_refresh.py`
- Modify: `deployment/install-media.sh`
- Test: `tests/test_media_turn_refresh.py`
- Test: `tests/test_media_deployment.py`

**Interfaces:**
- Consumes: a complete root-only long-term TURN secret, complete auth/static gateway environments, and an existing empty owner-only runtime TURN file.
- Produces: exactly the three validated short-lived `MTX_WEBRTCICESERVERS2_0_*` assignments via atomic replacement; non-empty rotation remains strict.

- [ ] Add a focused refresher test proving an empty mode-0600 runtime file fetches once and is atomically populated without exposing the long-term secret.
- [ ] Add installer integration/order coverage proving bootstrap runs under the held lock before media/timer activation.
- [ ] Run the focused tests; expect the current complete-environment validation to reject the empty runtime before any fetch.
- [ ] Add explicit empty trusted-file validation and initial serialization in the refresher, preserving strict replacement for every non-empty runtime file.
- [ ] Invoke the helper for an empty runtime during installation while the persistent lock is held and before media service activation.
- [ ] Re-run focused tests and the full media deployment/TURN refresh suite; expect all to pass.

### Task 3: Transactional media publication rollback

**Files:**
- Modify: `deployment/install-media.sh`
- Test: `tests/test_media_deployment.py`

**Interfaces:**
- Consumes: stable artifact paths, a private transaction backup directory, and captured enabled/active state for nginx, media services, and TURN units.
- Produces: exact prior artifacts or prior absence after any post-publication failure, with systemd/nginx reload prerequisites gating service-state restoration.

- [ ] Add failure-injection tests after proxy publication, media service activation, and timer activation; cover prior bytes/state plus first-install absence and candidate cleanup.
- [ ] Run each focused test against the current installer; expect candidate artifacts to remain and prior service state to stay disabled.
- [ ] Snapshot all stable files/directories/symlinks and relevant unit states under the lock before publication.
- [ ] Restore artifacts by sibling staging/rename, reload restored systemd units, validate/reload prior nginx where active, and restore exact enable/activity states only after prerequisites succeed.
- [ ] Keep backups on rollback failure, release the lock only after rollback, and return failure for any incomplete rollback.
- [ ] Re-run focused tests and the full media deployment/TURN refresh suite; expect all to pass.

### Task 4: Integrated verification and commit

**Files:**
- Review all modified files and tests.

**Interfaces:**
- Consumes: Tasks 1-3 green implementations.
- Produces: one reviewed commit and exact verification evidence.

- [ ] Run the relay/main/actuation/command/processor suite.
- [ ] Run the media deployment/TURN refresh suite.
- [ ] Run `.venv/bin/python -m pytest tests -q` and `.venv/bin/python -m unittest discover -s tests -v`.
- [ ] Run `bash -n` and `shellcheck` over all shell scripts, compileall, whitespace/diff checks, and inspect `git diff` for requirement coverage and secret leakage.
- [ ] Perform an independent read-only code review, address any critical/important findings with another RED/GREEN cycle, and rerun affected/full gates.
- [ ] Commit the complete fix with a clear message and report the SHA, RED/GREEN evidence, final commands, and residual risks.
