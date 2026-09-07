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
- Set `GATE_TELEMETRY_RETENTION_DAYS` to an integer from 1 through 3650 when the
  default 30-day local diagnostic retention window is unsuitable.
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

The managed systemd drop-in keeps transport selection on `auto`, which first
tries QUIC and falls back to HTTP/2 when UDP cannot connect. All four outbound
tunnel connections remain encrypted. Bootstrap records the existing service
state and drop-in before activation. When the effective drop-in changes and
`cloudflared.service` was active, bootstrap queues a non-blocking restart, tracks
the PID 1 job with a 30-second bound, and explicitly confirms it is active
afterward. A timed-out job is cancelled and the unit must leave every
transitional state before rollback can queue another restart. An inactive or
absent service is never started, the previous enabled state is left unchanged,
and an unchanged drop-in does not restart the tunnel during rollback. If later
bootstrap activation fails, the exact previous drop-in, including its absence,
is restored before an originally active service is restarted and checked
against the prior setting. Exact-restore or `daemon-reload` failure is reported
without being masked by later rollback work; the backup and rollback diagnostics
remain under the printed `/run/gate-controller-install.*` path for recovery.

After bootstrap, confirm the effective setting and selected connection
protocol:

```sh
sudo systemctl show cloudflared.service --property=Environment
sudo journalctl -u cloudflared.service -n 100 --no-pager | grep 'protocol='
```

## Cloudflare Event Ingest And Retention

The controller posts authorized-plate refreshes, heartbeats, and queued events
to the HTTPS Worker origin in `GATE_CLOUDFLARE_API_URL`, authenticated with the
two Access service-token variables. Event ingest is `POST
/api/controller/events`; it is idempotent by the controller event key and may
include a bounded JPEG whose SHA-256 digest is in the event payload.

Keep the Pi's resolver on the router and a public resolver, not on Tailscale.
With `tailscale set --accept-dns=true` every fresh OCR connection depends on
tailscaled forwarding the lookup over DNS-over-HTTPS across the same uplink,
and its journal shows those forwards failing whenever the link degrades
(`dns udp query: resolving using "https://cloudflare-dns.com/dns-query"`).
On 2026-09-06 the Pi was switched to `tailscale set --accept-dns=false`;
`/etc/resolv.conf` then carries the DHCP resolvers. The controller also opens
its OCR connection the moment a camera event arrives (`prewarm`), so the
first request of a vehicle reuses a warm TLS session instead of paying for a
lookup and two handshakes at the critical moment.

The controller assumes the Worker can be unavailable for days. Recognition keeps
using the last complete plate snapshot for 14 days
(`GATE_AUTHORISATION_MAX_STALENESS_SECONDS`, `0` for no bound) before failing
closed, so a Cloudflare, D1 quota, or uplink outage does not stop authorised
vehicles. Queued events wait in the local outbox with per-item exponential
backoff (5 s to 5 minutes) instead of retrying every poll, and heartbeat and
plate-refresh failures appear in the journal as `gate_cloud stage=*_failed`
transitions with the returned HTTP status, repeated at most every ten minutes,
followed by `stage=*_recovered` when the path returns. Check those lines first
when the app shows the controller as not reporting.

### Heartbeat Health Blocks

The 15 s heartbeat is the only telemetry that survives a Pi the owner cannot
SSH into, so it carries host health as well as capabilities. **Deploy the app
before the controller starts sending these.** The heartbeat's allow-list
*drops* keys it does not know rather than rejecting the body, so an out-of-date
Worker still answers `200` and still records the heartbeat — it silently
discards `host`, `network`, `cloud` and `recognition.trigger_capture`, and the
status header reads "unknown" indefinitely. That is a quiet failure of exactly
the instrument this work exists to provide, which is why the ordering matters
even though nothing breaks.

`POST /api/controller/metrics` is the stricter one: an unrecognised key there
rejects the whole body with `400`. Nothing in this phase posts to it yet, but
the same rule applies when the phase-2 rollup does.

