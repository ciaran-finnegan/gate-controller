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

### Vehicle Direction (Shadow)

Every event carries a `telemetry.direction` block saying which way the vehicle
was going: `{verdict, method, score, slope, frames, span_ms}`, with `verdict`
one of `entering`, `exiting`, `stationary`, `unknown` and `method` one of
`box_width`, `none`. The app already accepts it at `schema_version` 3
(access-gate-ui #49), so nothing about the wire version changes here.

**This is shadow telemetry and nothing acts on it.** It reaches no decision,
no relay claim, no presence session and no lookup budget. Suppressing an
opening for a departing vehicle is gate-controller#95 and is a separate
change.

The signal is the least-squares slope of `log(box width)` against time, over
the boxes the pipeline already produced for one camera alarm — the on-device
detector's plate box and the cloud read's vehicle and plate boxes, never
mixed, because three detectors give three scales. No new model runs and no
frame is decoded twice. One burst is usually one frame, so the samples are
pooled by the camera alarm the burst's correlated trigger identifies, which
is the same identity the burst queue already uses to decide which queued
frame supersedes which. A burst with no correlated trigger keys on its own
trace and can only ever fit its own frames.

The verdict is gated at **at least 3 boxed frames spanning at least 2 s**.
Measured over 42 hand-labelled passages
(`gate-controller-data/analysis/vehicle-direction-2026-09-08.md`), that gate
separates entering from exiting completely *among the passages that clear
it*: 7/7 gated exits caught with **0/15** false exits on a gated entering
car. Ungated, the same rule called one entering car an exit. The gate may be
tightened by configuration; the controller refuses to start only with the
gate loosened (`GATE_DIRECTION_MIN_FRAMES` below 3 or
`GATE_DIRECTION_MIN_SPAN_SECONDS` below 2.0), and every other malformed
`GATE_DIRECTION_*` value falls back to its shipped default with one
`gate_direction key=… status=rejected using=…` line.

**Recall on the whole labelled set is 7 of 12.** Twelve of the 42 passages
are hand-labelled `exiting`; only seven of them produce three boxed frames
over two seconds, so the other **five exits fail the gate and ship
`unknown`** — they are misses, not errors, and they are the price of the
zero-false-exit result above. Do not read "7/7" as recall.

**What the replay test does and does not prove.** `tests/test_direction.py`
replays the analysis table, and that table holds each passage's *already
fitted* slope, frame count and span — the per-frame boxes live in R2 and D1,
not in this repository. The replay reconstructs a width series
log-linearly from the recorded slope and checks the fitter recovers it, so it
exercises the **thresholds and the gate against the 42 recorded slopes**. It
does **not** validate the fitter against raw box widths, and it cannot catch
a regression in how a box becomes a width.

Night is unmeasured — exactly one labelled passage is genuinely dark — so a
passage whose **brightest** frame is darker than
`GATE_DIRECTION_MIN_BRIGHTNESS` reports `unknown`/`none` rather than a slope
nobody has validated. A single dark frame does **not** suppress the passage:
one lit frame is enough for the boxes to have been measurable, and vehicle
headlights alone will darken individual frames of a passage that was
otherwise perfectly readable.

What to read in the journal:

```
gate_direction trace_id=… verdict=exiting method=box_width slope=-0.3345 \
    frames=7 span_ms=6700 score=0.842 source=vehicle_box
gate_direction stage=counters estimates=25 entering=12 exiting=7 stationary=2 \
    unknown=4 night=0 no_boxes=8 no_passage=1 samples=71 disagreements=1 \
    reused_keys=0 evicted=0 expired=3
gate_direction stage=source_disagreement vehicle=exiting local=entering \
    cloud=- using=vehicle_box
gate_direction stage=passage_restarted camera=reolink_webhook|vehicle|… idle=41.2
```

One line per event, plus a rollup every 25 estimates. `estimates=` is the
number of events asked, so it is the denominator for every other counter on
the line. Per-frame widths are at `DEBUG` (`gate_direction stage=sample`).
The counters are journal-only on purpose: the app's heartbeat allowlist
(`PI_STATUS_CAPABILITY_KEYS` in access-gate-ui `worker/routes/controller.ts`)
has no slot for them, and a key the heartbeat has not been taught is dropped
rather than stored.

Two lines are worth watching during the shadow week:

- `stage=source_disagreement` — two box series of one passage fitted
  contradicting verdicts. The verdict shipped is the `using=` one: the
  vehicle box first (it is the series the analysis measured), then the
  on-device plate box (densest — that detector runs on every frame), then
  the cloud plate box (it only exists where a paid lookup returned a
  result). Counted as `disagreements=` in the rollup.
- `stage=passage_restarted` — a camera alarm identity arrived again after
  its passage had gone quiet, so a fresh passage was started rather than the
  two vehicles' boxes being pooled. This camera has a history of repeating a
  webhook body verbatim and the alarm key is a hash of `alarmTime`, so this
  is expected to be non-zero. Counted as `reused_keys=`.

**Promotion rule.** This stays shadow until **zero** false `exiting` verdicts
on a passage hand-labelled entering, over at least **100** labelled passages
— against the 42 available today. Exit recall is secondary: a missed exit
costs lookups, a false exit locks a household member out. Review weekly with
the same contact-sheet method the analysis used, and re-fit the thresholds
after the shadow week rather than treating them as calibrated; every one of
them was chosen on 22 points.

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
rejects the whole body with `400`. The five-minute rollup described below is
what posts to it, and that rule is why its wire format is an allow-list
checked against a copy of the app's own contract in
`tests/test_metrics_contract.py`.

The heartbeat's `cloud` block also carries `recognition_lookups_month_to_date`
and `recognition_lookup_quota`, so the quota burn-down is live at 15 s rather
than up to five minutes stale. Both keys are in the app's heartbeat allow-list
already; nothing here invents one.

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

### Five-Minute Metrics Rollup

The heartbeat says what is true now; it keeps no history at all, because
`controller_status` is one row upserted every 15 s. The rollup is the history,
and it is deliberately the cheapest possible one: the Pi aggregates, the cloud
stores five-minute buckets, and a day of production traffic writes about 288
rows against a free-plan allowance of 100,000 a day.

`GATE_METRICS_ENABLED` (default `true`) adds **one** thread on a
`GATE_METRICS_ROLLUP_SECONDS` poll (default `300`, accepted range 60-3600,
quantised down to a whole number of five-minute buckets and never below one)
inside the existing controller process -- no new systemd unit, no new
credential. The POST goes through the same `CloudflareServiceClient` and
Access service token as event ingest and the heartbeat.

**What it carries.** One entry per whole minute, oldest first, in whole
five-minute buckets, at most twelve buckets -- 60 minutes -- per POST, so a
25-minute outage replays completely on reconnect instead of leaving a hole:

```json
{"controller_id": "primary", "schema_version": 1,
 "minutes": [{"minute_start": "2026-09-08T10:20:00Z", "heartbeats": 4,
   "recognition": {"ocr_attempts": 3, "billed_lookups": 2, "recognized": 1,
     "unread_frames": 1, "ocr_error": 1, "ocr_timeout": 0, "ocr_busy": 0,
     "http_429": 1, "ocr_ms_p50": 940, "ocr_ms_p90": 2000,
     "local_attempts": 3, "local_recognized": 1},
   "cloud": {"recognition_lookups_month_to_date": 412,
     "recognition_lookup_quota": 2500}}]}
```

| Key | What it counts, per minute |
| --- | --- |
| `heartbeats` | Heartbeat POSTs that were acknowledged in that minute |
| `ocr_attempts` | Every OCR attempt the burst pipeline made. A burst is counted when it finishes but credited to the minute it *started* in, so one that runs past a boundary lands where it happened rather than a minute late; a start the ring cannot trust -- in the future, more than one bucket old, or inside a bucket already delivered -- falls back to the minute in progress |
| `billed_lookups` | The attempts Plate Recognizer charges for -- see below |
| `recognized` | Attempts that returned a plate |
| `unread_frames` | Attempts that returned no plate. **Never `no_plate`:** the contract refuses any key matching `/plate/` and rejects the whole body for it |
| `ocr_error`, `ocr_timeout`, `ocr_busy` | Failed, abandoned at the decision deadline, and never sent because the OCR slot was taken |
| `http_429` | Requests the 1 req/s throttle refused, counted in the minute the throttle happened in rather than the minute the burst finished. The client retries and usually succeeds, so without this counter a throttled request leaves no trace outside the journal. A 429 in a minute the ring never opened is counted in the drain minute instead -- late rather than lost. The rollup cycle drains the client's counter as well as a finished burst, so a 429 on the last burst before a quiet spell is attributed before its own bucket closes |
| `ocr_ms_p50`, `ocr_ms_p90` | Attempt durations, from a bounded per-minute sample |
| `local_attempts`, `local_recognized` | Frames the on-device reader completed, and events it answered |
| `recognition_lookups_month_to_date`, `recognition_lookup_quota` | The burn-down against the 2,500/month allowance |

**The billing rule.** The allowance is charged for a request the service
actually processed, so the first question is not what an attempt read but
whether a request went out at all.

**A read the on-device recogniser answered is not billed**, because in
`GATE_LOCAL_OCR_MODE=active` with `GATE_LOCAL_OCR_CLOUD=fallback` it returns
before any request is posted -- that saving is the entire point of on-device
recognition, and billing it would erase the saving from the burn-down it is
supposed to show. `GATE_LOCAL_OCR_CLOUD=always` is the opposite case: the
local read decides *and* the request goes out to label the frame for the
corpus, so the lookup is spent and is billed. What decides is whether a
request was made, never which reader answered.

Given a request did go out:

- **Billed:** `recognized` and `unread_frames` (a 2xx with or without a plate);
  `read_timeout` -- the request was sent in full and the reply never arrived,
  which is exactly why `ocr.py` refuses to retry it; `ocr_timeout` -- see
  below; and the response-shape failures (`invalid_json`, `invalid_payload`,
  `invalid_results`, `invalid_result_entry`, `invalid_confidence`,
  `invalid_response`, `no_usable_plate`), which can only arise after a
  response body was received.
- **Not billed:** `ocr_busy` (never left the Pi), `connect_timeout`,
  `tls_error`, `connection_error` and `request_error` (never arrived), and
  every `http_*` cause including `http_429` -- a throttled request is refused
  before it is processed.
- **`ocr_timeout` is billed when the request went out, and only then.** An
  abandoned request that was posted is the same physical event as a
  `read_timeout` -- the reply did not come back in time -- seen from the
  decision's clock rather than the socket's, so classifying the two
  differently made the burn-down disagree with itself depending on which
  timer fired first. But "the request went out" is not what `ocr_started`
  means: it fires when the request *thread* starts, and between that and
  `session.post` sit the client's 1.05 s pacing window (the service allows
  one request a second), the downscaled upload and the on-device guard. The
  second frame of a burst waiting out that window and being abandoned there
  is the ordinary case, not a corner, and billing it billed the throttle. So
  the dispatch site says whether a request was posted, the processor carries
  that on the attempt as `cloud_lookup`, and the burn-down counts it. The
  residual error is a recogniser that cannot report its dispatches, which
  keeps the old assumption that a cloud read posts; every recogniser this
  controller ships reports.

