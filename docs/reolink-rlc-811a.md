# Reolink RLC-811A Deployment and Night Calibration

## Network Boundary

Connect the RLC-811A and Raspberry Pi to the private LAN or a dedicated camera
VLAN. Give the camera a DHCP reservation and allow it to initiate FTP uploads
to the Pi only. Do not forward RTSP, ONVIF, the Reolink web interface, FTP, or
the Pi GPIO interface to the internet. Remote users use the authenticated web
application, which sends an Access-protected request through Cloudflare Tunnel
to the Pi's loopback command server. The Pi applies the local safety policy.

Use a dedicated `ftp-user` with a home or upload directory at
`/var/lib/gate-controller/uploads`. Limit the FTP service to the camera VLAN
and the camera address where possible. Use FTPS when both camera firmware and
the FTP server support it. The controller only accepts completed `.jpg` or
`.jpeg` files. Its recognition burst does not depend on RTSP.

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
quiet window. It is logged as `source=camera_ftp subtype=unverified`: an FTP
upload alone does not prove whether the camera used generic AI vehicle, line
crossing, manual/test, or another alarm subtype. Do not label it as line
crossing without a separate authenticated event signal.

## Optional HTTPS Snapshot Augmentation

The controller can use the Reolink HTTPS Snap API to collect a later bounded
sample without delaying the first FTP OCR attempt. Put these settings in the
root-readable `/etc/gate-controller.env` file:

```sh
GATE_REOLINK_SNAPSHOT_BASE_URL=https://192.168.0.54
GATE_REOLINK_SNAPSHOT_USERNAME=
GATE_REOLINK_SNAPSHOT_PASSWORD=
GATE_REOLINK_SNAPSHOT_ALLOW_SELF_SIGNED=true
GATE_REOLINK_SNAPSHOT_COUNT=2
GATE_REOLINK_SNAPSHOT_TIMEOUT_SECONDS=2.25
GATE_REOLINK_SNAPSHOT_MAX_BYTES=4194304
```

Use the camera's current reserved private address and a dedicated least-privilege
camera account where the firmware supports one. The origin must be HTTPS with a
private or loopback IP literal and no credentials, path, query, or fragment.
Self-signed TLS must be opted into explicitly. Counts above four, timeouts above
three seconds, and response limits above the controller image ceiling are
rejected at startup.

The default takes two additional 4K snapshots sequentially under one global
2.25-second deadline. They are written temporarily beneath the upload root in
an ignored owner-only `.reolink-snapshots` directory, validated as bounded
JPEGs, and added together to a separate progressive OCR burst with the original
FTP `received_at`. Generated files cannot recursively request another sample.
They use the existing ranking, recognition, authorization, durable actuation
claim, and cooldown path; a camera trigger or snapshot never actuates the relay
by itself. Failure or unavailable configuration leaves the first FTP attempt
and its normal quiet window unchanged.

Do not configure on-demand ffmpeg sampling from
`rtsp://127.0.0.1:8554/camera` for this feature. Live Pi measurements took
4.88-5.44 seconds for three frames because capture waited for the roughly
five-second upstream H.264 keyframe interval. Sequential HTTPS snapshots took
about 0.78-0.96 seconds each, while concurrent requests serialized and produced
duplicates. RTSP remains available to the separately isolated media gateway;
it is not the recognition augmentation source.

The exact labels vary by firmware. The RLC-811A supports vehicle detection,
FTP upload, optical zoom, and two-way audio; firmware should be current before
commissioning. Live media and talkback still require the separate authenticated
media gateway because camera credentials must never be sent to the browser.

## Detection Zone and Position

Draw a tight vehicle-detection zone around the approach and stop position. Do
not include public pavement, adjacent driveways, trees, or reflective signs.
Set a minimum object size that includes a car at the farthest intended trigger
point but excludes distant traffic. Start with moderate vehicle sensitivity;
raise it only after confirming approaching vehicles are not missed.

For this entrance, mount the camera beside the outdoor cabinet on the right as
a vehicle approaches from the road, roughly 1-1.5 metres high. Aim across the
final part of the curve at one repeatable capture point before the gate rather
than along the whole driveway. This position should reduce direct headlight
glare, but confirm it after dark before fixing the bracket permanently.

Use the optical zoom to frame the capture point tightly. A useful commissioning
target is a plate around 150-250 pixels wide in the uploaded JPEG, with modest
horizontal and vertical angles. Keep the motion light itself outside the frame
and avoid aiming it directly at the plate or camera cover. Capture sample bursts
at the closest and farthest expected positions before enabling relay output.

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
  A short first interval with no relay time indicates a rejected policy,
  cooldown, duplicate, or relay error rather than a slow camera.

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
