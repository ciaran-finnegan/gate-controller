# Raspberry Pi Deployment

The production controller and its updater are deliberately independent of
Tailscale. Normal gate operation uses only local files, local GPIO, and the
configured outbound services. Updates use outbound HTTPS and Git from the Pi;
an unavailable network, GitHub, or package index leaves the active release
running unchanged.

## Prerequisites

- Raspberry Pi OS with systemd, Git, `flock`, `systemd-analyze`, the GPIO group,
  and Python 3.10 or newer including `venv` support.
- A working `/etc/gate-controller.env`, owned by `root:root` with mode `0600`.
  The installer refuses to create or replace this file. Configure
  `GATE_CLOUDFLARE_API_URL`, `GATE_CLOUDFLARE_ACCESS_CLIENT_ID`, and
  `GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET` as one Access-authenticated controller
  API group; a partial remote-control configuration fails bootstrap.
- A dedicated `ftp-user` already created by the selected FTP server setup. The
  installer deliberately does not create or assign credentials to an FTP
  account.
- The persistent controller path at `/var/lib/gate-controller`. Bootstrap
  rejects a symlink or non-directory at this path or its `uploads` child before
  changing accounts or activation state.
- The three CI checks below configured as required branch-protection checks on
  `master` before automatic updates are enabled.

Required check names:

- `Gate controller tests (Python 3.10)`
- `Gate controller tests (Python 3.11)`
- `Deployment syntax checks`

This repository deploys `master` by default. A different branch may be selected
with `GATE_UPDATE_BRANCH` in both the bootstrap environment and the root-owned
`/etc/gate-controller-updater.env`; protect exactly the branch the Pi follows.

Require pull requests and these checks before merge. If GitHub merge queue is
enabled, the workflow also runs for `merge_group`. The Pi independently waits
for the complete `Gate Controller CI` push workflow to succeed for the exact
40-character commit currently at `master`; branch protection is an additional
guard, not a replacement for the Pi-side check.

## One-Time Bootstrap

Use a fresh clean checkout so the running legacy files at
`/opt/gate-controller` are not changed during migration:

```sh
git clone https://github.com/ciaran-finnegan/gate-controller.git /tmp/gate-controller-bootstrap
cd /tmp/gate-controller-bootstrap
git checkout master
sudo deployment/install.sh --source "$PWD" --enable-updates
```

Nothing is enabled merely by cloning the repository. The installer requires the
explicit `--enable-updates` flag and refuses a dirty checkout or a non-commit
source. It stages the source commit, creates a release-local virtual environment,
installs requirements, runs all unit and syntax checks, validates the systemd
units, and only then changes the application service.

The installed layout is:

```text
/opt/gate-controller-deploy/
  current -> releases/<commit-sha>
  pending-activation.json             present only during activation/recovery
  releases/<commit-sha>/.venv/
/usr/local/libexec/gate-controller/
  gate-controller-updater.py          fixed root-owned helper
/etc/systemd/system/
  file-monitor.service                fixed non-root application policy
  gate-controller-updater.service     fixed root sandbox policy
/var/lib/gate-controller/             persistent state
  uploads/                             FTP writes; controller reads/watches
  event-evidence/                      private pending event JPEGs
/etc/gate-controller.env              application configuration and secrets
```

`event-evidence` is content-addressed and private to the service. Each file is
at most 512KB. A confirmed event delivery removes its JPEG after the SQLite row
is marked complete; interrupted cleanup is reconciled on startup. Pending files
are never pruned before receiver success, so a sustained event-endpoint outage
can grow this directory by up to 512KB per distinct queued image. Monitor free
space and the controller's outbox queue depth together.

Bootstrap creates a separate unprivileged `gate-controller-build` account for
candidate dependency installation and tests. It has no GPIO group membership or
access to controller state. Verified release files are returned to root ownership
before activation.

Before granting the build account access to candidate files, bootstrap copies
the updater helper and systemd units into a root-owned, read-only handoff. Fixed
trust anchors are installed only from that handoff.

Bootstrap installs the updater helper and application/updater systemd units as
fixed root-owned trust anchors outside the managed releases. The timer always
executes `/usr/local/libexec/gate-controller/gate-controller-updater.py`; it
never executes updater code through `current`. Automatic releases do not replace
either service unit. Changes to the helper, application service policy, updater
sandbox, or timer remain inactive until an operator deliberately reruns this
bootstrap command from a clean protected `master` checkout. Bootstrap backs up and
restores the prior fixed helper and units if refreshed startup fails.
Rerunning bootstrap for an already-staged `master` SHA is allowed specifically so
these fixed trust anchors can be refreshed without manufacturing a new release.

