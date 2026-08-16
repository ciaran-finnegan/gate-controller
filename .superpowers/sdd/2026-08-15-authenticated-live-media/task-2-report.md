# Task 2 Report: Isolated Pi Media Gateway and Token Verifier

## Status

Implemented on `codex/media-gateway` from `2fddf9c`.

- Added a stdlib-only loopback MediaMTX HTTP authorization sidecar.
- Enforced strict duplicate-key JSON parsing, an 8 KiB request cap, opaque
  bounded request IDs, only `200`/`401` authorization responses, and no request
  or token logging.
- Validated canonical HS256 JWTs with timing-safe HMAC comparison and the exact
  session claim contract: expiry, issue time, `read`, `gate`, and `primary`.
- Added MediaMTX loopback configuration, isolated systemd units, and a local
  archive-only installer that requires an approved version/architecture SHA-256
  map before installation and verifies the installed version.
- Added atomic nonsecret media capabilities, conservative heartbeat ingestion,
  and deployment guidance. Talkback remains `hardware_unverified`.
- The media package has no relay, actuation, PiRelay, or controller-state import
  or call path.

## Verification

- `python3 -m unittest tests.test_media_auth tests.test_media_deployment -v`: 18 passed.
- `python3 -m compileall -q gate_media_auth gate_controller deployment`: passed.
- `bash -n deployment/install-media.sh` and `bash -n deployment/install.sh`: passed.
- `git diff --check`: passed.

## Environment Limitation

The required combined command cannot import `tests.test_main` under Homebrew
Python 3.14 because the baseline lacks `requests`:

```text
ModuleNotFoundError: No module named 'requests'
```

Full `unittest discover` ran 123 tests and retained the known seven baseline
import errors: `requests` for `test_authorisation`, `test_control_plane`, and
`test_main`; `Pillow` for `test_images`, `test_outbox`, and `test_processor`;
and `watchdog` for `test_worker`. No dependencies were installed and no network
was accessed.

## Operational Concerns

- No MediaMTX archive or checksum is committed. Operators must physically fetch
  and independently approve an architecture-specific archive and SHA-256 map.
- Physical camera video/listen acceptance and the Reolink backchannel probe are
  still required before setting any verified capability. Talkback must remain
  unavailable until that separate probe succeeds.
