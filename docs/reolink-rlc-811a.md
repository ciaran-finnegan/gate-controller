# Reolink RLC-811A Gate Camera Swap

The installed gate camera is an RLC-810A: fixed 4 mm lens, no optical zoom, no
two-way audio. The gate has one Ethernet port, so only one camera can be
fitted. The RLC-811A replaces the RLC-810A on the same pillar mount; the
RLC-810A is removed and kept as the rollback unit. This document covers only
what differs from
[RLC-810A deployment and night calibration](reolink-rlc-810a.md). Network
boundary, FTP burst setup, authenticated trigger provenance, stream settings,
and the Pi validation harness apply unchanged to the fitted camera.

## Hardware Differences

| | RLC-810A (installed) | RLC-811A (replacement) |
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
audio is a separate concern; the media stack keeps talkback
`hardware_unverified` until a physical backchannel acceptance test on the
RLC-811A is complete.

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

Vehicles stop at the closed gate, and a stopped vehicle gives the sharpest
plate the camera can produce: no motion blur at any shutter, and one fixed
position to aim and zoom at. Historical captures of moving vehicles are the
blurry ones. The capture point is therefore the stop, not the approach.

The controller now waits for the stop. The camera webhook fires as the
vehicle crosses the line; the controller waits `GATE_TRIGGER_CAPTURE_DELAY_SECONDS`
(1.5 s by default), then grabs a short series of clear-stream frames
(`GATE_TRIGGER_CAPTURE_COUNT` frames, `GATE_TRIGGER_CAPTURE_SPACING_SECONDS`
apart, 2 frames one second apart by default) and recognises each. The 4K FTP
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

A motion-controlled spotlight lights the approach at night. For plates it
must:

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

- A saved production capture from the RLC-811A shows a plate 150-250 px wide
  or wider at the capture point, day and night, with no visible motion blur
  across characters.
- The event trace shows the RLC-811A webhook rule as the trigger source.
- A before/after recognition comparison over comparable passages is recorded
  before any policy or threshold change relies on the new view.