After merging a change to `file-monitor.service`, the updater service, or the
fixed updater helper, automatic application updates are not enough. Rerun the
bootstrap command above once from the merged, clean `master` checkout. This is
required for the relay startup/stop safety hooks in this release.

The stop hook provides a best-effort immediate relay-off action when systemd can
observe the daemon exiting, including a forced process kill. Software cannot
de-energize GPIO during a kernel failure or unstable power. Use a pulse-limited
hardware relay or monostable timer for a fail-safe physical upper bound.

## FTP Upload Ownership

Bootstrap adds `ftp-user` to the `gate-controller` group, configures
`/var/lib/gate-controller` as `gate-controller:gate-controller` mode `0710`, and
creates `uploads` as `ftp-user:gate-controller` mode `2770`. During activation it
changes the FTP account home to that uploads directory, restoring the previous
home if activation rolls back. It then checks that the FTP account can traverse
state and write uploads and that the application account can read and watch
uploads. Any failed access check stops bootstrap. FTP daemons with an explicit
`local_root` must point it at the same directory.

Configure the FTP daemon to create group-readable files. For vsftpd, use a local
umask equivalent to `0007`; apply the corresponding owner/group and umask policy
for another daemon. Keep `GATE_WATCH_DIRECTORY=/var/lib/gate-controller/uploads`
in `/etc/gate-controller.env`.

The legacy `/opt/gate-controller` directory is retained. If its old database is
present at `/opt/gate-controller/data/gate-controller-database.db` and the new
persistent database is absent, bootstrap copies it once to
`/var/lib/gate-controller/gate-controller.db`. An existing persistent database
is never replaced. The legacy `authorised_licence_plates.csv` is likewise copied
once to `/var/lib/gate-controller/authorised_licence_plates.csv`; an existing
persistent plate snapshot is never replaced.

After bootstrap, verify both services:

```sh
sudo systemctl status file-monitor.service
sudo systemctl status gate-controller-updater.timer
sudo systemctl list-timers gate-controller-updater.timer
sudo journalctl -u gate-controller-updater.service -n 100 --no-pager
```

## Cloudflare Tunnel

Cloudflare Tunnel exposes only the loopback command endpoint and the hardened
nginx WHEP gateway. Copy `deployment/cloudflared/gate-controller-tunnel.yml` to the Pi,
replace the example tunnel UUID, credentials path, and hostnames with the values
created in Cloudflare, then store its credentials JSON at the configured
root-owned path. Do not add ingress rules for the controller database, GPIO,
MediaMTX API or metrics, the media authorization sidecar, or SSH. The final
catch-all `http_status:404` rule is required.

Protect `gate-command.example.com` with a Cloudflare Access application that
accepts only the Worker service token used for direct commands. Apply the
separate human/media Access policy from the UI deployment to
`gate-media.example.com`.

The direct command path is `POST /commands` on the command hostname. The
tunnel forwards it only to `127.0.0.1:8765`; it does not expose a general Pi
HTTP service. Verify the Access application, service-token policy, and Worker
request path as a deployment smoke check. Do not run live Cloudflare account or
policy commands from this repository or installer.

Before installing or starting the tunnel, validate its ingress rules and confirm
the command hostname chooses the loopback command service:

```sh
sudo cloudflared tunnel ingress validate --config /etc/cloudflared/gate-controller-tunnel.yml
sudo cloudflared tunnel ingress rule https://gate-command.example.com --config /etc/cloudflared/gate-controller-tunnel.yml
```

The loopback command server runs inside `file-monitor.service`, sharing the
main process's `ActuationCoordinator`, relay, and local store. The service
requires `systemd-time-wait-sync.service`, so it does not begin image or command
handling until the clock reports synchronized. It binds only `127.0.0.1:8765`
and uses `GATE_CONTROLLER_ID` (default `primary`). Verify it with:

```sh
sudo systemctl status file-monitor.service
curl --fail-with-body http://127.0.0.1:8765/not-found || test $? -eq 22
```

Run `cloudflared` with the validated configuration through the operator-managed
Cloudflare package/service workflow. Do not run Cloudflare account commands or
create tunnel credentials from the controller installer.

## Cloudflare Event Ingest And Retention

