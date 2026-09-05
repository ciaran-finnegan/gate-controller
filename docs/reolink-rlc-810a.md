# Reolink RLC-810A Deployment and Night Calibration

The installed gate camera is an RLC-810A: fixed 4 mm lens, no optical zoom, no
two-way audio. Earlier issues that call the installed unit an RLC-811A are
mislabelled. The gate has one Ethernet port, so only one camera can be fitted.
The RLC-811A replaces the RLC-810A at the same mount; its zoom framing,
exposure, capture point, and cutover are covered in
[RLC-811A gate camera swap](reolink-rlc-811a.md). Everything below applies to
whichever camera is fitted unless that document says otherwise.

## Network Boundary

Connect the RLC-810A and Raspberry Pi to the private LAN or a dedicated camera
VLAN. Give the camera a DHCP reservation and allow it to initiate FTP uploads
to the Pi. Allow only the Pi media gateway to initiate RTSP requests to the
camera's reserved private address.
Do not forward RTSP, ONVIF, the Reolink web interface, FTP, HTTPS, or the Pi GPIO
interface to the internet. Remote users use the authenticated web application,
which sends an Access-protected request through Cloudflare Tunnel to the Pi's
loopback command server. The Pi applies the local safety policy.

Use a dedicated `ftp-user` with a home or upload directory at
`/var/lib/gate-controller/uploads`. Limit the FTP service to the camera VLAN
and the camera address where possible. Use FTPS when both camera firmware and
the FTP server support it. The controller only accepts completed `.jpg` or
`.jpeg` files. FTP remains the trigger; recent OCR candidates come from the
continuously decoded clear RTSP stream when it is healthy.

Create `ftp-user` before running the gate-controller bootstrap. Bootstrap adds
it to the `gate-controller` group and creates the upload directory with setgid
group-write permissions. Configure the FTP daemon with a `0007`-equivalent umask
so uploaded JPEGs remain group-readable by the controller; for vsftpd this is
`local_umask=0007`.

## FTP Burst Setup

In Reolink Client or the camera web interface, open **Device Settings >
Surveillance > FTP**:

1. Enable FTP, configure the Pi's private address, port, `ftp-user`, and its
   upload directory, then use **Test** before enabling scheduled uploads.
2. Select image upload. Configure the **Alarm** schedule for the times when
   gate access should be recognised; do not use a broad continuous upload
   schedule unless operationally required.
3. Select **Vehicle** detection where the firmware exposes per-type selection.
   Disable generic motion uploads for this pipeline so rain, foliage, and
   headlight changes do not create recognition bursts.
4. Leave overwrite disabled. The Pi groups the short burst of unique completed
   JPEGs, validates them, then ranks frames by sharpness.

Every first completed FTP JPEG is released to ranking and OCR after the normal
quiet window. Without a correlated authenticated event it is logged as
`camera_ftp/unverified`: an FTP upload alone does not prove whether the camera
used generic AI vehicle, line crossing, manual/test, or another alarm subtype.
Do not label it as line crossing without a separate authenticated event signal.

## Authenticated Trigger Provenance

The camera can send metadata about the rule that caused an upload to the
controller's bounded webhook endpoint. This metadata is observability only: it
never authorizes a vehicle, changes matching, invokes the relay, or actuates the
gate. The initial FTP recognition does not wait for the webhook or any new
camera connection; the controller performs a nearest-event lookup from bounded
in-memory state and otherwise retains the `camera_ftp/unverified` fallback.

Put a random 20-128 character secret in the root-readable
`/etc/gate-controller.env` file. Use letters and digits only: the secret is
also copied into the camera's webhook URL below, where `+`, `&`, `#` and `%`
would be split or decoded by the query parser and every post would be
rejected with 401.

```sh
GATE_REOLINK_WEBHOOK_SECRET=
GATE_REOLINK_WEBHOOK_HOST=0.0.0.0
GATE_REOLINK_WEBHOOK_PORT=8766
```

In the camera's Webhook settings (Surveillance > Push > Webhook), keep
**Content: Default** and use this private-LAN URL with the same secret as a
query parameter:

