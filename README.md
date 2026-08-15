# Vehicle Gate Controller

The controller watches completed JPEG uploads from a local camera, recognises
plates, applies a fail-closed local policy, and activates a Pi relay. Recognition
never waits for optional Supabase, event delivery, or audio services. A
cloud-managed plate snapshot remains usable only for its configured bounded
staleness window, after which authorization fails closed.

## Safety Model

- Exact normalised authorised plates may open the gate; fuzzy matches require
  two high-confidence frames and one recognised OCR-confusion substitution.
- OCR, parsing, network, cloud, and audio failures leave the gate closed.
- A durable global activation marker is committed before GPIO is energized;
  optional event delivery remains off the recognition path.
- Remote commands are short-lived, claimed through Supabase RPC, acknowledged
  with `completed`, `failed`, or `expired`, and use a persisted idempotency key.
  Open-command expiry is checked again under the relay lock immediately before
  GPIO activation; an expired command is finalized without pulsing the relay.
- The image processor and every configured background worker are supervised.
  Unexpected exit or fatal failure terminates the process so systemd restarts it.
- The browser never reaches the Pi, GPIO relay, camera, RTSP stream, or FTP
  service directly. It uses authenticated Supabase RPC only.

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
the installer. It contains the Plate Recognizer token and, only when remote
control is enabled, the Pi's Supabase service-role key. Never commit that file
or put its values in a systemd unit.

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
service waits for `network-online`,
restarts failed processes after five seconds, limits restart bursts, and can
write only beneath `/var/lib/gate-controller`. Its local camera upload
directory must match `GATE_WATCH_DIRECTORY`.

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
the conditional `RPi.GPIO` requirement applies on supported Pi architectures.
Install the relay board vendor library if a different relay adapter is used.

## Optional Control Plane

Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `GATE_CONTROLLER_ID` to
enable the Pi control plane. The browser calls `request_gate_command`; the Pi
uses only `claim_gate_command`, `complete_gate_command`, and
`update_controller_status`. Supported command values are `open_gate` and
`play_prompt`. Prompt files are fixed by the two `GATE_PROMPT_*` settings;
request payloads cannot select arbitrary commands or file paths. `SUPABASE_URL`
must be an absolute HTTPS URL, including in local and test configuration; the
controller rejects it before constructing service-role authentication headers.

The controller ID defaults to `primary`, matching the web app. The same
background service refreshes `public.plates` through Supabase REST every 30
seconds, atomically updates the local snapshot, applies revocations without a
restart, and fails closed when a cloud-managed snapshot is older than
`GATE_AUTHORISATION_MAX_STALENESS_SECONDS`. Recognition only reads the in-memory
snapshot and never waits for Supabase.

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

## Camera Deployment

See [RLC-811A deployment and night calibration](docs/reolink-rlc-811a.md).

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