| Block | What it carries |
| --- | --- |
| `host` | `soc_temp_c`, `throttled` (raw hex plus `under_voltage` / `arm_capped` / `currently_throttled`), `load_1m/5m/15m`, `mem_total_kib` / `mem_available_kib` / `swap_free_kib`, `disk_free_bytes` / `disk_total_bytes`, `oom_kill_total`, `uptime_seconds`, `process_uptime_seconds`, `disk_sectors_written` |
| `network` | the last completed probe cycle: `mode`, `skipped_reason`, `age_seconds`, `hops.lan` / `hops.router` (`state`, `loss`, `samples`, `min_ms` / `p50_ms` / `p95_ms` / `max_ms` / `mean_ms` / `jitter_ms`), `hops.internet` (`state`, `dns_ms`, `connect_ms`, `tls_ms`, `total_ms`, `age_seconds`), `interface` (`name`, `link_mbps`, receive/transmit bytes, packets, dropped and error **rates**, `receive_dropped_pct`) |
| `cloud` | `heartbeat_rtt_ms`, `heartbeat_consecutive_failures`, `plates_consecutive_failures`, `oldest_pending_outbox_age_s` |
| `recognition.trigger_capture` | the presence and skip counters described in `reolink-rlc-810a.md` |
| `corpus` | `local` (bytes, records, pruned, discarded), `upload` (`pending`, `oldest_pending_age_s`, `last_success_at`, `consecutive_failures`, `last_blocked_by`) and `backpressure` (`quiet_window_seconds`, `quiet_for_seconds`, `busy`) |

Everything here degrades to an absent field rather than to a healthy-looking
default. An unreadable `/proc` file omits its metric; a failed probe omits its
block; a wedged capture worker omits `trigger_capture`. The heartbeat still
goes out. The app must render an absent field as "unknown".

Two measurements name the failures that prompted this. `oom_kill_total` is the
kernel's monotonic counter and needs no threshold: any increase means
something on the board was killed, which is what happened on 2026-09-07.
`process_uptime_seconds` falling below `uptime_seconds` means
`file-monitor.service` restarted without the board rebooting.

Nothing in the heartbeat carries a filesystem path, a plate, an image digest,
a credential or an IP address. The absolute path of the latest camera frame
used to be sent as `latest_camera_image`; it is now reported as
`latest_camera_image_available` plus `latest_camera_image_age_seconds`. The
default gateway is read from `/proc/net/route` and used only as a ping target,
never reported.

### Network Probe

`GATE_NET_PROBE_ENABLED` (default `true`) adds **one** thread on a 60 s poll
inside the existing controller process — no new systemd unit.

#### Three hops, because only the comparison is diagnostic

The probe cannot answer "is the network bad"; it can answer "which hop", and
that is the question worth asking. Each cycle measures, and labels:

| Hop | Target | What it isolates |
| --- | --- | --- |
| `lan` | `GATE_NET_PROBE_LAN_HOST`, normally the camera | the gate switch alone — this traffic never crosses the powerline bridge |
| `router` | the default gateway from `/proc/net/route` | the same switch **plus** the powerline bridge |
| `internet` | `GATE_NET_PROBE_TLS_HOST` | the whole path, end to end |

`lan` clean beside `router` lossy isolates the bridge. `lan` and `router`
equally clean exonerates it, and the fault is somewhere else. Both are useful
answers, which is why the same-switch hop is not optional in practice: leave
`GATE_NET_PROBE_LAN_HOST` unset and the hop reports `lan=unconfigured`, the
comparison is unavailable, and the probe can neither prove nor disprove the
claim. **Set it to the camera's address.** It is never sent to the cloud and
never appears in the heartbeat.

#### Distributions, not a single number

Each ping hop sends `GATE_NET_PROBE_PING_COUNT` packets (default 10, at
0.2 s) and reports loss plus `min`, `p50`, `p95`, `max`, `mean` and jitter.
The tail is the point. A router at a 167 ms mean is imperceptible in a camera
web console — one small request — and ruinous for a recognition upload, which
pays several round trips plus a bulk transfer and so pays `p95` and every
retransmit. Percentiles are linearly interpolated so `p95` is not silently the
maximum, and jitter is the mean absolute difference between *consecutive*
round trips, not deviation from the mean.

Per-cycle loss is quantised at `1 / ping_count`, so a single cycle reading
`10 %` means one packet of ten. The signal is the rate across cycles: 60
cycles an hour is 600 packets a hop.

#### Interface counters as rates

`rx_dropped`, `rx_errors`, `tx_errors`, packets, bytes and the negotiated
`link_mbps` come from `/proc/net/dev` and `/sys/class/net/<iface>/speed`.
Lifetime totals are never reported — 2.9 M dropped frames after a month of
uptime says nothing about today — only the delta over the cycle.