The controller posts authorized-plate refreshes, heartbeats, and queued events
to the HTTPS Worker origin in `GATE_CLOUDFLARE_API_URL`, authenticated with the
two Access service-token variables. Event ingest is `POST
/api/controller/events`; it is idempotent by the controller event key and may
include a bounded JPEG whose SHA-256 digest is in the event payload.

The Worker deployment owns evidence retention. Store accepted JPEGs only in a
private R2 bucket under the verified digest, keep bucket access limited to the
Worker and approved operators, and configure the site's approved R2 lifecycle
retention before accepting production traffic. Confirm that event metadata and
the R2 object share the digest before considering ingest healthy. The Pi keeps
its local evidence until it receives a 2xx response and never performs R2
credentials, deletion, or lifecycle management directly.

## Controller Cutover And Decommission

Before decommissioning the previous remote-control release, deploy the Worker,
R2 bucket, Access policies, service token, and validated tunnel routes. Install
the Cloudflare-enabled controller release, verify plate refresh, heartbeat,
event ingest, and a non-actuating command path, then retain the previous managed
release and its rollback-ready configuration through acceptance.

If any acceptance check fails, restore the previous release before removing its
remote-control configuration or decommissioning the prior service. Rollback is
the managed-release procedure below; do not attempt it by manually changing a
live tunnel or editing SQLite state.

## Update And Rollback Behavior

The timer starts five minutes after boot and no more frequently than once every
five minutes, with a small randomized delay. When the installed commit is still
current, each poll uses one GitHub API request. While a newer commit is awaiting
CI it uses two, remaining below the public unauthenticated limit of 60 requests
per hour.

Each run is serialized by `/usr/bin/flock` using
`/run/gate-controller-updater/update.lock`. A candidate is fetched by immutable
SHA into a temporary directory, checked out detached, and verified before the
`current` symlink is touched. Git checkout, package installation, tests,
compilation, and candidate shell syntax checks all run as the unprivileged
`gate-controller-build` account. The root helper only prepares ownership,
durably records activation, switches the managed symlink, and asks systemd to
restart the fixed service.

Bootstrap acquires that same non-blocking lock before staging or refreshing
trust anchors, so it cannot race the timer-driven updater.

Before switching, the updater atomically writes and fsyncs
`pending-activation.json` with the candidate and previous release SHAs. It then
atomically replaces and fsyncs `current`, restarts the fixed application service,
and checks that it remains active every second for the configured health window.
The marker is removed and that removal is fsynced only after confirmed health.

Every updater start reconciles an existing marker before contacting GitHub. If
`current` is the candidate, it restarts and health-checks the candidate rather
than accepting the matching SHA. Failed health durably rolls back to the recorded
previous release and confirms it. If `current` is already the previous release,
the updater confirms that deterministic rollback state. Malformed records,
symlinked records/releases, missing releases, or any unrelated `current` target
fail closed and leave the marker present.

The updater systemd sandbox makes the host filesystem read-only except for the
managed deployment and private runtime-lock directories. It has no device access
or privilege escalation and retains only the capabilities needed to change
release ownership and drop candidate commands to `gate-controller-build`.

The active release plus two prior releases are retained by default. Pruning only
runs after successful activation and only removes inactive directories whose
names are full commit SHAs.

To stop automatic adoption without stopping the gate controller:

```sh
sudo systemctl disable --now gate-controller-updater.timer
```

To retry a poll manually:

```sh
sudo systemctl start gate-controller-updater.service
sudo journalctl -u gate-controller-updater.service -n 100 --no-pager
```

For manual rollback to a retained managed release, disable the timer first,
select a SHA directory, atomically replace only `current`, and restart the fixed
application service:

```sh
sudo systemctl disable --now gate-controller-updater.timer
release=/opt/gate-controller-deploy/releases/REPLACE_WITH_FULL_SHA
sudo test -d "$release"
sudo ln -s "$release" /opt/gate-controller-deploy/current.manual
sudo mv -Tf /opt/gate-controller-deploy/current.manual /opt/gate-controller-deploy/current
sudo systemctl restart file-monitor.service
sudo systemctl is-active file-monitor.service
```

Replace the example SHA before running those commands. The updater never pulses
the relay as a deployment health check; health means the supervised controller
process remained active, not that a physical gate cycle was attempted.

## Dependency Pinning And Package Trust

`requirements.txt` pins direct runtime packages, Requests' transitive packages,
and the conditional Raspberry Pi GPIO package to exact versions. CI and the Pi
therefore request the same versions instead of independently resolving broad
ranges.

