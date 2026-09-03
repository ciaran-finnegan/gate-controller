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
`/etc/gate-controller.env` file:

```sh
GATE_REOLINK_WEBHOOK_SECRET=
GATE_REOLINK_WEBHOOK_HOST=0.0.0.0
GATE_REOLINK_WEBHOOK_PORT=8766
```

In the camera's Webhook settings, use this private-LAN URL:

```text
http://PI_PRIVATE_ADDRESS:8766/reolink/events
```

Use the camera's default JSON body and include the same secret at the top
level. The camera substitutes the placeholders; `channel` arrives as a JSON
number and `alarmTime` with a `+0000` style offset, both of which the
controller accepts. The controller bounds the whole request and each relevant
field, then retains only normalized type, rule, event time, and correlation
timing:

```json
{
  "alarm": {
    "alarmTime": "time",
    "channel": 0,
    "message": "message",
    "name": "name",
    "type": "type"
  },
  "secret": "same-random-secret",
  "type": "type"
}
```

Top-level camera `test` and `manual` notifications are recorded as
`manual_test`. Invalid, stale, duplicate, unauthorized, and oversized requests
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
GATE_HOT_STREAM_SELECTION_COUNT=2
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

- **Clear/main:** 3840x2160, 10 fps, H.265, 6144 Kbit/s.
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
slightly down at one repeatable capture point roughly 4-6 metres inside the
entrance after the vehicle has straightened.

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