Read `rx_dropped` carefully. On this board `rx_errors` is `0` while
`rx_dropped` climbs on **both** `eth0` and `wlan0`, which is the signature of
the kernel discarding frames no socket wanted, not of a damaged link. A drop
rate is evidence only when it moves with the ping loss on the same hop, or
when `rx_errors` moves with it. The probe reports both and draws no conclusion.

#### No throughput test, ever

The uplink is about 4.5 Mbit/s and OCR uploads already saturate it, so a speed
test would compete with the thing it measures. The `internet` hop opens one
connection and times DNS, TCP connect and TLS separately — no HTTP request, so
nothing is billed and no quota is consumed against the one-request-per-second
throttle. `/proc/net/dev` carries the actual load.

#### Two tiers, and an honest governor

The board idles at 66–75 C against an 80 C ceiling and a four-core load
average around 0.5. A single governor at those thresholds withheld the *whole*
cycle exactly when a recognition burst made the network interesting, which
biases the record towards looking healthy. So:

* the **ping tier always runs**. Two `ping` children under a 64 MiB
  address-space limit are nothing like the 4K decode that OOM-killed this
  board. It is withheld only below a hard floor — `soc_temp_c >= 85.0`, the
  firmware throttle point, or under 96 MB available — and then the whole cycle
  logs `outcome=skipped reason=critical_temp` / `critical_memory`;
* the **expensive extras** (the `internet` hop and `vcgencmd get_throttled`)
  stay behind the original governor: `soc_temp_c >= GATE_NET_PROBE_MAX_TEMP_C`
  (80.0), `load_1m >= GATE_NET_PROBE_MAX_LOAD` (3.0), or under 300 MB
  available. The cycle then logs `mode=ping` with `reason=hot` / `loaded` /
  `low_memory`, so the gap is explicit rather than silent.

At most one child process is ever alive (a `BoundedSemaphore(1)`), each child
runs under `RLIMIT_AS` of 64 MiB with a capped stdout and `stderr` to
`/dev/null`. `ping` carries its own `-w` deadline so it ends itself; the
parent's kill-and-reap deadline is derived from the ping plan and hard-capped
at 10 s. ffmpeg, image or video decode, numpy, onnxruntime, model loads,
`journalctl` and throughput tests are permanently forbidden in
`gate_controller/net_probe.py` and `host_metrics.py`, and
`tests/test_net_probe.py` asserts it along with the measured ceilings: under
5 MB of steady-state growth and under 0.5 % of one core.

#### The journal line

Every cycle writes exactly one `key=value` line at `INFO`, successes included,
so the device is diagnosable over SSH with no cloud involvement and with no
health page in the app:

```
journalctl -u file-monitor -f | grep gate_net_probe
```

```
gate_net_probe outcome=ok mode=full lan=ok lan_loss_pct=0 lan_n=10 lan_min_ms=0.31 \
 lan_p50_ms=0.37 lan_p95_ms=0.63 lan_max_ms=0.82 lan_jitter_ms=0.14 \
 router=ok router_loss_pct=10 router_n=9 router_min_ms=70.1 router_p50_ms=154.2 \
 router_p95_ms=228.44 router_max_ms=245 router_jitter_ms=85.15 \
 internet=ok internet_dns_ms=12.4 internet_connect_ms=38.1 internet_tls_ms=96.2 \
 internet_total_ms=146.7 internet_age_s=0 \
 iface=eth0 link_mbps=100 rx_bytes_per_s=120400 tx_bytes_per_s=8100 \
 rx_pkt_per_s=332.9 tx_pkt_per_s=20 rx_drop_per_s=0.4 rx_drop_pct=0.12 \
 rx_err_per_s=0 tx_err_per_s=0
```

(One line on the device; wrapped here.) `outcome` describes the *cycle*, not
the network: `ok` means it ran, and the per-hop `state` says what happened.
A hop is `ok`, `lost` (it answered with 100 % loss), `failed` (`ping` itself
produced no usable output — deliberately *not* reported as 100 % loss, because
a broken child is not a broken network) or `unconfigured`. **A measurement
that could not be taken is an absent key, never a zero**, so `lan_loss_pct=0`
and no `lan_loss_pct` at all mean different things.

To answer "is the gate's network actually a problem", compare over a day:

```
journalctl -u file-monitor --since -24h | grep -o 'lan_loss_pct=[0-9.]*' | sort | uniq -c
journalctl -u file-monitor --since -24h | grep -o 'router_loss_pct=[0-9.]*' | sort | uniq -c
journalctl -u file-monitor --since -24h | grep -o 'router_p95_ms=[0-9.]*' | sort -t= -k2 -n | tail
```