The GPIO dependency is `rpi-lgpio`, which preserves the imported `RPi.GPIO` API
while using the gpiochip interface required by Raspberry Pi 5. Do not install
the original `RPi.GPIO` package in the same release virtual environment because
both distributions provide the same Python module.

The file is not a cross-architecture hash lock. Raspberry Pi and x86 CI can
receive different wheels or source distributions for an exact version, and pip
still trusts TLS, the configured package index, package-account security, and the
artifact served for that platform. A complete hash lock would need separately
generated and reviewed hashes for every supported Python version, Pi architecture,
and source/wheel artifact; that remains outside this deployment patch.

## Optional Private Repository Token

No token is needed while the repository is public. If it becomes private,
create `/etc/gate-controller-updater.env` as a root-readable file containing a
fine-grained, read-only GitHub token:

```sh
sudo install -m 0600 -o root -g root /dev/null /etc/gate-controller-updater.env
sudoedit /etc/gate-controller-updater.env
```

Add `GITHUB_TOKEN=` followed by the token value. Never put the token in this
repository, the application environment file, a systemd unit, or a command-line
argument.

## Tailscale

Tailscale can remain installed for break-glass SSH diagnostics, log inspection,
or a supervised manual rollback. It is not referenced by GitHub Actions, the
updater, the application service, release health checks, or rollback. A broken
or logged-out Tailscale client therefore cannot stop the gate or block automatic
updates.

## Isolated Live Media Gateway

Live media is an optional, separate MediaMTX service. It has its own
`gate-media` and `gate-media-auth` accounts, neither of which belongs to the
GPIO group or can write `/var/lib/gate-controller`. A failed MediaMTX process,
authorization sidecar, camera RTSP source, or media health check cannot stop or
delay the gate controller, its heartbeat, OCR, command worker, or relay.

The only integration is the nonsecret, atomically replaced
`/run/gate-media/capabilities.json` snapshot. The controller treats a missing,
stale, or malformed snapshot as unavailable media and continues its normal
heartbeat. `video`, `listen`, and `talkback` are independent. All default to
false; talkback remains `hardware_unverified` until a separate physical
backchannel acceptance test is complete.

Create two disjoint root-owned environments before enabling the services. Both
must remain regular non-symlink `root:root` mode `0600` files under the
root-controlled `/etc` directory; never put either file in this repository or a
systemd unit. The parser allows exactly one unquoted `KEY=value` assignment per
line, requires a final newline, and rejects comments, blank lines, whitespace,
duplicates, unknown keys, and cross-file secrets.

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/gate-media-auth.env
sudo install -o root -g root -m 0600 /dev/null /etc/gate-media-gateway.env
sudoedit /etc/gate-media-auth.env
sudoedit /etc/gate-media-gateway.env
```

The auth environment contains exactly these keys. The HMAC secret must be 32 to
256 UTF-8 bytes. Keep every capability false through initial deployment.

```text
GATE_MEDIA_HMAC_SECRET=REPLACE_WITH_32_TO_256_BYTE_SECRET
GATE_MEDIA_VIDEO_CONFIGURED=false
GATE_MEDIA_VIDEO_VERIFIED=false
GATE_MEDIA_LISTEN_CONFIGURED=false
GATE_MEDIA_LISTEN_VERIFIED=false
GATE_MEDIA_TALKBACK_CONFIGURED=false
```

The gateway environment contains exactly these MediaMTX 1.19.3 overrides.
`MTX_PATHS_GATE_SOURCE` must use `rtsp` or `rtsps`. Both ICE listeners must use
the same explicit, non-loopback, non-wildcard IP that is reachable on the Pi;
hostnames are not accepted for binds. `MTX_WEBRTCADDITIONALHOSTS` must be that
exact IP so MediaMTX can advertise the listeners while interface discovery is
disabled. It is one IP, not a comma-separated list. A MediaMTX-side TURN entry
and nonempty credentials are mandatory, and `CLIENTONLY=false` allows both
MediaMTX and the browser to use that relay.

```text
MTX_PATHS_GATE_SOURCE=rtsp://REPLACE_USER:REPLACE_PASSWORD@REPLACE_CAMERA_IP:554/REPLACE_PATH
MTX_WEBRTCLOCALUDPADDRESS=REPLACE_PI_IP:8189
MTX_WEBRTCLOCALTCPADDRESS=REPLACE_PI_IP:8189
MTX_WEBRTCADDITIONALHOSTS=REPLACE_PI_IP
MTX_WEBRTCICESERVERS2_0_URL=turns:REPLACE_TURN_HOST:5349?transport=tcp
MTX_WEBRTCICESERVERS2_0_USERNAME=REPLACE_TURN_USER
MTX_WEBRTCICESERVERS2_0_PASSWORD=REPLACE_TURN_PASSWORD
MTX_WEBRTCICESERVERS2_0_CLIENTONLY=false
```

Camera and TURN credentials remain only in `/etc/gate-media-gateway.env`. The
verifier never receives that file, and MediaMTX never receives the HMAC file.
The non-root gateway launcher validates the effective `MTX_` values on every
start and refuses to execute MediaMTX if source, ICE, or TURN validation fails.

MediaMTX is pinned to `1.19.3` and is deliberately not fetched by either
bootstrap or the ordinary updater. Obtain that exact release archive and its
independently verified SHA-256 through the approved release process. Place the
map beneath a root-owned directory that is not group/other writable. The map
must be a regular non-symlink `root:root` mode `0600` file containing exact
single-space rows: version, architecture (`arm64` or `armv7`), and lowercase
SHA-256. Rows for any other MediaMTX release are rejected.

```sh
sudo install -d -o root -g root -m 0700 /root/gate-media-release
sudo install -o root -g root -m 0600 /dev/null /root/gate-media-release/checksums.txt
sudoedit /root/gate-media-release/checksums.txt
sudo deployment/install-media.sh --source "$PWD" \
  --mediamtx-archive /root/gate-media-release/mediamtx.tar.gz \
  --mediamtx-version 1.19.3 \
  --checksum-map /root/gate-media-release/checksums.txt \
  --allowed-origin https://REPLACE_WITH_EXACT_APP_ORIGIN