```text
http://PI_PRIVATE_ADDRESS:8766/reolink/events?secret=same-random-secret
```

The firmware sends a **Custom** body verbatim: it does not substitute
placeholders such as `time` or `chn`, so a custom body can never carry the
real alarm time or type. The default body does, and it looks like this
(`channel` is a JSON number and `alarmTime` has a `+0000` offset, both of
which the controller accepts). The controller bounds the whole request and
each relevant field, then retains only normalized type, rule, event time, and
correlation timing:

```json
{
  "alarm": {
    "alarmTime": "2026-09-04T21:28:22.000+0000",
    "channel": 0,
    "channelName": "Front Gate",
    "device": "Front Gate",
    "deviceModel": "RLC-810A",
    "message": "...",
    "name": "...",
    "title": "...",
    "type": "VEHICLE"
  }
}
```

A top-level `"secret"` in a custom body is still accepted, and a wrong body
secret is never rescued by the URL. The camera's own **Test** button posts
`"type": "TEST"`, which the controller records as `manual_test` and never
captures from, so the journal line `gate_trigger_capture outcome=skipped_type`
after a test proves the URL, secret and listener end to end. Through the
camera API the same settings are `SetWebHook` with a base64-encoded
`hookBody`; `TestWebHook` sends the test post.

Top-level camera `test` and `manual` notifications are recorded as
`manual_test`.

### Webhook-Triggered Capture

Vehicles stop at the closed gate, and a stopped vehicle is the sharpest plate
the camera can give. When the listener is enabled, an accepted `vehicle`,
`line_crossing`, or `other` event schedules a short series of full-resolution
frames from the loopback MediaMTX clear path (`rtsp://127.0.0.1:8554/clear`),
each handed to the normal burst pipeline. With
`GATE_TRIGGER_CAPTURE_HOT_KEYFRAMES=true` (the default) the controller keeps
the clear stream's keyframes decoded continuously (one 4K decode per second
at the camera's 1x interval), so
the first frame of the series is the keyframe taken moments before the alarm
and is injected the instant the webhook arrives, ahead of the FTP upload. The
controller then waits `GATE_TRIGGER_CAPTURE_DELAY_SECONDS` for the vehicle
to come to rest and takes the remaining frames of `GATE_TRIGGER_CAPTURE_COUNT`
`GATE_TRIGGER_CAPTURE_SPACING_SECONDS` apart, each newer than the last. A stale
ring falls back to an on-demand grab (`keyframe=unavailable fallback=grab`).
The journal line for each frame carries `source=keyframe|grab` and
`frame_age_ms`.

Decoding and re-encoding 4K HEVC in software costs most of one Pi 5 core and
about 330 MB, enough to push a fanless Pi 5 into thermal throttling. Set
`GATE_TRIGGER_CAPTURE_HWACCEL=drm` to decode through the Pi 5's hardware HEVC
block (the service needs the `video` and `render` groups, which the unit
grants) and `GATE_TRIGGER_CAPTURE_FRAME_WIDTH=1920` to scale frames before the
JPEG encode. Together they cut the decoder to about a fifth of a core.
Recognition loses nothing: OCR uploads are already downscaled to
`GATE_OCR_MAX_UPLOAD_WIDTH=1920` and evidence to 1280. Both settings apply to
the on-demand grab as well. Measured on the RLC-810A stream over ten
seconds: software 4K 8.2 s CPU, hardware decode plus 1920 scale 2.0 s. The default `GATE_TRIGGER_CAPTURE_COUNT=3` keeps the series
covering the stop after the immediate frame. The journal records one of these scheduling outcomes per event:
`scheduled`; `disabled` when `GATE_TRIGGER_CAPTURE_ENABLED=false`;
`skipped_type` for `manual_test` events, which never capture;
`skipped_interval` inside `GATE_TRIGGER_CAPTURE_MIN_INTERVAL_SECONDS` of the
previous series; and `skipped_busy` while a series is already queued. A
skipped event still has its FTP frame recognised as before.