If `lan_loss_pct` is `0` in every cycle while `router_loss_pct` is not, the
powerline bridge is the fault and the switch is fine. If both are `0` and
`router_p95_ms` stays low, the network is not the problem and the OCR latency
has another cause — which is the answer the record should be allowed to give.

Set `GATE_NET_PROBE_ENABLED=false` to remove the thread entirely; the rest of
the heartbeat is unaffected.

### Gate Audio Capture

`GATE_AUDIO_CAPTURE_ENABLED` (default `false`) records a short clip of the gate
around each event. It exists because the controller currently fires the relay
and *assumes* the gate moved: if the motor failed, the relay stuck or the gate
jammed, the event log would still read `activated` and nothing would know
otherwise. This collects the evidence that would close that gap. There is no
classifier, no inference and no detection claim — it collects data and verifies
the capture works.

**Every clip labels itself.** The controller knows the instant it energised the
relay, so the actuation time, source, plate and outcome are written into the
sidecar as the event happens. A clip with an actuation is a labelled positive;
a clip without one is a negative, and among those, a passage where the gate
moved anyway is a remote, keypad or manual opening — which is the direct
explanation for gate openings with no recognition event.

The audio track is already inside the Pi: MediaMTX carries MPEG-4 Audio on both
the `camera` and `clear` paths, so nothing new is pulled from the camera and
nothing is added to the 4.5 Mbit/s uplink. The stream is AAC-LC, 16 kHz, mono,
~65 kbit/s — about 8 KB/s, so a 40 s clip is roughly 325 KB.

**Nothing is ever decoded.** The capture is `-vn -c:a copy`: video is dropped
before any packet reaches a decoder and the AAC packets are remuxed untouched.
No pixel, no PCM sample, no resample, no analysis on the device. This is the
same command measured on the live board on 2026-09-07 for no measurable thermal
cost (69.2 C before, 69.2 C after).

The same governor as the network probe skips a capture — journalling the reason
so the gap in the corpus is explicit — when

* `soc_temp_c >= GATE_AUDIO_CAPTURE_MAX_TEMP_C` (default 80.0), or
* `load_1m >= GATE_AUDIO_CAPTURE_MAX_LOAD` (default 3.0), or
* available memory is under 300 MB.

One thread owns all spawning, so exactly one capture can exist at a time; a
second request while one is in flight is coalesced and counted, never queued.
A capture that cannot start is skipped and journalled and **never retried**, so
no failure can become a loop. Length is bounded three times over — ffmpeg's own
`-t`, a wall-clock read deadline, and a byte cap that stops a runaway stream
without ever holding it — and the child runs at `nice 19` under `RLIMIT_AS`.
Both trigger points are non-blocking by construction, which matters most for
the relay one: it runs while the relay is energised, so it does nothing but
take an uncontended lock and write a few fields.

Clips are written as `<stem>.aac` plus `<stem>.json` with the same stem
convention, permissions (0700 directory, 0600 files) and pruning as the image
corpus, in an `audio` directory beside `GATE_TRAINING_CORPUS_DIR` unless
`GATE_AUDIO_CAPTURE_DIR` overrides it. An exporter that walks the corpus root
pairing a payload with its sidecar therefore carries audio without a second
uploader; `kind: "gate_audio"` is what tells the two apart.

**Retention.** Clips are kept for `GATE_AUDIO_CAPTURE_RETENTION_DAYS` (default
30) inside a `GATE_AUDIO_CAPTURE_MAX_TOTAL_BYTES` cap (default 256 MiB),
whichever bites first, pruned oldest first. They are recordings of the owner's
own gate on his own premises, held on his own hardware.

The Worker deployment owns evidence retention. Store accepted JPEGs only in a
private R2 bucket under the verified digest, keep bucket access limited to the
Worker and approved operators, and configure the site's approved R2 lifecycle
retention before accepting production traffic. Confirm that event metadata and
the R2 object share the digest before considering ingest healthy. The Pi keeps
its local evidence until it receives a 2xx response and never performs R2
credentials, deletion, or lifecycle management directly.

## Training Corpus Off The Card

Every frame the controller sends to OCR is kept under
`GATE_TRAINING_CORPUS_DIR` as a JPEG plus a JSON sidecar carrying the plate,
score, box, candidates, crop geometry and the on-device read. That is the
training set for the local recogniser, and until this change it existed in
exactly one place: a single SD card in a warm cabinet, with the oldest
examples deleted permanently once the directory reached its cap. Cards in warm
Pis fail. The loss is silent and it is not recoverable.

