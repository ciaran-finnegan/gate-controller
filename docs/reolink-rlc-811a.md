# Reolink RLC-811A Gate Camera Swap

The RLC-811A was fitted on 2026-09-11 at about 20:00 Irish time. The camera it
replaced is an RLC-810A: fixed 4 mm lens, no optical zoom, no two-way audio.
The gate has one Ethernet port, so only one camera can be fitted. The RLC-811A
sits on the same pillar mount; the RLC-810A is removed and kept as the
rollback unit. The first week after the swap produced one gate opening,
because the steps below were not all carried out on the new unit; the
evidence and the order to do them in are in
[reviews/2026-09-16-rlc-811a-first-week.md](reviews/2026-09-16-rlc-811a-first-week.md). This document covers only
what differs from
[RLC-810A deployment and night calibration](reolink-rlc-810a.md). Network
boundary, FTP burst setup, authenticated trigger provenance, stream settings,
and the Pi validation harness apply unchanged to the fitted camera.

## Hardware Differences

| | RLC-810A (removed, rollback unit) | RLC-811A (installed) |
| --- | --- | --- |
| Lens | fixed 4 mm | motorised 2.7-13.5 mm, 5x optical zoom |
| Horizontal field of view | 87 degrees | 105 degrees wide to 31 degrees at full zoom |
| Aperture | f/2.0 | f/1.6 wide to f/3.3 at full zoom |
| Sensor | 1/2.49 inch 8 MP | 1/2.8 inch 8 MP |
| Two-way audio | no | yes |
| Power | PoE | PoE |

Values are from Reolink's specification sheets. The sensors are close in size
and resolution, so per-pixel image quality is similar rather than identical.
The gain from the RLC-811A is entirely
framing: the zoom puts more of the 3840-pixel frame width on the plate. Two-way
audio is a separate concern, covered in [Push-to-talk](talkback.md). On the
fitted camera (firmware `v3.1.0.4695_2504301440`, hardware `IPC_560B158MP`)
the talk channel negotiates `adpcm16000x1024`: 16 kHz mono DVI ADPCM in 516-byte
blocks, full duplex. It passed the remote check (`scripts/talk_tone_check.py`)
on 2026-09-19, and two of that check's measurements shape the service:
- the speaker plays into the camera's own microphone at about -8 dBFS
- the camera holds about 0.5 s of talk audio, which `TalkReset` throws away

The supervised acceptance test at the gate has not been done yet, so talkback
still reports `hardware_unverified`.

## Single-Camera Controller

The controller assumes one camera and the single Ethernet port makes that the
physical reality too. There is no per-camera burst grouping, no camera key in
webhook correlation, and one MediaMTX `camera`/`clear` source pair
(`MTX_PATHS_CAMERA_SOURCE` and `MTX_PATHS_CLEAR_SOURCE`). Every one of those
points at the fitted camera. Do not attempt to run both cameras into the
controller through a switch at the gate; that needs controller changes that
do not exist.

## Mounting Height

The RLC-810A is mounted on the gate pillar at about 1.2 m, and the RLC-811A
goes on the same mount at the same height. That is correct. Do not mount it
lower:

- Plates sit at roughly 0.4-0.7 m. From 1.2 m the camera looks slightly down
  at the plate, which keeps the vertical angle small at every useful distance
  and keeps the plate clear of the bonnet line.
- Headlights sit at roughly 0.6-0.9 m. At 1.2 m the lens is just above them.
  Lower puts the lens in the beam at night; higher than about 2 m increases
  the vertical angle and puts more sky and foliage in the frame.
- Lower also means more spray, mud, and leaf litter on the cover.

Approximate plate angles from a 1.2 m pillar mount, for a plate at 0.5 m
height and a vehicle centre line about 1.75 m to the side of the pillar:

| Distance along the drive | Vertical | Horizontal | Combined |
| --- | --- | --- | --- |
| 2 m | 19 degrees | 41 degrees | too oblique |
| 3 m | 13 degrees | 30 degrees | about 32 degrees |
| 4 m | 10 degrees | 24 degrees | about 26 degrees |
| 5 m | 8 degrees | 19 degrees | about 21 degrees |
| 6 m | 7 degrees | 16 degrees | about 17 degrees |
| 8 m | 5 degrees | 12 degrees | about 13 degrees |

The commissioning target is a combined angle below 30 degrees, ideally 10-20
degrees. The capture point is where vehicles stop at the closed gate, so
measure that distance, read the row, and confirm with a parked-vehicle test.
If the plate does not read at the stop, fix the geometry rather than moving
the capture point out: aim the camera across the drive at the stop position,
or move the mount towards the drive centre line so the horizontal angle drops.