- The webhook still authorises nothing. Every captured frame goes through the
  same recognition, authorisation, claim, cooldown, and relay code as an FTP
  upload. A forged webhook can at most cost one bounded capture series and
  its OCR requests, rate limited to one series per
  `GATE_TRIGGER_CAPTURE_MIN_INTERVAL_SECONDS`.
- The FTP path is unchanged. The 4K FTP JPEG is still taken at the line
  crossing; place the line where the vehicle is already rolling to a halt.
  Whichever frame reads an authorised plate first opens the gate and the
  later bursts record `cooldown`. Each frame is a separate event with its own
  content identity on purpose: a frame that fails to read must never
  suppress the next one, so an unrecognised or denied vehicle produces one
  event per frame rather than one per crossing.
- Capture is serial and bounded: one ffmpeg child at a time, killed at the
  timeout, frames validated as JPEG and written owner-only under
  the controller state directory (`trigger-capture` beside the database,
  or `GATE_TRIGGER_CAPTURE_DIRECTORY`), never under the FTP upload tree.
- Set the camera's Clear stream **I-frame interval** (Interframe Space) to
  1. An on-demand grab decodes only from the next keyframe, so a longer
  interval adds up to that many seconds; with the interval at 2 the grab took
  about 2.7 s against the default 2.5 s timeout and only the FTP path ran.
  The grab skips ffmpeg's stream probing and every frame before the first
  keyframe: a frame decoded against a synthetic grey reference is a valid
  JPEG of nothing, and used to cost an OCR request. Measured on the Pi 5 at
  4K H.265 with the interval at 1 the grab takes about 1.8 s.
- MediaMTX pulls both camera profiles over TCP (`rtspTransport: tcp` per
  path). The automatic transport used UDP, and on this LAN the 4K stream
  logged lost RTP packets and "invalid fragmentation unit" errors, which
  corrupt exactly the keyframes capture depends on.

```sh
GATE_TRIGGER_CAPTURE_ENABLED=true
GATE_TRIGGER_CAPTURE_SOURCE=rtsp://127.0.0.1:8554/clear
GATE_TRIGGER_CAPTURE_TIMEOUT_SECONDS=3
GATE_TRIGGER_CAPTURE_HOT_KEYFRAMES=true
GATE_TRIGGER_CAPTURE_DELAY_SECONDS=1.5
GATE_TRIGGER_CAPTURE_COUNT=3
GATE_TRIGGER_CAPTURE_SPACING_SECONDS=1
GATE_TRIGGER_CAPTURE_MIN_INTERVAL_SECONDS=5
```

Watch the journal for `gate_trigger_capture outcome=captured` followed by the
recognition trace. `outcome=failed reason=timeout` on every event means the
keyframe interval is still too long or the clear path is not being served.
Each captured frame is an extra OCR request, so with the default series of
three expect roughly four Plate Recognizer calls per vehicle instead of one.

### OCR Request Pacing

Plate Recognizer's cloud API allows one request per second per account and
answers a faster follow-up with HTTP 429 (`Retry-After: 1`), counted from
when the previous upload finished arriving. The controller paces every
request process-wide from the previous response (1.05 s), retries a throttled
or connection-level failure once, and recycles a keep-alive connection that
has been idle for 20 s because the API closes those server-side and the
next post on one fails instantly. The journal shows the retry as
`gate_ocr stage=retry cause=http_429 wait_ms=...`. Before this pacing, the
hot-stream burst of three frames lost its second request to a 429 on every
vehicle from the day fluent frames joined the burst.

Because each paced call costs at least a second, keep
`GATE_DECISION_TIMEOUT_SECONDS` at 6, and turn the fluent fallback off
(`GATE_HOT_STREAM_ENABLED=false`) once webhook capture is running: across
the recorded event telemetry the 640x360 fluent frames read a plate in 3 of
189 attempts while each cost about 0.7 s of the throttle window, whereas 4K
frames read one in four. The webhook keyframes supply the sharp frames now. A matching first frame still opens the gate as soon as its
own request returns; the deadline only bounds the fallbacks.
Tune the delay from the captured frames: movement means wait longer, a
vehicle already stopped in the FTP frame means the delay can be shorter. Invalid, stale, duplicate, unauthorized, and oversized requests
cannot reach recognition or relay code. Raw payloads, camera identifiers, and
the secret are not logged or stored. Restrict TCP/8766 with the host firewall or
camera VLAN so only the camera can connect, and do not port-forward it.
No nginx or ONVIF listener is needed or installed for trigger provenance.