The corpus now goes to R2 with its index in D1, and the local directory
becomes a **buffer**: an artefact the cloud has confirmed is deleted from the
card, so what remains is only what has not shipped yet.
`GATE_TRAINING_CORPUS_MAX_BYTES` stays as the backstop for a long outage.

### Backpressure

Bandwidth is not the constraint. About 5.6 MB and 48 frames a day is roughly
ten seconds of the 4.5 Mbit/s uplink; what matters is never spending those
seconds while a vehicle is at the gate. The priority is explicit: **gate
decisions, then event delivery, then the corpus.** In order:

1. **Nothing while the gate is working.** A camera event, a presence session or
   an OCR request in flight blocks a start outright. Each marks
   `ActivityGate` around the work it is doing.
2. **A quiet period first** (`GATE_CORPUS_QUIET_SECONDS`, default 60 s). The
   gaps inside a presence session — the spacing between frames, the wait for a
   verdict — are much shorter, so a session is never mistaken for quiet.
3. **Real events first.** A non-empty outbox blocks the corpus entirely. An
   owner waiting on an evidence image outranks a training frame, and delivery
   lag is already p90 84 s with p99 pinned at the 600 s ceiling. A queue depth
   that cannot be read counts as work pending.
4. **Abandon, do not finish.** The request body is produced in 8 KB chunks and
   checks the activity epoch between them, so a transfer already running is
   torn down the instant an event begins rather than completing. The epoch,
   not a busy flag, is what makes this reliable: an event that starts and ends
   between two chunks still aborts the transfer.
5. **A modest fraction of the link** — `GATE_CORPUS_UPLOAD_BYTES_PER_SECOND`,
   default 64 KB/s, about an eighth of the uplink.
6. **Defer rather than compete.** Any block ends the pass and waits for the
   next poll (`GATE_CORPUS_POLL_SECONDS`, default 300 s). Failures back off
   per pass from 60 s to 1 hour with jitter.

Nothing here can affect a gate decision. It runs on its own background worker,
holds no lock the pipeline takes, catches every failure, and a failure always
leaves the local copy exactly where it was.

### Watching It

`gate_corpus stage=uploaded|deferred|aborted|upload_failed|unshippable` is the
one journal prefix, and the heartbeat's `corpus` block carries the same state.
The thing to watch for is `pending` climbing while `last_success_at` stands
still: that is the buffer filling because uploads are failing, which is the
corpus going back to being one copy on one card.

`stage=unshippable` means an artefact cannot be sent as it stands — an empty
payload, an unreadable sidecar. It is kept, not deleted, and counted where the
heartbeat can see it; deleting it would be the very loss this exists to
prevent.

### Audio

The pipeline moves **artefacts**, not frames. A sidecar names its `kind` and
`media_type`, and discovery, upload, indexing and discard all read those
rather than assuming a JPEG. When audio capture lands it needs to write a clip
and a sidecar under the same stem convention and nothing here changes.

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
`current` symlink is touched. Git checkout, package installation, the import
smoke check, compilation, and candidate shell syntax checks all run as the
unprivileged `gate-controller-build` account. The root helper only prepares
ownership, durably records activation, switches the managed symlink, and asks
systemd to restart the fixed service.

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

## What On-Device Verification Checks

The updater deliberately does not repeat the unit suite that GitHub CI has
already run. A candidate is not staged at all until a completed, successful
`Gate Controller CI` push workflow exists for that exact 40-character commit,
and that workflow runs the whole suite on Python 3.10 and 3.11. On-device
verification therefore runs only the checks CI cannot perform, because they
depend on this board's architecture and its installed wheels:

- Building the release-local virtual environment.
- Installing `requirements.txt`. This is the single most valuable
  device-specific check: it catches a missing or incompatible `aarch64` wheel.
  The wheels-only policy lives in `requirements.txt` itself, on an
  `--only-binary=` line naming the heavy recognition stack — onnxruntime,
  opencv-python-headless, numpy, protobuf, flatbuffers and their closure — so a
  missing wheel for any of those fails in seconds rather than starting a source
  build that could not finish inside the command timeout. It is deliberately
  not `--only-binary=:all:`: `lgpio` publishes its `aarch64` wheel as
  `manylinux_2_34`, which installs on Bookworm's glibc 2.36 but not on an older
  image, where pip falls back to a source build that takes seconds. A blanket
  wheels-only rule would turn that working fallback into a hard install
  failure, and `verify_release` would then reject every release — exactly the
  silent freeze this section exists to prevent.
