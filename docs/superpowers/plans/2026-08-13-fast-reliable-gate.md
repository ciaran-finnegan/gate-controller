# Fast, Reliable Gate Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a long-running, fail-closed gate controller that opens quickly for trustworthy plate matches and accepts audited Supabase commands.

**Architecture:** Pure matching and image-selection modules feed a dependency-injected processor. SQLite owns cooldown and an asynchronous outbox, while separate background workers handle cloud synchronisation and remote commands.

**Tech Stack:** Python 3, unittest, requests, Pillow, watchdog, SQLite, systemd, Supabase REST.

**Spec:** `docs/superpowers/specs/2026-08-13-fast-reliable-gate.md`

## Global Constraints

- OCR and control-plane failures fail closed.
- Relay activation precedes all optional network work.
- A fuzzy result cannot open from one frame or from a partial plate.
- Missing cloud and audio configuration must not stop local recognition.
- No secret values are committed or written into the systemd unit.

---

### Task 1: Matching policy and OCR client

**Files:**
- Create: `gate_controller/matching.py`
- Create: `gate_controller/ocr.py`
- Create: `gate_controller/models.py`
- Test: `tests/test_matching.py`
- Test: `tests/test_ocr.py`

**Interfaces:**
- `normalise_plate(value: str) -> str`
- `decide_access(observations, authorised) -> MatchDecision`
- `PlateRecognizerClient.recognise(path: Path) -> PlateObservation`

- [ ] Write tests proving exact matches, rejection of substrings, rejection of ambiguous candidates, and two-frame known-confusion consensus.
- [ ] Run `python3 -m unittest tests.test_matching -v` and verify the missing module fails.
- [ ] Implement immutable observation/decision models and the minimum matching policy.
- [ ] Run the matching tests and verify they pass.
- [ ] Write OCR tests for timeout propagation, non-2xx responses, malformed/empty results and confidence extraction.
- [ ] Run the OCR tests and verify they fail before implementation.
- [ ] Implement a requests-session client with `(2, 4)` default timeouts and explicit response validation.
- [ ] Run both test modules and commit.

### Task 2: Local state, cooldown and relay ordering

**Files:**
- Create: `gate_controller/store.py`
- Create: `gate_controller/relay.py`
- Create: `gate_controller/processor.py`
- Test: `tests/test_store.py`
- Test: `tests/test_processor.py`

**Interfaces:**
- `LocalStore.was_opened_since(cutoff) -> bool`
- `LocalStore.record_event(event) -> int`
- `RelayController.trigger(source, idempotency_key=None) -> RelayResult`
- `GateProcessor.process(paths) -> ProcessingResult`

- [ ] Write SQLite regression tests covering legacy `True`, `Yes` and numeric cooldown values.
- [ ] Verify the tests fail, then implement schema migration, event timing fields and an outbox table.
- [ ] Write processor tests proving relay activation happens before outbox work, duplicate events do not reactivate, and OCR errors fail closed.
- [ ] Verify those tests fail, then implement dependency-injected processor and lazy PiRelay adapter.
- [ ] Run all focused tests and commit.

### Task 3: Completed-upload burst ingestion

**Files:**
- Create: `gate_controller/images.py`
- Create: `gate_controller/worker.py`
- Create: `gate_controller/__main__.py`
- Modify: `file_monitor.sh`
- Test: `tests/test_images.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- `wait_until_readable(path, timeout) -> bool`
- `rank_images(paths) -> list[Path]`
- `BurstCollector` emits one ranked tuple per configurable quiet window.

- [ ] Write tests for partial files, non-images, sharpness ordering and burst coalescing.
- [ ] Verify failures, then implement Pillow validation and watchdog close/move handling with a safe created-file fallback.
- [ ] Replace the shell loop with an `exec python3 -m gate_controller` compatibility launcher.
- [ ] Run focused and full unit tests and commit.

### Task 4: Background sync, commands and heartbeat

**Files:**
- Create: `gate_controller/control_plane.py`
- Create: `gate_controller/outbox.py`
- Create: `gate_controller/audio.py`
- Modify: `gate_controller/worker.py`
- Test: `tests/test_control_plane.py`
- Test: `tests/test_outbox.py`

**Interfaces:**
- `SupabaseControlPlane.claim_command() -> GateCommand | None`
- `SupabaseControlPlane.heartbeat(status) -> None`
- `OutboxWorker.run_once() -> int`
- `PromptPlayer.play(prompt_key) -> bool`

- [ ] Write tests for expired commands, duplicate ids, failed acknowledgements, heartbeat payloads and fixed prompt-key validation.
- [ ] Verify failures, then implement bounded REST calls and capability reporting.
- [ ] Write tests proving failed cloud sync remains queued and successful sync is marked complete.
- [ ] Verify failures, implement workers and wire them into the daemon without blocking image processing.
- [ ] Run the unit suite and commit.

### Task 5: Deployment configuration and verification

**Files:**
- Modify: `file-monitor.service`
- Modify: `requirements.txt`
- Modify: `README.md`
- Create: `.env.example`
- Create: `docs/reolink-rlc-811a.md`

- [ ] Move secrets to `/etc/gate-controller.env`, use network-online ordering, restart backoff and narrowly scoped write paths.
- [ ] Declare runtime dependencies and document optional Raspberry Pi GPIO installation.
- [ ] Document Reolink FTP burst, vehicle-zone and night exposure calibration plus stage-timing interpretation.
- [ ] Run `python3 -m unittest discover -s tests -v` and `python3 -m compileall gate_controller`.
- [ ] Review the complete diff against the spec and commit.
