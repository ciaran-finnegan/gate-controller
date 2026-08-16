# Task 2 Report: Isolated Pi Media Gateway and Token Verifier

## Status

Completed on `codex/media-gateway` from starting HEAD `2fddf9c`.

- MediaMTX receives the camera source only through its dedicated root-owned
  `/etc/gate-media-gateway.env` as `MTX_PATHS_GATE_SOURCE`; the committed YAML
  contains neither source interpolation nor credentials. The verifier receives
  only `/etc/gate-media-auth.env`, so MediaMTX never receives the HMAC secret and
  the verifier never receives camera credentials.
- RTSP, API, metrics, WebRTC HTTP, and default WebRTC UDP/TCP transports bind to
  loopback. A root-rendered nginx include exposes only exact WHEP creation and
  bounded teardown paths with one validated HTTPS CORS origin.
- The stdlib auth sidecar requires the exact ten-field current MediaMTX HTTP auth
  schema, `protocol == "webrtc"`, action `read`, path `gate`, controller
  `primary`, canonical short-lived HS256 claims, and timing-safe signature
  comparison. It retains strict duplicate-key parsing, the 8 KiB request cap,
  exact `200`/`401` responses, and token-safe logging.
- Gateway readiness uses a bounded loopback API response and requires the `gate`
  path to be ready/available with nonempty bounded tracks.
- Capability ingestion uses one nonblocking, no-follow open, validates `fstat`
  regular-file type and byte bounds, strict JSON, timestamps, and capability
  semantics, and fails closed for FIFO, symlink, malformed, oversized, stale,
  and future-dated inputs. Talkback is always forced to
  `hardware_unverified`.
- The installer stages one root-owned private stable archive, hashes and extracts
  that same file, rejects symlinked archive directories and nonregular or
  symlinked executables, checks the candidate version before atomic replacement,
  and disables both media services on every trapped failure. It requires an
  operator-supplied version/architecture checksum map and performs no download.
- Both services use dedicated non-root, non-GPIO users with positive state/GPIO
  `InaccessiblePaths`, execution allowlists, resource limits, restart limits,
  and no write path to controller state. The ordinary updater copies fixed
  bootstrap references but cannot install the MediaMTX binary.
- No relay, actuation, `PiRelay`, hold-open, or second-relay code was changed or
  made reachable. Capability publication and heartbeat ingestion remain
  best-effort and cannot affect the main worker or heartbeat.

## Commits

- `dea5ac9 feat: add isolated gate media gateway`
- `3914752 fix: harden isolated media gateway`

## Tests

- `python3 -m unittest tests.test_media_auth tests.test_media_deployment -v`:
  35 passed.
- `python3 -m unittest tests.test_deployment tests.test_runtime -v`: 18 passed,
  one skipped because the Linux `flock` command is unavailable on macOS.
- `python3 -m unittest discover -s tests -v`: 140 entries; 132 passed, seven
  pre-existing import errors, and one Linux-only skip. The import errors are
  exactly `requests` for `test_authorisation`, `test_control_plane`, and
  `test_main`; Pillow (`PIL`) for `test_images`, `test_outbox`, and
  `test_processor`; and `watchdog` for `test_worker` under Homebrew Python
  3.14.6.
- `python3 -m compileall -q gate_media_auth gate_controller deployment
  tests/test_media_auth.py tests/test_media_deployment.py`: passed.
- `bash -n deployment/install-media.sh deployment/install.sh`: passed.
- `git diff --check`: passed.

## Concerns

- No dependencies were installed and no network, download, deploy, push, or
  merge operation was performed. The seven baseline dependency import errors
  therefore remain.
- `systemd-analyze`, nginx, and MediaMTX are unavailable in this macOS
  environment, so Linux unit loading, nginx config loading, and live MediaMTX
  integration were not executed here.
- No release archive or checksum is committed. Operators must physically fetch
  and independently approve the exact architecture-specific archive and SHA-256
  map before running the dedicated installer.
- Physical Pi camera video/listen acceptance, TURN or exact private transport
  binding, and RLC-811A backchannel validation remain deployment work. Talkback
  remains unavailable until that hardware validation is completed.
