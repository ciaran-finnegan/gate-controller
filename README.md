# Vehicle Gate Controller

The controller watches completed JPEG uploads from a local camera, recognises
plates, applies a fail-closed local policy, and activates a Pi relay. Recognition
never waits for optional Cloudflare delivery, event delivery, or audio services. A
cloud-managed plate snapshot remains usable only for its configured bounded
staleness window, after which authorization fails closed.

## Safety Model

- Exact normalised authorised plates may open the gate; fuzzy matches require
  two high-confidence frames and one recognised OCR-confusion substitution.
- OCR, parsing, network, cloud, and audio failures leave the gate closed.
- A durable global activation marker is committed before GPIO is energized;
  optional event delivery remains off the recognition path.
- Remote commands reach a loopback-only command server through Cloudflare
  Tunnel and Access. They are short-lived, controller-bound, and use a
  persisted idempotency key. Expiry is checked again under the relay lock
  immediately before GPIO activation; an expired command never pulses the relay.
- The image processor and every configured background worker are supervised.
  Unexpected exit or fatal failure terminates the process so systemd restarts it.
- The browser never reaches the Pi, GPIO relay, camera, RTSP stream, or FTP
  service directly. Cloudflare Access protects the tunnelled command endpoint.

## Install And Update

Python **3.10 or newer** is required. The launcher and systemd unit reject
older interpreters before the controller starts.

Production releases use an atomic managed layout under
`/opt/gate-controller-deploy`. Each immutable commit has its own virtual
environment and `/opt/gate-controller-deploy/current` points to the active
release. Persistent state and configuration remain outside that tree. Prepare
the root-readable environment file and configure the dedicated `ftp-user`
account first. The installer creates the controller accounts, persistent state
directory, and shared upload directory when they are absent:

```sh
sudo install -m 0600 -o root -g root .env.example /etc/gate-controller.env
```

Edit `/etc/gate-controller.env` with real credentials and paths before running
the installer. It contains the Plate Recognizer token and the Cloudflare Access
service-token credentials when the Cloudflare control plane is enabled. Never
commit that file or put its values in a systemd unit.

Configure branch protection on `master` to require the three stable checks listed
in [the deployment guide](docs/deployment.md). Then bootstrap from a clean
checkout with the explicit update-enablement flag:

```sh
git clone https://github.com/ciaran-finnegan/gate-controller.git /tmp/gate-controller-bootstrap
cd /tmp/gate-controller-bootstrap
git checkout master
sudo deployment/install.sh --source "$PWD" --enable-updates
```

The installer does not overwrite `/etc/gate-controller.env`, existing files
beneath `/var/lib/gate-controller`, or the legacy `/opt/gate-controller`
checkout. It configures state/upload directory ownership and copies the legacy
SQLite database only when the persistent destination is absent. The controller
service requires `systemd-time-wait-sync.service` to complete before image or
command handling starts, restarts failed processes after five seconds, limits
restart bursts, and can write only beneath `/var/lib/gate-controller`. Its
local camera upload directory must match `GATE_WATCH_DIRECTORY`.

Automatic deployment is outbound and pull-based. Every five minutes the Pi
checks the exact commit at the configured release branch (`master` by default) and
adopts it only after that SHA's complete
`Gate Controller CI` push workflow succeeds. Network, GitHub, rate-limit,
dependency, staging, or verification failures leave the running release
untouched; activation failures restore the previous managed symlink. The root
updater helper and all systemd units are fixed copies installed only by explicit
bootstrap, so changes to them require a deliberate bootstrap refresh rather
than an automatic release.
Tailscale is optional break-glass administration only and is not required for
gate operation or updates. See [Raspberry Pi deployment](docs/deployment.md)
for migration, logs, retention, manual rollback, and optional private-token
configuration.

The bundled `PiRelay.py` adapter additionally needs Raspberry Pi GPIO support;
the conditional `rpi-lgpio` package provides its `RPi.GPIO`-compatible API on
supported Pi architectures, including Raspberry Pi 5. Do not install it in the
same virtual environment as the original `RPi.GPIO` package.
Install the relay board vendor library if a different relay adapter is used.

## Cloudflare Control Plane

Cloudflare is the active remote-control path. Set
`GATE_CLOUDFLARE_API_URL`, `GATE_CLOUDFLARE_ACCESS_CLIENT_ID`,
`GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET`, and `GATE_CONTROLLER_ID` together to
enable Cloudflare-backed plate refresh, status reporting, and event delivery.
The API URL is the HTTPS Worker origin; the client ID and secret are the
Cloudflare Access service token sent only from the controller. The controller ID
defaults to `primary`. Plate snapshots are atomically applied to the local cache
and fail closed when older than
`GATE_AUTHORISATION_MAX_STALENESS_SECONDS`; recognition only reads that in-memory
snapshot and never waits for a network request.