**Where the month-to-date figure comes from: the controller itself.** It is
its own count, persisted in `metrics-quota.json` beside the database and reset
when the UTC month changes, not a figure read back from the service. The
Snapshot API reference documents no usage endpoint for the **cloud** API --
`total_calls` and `usage.calls` are returned by the on-premise `/info/`
endpoint, which this deployment does not run; it posts to
`https://api.platerecognizer.com/v1/plate-reader/`. Even if a cloud usage
endpoint were available it would put a second dependency on the token the
decision path holds, and the app's contract asks for "the controller's own
count" precisely because it cannot reconstruct one: most attempts return no
plate and never become an event in D1. Cross-check it against the Plate
Recognizer account dashboard rather than against anything the controller says.
The allowance itself is configuration (`GATE_RECOGNITION_LOOKUP_QUOTA`,
default `2500`), not a measurement -- if the plan changes, change the variable.

The counter is written by the rollup thread, never by the burst that counted
it: `record_billed` takes a lock around one integer and sets a flag, and the
`fsync` and `os.replace` happen on the next rollup cycle. An SD-card write on
the path that has just opened the gate is not worth the at most one rollup
period of counting a power cut could cost.

**If the counter cannot be vouched for, it is not reported at all.** When the
stored ledger comes back unreadable, or the last write failed, the two quota
keys are simply left out of the heartbeat and the rollup, so the tile reads
"not reported". A stamped `0` would read as a full allowance still to spend on
the day the card stopped taking writes, which is the reading that lets the
gate quietly stop opening. There is no status token to send instead: the app
narrows the heartbeat's `cloud` block with `boundedNumbers(value,
CLOUD_CEILINGS)`, which accepts numbers only, so the absence *is* the signal
and the transition is journalled (`gate_metrics stage=quota_unreported`). A
failed read clears when the month rolls over, because a new month's zero is a
number the controller is sure of again; a failed write clears on the first
one that succeeds.

**Whole buckets, once each.** The app stores one row per five-minute bucket
and `upsertControllerHealth` **replaces** that row with whatever arrives for
it (`do update set metrics = excluded.metrics`), then advances the quota
ledger by *(incoming minus stored)* for the same buckets. A post landing three
minutes into a bucket therefore does not fill the row in later -- the next
post throws those three minutes away and walks the ledger back to match. So:

- a minute is offered only once the whole **bucket** it belongs to has closed,
  not merely once the minute has; and
- a POST carries a whole number of buckets, at most twelve, because
  `storedBucketMetrics` reads back at most twelve to compute that delta and a
  thirteenth would be counted again on every re-send.

The cadence is aligned to the wall clock rather than to when the thread came
up: the worker wakes a few seconds after each five-minute boundary, so the
bucket it posts is the one that has just closed.
`GATE_METRICS_ROLLUP_SECONDS` is quantised down to a whole number of buckets
and never below one, so `60` behaves as `300` -- there is nothing new to send
before a bucket closes.

**What it will not do.** The rollup ranks below event delivery, which ranks
below the gate:

- it stands down entirely while a gate decision is in flight, on the same
  `ActivityGate` the corpus uploader uses -- before the ledger write as well
  as before the POST, since the `fsync` and `os.replace` are the half of a
  cycle that touches the card the decision is on;
- it holds no lock across the POST, so a stalled endpoint cannot block the
  pipeline that fills the ring;
- a failure backs off from 5 s to 5 minutes rather than retrying in a tight
  loop -- the behaviour that made the 2026-09-05 D1 outage worse -- and the
  minutes stay pending until a 2xx. The backoff is a schedule the worker
  keeps: a pending retry pulls the next wake earlier than the boundary would.
  No wait is ever below 5 s, on either path: a cycle that runs just before a
  boundary would otherwise compute a wait of a millisecond and come straight
  back, flushing the ledger each time, for a bucket that has already closed;
- the ring is memory only and fixed at 180 minutes; the oldest minute is
  dropped when it is full, and nothing but the small month-to-date counter is
  written to the SD card;
- no bucket is ever delivered in pieces, so none is half-counted, and each
  `bucket_start` goes out once.

**In the journal**, one line per state change, never one per cycle:

| Line | Meaning |
| --- | --- |
| `gate_metrics stage=rollup_failed detail=<http_status or error class> consecutive=<n>` | The POST failed; repeated at most every ten minutes |
| `gate_metrics stage=rollup_recovered failures=<n>` | It is delivering again |
| `gate_metrics stage=deferred reason=<activity>` / `stage=resumed` | Stood down for a gate decision, and back |
| `gate_metrics stage=ring_full dropped=<n> capacity=<n>` | Minutes are ageing out undelivered -- the cloud path has been down for hours |
| `gate_metrics stage=quota_write_failed detail=<error class>` / `stage=quota_read_failed` | The month-to-date counter could not be persisted or read; counting continues in memory |
| `gate_metrics stage=quota_unreported detail=<load or write>` / `stage=quota_reported` | The burn-down is being withheld because the ledger cannot vouch for it, and the moment it can again. The tile reads "not reported" in between, never `0` |
| `gate_metrics stage=record_failed` / `stage=cycle_failed` | A metric was dropped rather than allowed to raise into the pipeline |

Setting `GATE_METRICS_ENABLED=false` constructs nothing: no ring, no ledger,
no worker, no thread.

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
without ever holding it — and the child runs at `nice 19` under `RLIMIT_AS` of
`GATE_AUDIO_CAPTURE_MAX_ADDRESS_SPACE_BYTES` (default 1 GiB, floor 256 MiB,
ceiling 2 GiB).

**Why 1 GiB and not the network probe's 64 MiB.** `RLIMIT_AS` bounds address
space, not resident memory, and it counts every shared library the loader maps
before ffmpeg runs a line of its own code. The first limit here was 128 MiB and
it was too small for that alone: on the Pi on 2026-09-08 every capture died
55 ms in with `outcome=failed reason=exit_status`, and the child's stderr said
`error while loading shared libraries: libcodec2.so.1.0: failed to map segment
from shared object`. The same command under `ulimit -v 131072` reproduces it;
without the limit it produced 24,393 bytes of valid AAC in 3 s. Resident memory
was never the issue — a `-vn -c:a copy` stream copy decodes nothing, so RSS
stays at 2.5–50 MB. 1 GiB clears the mappings and is still far below the 3.4 GB
runaway ffmpeg that OOM-killed the board on 2026-09-07, which is what the limit
exists to stop. The probe's `ping` children are unaffected and keep their own
64 MiB.

**A failed capture says why.** The child's stderr is read alongside its stdout
(never after it, so a chatty child cannot block on a full pipe) and the last
200 characters are journalled on the failure line:

```
gate_audio_capture outcome=failed reason=exit_status stderr=error while loading shared libraries: libcodec2.so.1.0: failed to map segment from shared object
```

The same tail appears as `last_capture.stderr` in the audio section of status.
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
managed deployment, the private runtime-lock directory, and
`/usr/local/libexec/gate-controller`, which holds the fixed helper the unit
executes and which the updater refreshes for itself (below). It has no device
access or privilege escalation and retains only the capabilities needed to
change release ownership and drop candidate commands to
`gate-controller-build`.

The active release plus two prior releases are retained by default. Pruning only
runs after successful activation and only removes inactive directories whose
names are full commit SHAs.

### The Updater Ships Its Own Fixes

`gate-controller-updater.service` does not execute the updater out of the
release tree; it executes the fixed helper at
`/usr/local/libexec/gate-controller/gate-controller-updater.py`, so that a bad
release cannot rewrite the program that would otherwise have to roll it back.
For a long time only `deployment/install.sh` ever wrote that path, which meant
every fix to the updater itself shipped to nobody: the Pi kept running whichever
helper was last installed by hand. On 7 September 2026 that cost an hour and
three quarters of gate availability — the fix that stopped the updater running
the full 17-to-40-minute on-device suite had been merged and adopted, but the
installed helper predated it, so the Pi kept re-running that suite at 84 °C for
six more hours.

Now, immediately after a release is activated and confirmed healthy, the updater
copies that release's `deployment/gate_controller_updater.py` over the installed
helper when the two differ, and journals:

```text
gate-controller-updater: refreshed installed updater from release <sha>
```

The rules it works to:

- **Only from a release that already passed both gates.** The copy happens only
  at the one call site where the release has a completed, successful exact-SHA
  CI run (`has_successful_ci_run`) *and* has passed on-device `verify_release`
  *and* is now the live `current` target. An unverified or deferred release
  never reaches the helper path.
- **It cannot disturb the run that performs it.** The running process is that
  very script, but CPython read and compiled the whole file before `main()` was
  entered and never re-reads it, and `os.replace` swaps a directory entry rather
  than writing through the old inode. The refreshed helper first runs at the
  next timer firing, five minutes later.
- **Atomic, root-owned, `0755`.** The new content is written to a temporary file
  beside the helper, fsynced, chmodded, and renamed into place, so no reader
  ever sees a partial file and no staging file is left behind.
- **`py_compile` before replacing.** A helper this interpreter cannot compile is
  refused and the installed one is kept.
- **A failed refresh never fails the activation.** The release is already active
  and healthy by that point, so a refusal or a write error is logged as
  `Release activated but the installed updater was not refreshed: ...` and
  nothing is rolled back. The consequence is only a stale helper until the next
  release.
- A symlinked helper, a symlinked source, a missing helper directory, or a
  release with no `deployment/gate_controller_updater.py` are all refused rather
  than followed or created.

`deployment/install.sh` still installs the helper itself during bootstrap, and
still backs it up and restores it on rollback. The two paths agree on content,
mode, and ownership.

#### One-time manual install, once this change is live

This change cannot install itself: the helper currently on the Pi does not
contain the refresh, so it will adopt this release without refreshing anything.
The unit also has to be reinstalled, because the refresh writes into
`/usr/local/libexec/gate-controller` and the previously installed unit does not
list that directory in `ReadWritePaths` under `ProtectSystem=strict`.

Once the timer has adopted a release containing this change — confirm with
`readlink -f /opt/gate-controller-deploy/current` — run this **once**:

```sh
RELEASE=$(readlink -f /opt/gate-controller-deploy/current)
sudo install -o root -g root -m 0755 \
  "$RELEASE/deployment/gate_controller_updater.py" \
  /usr/local/libexec/gate-controller/gate-controller-updater.py