## Capture At The Stop

**Superseded for this mount on 2026-09-16.** The camera is fixed to the
wooden fence about 1 m before the gate on the right-hand side, roughly 1 m
high and 1.5 to 2 m to the right of a vehicle's centreline. Drivers stop
anywhere between 1 m and 3 m from the gate, which puts the plate level with
the lens or 1 to 2 m ahead of it at a horizontal angle of 40 to 90 degrees,
and no zoom or tilt makes that readable. The first week of the RLC-811A was
spent aimed across the drive at the stop, and the evidence frames show
exactly that oblique view (see
[reviews/2026-09-16-rlc-811a-first-week.md](reviews/2026-09-16-rlc-811a-first-week.md)).
The RLC-810A on the same mount read cars on the approach, 4 to 5 m from the
gate and nearly head-on, with the tree line in the upper third of the frame.

Approximate plate angles from this mount for a car on the drive centreline:

| Car from gate | Plate ahead of camera | Horizontal | Vertical | Readable |
| --- | --- | --- | --- | --- |
| 1 m | 0 m | about 90 degrees | steep | no |
| 2 m | 1 m | 60 to 65 degrees | 27 degrees | no |
| 3 m | 2 m | 40 to 45 degrees | 14 degrees | marginal |
| 5 m | 4 m | 20 to 25 degrees | 7 degrees | yes |
| 7 m | 6 m | 15 to 17 degrees | 5 degrees | yes |

So on this site the capture point is the **approach at 4 to 7 m from the
gate**, and the aim is:

1. Point the camera up the drive towards oncoming vehicles, not across it at
   the gate, tilted about 5 degrees below level. The tree line sits across the
   top third; the near gate post is at the very edge or out of frame. Dashboard
   event 3714 (2026-09-11) is the reference view.
2. A car parked 5 m from the gate sits mid-frame with its plate at about
   mid-height and the whole car visible.
3. Zoom only until the drive at the 5 m point fills the frame width; tighter
   and a car hugging one side leaves the frame.
4. Draw the vehicle detection zone over the drive from about 3 m to 8 m out,
   not over the area in front of the gate, sensitivity 80.
