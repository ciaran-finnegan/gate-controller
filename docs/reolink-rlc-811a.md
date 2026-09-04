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
degrees, so the capture point must be at least 4 m from the pillar and is best
at 5-8 m.

## Capture Point And The Stop At The Gate

Vehicles slow and stop at the closed gate. That does not by itself give a
sharp plate, for two reasons:

- A vehicle stopped at the gate is 1-3 m from the pillar. From the table above
  that is too oblique to read. The stopped position only helps if the vehicle
  stops at least 4-5 m out; measure where they actually stop.
- The controller reads the plate at the moment the camera fires. The 4K FTP
  JPEG is taken at the line crossing, the fluent fallback frames are those no
  more than one second old when the trigger arrives, and every OCR attempt
  runs inside the decision deadline. Nothing waits for the vehicle to come to
  rest. The line-crossing line is therefore the only control over when the
  plate is captured.

Use the deceleration zone instead. A vehicle 5-8 m from a closed gate is
already slowing, typically below 10 km/h, which is under 3 m per second:

| Shutter | Plate movement during exposure | Blur on a 400 px plate |
| --- | --- | --- |
| 1/500 s | up to 6 mm | about 4 px |
| 1/250 s | up to 11 mm | about 9 px |
| 1/100 s | up to 28 mm | about 21 px |

At 1/500 s a slowing vehicle at 5-8 m is effectively still. Set it up as:

1. Aim the camera along the approach at a point 6 m from the pillar, not down
   at the tarmac in front of it.
2. Zoom until a parked test vehicle's plate at 6 m is 250-350 px wide in a
   saved 4K JPEG. That is roughly a 45-60 degree horizontal view; at that zoom
   the plate at 5 m is about 300-430 px and at 8 m about 190-270 px, so the
   whole 5-8 m corridor is above the 150 px minimum.
3. Draw the vehicle detection zone around that corridor only.
4. Place the line-crossing line across the drive at about 6 m from the pillar,
   vehicle-only, inbound direction, so the FTP JPEG is taken while the plate
   is square-on and the vehicle is slowing. Keep sensitivity at the frozen 80
   baseline until day and night captures have been inspected.
5. Save JPEGs at 5 m and 8 m before enabling the alarm FTP schedule.

## Exposure

Zoom makes motion blur worse, not better: the same vehicle movement covers
more pixels at a longer focal length. Set **Exposure** to **Manual**, shutter
limit 1/500 s, and test no slower than 1/250 s with a moving vehicle. Cap gain
before slowing the shutter. Disable WDR/HDR for the plate corridor; it
brightens the retroreflective plate into the bonnet and produces two-exposure
ghosting on a moving vehicle.

At the zoom above the aperture is about f/2-f/2.5, level with or up to about
two thirds of a stop darker than the RLC-810A's f/2.0. At full zoom it is
f/3.3, about 1.4 stops darker. Night captures rely on the light below, not on
a slower shutter.

## Night Light

A motion-controlled spotlight lights the approach at night. For plates it
must:

- **Be on before the vehicle reaches 8 m from the pillar.** Its motion sensor
  must see the vehicle further out than the capture corridor, and the lamp
  must reach full brightness before the crossing. A light that comes on as the
  vehicle reaches the gate lights the stopped, oblique position, not the
  capture.
- **Light the plate from near the camera axis.** Plates are retroreflective:
  light returns to where it came from. A lamp beside the camera lights the
  plate brightly; a lamp behind or above the vehicle does not.
- **Stay out of the frame and off the lens.** No part of the lamp or its beam
  should hit the camera cover.
- **Be the only light.** With a white spotlight covering the corridor, turn the
  RLC-811A's own spotlight off and keep IR off. Two sources double the plate
  return and wash out the characters. If the external lamp cannot cover the
  corridor, use the camera's spotlight alone instead.

Keep the camera in colour mode with the lamp on. Set day/night switching so it
does not flip during an approach; a mode change mid-corridor loses the frame.
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
4. Set framing, line, exposure, and light per the sections above. Save 5 m and
   8 m test captures by day and again at night with the spotlight.
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
