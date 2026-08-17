# Controller Final Review Post-Follow-Up Fix Report

Base commit: `fbf56b7`

Implementation commit: `33e25d1bbf55ede74edacb0206c5de0db21cb729` (`fix: harden controller cutover safety gates`)

## Scope

- Added a required `systemd-time-wait-sync.service` dependency before the controller can start handling direct commands.
- Made updater legacy-service probes fail closed on execution errors, timeouts, and unrecognised results.
- Claimed prompt commands durably before starting audio and retained an indeterminate claim if finalisation fails.
- Removed every tracked `.pyc` file.

## RED Evidence

Before implementation, the focused regression command failed four tests:

```sh
.venv/bin/python -m unittest \
  tests.test_deployment.SystemdTrustBoundaryTests.test_command_server_is_owned_by_the_time_synchronised_main_service \
  tests.test_updater.ActivationRecoveryTests.test_legacy_probe_recognises_confirmed_inactive_and_not_found_states \
  tests.test_updater.ActivationRecoveryTests.test_legacy_probe_timeout_raises_update_error \
  tests.test_updater.ActivationRecoveryTests.test_activation_aborts_before_retiring_legacy_service_on_partial_probe \
  tests.test_command_server.DirectCommandExecutorTests.test_prompt_finalization_failure_leaves_a_restart_safe_indeterminate_claim -v
```

Observed failures:

- `file-monitor.service` did not require or order after `systemd-time-wait-sync.service`.
- A timed-out `systemctl` probe did not raise `UpdateError`.
- A successful enabled probe plus a timed-out active probe still allowed activation to retire the legacy service.
- A prompt finalisation failure returned `completed`, leaving a retry free to replay audio.

An additional RED run demonstrated that the probe still used `systemctl --quiet`, which suppresses the state output needed to classify confirmed negative results. The flag was removed and probe-state coverage added.

## GREEN Evidence

Focused verification passed:

```sh
.venv/bin/python -m unittest tests.test_updater tests.test_command_server tests.test_deployment -v
```

Result: 78 tests passed, with 1 platform-specific `flock` skip.

Required final verification passed:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q gate_controller deployment tests scripts
bash -n deployment/install.sh
sh -n file_monitor.sh
git diff --check
```

Result: 382 tests passed, with 2 documented platform-specific skips. Compilation, both shell syntax checks, and the whitespace check exited successfully.

## Changed Files

- `file-monitor.service`
- `deployment/gate_controller_updater.py`
- `gate_controller/command_server.py`
- `tests/test_deployment.py`
- `tests/test_updater.py`
- `tests/test_command_server.py`
- `README.md`
- `docs/deployment.md`
- Removed `__pycache__/db_utils.cpython-311.pyc`
- Removed `__pycache__/logger.cpython-311.pyc`
- Removed `__pycache__/s3_utils.cpython-311.pyc`

## Concerns

No code-level concerns remain. Live Raspberry Pi/systemd and Cloudflare checks were intentionally not run because this fix wave permits local, non-actuating verification only. The fixed systemd unit still requires the documented bootstrap refresh before deployment.
