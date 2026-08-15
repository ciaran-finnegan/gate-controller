# Outbound Deployment Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CI-gated, outbound, atomic Raspberry Pi releases with automatic rollback and no Tailscale dependency.

**Architecture:** A stdlib-only Python updater makes fail-closed release decisions and orchestrates exact-SHA staging, verification, activation, health checking, rollback, and retention. A root systemd timer invokes it through `flock`; an explicit bootstrap script migrates a clean legacy checkout into a separate release root and installs the units.

**Tech Stack:** Python 3.10+ standard library, Bash, Git, systemd, GitHub Actions, `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-14-outbound-deployment-reliability.md`

## Global Constraints

- Production gate operation and deployment must not depend on Tailscale.
- Do not modify plate recognition, authorization, control-plane, or relay behavior.
- Do not add updater runtime Python package dependencies.
- Never overwrite or delete `/var/lib/gate-controller`, `/etc/gate-controller.env`, or the legacy `/opt/gate-controller` checkout.
- A candidate must have a successful `ci.yml` push workflow for the exact immutable `master` SHA before staging.
- Transient network, API, Git, dependency, verification, and rate-limit failures leave `current` untouched.

---

### Task 1: Test CI-Gated Release Decisions

**Files:**
- Create: `tests/test_updater.py`
- Create: `deployment/__init__.py`
- Create: `deployment/gate_controller_updater.py`

**Interfaces:**
- Produces: `read_main_sha(payload) -> str`
- Produces: `has_successful_ci_run(payload, sha) -> bool`
- Produces: `decide_update(current_sha, main_payload, runs_payload) -> UpdateDecision`
- Produces: `releases_to_prune(entries, current_sha, keep) -> list[str]`

- [ ] **Step 1: Write failing tests** for exact 40-character SHA parsing,
  rejection of a successful run for the wrong SHA, rejection of pending/failed
  runs, acceptance of only the successful `master` push run for `ci.yml`, no-op
  for the active SHA, and retention that never removes the active release.
- [ ] **Step 2: Run `python3 -m unittest tests.test_updater -v`** and verify it
  fails because `deployment.gate_controller_updater` does not exist.
- [ ] **Step 3: Implement the pure decision and retention functions** with
  explicit payload validation and no network side effects.
- [ ] **Step 4: Re-run `python3 -m unittest tests.test_updater -v`** and verify
  every updater decision test passes.

### Task 2: Implement Fail-Safe Release Orchestration

**Files:**
- Modify: `deployment/gate_controller_updater.py`
- Create: `deployment/systemd/gate-controller-updater.service`
- Create: `deployment/systemd/gate-controller-updater.timer`
- Modify: `file-monitor.service`

**Interfaces:**
- Consumes: pure CI decision and retention functions from Task 1.
- Produces: an updater CLI whose default action is one serialized poll/deploy
  attempt and whose transient failures leave the active symlink untouched.

- [ ] **Step 1: Add tests** around configuration bounds and active-release
  discovery, then run them red.
- [ ] **Step 2: Add stdlib GitHub API reads** with bounded timeouts, optional
  bearer token headers, and defer-on-network/rate-limit behavior.
- [ ] **Step 3: Stage the exact SHA** with bounded Git commands into a temporary
  sibling of the immutable SHA release directory and verify fetched `HEAD`.
- [ ] **Step 4: Build a release-local virtual environment and verify** the unit
  suite, `compileall`, shell syntax, and systemd unit validity before activation.
- [ ] **Step 5: Atomically install the current symlink, restart and continuously
  health-check the fixed app service**, restoring the previous symlink
  automatically on any activation failure. Fixed unit changes require bootstrap.
- [ ] **Step 6: Prune only SHA-named inactive releases** after successful health
  verification, retaining the configured active release plus prior releases.
- [ ] **Step 7: Add a five-minute systemd timer** whose root oneshot service uses
  non-blocking `/usr/bin/flock` and reads an optional root-only environment file.

### Task 3: Add Explicit Bootstrap And Migration

**Files:**
- Create: `deployment/install.sh`
- Create: `docs/deployment.md`
- Modify: `README.md`

**Interfaces:**
- Produces: `sudo deployment/install.sh --source /opt/gate-controller` as the
  only path that creates/enables the new deployment layout and timer.

- [ ] **Step 1: Implement strict installer validation** for root, a clean source
  Git checkout, a 40-character commit, required system tools, Python 3.10+, and
  the existing environment file.
- [ ] **Step 2: Stage and verify the initial release** without touching the
  current service, state, configuration, or legacy checkout.
- [ ] **Step 3: Migrate the legacy database only when absent**, install units,
  atomically activate, and restore the old service unit/symlink if startup fails.
- [ ] **Step 4: Document one-time migration, rollback, logs, optional private
  token configuration, branch protection check names, and Tailscale as optional
  diagnostics only.**

### Task 4: Add Stable GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces stable required checks `Gate controller tests (Python 3.10)`,
  `Gate controller tests (Python 3.11)`, and `Deployment syntax checks`.

- [ ] **Step 1: Configure read-only workflow permissions and triggers** for
  `pull_request`, `merge_group`, and `push` to `master`.
- [ ] **Step 2: Install requirements and run all `unittest` tests plus
  `compileall`** in explicit Python 3.10 and 3.11 jobs.
- [ ] **Step 3: Validate Bash, POSIX shell, and systemd unit syntax** in a stable
  deployment check that can be required by branch protection.

### Task 5: Verify And Commit

**Files:**
- Review every path changed by Tasks 1-4.

- [ ] **Step 1: Run updater tests and the complete unit suite.**
- [ ] **Step 2: Run Python compilation for application, updater, and tests.**
- [ ] **Step 3: Run `bash -n deployment/install.sh`, `sh -n file_monitor.sh`,
  and `systemd-analyze verify` for all units.**
- [ ] **Step 4: Inspect `git diff --check`, scope, executable modes, and confirm
  no credential or persistent-path write was introduced.**
- [ ] **Step 5: Commit all deployment reliability files in one focused commit.**