The moved-line experiment baseline is vehicle-only line crossing, sensitivity 80,
with the line spanning the driveway at the configured inbound capture point.
Keep that baseline fixed while collecting matched and unverified events; change
one camera variable at a time only after comparing missed entries and false
positives across day, night, rain, and headlights.

## Continuously Hot Recognition Stream

MediaMTX continuously pulls both camera profiles: `camera` is Fluent and
`clear` is Clear. The controller keeps one ffmpeg decoder attached to the
loopback fluent path from service startup, so an FTP event never opens a new
stream or waits for the camera's roughly five-second keyframe interval.

The decoder samples Fluent at 5 fps into an eight-frame, byte-bounded in-memory
ring. On the first completed FTP JPEG, the two newest distinct fluent frames
are materialised into an owner-only ignored directory and added to the same
200 ms burst. The high-resolution FTP image remains the first OCR attempt, then
the fluent fallbacks use the remaining two requests in quality order. There is
no on-trigger HTTPS request, second OCR queue, or additional recognition
cooldown. If the stream is unavailable, the original FTP/cloud path proceeds
unchanged.

Enable the reviewed preset in `/etc/gate-controller.env`:

```sh
GATE_HOT_STREAM_ENABLED=true
GATE_HOT_STREAM_SAMPLE_FPS=5
GATE_HOT_STREAM_FRAME_COUNT=8
GATE_HOT_STREAM_SELECTION_COUNT=1
GATE_HOT_STREAM_MAX_AGE_SECONDS=1
```

Camera credentials remain only in `/etc/gate-media-gateway.env`; the controller
connects only to `rtsp://127.0.0.1:8554/camera`, and the web UI receives
only non-secret effective profile and health fields.

The exact labels vary by firmware. The installed RLC-810A has a fixed 4 mm lens,
supports vehicle detection and FTP upload, and has no optical zoom. Firmware
should be current before commissioning. Live media still uses the separate
authenticated media gateway because camera credentials must never be sent to
the browser.

## Stream and Image Settings

Use the following measured starting point for rapid vehicle recognition:

- **Clear/main:** 3840x2160, 10 fps, H.265, 6144 Kbit/s, I-frame interval
  1x (a keyframe every second) so webhook-triggered capture never waits
  longer than that for a decodable frame.
- **Fluent/sub:** 640x360, 10 fps, H.264, 256 Kbit/s.
- **Frame Rate Mode:** Constant.

Ten camera frames per second bounds source-frame delay to about 100 ms. The
high-resolution FTP JPEG remains the first OCR attempt. The controller also
continuously decodes Fluent and retains JPEG fallbacks at 5 fps, adding the two
newest frames only when an FTP trigger arrives. Clear remains continuously
available through MediaMTX without imposing a permanent 4K software-decode load
on the Pi.
Keeping 6144 Kbit/s at 10 fps preserves more detail per Clear frame. Frame rate
does not freeze a moving plate by itself; exposure time controls motion blur.

`GATE_OCR_MAX_UPLOAD_WIDTH` optionally downscales frames wider than the value
before they are sent to OCR; the file on disk is untouched. Set it to `1920`:
the property uplink measured about 4.5 Mbit/s, so a 1.2-2.2 MB 4K JPEG spends
2-4 s uploading while Plate Recognizer's own processing takes about 60 ms, and
at 1920 the same frame uploads in well under a second. A plate 300 pixels wide
at 4K stays above the 150 pixel target at 1920. Only drop back to `0` if a
saved capture shows plates narrower than that at the capture point.