Remote commands are accepted only by the loopback command server hosted inside
the main controller process at
`POST /commands`, exposed through the authenticated Cloudflare Tunnel. Supported
commands are `open_gate` and `play_prompt`; fixed `GATE_PROMPT_*` settings map
prompt keys to local files, and request payloads cannot select arbitrary shell
commands or file paths.

Set `GATE_OUTBOX_URL` only for an authenticated server-side event endpoint and
set `GATE_OUTBOX_BEARER_TOKEN` to the secret it validates. Do not point this at
a browser route or expose the token to the web app. The versioned JSON contains
the local event ID, timestamps, source, decision, open state, plate match, and
confidence. Schema version 2 also persists the controller ID and, when evidence
is available, `image_sha256`. The same digest appears in the embedded JPEG
object. Each request sends an `Idempotency-Key` equal to the SHA-256 digest of
`<controller_id>:<local_event_id>`.
Events are first stored in SQLite, then retried in a background worker. A
failed delivery remains queued and does not delay image processing or relay
activation.

The Worker ingests events at `/api/controller/events` using the event
idempotency key. It must store any accepted evidence in private R2 under its
digest and apply the site-approved R2 lifecycle retention policy. The Pi only
removes its local evidence after the Worker returns a 2xx response; R2 retention
is owned by the Cloudflare deployment, not by the controller process.

Before an event is queued, its best-ranked JPEG is EXIF-normalised, converted
to RGB, bounded to 1280 pixels and 512KB, and atomically written as
`event-evidence/<sha256>.jpg` beside the SQLite database. The private spool never
stores a mutable camera path in the network payload. Retries verify and reuse
the exact queued bytes. Missing or corrupt expected evidence leaves the event
pending instead of substituting another image. After the receiver confirms a
2xx response, SQLite retains the digest and completion timestamp while the
spool file is unlinked once no other pending event references it. Startup also
removes completed leftovers from an interrupted cleanup. During a prolonged
receiver outage, pending evidence can grow by at most 512KB per distinct image;
monitor the state filesystem together with outbox depth.

Image work is bounded and freshness checked. Startup JPEGs older than
`GATE_MAX_IMAGE_AGE_SECONDS`, coalesced queue entries, OCR failures, no-plate
results, authorization errors, and worker exceptions are stored as denied/error
events where SQLite remains available. `GATE_DECISION_TIMEOUT_SECONDS` is the
overall event-decision budget, including frame ranking and content hashing; each
Plate Recognizer request also uses a shorter bounded timeout and exact matches
still stop the burst immediately. `GATE_MAX_BURST_CANDIDATES` defaults to 8 and
`GATE_MAX_CANDIDATE_IMAGE_BYTES` defaults to 8 MiB. Startup rejects values above
the hard safety ceilings of 16 candidates or 16 MiB per candidate. Within that
bounded set, the newest candidates are retained and then ranked by image quality.
The Python entry point and production systemd unit both use a 200 ms
completed-upload quiet window. This is calibrated from the latest ten production
camera recognition events: each contained one 3840x2160 frame, with no second
frame arriving inside the previous 500 ms window. The collector still coalesces
and ranks completed frames inside 200 ms. Future camera settings that emit frames
farther apart require fresh telemetry and calibration. CLI overrides must be
finite and between 100 ms and 2 seconds.

## Camera Deployment

See [RLC-811A deployment and night calibration](docs/reolink-rlc-811a.md).
The Pi performance harness is documented in
[Pi Cloudflare performance validation](docs/pi-cloudflare-performance.md). It
is intentionally deferred until reliable on-site network access is available.

Make and colour returned by OCR are reserved for telemetry and operator review;
they are intentionally not gate authorization factors. This service does not
implement live video or two-way audio. It only exposes camera/prompt capability
status and can play the two configured local WAV prompts. Camera status reports
whether the upload directory is configured and ready separately from last-upload
activity. It reports the camera connection as unprobed instead of inferring a
connection failure from a quiet vehicle trigger. Relay status is based on measured
initialization and actuation outcomes.

## Verification

Run the unit suite with `python3 -m unittest discover -s tests -v`, compile with
`python3 -m compileall gate_controller deployment tests`, and validate shell
entry points with `bash -n deployment/install.sh` and `sh -n file_monitor.sh`.