- An import smoke check that imports exactly what `file-monitor.service` loads
  at startup — `gate_controller`, `gate_controller.relay_safe`, and
  `gate_controller.__main__` — against the freshly installed wheels. This is the
  real device-specific failure mode, and it takes well under a second.
- `compileall` over whichever of `gate_controller`, `deployment`, `tests`, and
  `scripts` the candidate contains.
- `sh -n file_monitor.sh` and `bash -n deployment/install.sh`, each run only
  when the candidate actually contains that file. A check for an absent script
  would exit 127, and the updater treats a failed verification as a deferral, so
  an unconditional check against a file a future release removes would freeze
  automatic updates indefinitely while still logging success.

What this trades away is honest and worth stating: the Pi no longer independently
re-proves the test suite against its own interpreter build, so a defect that only
appears on `linux/arm64` and is not an import error can now reach the gate. CI is
the gate. If the required checks on the release branch are ever removed or
weakened, this verification will not catch it. In exchange, an update no longer
pegs all four cores for 15 to 40 minutes at 84 °C against an 85 °C throttle limit
while a live recognition session is competing for the same CPU.

To restore the old behaviour and run the complete suite on the device as well,
add `GATE_UPDATE_RUN_TESTS=1` to `/etc/gate-controller-updater.env`:

```sh
sudo install -m 0600 -o root -g root /dev/null /etc/gate-controller-updater.env
sudoedit /etc/gate-controller-updater.env
```

Budget for it. The suite is roughly 850 tests and grows; the updater unit allows
45 minutes per run and `GATE_UPDATE_COMMAND_TIMEOUT_SECONDS` (default 900,
maximum 3600) bounds each individual command. Leave it unset for normal
operation.

One-time bootstrap through `deployment/install.sh` is unchanged and still runs
the complete suite. Bootstrap is manual, infrequent, and supervised, and it is
the only path that does not consult CI, so the extra confidence is worth the
wall-clock cost there. It runs the same plain
`pip install -r requirements.txt` as the updater, so both inherit the same
named wheels-only policy from `requirements.txt` and agree on what counts as
installable on this board.

When a verification command does fail, the deferral warning in the journal now
carries the failing command, whether it exited non-zero or timed out, and a
bounded tail of the command's own output:

```sh
sudo journalctl -u gate-controller-updater.service -n 100 --no-pager
```

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

Live media is an optional, separate MediaMTX service. It has dedicated
`gate-media` and `gate-media-auth` accounts plus a dynamic transcoder user. None
belongs to the GPIO group or can write `/var/lib/gate-controller`. A failed
MediaMTX process, authorization sidecar, camera RTSP source, transcoder, or media
health check cannot stop or delay the gate controller, its heartbeat, OCR,
command worker, or relay.

The only integration is the nonsecret, atomically replaced
`/run/gate-media/capabilities.json` snapshot. The controller treats a missing,
stale, or malformed snapshot as unavailable media and continues its normal
heartbeat. `video`, `listen`, and `talkback` are independent. All default to
false; talkback remains `hardware_unverified` until a separate physical
backchannel acceptance test is complete.

Create the two operator-managed root-owned environments before enabling the
services. Both must remain regular non-symlink `root:root` mode `0600` files
under the root-controlled `/etc` directory; never put either file in this
repository or a systemd unit. The installer creates a third root-only generated
environment at `/var/lib/gate-media/turn.env` inside a mode `0700` state
directory. The parser allows exactly one unquoted `KEY=value` assignment per
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

The static gateway environment contains exactly these MediaMTX 1.19.3 overrides.
`MTX_PATHS_CAMERA_SOURCE` and `MTX_PATHS_CLEAR_SOURCE` must use `rtsp` or
`rtsps` and identify distinct Fluent and Clear paths. Both ICE listeners must use
the same explicit, non-loopback, non-wildcard IP that is reachable on the Pi;
hostnames are not accepted for binds. `MTX_WEBRTCADDITIONALHOSTS` must be that
exact IP so MediaMTX can advertise the listeners while interface discovery is
disabled. It is one IP, not a comma-separated list. `CLIENTONLY=false` allows
both MediaMTX and the browser to use the generated relay.

