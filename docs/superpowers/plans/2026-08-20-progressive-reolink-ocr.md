# Progressive Reolink OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two bounded fresh 4K Reolink snapshots to the existing ranked OCR pipeline immediately after the first FTP image arrives.

**Architecture:** Preserve FTP as the proven trigger and first recognition attempt. Run a single best-effort HTTPS snapshot sampler in parallel, publish its validated JPEGs as a progressive second burst, and require every candidate to pass the existing OCR, authorisation, actuation, and cooldown pipeline.

**Tech Stack:** Python 3.10+, unittest, urllib, Pillow, watchdog, Reolink HTTPS CGI, systemd.

**Spec:** `docs/superpowers/specs/2026-08-20-progressive-reolink-ocr.md`

## Global Constraints

- The first FTP recognition burst must never wait for snapshot augmentation.
- A camera trigger or snapshot must never actuate the relay directly.
- Camera credentials and session tokens must never appear in logs or committed files.
- Snapshot count, total duration, response size, queue depth, and temporary storage must be bounded.
- Failures must preserve the original FTP attempt and fail closed for access.
- Work only in `codex/dual-stream-ocr`; do not modify other agents' worktrees.

---

### Task 1: Prove the configuration contract fails before implementation

**Files:**
- Create: `tests/test_progressive_snapshot_contract.py`

**Interfaces:**
- Consumes: none.
- Produces: an executable contract for `load_reolink_snapshot_config(environment, upload_root, max_candidate_bytes=...) -> ReolinkSnapshotConfig`.

- [ ] **Step 1: Write the failing contract test**

```python
import importlib
import tempfile
import unittest
from pathlib import Path


class ProgressiveSnapshotContractTests(unittest.TestCase):
    def test_complete_private_camera_configuration_enables_two_frame_sampling(self):
        try:
            module = importlib.import_module("gate_controller.reolink_snapshots")
        except ModuleNotFoundError:
            self.fail("progressive Reolink snapshot support is missing")
        with tempfile.TemporaryDirectory() as directory:
            config = module.load_reolink_snapshot_config({
                "GATE_REOLINK_SNAPSHOT_BASE_URL": "https://192.168.10.20",
                "GATE_REOLINK_SNAPSHOT_USERNAME": "viewer",
                "GATE_REOLINK_SNAPSHOT_PASSWORD": "secret",
            }, Path(directory), max_candidate_bytes=8 * 1024 * 1024)
        self.assertTrue(config.enabled)
        self.assertEqual(2, config.candidate_count)
        self.assertEqual(2.25, config.timeout_seconds)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_progressive_snapshot_contract -v`

Expected: FAIL with `progressive Reolink snapshot support is missing` because production has no snapshot module.

### Task 2: Integrate the bounded progressive sampler

**Files:**
- Create: `gate_controller/reolink_snapshots.py`
- Modify: `gate_controller/worker.py`
- Modify: `gate_controller/__main__.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/reolink-rlc-811a.md`
- Test: `tests/test_reolink_snapshots.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `BurstCollector.add(path, received_at)` and the existing `run_worker` processing queue.
- Produces: `ReolinkSnapshotConfig`, `load_reolink_snapshot_config`, `ReolinkSnapshotClient`, and `ReolinkSnapshotSampler.request(received_at) -> bool`.

- [ ] **Step 1: Reuse the reviewed feature commit**

Run: `git cherry-pick 922a0bf6b00273229c4ad33ced24b0f9692456fb`

Expected: the progressive snapshot module, worker wiring, documentation, and comprehensive tests apply cleanly because the commit is based on `origin/master`.

- [ ] **Step 2: Verify GREEN for the contract and focused feature suite**

Run: `.venv/bin/python -m unittest tests.test_progressive_snapshot_contract tests.test_reolink_snapshots tests.test_worker tests.test_main -v`

Expected: all tests PASS; the feature tests cover private-origin validation, authentication, redirect rejection, bounded reads, sequential capture, progressive publication, cleanup, shutdown, and failure isolation.

### Task 3: Correct the installed-camera documentation

**Files:**
- Create: `docs/reolink-rlc-810a.md`
- Delete: `docs/reolink-rlc-811a.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the camera-reported model and encoding settings recorded in the spec.
- Produces: operator instructions for the fixed-lens RLC-810A and the measured snapshot path.

- [ ] **Step 1: Rename the camera guide and replace zoom assumptions**

Move the guide to `docs/reolink-rlc-810a.md`. State that the installed camera has a fixed 4 mm lens and that recognition uses software crops from 4K frames. Document Clear 3840x2160/10 fps/H.265/6144 Kbit/s, Fluent 640x360/10 fps/H.264/256 Kbit/s, and Constant frame-rate mode.

- [ ] **Step 2: Correct README links and measured timings**

Update every repository link from `docs/reolink-rlc-811a.md` to `docs/reolink-rlc-810a.md` and record the 625 ms/677 ms snapshot measurements without camera address or credentials.

- [ ] **Step 3: Verify documentation references**

Run: `rg -n "RLC-811A|reolink-rlc-811a" README.md docs/reolink-rlc-810a.md gate_controller tests`

Expected: no stale production-camera model or guide-path references remain except explicit comparative statements that distinguish the RLC-811A from the installed RLC-810A.

### Task 4: Verify and stage a safe Pi rollout

**Files:**
- Modify only at deployment time: `/etc/gate-controller.env` on the Pi.

**Interfaces:**
- Consumes: the release updater and existing root-owned camera credentials.
- Produces: two progressive 4K candidates per new FTP event, observable through redacted sampler logs.

- [ ] **Step 1: Run repository verification**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Run: `.venv/bin/python -m compileall -q gate_controller gate_media_gateway gate_media_auth`

Expected: 591 application tests pass with only documented binary-dependent skips, and compileall exits zero.

- [ ] **Step 2: Re-check production concurrency before deployment**

Read the Pi's `/opt/gate-controller-deploy/current` target and active updater/service state. Stop if the deployed SHA is no longer `a29937647f1aea41e236605f527f8b4f2c22f01a` or an updater/install operation is active.

- [ ] **Step 3: Publish through the repository's normal protected release path**

Push `codex/dual-stream-ocr` for review and merge. Do not copy an unreviewed checkout over the active release and do not bypass the updater's exact-CI requirement.

- [ ] **Step 4: Activate configuration after the release is installed**

Add the snapshot origin and credentials to `/etc/gate-controller.env` with mode 0600, set count 2, timeout 2.25 seconds, maximum bytes 4194304, and explicit self-signed TLS acceptance. Restart through systemd, verify the service is active, then confirm logs contain only redacted augmentation outcome/count/reason/duration fields.