5. Set `GATE_TRIGGER_CAPTURE_DELAY_SECONDS=0` so the clear-stream series is
   taken while the car is still on the approach, and re-derive
   `GATE_PLATE_REGION` from `gate_ocr plate_box=` lines after re-aiming — done
   2026-09-20/21, recorded in
   [Re-aim, 2026-09-20, and the band re-fitted to it](#re-aim-2026-09-20-and-the-band-re-fitted-to-it).

Moving the camera 1 m forward onto the galvanised gate post gains about 1 m
of distance for the same stop position and does not change this conclusion.
Reading at the stop would need a camera in front of the vehicle, which the
swinging leaves rule out. The original reasoning follows for a mount that can
see the stop head-on.

Vehicles stop at the closed gate, and a stopped vehicle gives the sharpest
plate the camera can produce: no motion blur at any shutter, and one fixed
position to aim and zoom at. Historical captures of moving vehicles are the
blurry ones. The capture point is therefore the stop, not the approach.

The controller now waits for the stop. The camera webhook fires as the
vehicle crosses the line; for an eligible event the controller waits
`GATE_TRIGGER_CAPTURE_DELAY_SECONDS` (1.5 s by default), then grabs a short
series of clear-stream frames (`GATE_TRIGGER_CAPTURE_COUNT` frames,
`GATE_TRIGGER_CAPTURE_SPACING_SECONDS` apart, 2 frames one second apart by
default) and recognises each. Events inside the rate-limit interval, arriving
while a series is queued, or of type `manual_test` are skipped and rely on
the FTP frame alone; the RLC-810A document lists the journal outcomes. The 4K FTP
JPEG is still taken at the crossing, so put the line where the vehicle is
already rolling to a halt and that frame is nearly still too.

Set it up as:

1. Measure where vehicles actually stop. Mark the plate position on the
   drive with a parked car.
2. Aim the camera at that mark, across the drive if necessary, not along the
   approach and not down at the tarmac in front of the pillar.
3. Zoom until the parked car's plate is 300-600 px wide in a saved 4K JPEG.
   Do not zoom tighter: a vehicle that stops half a metre short or long must
   still have its whole plate in frame.
4. Draw the vehicle detection zone around the stop position and the last
   two or three metres of approach only.
5. Place the line-crossing line across the drive about 1 m before the plate
   mark, vehicle-only, inbound direction, so it fires as the vehicle rolls to
   a halt. Keep sensitivity at the frozen 80 baseline until day and night
   captures have been inspected.
6. Tune the delay from the journal: if `gate_trigger_capture outcome=captured`
   frames still show movement, raise `GATE_TRIGGER_CAPTURE_DELAY_SECONDS`; if
   the vehicle is already stopped in the FTP frame, lower it.
7. Save JPEGs of a car stopped at the mark, and of one stopped half a metre
   short and half a metre long, before enabling the alarm FTP schedule.

## Frame Rate, And The Setting That Decides Whether You Get It

Measured on 2026-09-17, after the swap: the camera was configured for 10 fps
and delivering about 5, and at 6 fps it delivered 2.9. `Isp.constantFrameRate`
was **2**; the RLC-810A ran at **1**, and the swap did not carry it over. At 2
the encoder drops frames on a quiet scene, which an empty driveway always is,
so the controller saw roughly half the pictures it was configured for and the
`GATE_CLEAR_STREAM_SOURCE_FPS` it was told about was fiction.

```sh
sudo /root/camtool.py raw '[{"cmd":"SetIsp","action":0,"param":{"Isp":{<GetIsp.value.Isp with constantFrameRate: 1>}}}]'
```

With `constantFrameRate: 1` the same 6 fps configuration delivered 5.9 fps over
ten seconds. Check it by measuring rather than by reading the setting back:

```sh
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/clear -t 10 -an -f null -
```

Sixty video frames in ten stream-seconds is 6 fps; thirty is the encoder
quietly deciding the scene was dull.

**Rate against bitrate.** The stream is CBR, so the bitrate is spent whatever
the frame rate: halving the rate doubles the data in each picture. At 4K the
site now runs **6 fps at 6144 kbit/s**, which is 1024 kbit a frame against 614
at the previous 10 fps, and costs the NVR nothing because the bitrate is
unchanged. The camera offers 25/22/20/18/16/15/12/10/8/6/4/2 fps and bitrates
to 8192; 8192 was measured and rejected as diminishing returns for a third
more recording storage, since plate pixel width and night exposure dominate,
not compression. Six is deliberately just above the five frames a second the
on-device reader can consume.

Two things must move with the frame rate, every time:

- `GATE_CLEAR_STREAM_SOURCE_FPS` on the Pi. The session decoder reads an
  Annex-B pipe with no timestamps, so a stated rate that is not the real one
  silently changes how many pictures it keeps.
- The I-frame interval, at 1x the frame rate, so there is still a keyframe
  every second for an on-demand grab to start from.

Exposure is **not** one of them, as long as it stays `Manual`. On `Auto` a
lower frame rate lets the shutter stretch towards 1/6 s, which would brighten
the picture and smear every moving plate. Read `Isp.exposure` back after any
frame-rate change.


### It still under-delivers, by about a quarter

Measured again on 2026-09-17 in daylight, with `frameRate 6`, `bitRate 6144`,
`gop 1` and `constantFrameRate 1` all confirmed in place:

| Sample | Frames | Over | Delivered |
| --- | --- | --- | --- |
| 10 s | 41 | 10.0 s | 4.1 fps |
| 20 s | 92 | 19.8 s | 4.6 fps |

So about **4.5 fps against a configured 6**, or 75%. Setting `constantFrameRate`
to 1 recovered the worst of it -- it was 2.9 before -- but not all of it.

This caps everything downstream. The session decoder is asked for
`GATE_SESSION_FPS=5`, which is *above* what arrives, so its `fps` filter has
nothing to thin and repeats frames instead to make the rate up. A repeated
frame costs the on-device reader its full ~200 ms for an answer already known,
which is why the sweep now skips a frame identical to the one before it and
reports `duplicates=` and `read_fps=` when the window ends.

Do not chase this with a higher frame rate. At a fixed 6144 kbit/s more frames
means fewer bits each, and the binding constraint on recognition at this site
is plate size -- measured at 135-170 px at the stopping position on
2026-09-17, against the 300 px the lens could give after re-aiming.

## Webhook Capture And Keyframes

The delayed capture series above grabs frames from the clear stream on
demand. Each grab waits for the next keyframe, so set the Clear stream's
**I-frame interval** to 1x the frame rate on the RLC-811A at commissioning.
With a longer interval the grab can wait several seconds and time out, and
only the FTP frame is recognised. Details are in the RLC-810A document under
Webhook-Triggered Capture.

Once a saved 4K capture shows the plate at least 300 pixels wide, set
`GATE_OCR_MAX_UPLOAD_WIDTH=1920` to shorten the OCR upload. Do not enable it
before that check.

## Exposure

The clear-stream captures see a stopped vehicle, so shutter speed matters
less for them than for the FTP frame taken at the crossing. Zoom still makes
any residual movement cover more pixels. Set **Exposure** to **Manual** with
the shutter capped at 1/250 s so the crossing frame stays sharp, and cap gain
before slowing the shutter. Disable WDR/HDR around the stop position; it
brightens the retroreflective plate into the bonnet and produces two-exposure
ghosting on a moving vehicle.

At the zoom above the aperture is about f/2-f/2.5, level with or up to about
two thirds of a stop darker than the RLC-810A's f/2.0. At full zoom it is
f/3.3, about 1.4 stops darker. Night captures rely on the light below, not on
a slower shutter.

## Night Light

A PIR-triggered white floodlight lights the approach at night (see
[Site Lighting](reolink-rlc-810a.md#site-lighting) and the
[Floodlight subsection](reviews/2026-09-06-camera-night-configuration.md) of
the night configuration review for what is and is not yet verified about it —
notably whether it reliably covers the stop position and its trigger delay).
For plates it must:

- **Be on and at full brightness before the vehicle stops.** Its motion
  sensor must see the approach, not just the gate, so the lamp has come up
  before the crossing frame and the delayed captures. It must light the stop
  position evenly.
- **Light the plate from near the camera axis.** Plates are retroreflective:
  light returns to where it came from. A lamp beside the camera lights the
  plate brightly; a lamp behind or above the vehicle does not.
- **Stay out of the frame and off the lens.** No part of the lamp or its beam
  should hit the camera cover.
- **Be the only light.** With a white spotlight covering the stop position, turn the
  RLC-811A's own spotlight off and keep IR off. Two sources double the plate
  return and wash out the characters. If the external lamp cannot cover the
  stop position, use the camera's spotlight alone instead.

Keep the camera in colour mode with the lamp on. Set day/night switching so it
does not flip during an approach; a mode change as the vehicle arrives loses the frame.
If plates wash out at night, reduce exposure or gain, never add a second light.
If they are dark, move or re-aim the lamp closer to the camera axis before
slowing the shutter.

## Fitted Unit, 2026-09-11

The RLC-811A was fitted on the evening of 2026-09-11. What follows is the
record of that cutover and the settings actually applied, so the next person
does not rediscover the firmware's quirks.

| | |
| --- | --- |
| Model / hardware | RLC-811A, `IPC_560B158MP`, item `P430` |
| Firmware at fitting | `v3.1.0.4695_2504301440` (older than the RLC-810A's `5764`; `AutoUpgrade` is on) |
| MAC | `ec:71:db:64:63:9f` |
| Address | static `192.168.0.54`, set in the Reolink app. It is the retired unit's address, so no file on the Pi changed |
| Name | `Front Gate` (the webhook `device`/`channelName` and the FTP file prefix) |

### Getting the API open

Current Reolink firmware ships with only the app protocol on port 9000
enabled; HTTP, HTTPS, RTSP, ONVIF and RTMP are all off, so the Pi cannot pull
a stream, the API is unreachable, and a browser gets `ERR_CONNECTION_REFUSED`.
The mobile app's Port Settings toggles did **not** take on this unit. What
worked was the same protocol the app uses, from a scratch venv on the Mac:

```python
from reolink_aio.api import Host
from reolink_aio.baichuan.util import PortType
host = Host("192.168.0.54", "admin", password)
await host.baichuan.login()
for port in (PortType.http, PortType.https, PortType.rtsp):
    await host.baichuan.set_port_enabled(port, True)
print(await host.baichuan.get_ports())
```

Confirm with `GetNetPort` (`httpEnable`, `httpsEnable`, `rtspEnable` all 1;
leave ONVIF and RTMP at 0).

### Settings applied

[`scripts/reolink/configure-rlc811a.py`](../scripts/reolink/configure-rlc811a.py)
(installed on the Pi as `/root/configure-rlc811a.py`, next to
[`camtool.py`](../scripts/reolink/camtool.py)) reads every block, prints a
field-level diff, and writes only with `--apply`, backing each block up under
`/root/rlc811a-swap-<stamp>/` and reading it back to verify. The table is the
configuration as it stands **today**, not as it was written on cutover day:
the frame rate and `constantFrameRate` rows below were corrected after the
2026-09-17 measurements in [Frame Rate](#frame-rate-and-the-setting-that-decides-whether-you-get-it),
and `tests/test_camera_setup_script.py` pins them by rehearsing the script
against saved camera blocks, so they cannot drift back.

| Block | Setting |
| --- | --- |
| `SetEnc` | clear 3840x2160 H.265 6144 kbit/s **6 fps, gop 1** (keyframe every second); `audio 1`; the fluent stream is left as found |
| `SetIsp` | `exposure Manual`, `shutter 4/4` (1/250 s), `gain 16/16`, `antiFlicker Off`, `backLight Off`, `hdr 0`, `nr3d 1`, **`dayNight Color`**, **`constantFrameRate 1`** |
| `SetNtp` | `enable 1`, `pool.ntp.org`, 60 min. The time itself is never written here: `gate-camera-control` owns the camera clock (see [Camera clock reconcile](camera-control.md#camera-clock-reconcile)) |
| `SetIrLights` | `Off` |
| `SetWhiteLed` | `mode 0`, `state 0`: the PIR floodlight is the only plate light (Night Light above) |
| `SetFtpV20` | server `192.168.0.33:21`, `ftp-user`, `onlyFtps 0`, `streamType 3`, `picInterval 5`, 4K stills, schedule `AI_VEHICLE` only |
| `SetPushV20` / `SetPushCfg` | enabled, schedule `AI_VEHICLE` only, `pushInterval 20` (firmware minimum) |
| `SetWebHook` | slot 0 enabled, `http://192.168.0.33:8766/reolink/events?secret=<GATE_REOLINK_WEBHOOK_SECRET>`, Content Default |
| `SetAiAlarm` | `vehicle` sensitivity 80 (the frozen baseline) |

Left as found on purpose: SD recording on all AI and motion rules, the email
block (no address), OSD, `AutoUpgrade 1`, `PowerLed On`, the displayed time and
the DST block, and **zoom and focus**. The lens position follows the physical
re-aim in [Capture At The Stop](#capture-at-the-stop) and is set at the gate;
the script holds no position and sends no `ZoomFocus` command, so it can never
undo a re-aim. It also warns when `GATE_CLEAR_STREAM_SOURCE_FPS` on the Pi
disagrees with the frame rate it is about to write, because the two must move
together.

### FTP credentials are masked by the API

`GetFtpV20` returns `userName` as `ft****er` and `password` as asterisks, and
the older backups on the Pi hold exactly those masked strings. Round-tripping
a masked block into an already-provisioned camera leaves the credentials
alone, which is why schedule edits on the RLC-810A worked; it cannot provision
a fresh unit. The `ftp-user` account's password was not recorded anywhere, so
[`scripts/reolink/reset-ftp-user-password.sh`](../scripts/reolink/reset-ftp-user-password.sh)
(on the Pi as `/root/reset-ftp-user-password.sh`) gives it a new random one,
stores it root-only in `/root/ftp-user.credentials`, and proves an FTP login;
the configuration script reads it from there. The camera's `TestFtp` uploads a
small `.txt` into the watched tree, which the controller ignores, and the
vsftpd log shows the login and upload.

### API shapes on this firmware

- `GetWebHook` needs `param {"channel": 0}`; it returns four slots
  (`index`, `indexEnable`, `hookUrl`, `bCustom`, `hookBody`).
- `SetWebHook` takes `param {"WebHook": {"channel", "index", "indexEnable",
  "hookUrl", "bCustom", "hookBody"}}`.
- `TestWebHook` accepts `param {"channel": 0, "index": 0}` but answers
  `rspCode -100 "test failed"` on this firmware, and the Pi saw no connection
  attempt on port 8766 for any URL or body variant tried. The listener itself
  was proven with a synthetic post from the Pi (`type TEST` is journaled as
  `manual_test`; a synthetic `VEHICLE` ran a full clear-stream session and a
  41 s audio clip). Treat the first real passage as the webhook acceptance
  test: look for `reolink_webhook` and `gate_trigger_capture outcome=captured`
  in the journal, and if only the FTP path fires, revisit the webhook.
- `GetPushCfg` / `SetPushCfg` carry `pushInterval`.
- `GetEnc` ranges: main `gop` 1-2, `frameRate` down to 2; `audio` boolean.
- `GetIsp` ranges: `shutter` 0-125, `gain` 1-100, `constantFrameRate` 0-2.

### Verified after the cutover

- MediaMTX `clear` and `camera` paths came back online within seconds of the
  encoder change; the camera-control service's breaker closed on its own and
  an IR lease (`Auto`, 1 minute) set and reverted correctly.
- Audio is in both streams and the controller captured a 41.5 s clip from the
  synthetic vehicle event.
- Talkback was not implemented in the media stack on cutover day; it has since
  shipped as `gate-camera-control`'s own Baichuan path and still reports
  `hardware_unverified` pending the supervised test at the gate. See
  [Push-to-talk](talkback.md) and the intro above.

### Still open

- **Zoom and aim.** The camera was physically re-aimed on 2026-09-20 and the
  plate band re-fitted and proven by replay; see
  [Re-aim, 2026-09-20, and the band re-fitted to it](#re-aim-2026-09-20-and-the-band-re-fitted-to-it)
  for the record, the new band, and what is still open on zoom.
- **Line-crossing rule** and a tighter vehicle detection zone (currently the
  full frame): app only, and this firmware exposes no `AI_CROSSLINE_*` schedule
  row at all (see the first-week review, section 3). The script therefore
  leaves any line-crossing row it does find enabled rather than zeroing it.
- **Day and night captures** of a stopped car as the acceptance baseline.

## Re-aim, 2026-09-20, and the band re-fitted to it

The camera was physically re-aimed (pan and tilt) on 2026-09-20. Comparing
stored photos side by side, the last event on the old across-the-drive view
landed at 16:58:08 UTC and the first event on the new view at 18:15:43 UTC;
telemetry shows nothing of the move, so the photo comparison is the only
record of when it happened. Zoom stayed at pos 2 (range 0-28), focus stayed
at pos 80 (range 0-238), autofocus enabled — only the aim moved.

`GATE_PLATE_REGION` is `x,y,w,h` frame fractions
(`gate_controller/plate_region.py`), **not** `x0,y0,x1,y1`. The band in force
until 2026-09-21 02:24 UTC was `0.25,0,0.75,0.6`: x 0.25-1.00, y 0.00-0.60,
journaled as `crop=480,0,1920,648` on the 1920x1080 decode.

### Post-re-aim plate measurements

This is a thin base: one arrival (dusk, Skoda 10CE1990, 12 frames) and one
departure (floodlit night, 4 rear-plate frames), measured from photos as the
threshold bounding box scaled ×3 to the 3840 frame, about ±10 px.

- **Approach (6 frames):** widths 192, 216, 216, 249, 258, 375 px; centre x
  0.19-0.47, y 0.42-0.54. Three of the six sat fully left of the old band and
  one was clipped at its left edge.
- **Rolling to a stop (1 frame):** 372 px at x 0.52, y 0.56 — inside the old
  band.
- **Stopped at the gate (5 frames):** 369-372 px, sharp, tilted about 15
  degrees, centre x 0.65-0.67, y 0.62-0.63, the plate spanning y 0.571-0.682
  — all five clipped by the old band's 0.60 bottom edge.

Nine of the twelve arrival frames were therefore missed or clipped by the old
band. Rear plates on the departure: 390, 390, 192, 138 px.

Median width over the twelve arrival frames: 370 px. The target is about 300
px; the pre-re-aim figure was 135-170 px at the stopping position (see
[Frame Rate](#frame-rate-and-the-setting-that-decides-whether-you-get-it)
above), and the nine journal `plate_box` lines of 19-20 September, before the
re-aim, ran 145-364 px, median 184.

### Consequence: a 41 s wait

The same evening this cost a driver 41 seconds at the gate: from the first
frame at 18:15:44.649 UTC to the relay firing at 18:16:25.516 UTC, both Pi
clock times (camera-vs-Pi clock skew was measured within 1 s and is ruled
out). The stopped car's plate sat below the band, so neither reader saw it;
the driver reversed and re-approached, and the plate was read while the car
was moving back through the band.

In context, over 24 plate-triggered openings between 16 and 20 September,
first-event-to-relay was a median of 2.2 s, p90 9.7 s, max 40.9 s, with the
next-largest at 15.3 s. The 41 s wait was the extreme case, not the typical
one.

### New band, applied 2026-09-21 02:24 UTC

`GATE_PLATE_REGION=0.10,0.15,0.75,0.75`: x 0.10-0.85, y 0.15-0.90 (crop
192,162 to 1632,972 on the 1920x1080 decode). It contains all twelve front
and four rear measured boxes above. The previous env file was kept as
`/etc/gate-controller.env.bak-2026-09-21-band`.

Making the band taller costs nothing at the detector. The local detector
(`yolo-v9-t-384`) letterboxes the crop with `r = min(384/h, 384/w)`; while the
crop stays wider than tall, only the crop's **width** sets how many pixels
the plate occupies at the detector, and width is unchanged at 0.75 (1440 px).
Each additional 0.05 of band width, by contrast, would cost about 6% of the
plate's pixels at the detector.

### Proof by replay

The band change was proven by replaying the same 41 s passage (deployed
release `989fa3d` models, `yolo-v9-t-384` + `cct-xs-v2`) through both bands on
the Pi. Caveats: dashboard 1280x720 copies upscaled to 1920x1080, JPEG q90;
one vehicle, one passage, dusk.

| Event | Received (UTC) | OLD band | NEW band |
| --- | --- | --- | --- |
| 3133 | 18:15:44.649 | 12C6827 0.179 | 1LE6911 0.110 (motion-blurred, headlight glare) |
| 3135 | 18:15:45.453 | no plate | 10CE1990 0.971 |
| 3134 | 18:15:45.889 | no plate | 10CE1990 0.969 |
| 3136 | 18:15:47.136 | no plate | 10CE1990 0.999 |
| 3137 | 18:15:48.323 | no plate | 10CE11900 0.641 |
| 3138 | 18:15:49.420 | no plate | 10CE1990 0.998 |
| 3139 | 18:16:10.412 | 99T 0.197 | 10CE1991 0.507 |
| 3141 | 18:16:11.264 | no plate | 10CE1990 0.991 |
| 3140 | 18:16:11.823 | no plate | 10CE1990 0.863 |
| 3142 | 18:16:13.428 | no plate | 0CE15301 0.293 |
| 3143 | 18:16:14.626 | 10CE1990 0.893 | 10CE1990 0.577 |
| 3144 | 18:16:24.113 | 10CE1990 0.804 | 10CE1990 0.974 |

Reads at or above 0.75: old band 2 of 12, new band 7 of 12. First
authorisable read after the first alarm: +30 s under the old band, +0.8 s
under the new (event 3135).

One frame reads worse under the new band: event 3143, 0.577 against 0.893
old. The weakest-character score is sensitive to small preprocessing
differences; see the carried-read fix in
[On-device plate recognition](local-recognition.md#the-sweeps-read-travels-with-its-frame)
(#175), not restated here.

### Band widened again, 2026-09-23 13:17 UTC

`GATE_PLATE_REGION=0.05,0.10,0.90,0.85`: x 0.05-0.95, y 0.10-0.95. Previous
env file kept as `/etc/gate-controller.env.bak-2026-09-23-band`.

**Why.** The band of 2026-09-21 was fitted to a car *stopped at the gate*
whose plate sat low, and it fixed the vertical clipping. It left the right
edge at x 0.85. A car at the gate on the new aim puts its plate at
**cx 0.877-0.889**, which is outside that edge, so the frames where the plate
is largest and squarest — the ones most likely to read at 1.000 — were being
cropped away.

**Measured.** 35 corpus frames from 2026-09-20 to 2026-09-22 (every frame R2
holds since the re-aim), replayed on the Pi through the deployed release
`add45b6` models (`yolo-v9-t-384` + `cct-xs-v2`). The two bands agree on 30 of
35 frames. The five that differ:

| Frame (UTC) | Band of 2026-09-21 | Band of 2026-09-23 |
| --- | --- | --- |
| 2026-09-20 16:57:40 | `131D26996` 0.727 (spurious 9) | `131D2696` 0.984 |
| 2026-09-20 18:16:25 | `13NE1111` 0.363 | `10CE19990` 0.966 (still wrong) |
| 2026-09-22 10:00:05 | `131D2696` 0.995 | `131D2696` 0.976 |
| 2026-09-22 16:04:22 | no plate (spurious 90x210 box) | `131D2696` **1.000** |
| 2026-09-22 16:04:26 | no plate | `131D2696` **1.000** |

Three correct reads recovered, one read corrected, one already-passing read
0.019 weaker. The cost predicted in the section above — about 6% of the
plate's detector pixels per 0.05 of width — is real but is worth paying: it
shows up as that 0.995 -> 0.976, while the plates it recovers were scoring
nothing at all.

**One caution.** 2026-09-20 18:16:25 reads `10CE19990` at 0.966 under the new
band, against a true plate of `10CE1990`. That is a wrong read clearing the
0.5 admission gate. It is stopped one layer later — `decide_access` requires
an exact match against the authorised list, and a nine-character read matches
nothing — but it is a reminder that the confidence gate is not the thing
keeping wrong plates out, the authorised-list match is.

### Plate width is necessary and not sufficient

The same replay, read as "how big does a plate have to be":

| Frame (UTC) | Plate width | Score |
| --- | --- | --- |
| 2026-09-22 16:04:20 | 98 px | no read |
| 2026-09-21 17:59:16 | 152 px | 0.903 |
| 2026-09-22 14:18:21 | 186 px | 0.239 |
| 2026-09-22 16:04:27 | 189 px | 0.103 |
| 2026-09-22 10:00:05 | 240 px | 0.995 |
| 2026-09-22 14:18:23 | 400 px | 1.000 |
| 2026-09-22 16:04:22 | 410 px | 1.000 |

Below about 100 px nothing reads. Above about 240 px everything measured here
reads at 0.995 or better. **In between, width does not decide it**: a clean
front-on plate at 152 px read 0.903, while motion-blurred ones at 186 and
189 px read 0.239 and 0.103. So a rule that waits for the plate to be "big
enough" before spending a read would have thrown away the 152 px read and kept
the 189 px one. Angle and motion blur dominate in that range, and neither is
measured today.

The practical consequence is that there is no useful frame-selection rule to
be had from box width alone, and the cheapest read is the one already being
taken: the reader costs 145-190 ms a frame here, against a decision budget of
4 s.

### Zoom: not changed, and why

Headroom scaling about the frame centre with a 5% margin is about x1.33,
limited by the far-approach plate at the left edge (x0 = 0.163, the first
approach frame above), not by the stopped plate, which would allow x2.11 on
the right edge or x2.47 on the bottom. Keeping **whole vehicles** in frame —
needed because the CLIP direction and farm-machine classifiers need the whole
vehicle — allows only about x1.07-1.11, because car bodies already reach x
about 0.08 on the left.

Zooming would also make the camera's own vehicle alarm fire later, and it
already fires late: on the 41 s passage the car was close and the plate
already about 360 px wide, motion-blurred and headlight-glared, at the first
alarm. Plates are already about 370 px where cars are actually read (at the
stop), above the 300 px target; only the far approach (192-258 px) is below
it, and a x1.33 zoom would bring that to about 255-343 px.

The zoom-position-to-scale mapping for this lens is not calibrated. The
daylight calibration procedure, not yet run:

1. Read `GetZoomFocus` and take a 4K snapshot.
2. Record pixel positions of three fixed features: the top of the
   ivy-covered fence post at x about 0.90; the small fence post at x about
   0.57, y about 0.35; the near fence post at the left edge.
3. Step zoom one position at a time, wait about 5 s for autofocus, and read
   back both positions.
4. Compute scale from the distance between the first two features relative
   to pos 2, solving for the true scaling centre rather than assuming the
   frame centre.
5. Stop at the first position where scale >= 1.3, or where x = 0.163 would
   map past x = 0.05.
6. Check focus by the edge sharpness of a fence rail at about 5 m, against
   the pos-2 baseline.
7. Roll back to zoom 2, then focus 80, and confirm by readback and that the
   three features sit within ±3 px of baseline.

If the zoom is later increased to about x1.33, the plates measured here map
to x 0.05-0.78, y 0.26-0.74, and the proposed band is `0,0.18,0.85,0.64` — to
be re-proven from frames taken after the change, never from this arithmetic
alone.

### To re-check

After the next several real daylight arrivals, confirm from `gate_local_sweep`
and `gate_ocr plate_box=` journal lines that both stopped and approach plates
fall inside the new band. The Pi journal retains only about 1.8 days (it is
size-capped), and the Pi keeps only the last three event photos in
`/var/lib/gate-controller/event-evidence/`; older frames have to be fetched
back from the dashboard.

## Cutover And Rollback

Only one camera is on the port at any time, so the swap is a short outage:

1. Before removing the RLC-810A, record its firmware, camera name
   (`front.station`), FTP settings, webhook settings, and the reserved LAN
   address. Save a current day and night capture as the baseline.
2. Fit the RLC-811A on the same mount. Give it the same reserved LAN address
   by moving the DHCP reservation to its MAC, so `/etc/gate-controller.env`
   and `/etc/gate-media-gateway.env` do not change. Verify the address before
   configuring anything else.
3. Update firmware, then configure FTP into the watched uploads tree, the
   webhook with the shared secret at the top level, and the stream settings
   from the RLC-810A document. Use a distinct webhook rule name so traces show
   which camera and rule fired.
4. Set framing, line, exposure, and light per the sections above. Save test
   captures of a vehicle stopped at the gate by day and again at night with
   the spotlight.
5. Watch the controller journal for `reolink_webhook status=rejected` on the
   first real event and read the `reason` field before changing anything.
   `payload` means the body is not what the controller expects; `unauthorized`
   means the secret does not match; `stale` means the camera clock or the
   delivery is more than fifteen seconds out; `content_type`, `content_length`,
   `body_too_large`, and `json` mean the request framing is wrong. A duplicate
   delivery is acknowledged and not logged as a rejection.
6. Rollback is refitting the RLC-810A and moving the reservation back.

## Acceptance

- A saved production capture from the RLC-811A shows a plate 300-600 px wide
  at the stop, day and night, with no visible motion blur across characters.
  150 px is the floor below which OCR is unreliable, not the target.
- The event trace shows the RLC-811A webhook rule as the trigger source.
- A before/after recognition comparison over comparable passages is recorded
  before any policy or threshold change relies on the new view.