```text
MTX_PATHS_CAMERA_SOURCE=rtsp://REPLACE_USER:REPLACE_PASSWORD@REPLACE_CAMERA_IP:554/REPLACE_PATH
MTX_PATHS_CLEAR_SOURCE=rtsp://REPLACE_USER:REPLACE_PASSWORD@REPLACE_CAMERA_IP:554/REPLACE_CLEAR_PATH
MTX_WEBRTCLOCALUDPADDRESS=REPLACE_PI_IP:8189
MTX_WEBRTCLOCALTCPADDRESS=REPLACE_PI_IP:8189
MTX_WEBRTCADDITIONALHOSTS=REPLACE_PI_IP
MTX_WEBRTCICESERVERS2_0_CLIENTONLY=false
```

The generated `/var/lib/gate-media/turn.env` contains exactly the TURN URL,
username, and password. Camera credentials remain only in the static gateway
file; generated TURN credentials remain only in the mutable state file. The
verifier receives neither file, and MediaMTX never receives the HMAC file or the
Cloudflare long-term TURN key. The non-root gateway launcher validates the
combined effective `MTX_` values on every start and refuses to execute MediaMTX
if source, ICE, or TURN validation fails.

An installer rerun safely migrates the former `MTX_PATHS_GATE_SOURCE` name and
the former eight-key combined gateway file. It writes the generated TURN file
first, atomically replaces the gateway file with the five canonical static
keys, and can be rerun after any interruption. If a valid generated TURN file
already exists, migration preserves it. Files containing both source names are
rejected without replacement.

### Automatic Cloudflare TURN Credential Rotation

