# Task 2 Report: Isolated Pi Media Gateway and Token Verifier

## Status

Completed on `codex/media-gateway` from starting HEAD `2fddf9c`.

- MediaMTX is pinned to `1.19.3`. Its committed effective listener surface
  disables RTSP service/transports, multicast transport, RTMP, HLS, SRT, MoQ,
  playback, and pprof; every inherited HTTP or transport address is explicitly
  loopback. API, metrics, auth, and WHEP HTTP signaling remain loopback-only.
- A non-root launcher fails closed before executing MediaMTX unless its exact
  effective environment contains a valid RTSP camera source, matching explicit
  non-wildcard/non-loopback ICE UDP and TCP binds, an exact
  `MTX_WEBRTCADDITIONALHOSTS_0` match for that bind IP, and complete
  MediaMTX-side TURN configuration. nginx exposes only exact WHEP creation and
  bounded teardown routes with one exact HTTPS CORS origin; it does not carry
  media.
- The pinned 1.19.3 config uses the real `moqHTTP2Address` and
  `moqHTTP3Address` fields on loopback while MoQ remains disabled. A hermetic
  fixture derived from the pinned `Conf` field surface rejects unknown global
  keys and probes the local-listener/additional-host validation invariant.
- MediaMTX receives camera and TURN credentials only through the root-owned
  `/etc/gate-media-gateway.env`; the verifier receives the HMAC and capability
  flags only through `/etc/gate-media-auth.env`. Strict file parsing rejects
  whitespace, duplicate or unknown assignments, cross-service secrets, invalid
  UTF-8/HMAC byte length, invalid RTSP/ICE/TURN values, and incomplete settings.
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
- The installer accepts only version `1.19.3`. Its checksum map must be a
  root-owned `0600` regular nonsymlink beneath an owner-controlled directory;
  it is bounded and parsed from the same stable opened descriptor before any
  candidate binary execution. The installer stages one root-owned private
  stable archive, hashes and extracts that same file, rejects symlinked archive
  directories and nonregular or symlinked executables, checks the candidate
  version before atomic replacement, and disables both media services on every
  trapped failure. It performs no download and requires operator-approved SHA
  values.
- Both services use dedicated non-root, non-GPIO users with positive state/GPIO
  `InaccessiblePaths`, including exact `-/opt/gate-controller`, execution
  allowlists, resource limits, restart limits, and no write path to controller
  state. The ordinary updater copies fixed bootstrap references but cannot
  install the MediaMTX binary.
- No relay, actuation, `PiRelay`, hold-open, or second-relay code was changed or
  made reachable. Capability publication and heartbeat ingestion remain
  best-effort and cannot affect the main worker or heartbeat.

## Commits

- `dea5ac9 feat: add isolated gate media gateway`
- `3914752 fix: harden isolated media gateway`
- `440c9b5 fix: harden isolated MediaMTX deployment`
- `dd890b9 fix: advertise pinned MediaMTX ICE host`

## Tests

- Strict red-green cycles covered the missing trusted validator, pinned config
  surface, launcher fail-closed behavior, unit/bootstrap restrictions, auth
  runtime validation, CRLF rejection, and effective TURN whitespace rejection
  before the corresponding implementations were added.
- The final P1 cycle first produced four expected failures for the omitted
  additional-host contract and invalid MoQ field, then passed all four after
  the minimal validator/config change.
- `python3 -m unittest tests.test_media_auth tests.test_media_deployment -v`:
  46 run; 45 passed and the optional real MediaMTX 1.19.3 binary probe skipped
  because no verified binary was available.
- `python3 -m unittest tests.test_deployment tests.test_runtime -v`: 18 passed,
  one skipped because the Linux `flock` command is unavailable on macOS.
- `python3 -m unittest discover -s tests`: 151 entries; 142 passed, seven
  pre-existing import errors, and two skips (the Linux-only test and unavailable
  verified MediaMTX binary). The import errors are exactly `requests` for
  `test_authorisation`, `test_control_plane`, and `test_main`; Pillow (`PIL`)
  for `test_images`, `test_outbox`, and `test_processor`; and `watchdog` for
  `test_worker` under Homebrew Python 3.14.6.
- `python3 -m compileall -q gate_media_config.py gate_media_gateway
  gate_media_auth gate_controller deployment tests/test_media_auth.py
  tests/test_media_deployment.py`: passed.
- `bash -n deployment/install-media.sh deployment/install.sh`: passed.
- `python3 -m json.tool tests/fixtures/mediamtx-v1.19.3-schema.json`: passed.
- `git diff --check`: passed.

## Concerns

- No dependencies were installed and no release asset, binary, or checksum was
  downloaded. No deploy, push, or merge operation was performed. The seven
  baseline dependency import errors therefore remain.
- The official pinned source and release metadata were inspected, but shell
  network permission for fetching the signed release assets was unavailable and
  no local MediaMTX binary existed. The committed optional real-binary config
  probe therefore skipped; the hermetic 1.19.3 schema/invariant probe passed.
- `systemd-analyze` and nginx are unavailable in this macOS environment, so
  Linux unit loading, nginx config loading, and live integration were not
  executed here.
- No release archive or checksum is committed. Operators must physically fetch
  and independently approve the exact `1.19.3` architecture-specific archive
  and SHA-256 map before running the dedicated installer.
- Video readiness must remain disabled until the configured ICE binds and TURN
  service pass an end-to-end test from a real non-loopback client, including a
  relay candidate and WHEP teardown. Physical Pi camera video/listen acceptance
  and RLC-811A backchannel validation remain deployment work. Talkback remains
  `hardware_unverified` until that hardware validation is completed.