sudo install -o root -g root -m 0644 \
  "$RELEASE/deployment/systemd/gate-controller-updater.service" \
  /etc/systemd/system/gate-controller-updater.service
sudo systemctl daemon-reload
```

Then confirm the helper and the release now agree, and that a poll still runs
clean:

```sh
sudo cmp "$RELEASE/deployment/gate_controller_updater.py" \
  /usr/local/libexec/gate-controller/gate-controller-updater.py
sudo systemctl start gate-controller-updater.service
sudo journalctl -u gate-controller-updater.service -n 50 --no-pager
```

From the next release onwards this is automatic. `cmp` staying silent is the
check worth repeating after any updater change; if the two ever diverge again,
the journal line naming the refresh — or the warning explaining why it did not
happen — is in `journalctl -u gate-controller-updater.service`.

One gap is left deliberately unclosed: the refresh runs on the activation path
only. If a release is activated and the process dies before the refresh, or an
interrupted activation is completed by the reconciliation path on the next run,
that release will not refresh the helper — the following release will.

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

Deploy it in this order. The ordering matters: the Access application must exist
before any DNS name resolves to this service, or the window between the two is
an unauthenticated camera control on the public internet.

1. **Write `/etc/gate-camera-control.env` first**, root:root 0600. One
   `KEY=value` per line, no whitespace around the `=` or at the end of a line, no
   quotes around values (they become part of the password), and a trailing
   newline.
2. **Validate it before installing anything**:
   `sudo python3 gate_media_config.py camera-control --env /etc/gate-camera-control.env`.
   Silence means valid. The installer runs the same check before it publishes,
   so a rejected file never replaces the running library.
3. **Run the installer**:
   `sudo deployment/install-camera-control.sh --source "$PWD"`.
4. **Verify locally**, with `systemctl status gate-camera-control` and
   `curl -s http://127.0.0.1:8767/camera/state`, and confirm the egress pin is
   really in force: `systemctl show gate-camera-control -p IPAddressAllow` must
   list the camera's `/32`, and the unit's journal must carry no
   "IP firewalling not supported" or "Failed to install BPF" line. On a kernel
   without cgroup BPF `IPAddressDeny=` is silently ignored and the service can
   reach the whole LAN, which voids the isolation the design rests on.
5. **Create the Cloudflare Access application and a separate service token for
   it — before any DNS route or ingress rule exists.** Not the `gate-command`
   token: the blast radius is different.
6. **Then** add the ingress rule to
   `deployment/cloudflared/gate-controller-tunnel.yml` ahead of the catch-all,
   create the DNS route, and reload cloudflared.
7. **Store the service token in the Worker**, exactly as the `gate-command`
   token is stored. The token never goes near the Pi.

Re-run the installer on **every** controller release. Activation deliberately
does not republish `/usr/local/lib/gate-camera-control` — that path sits outside
the managed release tree so an auto-update cannot silently change the one process
holding camera credentials — so a release that changes `gate_camera_control/` or
`gate_media_config.py` does not reach the running service until
`sudo deployment/install-camera-control.sh --source /opt/gate-controller-deploy/releases/<sha>`
is run.

The full HTTP contract, environment keys, journal lines, failure modes, and
rollback are in [Gate camera control](camera-control.md).

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