Cloudflare TURN credentials are deliberately short-lived. To enable automatic
rotation, create a separate root-only environment containing exactly the TURN
key ID and long-term API token. It must be a regular, non-symlink `root:root`
mode `0600` file with no comments, blank lines, quoting, or whitespace. Do not
put this token in the gateway environment, a systemd unit, shell history, or
the repository.

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/gate-media-turn.env
sudoedit /etc/gate-media-turn.env
```

```text
TURN_KEY_ID=REPLACE_WITH_CLOUDFLARE_TURN_KEY_ID
TURN_KEY_API_TOKEN=REPLACE_WITH_CLOUDFLARE_TURN_KEY_API_TOKEN
```

The media installer installs the root-only stdlib helper and the
`gate-media-turn-refresh.service` / `gate-media-turn-refresh.timer` pair. It
enables the timer only when this separate secret file passes validation, so a
media installation using manually managed TURN credentials remains
supported. The timer runs once shortly after boot and about every 4 hours with
up to 5 minutes of randomized delay. This leaves several retry opportunities
after a failed run before a 24-hour credential expires. Each successful run
requests a 24-hour credential from Cloudflare with the `gate-mate-pi` custom
identifier.

The helper accepts only Cloudflare's documented top-level `iceServers` response,
rejects port 53 and unauthenticated relays, and deterministically prefers
`turns:...:5349?transport=tcp`, then `turn:...:3478` UDP/TCP. It stages the
three MediaMTX TURN values, validates the complete auth, static gateway, and
generated environments with the canonical validator, and atomically replaces
only `/var/lib/gate-media/turn.env`. A gateway that was inactive remains
inactive. An active gateway is restarted and checked three times; activation
failure restores the old generated file and restarts and verifies the old
configuration. Generation, parsing, staging, or validation failure leaves the
old file and service state untouched.

After installing the secret and rerunning the media installer, verify rotation
without exposing either credential:

```sh
sudo /usr/local/lib/gate-media/gate_media_turn_refresh.py --validate-turn-environment
sudo systemctl start gate-media-turn-refresh.service
sudo systemctl is-active --quiet gate-media-gateway.service
sudo systemctl list-timers gate-media-turn-refresh.timer
sudo journalctl -u gate-media-turn-refresh.service -n 20 --no-pager
```

The helper and systemd status output never print the long-term token. If a
refresh fails, inspect the unit status and journal, correct the root secret or
network condition, and rerun the service; the previously working short-lived
gateway credentials remain in place until a validated replacement activates.

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
`/usr/local/bin/mediamtx`. It also requires the existing `/usr/bin/ffmpeg` to
provide the `libopus` encoder and RTSP/TCP output; it does not install a package.
Activation enables, restarts, and verifies the auth, gateway, and transcoder
services as one group. A transcoder in systemd's exact `activating/auto-restart`
state is accepted so a temporary camera outage does not defeat its recovery
policy. Any other installer or activation error disables all three.
Missing required environment values also leave them disabled. The fixed
application bootstrap may copy media scripts, units, and proxy templates as
root-owned references, but it never installs MediaMTX or ffmpeg. The ordinary
controller updater cannot replace these privileged media artifacts.

The pinned MediaMTX config disables RTMP, HLS, SRT, playback, pprof, MoQ,
interface-derived ICE addresses, and every unused inherited listener. Its RTSP
server is enabled only on `127.0.0.1:8554` and accepts TCP only. API
(`127.0.0.1:9997`), metrics (`127.0.0.1:9998`), WHEP HTTP
(`127.0.0.1:8889`), and the authorization sidecar (`127.0.0.1:9189`) also remain
loopback-only.

MediaMTX pulls the camera into the private `camera` path. The isolated ffmpeg
service reads that path over loopback RTSP/TCP, copies H264 without video
transcoding, converts optional camera audio to Opus, and publishes the browser-
facing `gate` path over loopback RTSP/TCP. Camera credentials exist only in the
root-owned gateway environment and never appear in ffmpeg arguments or its
environment. Authorization grants exactly the blank-credential loopback RTSP
operations `read camera` and `publish gate`; all other RTSP requests fail.
Browser WHEP remains a tokenized `read gate` operation.

If the camera or private RTSP path disappears, ffmpeg exits and systemd retries
it every five seconds. Start-rate lockout is disabled for this isolated unit so
an extended camera outage cannot leave audio permanently failed after MediaMTX
recovers; the fixed delay still bounds retry cadence.

The installer root-renders the exact allowed HTTPS origin into
`/etc/gate-media/nginx-whep-locations.conf` and owns the nginx include
`/etc/nginx/conf.d/gate-media-whep.conf`; no manual nginx include is needed.
That complete server block listens only on `127.0.0.1:8891`, which is the media
origin configured for Cloudflare Tunnel. It proxies `POST`/`OPTIONS`
on exact `/gate/whep` and `DELETE`/`OPTIONS` on bounded teardown resource paths;
no catch-all route proxies to MediaMTX. nginx carries WHEP HTTP signaling and
SDP only; it does not carry RTP/RTCP media. Actual media must traverse the exact
ICE listeners or the configured TURN relay. Do not expose API, metrics, the auth
sidecar, WHIP, RTSP serving, or camera administration.

## Camera Control Service

Camera *settings* (the IR illuminator and the on-demand 4K still) are owned by a
separate isolated service, `gate-camera-control`, installed with
`deployment/install-camera-control.sh --source "$PWD"`. It is the only process
that holds camera API credentials, in its own root-owned mode-0600
`/etc/gate-camera-control.env`. Those credentials are deliberately not added to
`/etc/gate-media-gateway.env`: that file's key set is pinned by
`validate_gateway_static_environment()` and holds the RTSP secret, so widening it
would widen who can read that secret. The service runs as its own
`gate-camera-control` user, binds `127.0.0.1:8767`, and its unit denies all
network egress except loopback plus the camera's exact `/32`. The controller
gains no camera credentials and no camera host from it; it only reads the
nonsecret `/run/gate-camera/state.json` into the `camera_control` heartbeat
block, exactly as it reads `/run/gate-media/capabilities.json` into `media`.

Expose it through its own tunnel hostname with its own Cloudflare Access
application and service token — not the `gate-command` token. The full HTTP
contract, environment keys, journal lines, failure modes, and rollback are in
[Gate camera control](camera-control.md).

Keep rollback-only Supabase credentials outside `/etc/gate-controller.env`, for
example in root-owned mode-0600 `/etc/gate-controller.rollback.env`. The active
environment rejects every non-empty `SUPABASE_URL` or
`SUPABASE_SERVICE_ROLE_KEY`; restore the prior release and its separate rollback
environment together if rollback is required.

Before setting `GATE_MEDIA_VIDEO_CONFIGURED=true` or
`GATE_MEDIA_VIDEO_VERIFIED=true`, complete a WHEP session from a separate
non-loopback client, verify video delivery, verify a TURN `relay` candidate is
usable from the intended remote network, and verify teardown. Test listen
separately and confirm the published gate path reports Opus, G722, or G711
before enabling its configured/verified flags; AAC and AC-3 do not count as
browser-listen readiness. Restart the auth service only after those checks.
Talkback remains false and
`hardware_unverified` until the separate physical backchannel acceptance test.
The sidecar requires every field in the MediaMTX 1.19.3 auth schema, protocol
`webrtc`, action `read`, controller `primary`, and path `gate`; it does not log
tokens, camera URLs, passwords, or request bodies.