The privacy mask may black out irrelevant scenery, but it must leave the whole
vehicle approach, plate capture corridor, and position variance unobscured.
Masked pixels cannot be recovered later. Do not treat the mask as the OCR crop;
software crops are taken from the remaining 4K image after capture.

## Detection Zone and Position

Draw a tight vehicle-detection zone around the approach and stop position. Do
not include public pavement, adjacent driveways, trees, or reflective signs.
Set a minimum object size that includes a car at the farthest intended trigger
point but excludes distant traffic. Start with moderate vehicle sensitivity;
raise it only after confirming approaching vehicles are not missed.

Keep the lens forward of the fence plane so nearby timber, rain droplets, and
integrated IR cannot dominate exposure or reflect into the cover. A practical
starting height is 1.5-2 metres, above most headlights, with the camera pointed
slightly down at one repeatable capture point: where vehicles stop at the
closed gate. A stopped vehicle removes motion blur entirely, so prefer the
stop over any point on the approach.

Because the RLC-810A lens is fixed, frame the capture point by physically aiming
or relocating the camera and use a software crop only after capture. The
RLC-811A frames the capture point by zoom instead; see its swap document. A useful
commissioning target is a plate around 150-250 pixels wide in the 4K JPEG, with
combined horizontal and vertical plate angle below 30 degrees and ideally
10-20 degrees. Keep the motion light outside the frame and avoid aiming it at
the plate or camera cover. Capture samples at the closest and farthest expected
positions before enabling relay output.

## Night Calibration

Night work is a balance: enough exposure for a legible plate, but little enough
motion blur and headlight bloom that characters remain distinct.

1. Calibrate after dark with representative vehicles, headlight states, wet
   road conditions, and a clean camera cover.
2. Start with auto day/night mode and compare IR-only, spotlight, and the
   existing motion light. Use only the combination that keeps the plate evenly
   exposed. If plates are washed out, lower brightness/exposure, reduce WDR,
   or alter the angle so headlights and the motion light do not point into the
   lens. If plates blur, favour a shorter exposure instead of more gain. Where
   firmware exposes shutter limits, start near 1/250 second and test no slower
   than 1/125 second with moving vehicles.
3. Tune day/night switching thresholds only after verifying that the camera is
   not repeatedly changing modes at dusk. Avoid settings that produce a burst
   of unstable frames during the vehicle approach.
4. Re-check the vehicle zone and sensitivity at night. Light changes can cause
   motion detections; use vehicle-only alerts and the tight zone before
   increasing sensitivity.
5. Repeat the test in rain, fog, wet-road reflections, and with dipped and full
   headlights. A small rain hood and clean cover help prevent IR reflection and
   droplets from obscuring the plate.
6. Retain a short, access-controlled local sample set while calibrating, then
   remove it according to the site's retention policy.

## Stage Timing

Each local event records `received_at`, `decision_at`, and, when opened,
`relay_activated_at`.

- `decision_at - received_at` measures file selection, OCR, and matching.
- `relay_activated_at - received_at` is the user-facing automatic-opening
  latency and should be monitored against the two-second median and
  four-second 95th-percentile targets.
- A long first interval points to uploads, OCR, image quality, or networking.
  A short first interval with no relay time can indicate an OCR no-match,
  authorization rejection, cooldown, duplicate, or relay error. Use the
  recorded outcome and reason to distinguish these cases from slow capture.

Control-plane heartbeats include the latest completed image path, SQLite outbox
queue depth, and whether a fixed local prompt is configured. They are health
signals, not an authority to bypass local recognition or relay safeguards.

## On-device Pi Performance Validation

The Cloudflare performance harness is available at
`scripts/pi-cloudflare-performance-harness.py` and is documented in
[`pi-cloudflare-performance.md`](pi-cloudflare-performance.md). Run it through
local-network SSH or directly on the Pi; Tailscale availability is not a
deployment prerequisite.

For a safe opportunistic check on the Pi, run the default non-actuating command
or use `--skip-network` when services are unavailable. The harness records its
run mode. Its network collection consists only of passive endpoint probes and
never submits a controller command.