```

The installer opens and validates the checksum map through one stable
descriptor before any candidate binary execution. It then stages the archive as
root-owned mode `0600` under
`/var/lib/gate-media/archives`, then hashes and extracts that same stable file.
It requires the extracted candidate to be a regular non-symlink executable,
verifies the candidate's version, and only then atomically replaces
`/usr/local/bin/mediamtx`. Every installer error disables both media services;
missing required environment values also leave them disabled. The fixed
application bootstrap may copy media scripts, units, and proxy templates as
root-owned references, but it never installs the MediaMTX binary.

The pinned MediaMTX config disables its RTSP server and all RTSP transports,
RTMP, HLS, SRT, playback, pprof, MoQ, interface-derived ICE addresses, and every
unused inherited listener. API (`127.0.0.1:9997`), metrics
(`127.0.0.1:9998`), WHEP HTTP (`127.0.0.1:8889`), and the authorization sidecar
(`127.0.0.1:9189`) remain loopback-only. MediaMTX uses RTSP only as a client for
the configured camera source.

The installer root-renders the exact allowed HTTPS origin into
`/etc/gate-media/nginx-whep-locations.conf`. Include that complete server block
from nginx's `http` context. It listens only on `127.0.0.1:8891`, which is the
media origin configured for Cloudflare Tunnel. It proxies `POST`/`OPTIONS`
on exact `/gate/whep` and `DELETE`/`OPTIONS` on bounded teardown resource paths;
no catch-all route proxies to MediaMTX. nginx carries WHEP HTTP signaling and
SDP only; it does not carry RTP/RTCP media. Actual media must traverse the exact
ICE listeners or the configured TURN relay. Do not expose API, metrics, the auth
sidecar, WHIP, RTSP serving, or camera administration.

Keep rollback-only Supabase credentials outside `/etc/gate-controller.env`, for
example in root-owned mode-0600 `/etc/gate-controller.rollback.env`. The active
environment rejects every non-empty `SUPABASE_URL` or
`SUPABASE_SERVICE_ROLE_KEY`; restore the prior release and its separate rollback
environment together if rollback is required.

Before setting `GATE_MEDIA_VIDEO_CONFIGURED=true` or
`GATE_MEDIA_VIDEO_VERIFIED=true`, complete a WHEP session from a separate
non-loopback client, verify video delivery, verify a TURN `relay` candidate is
usable from the intended remote network, and verify teardown. Test listen
separately before enabling its configured/verified flags. Restart the auth
service only after those checks. Talkback remains false and
`hardware_unverified` until the separate physical backchannel acceptance test.
The sidecar requires every field in the MediaMTX 1.19.3 auth schema, protocol
`webrtc`, action `read`, controller `primary`, and path `gate`; it does not log
tokens, camera URLs, passwords, or request bodies.
