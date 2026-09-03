# Reolink RLC-811A Plate Camera Commissioning

The installed gate camera is an RLC-810A: fixed 4 mm lens, no optical zoom, no
two-way audio. It stays the wide context and line-crossing trigger camera until
a before/after comparison exists. The RLC-811A is the additional camera being
commissioned as a dedicated plate-reading camera. This document covers only
what differs from [RLC-810A deployment and night calibration](reolink-rlc-810a.md).
Network boundary, FTP burst setup, authenticated trigger provenance, stream
settings, and the Pi validation harness apply to whichever single camera the
controller is pointed at. The controller is single-camera today; see
[Single-Camera Controller Limits](#single-camera-controller-limits) before
letting both cameras upload or stream at once.

## Hardware Differences

| | RLC-810A (installed) | RLC-811A (new) |
| --- | --- | --- |
| Lens | fixed 4 mm | motorised 2.7-13.5 mm, 5x optical zoom |
| Horizontal field of view | about 87 degrees | 105 degrees wide to 31 degrees at full zoom |
| Aperture | f/1.6 | f/1.6 wide to f/3.3 at full zoom |
| Sensor | 1/2.8 inch 8 MP | 1/2.8 inch 8 MP |
| Two-way audio | no | yes |

Per-pixel image quality is the same. The gain from the RLC-811A is entirely
framing: the zoom puts more of the 3840-pixel frame width on the plate. Two-way
audio is a separate concern; the media stack keeps talkback
`hardware_unverified` until a physical backchannel acceptance test on the
RLC-811A is complete.

## Framing By Zoom

Approximate plate width in the 4K frame for an Irish or UK 520 mm plate:

| Distance to plate | RLC-810A 87 degrees | RLC-811A 60 degrees | RLC-811A 31 degrees |
| --- | --- | --- | --- |
| 4 m | 263 px | 432 px | 900 px |
| 6 m | 175 px | 288 px | 600 px |
| 8 m | 132 px | 216 px | 450 px |
| 10 m | 105 px | 173 px | 360 px |
| 15 m | 70 px | 115 px | 240 px |
| 20 m | 53 px | 86 px | 180 px |

The commissioning target is unchanged: a plate 150-250 pixels wide at the
capture point, combined horizontal and vertical plate angle below 30 degrees
and ideally 10-20 degrees. From the gate pillar the RLC-810A cannot meet that
target beyond about 6 m. The RLC-811A at full zoom meets it out to about 20 m.

Frame the corridor by zoom, not by relocating the camera:

1. Mount at 1.2-2 m, above most headlights, forward of the fence plane.
2. Pick one capture point on the stretch where vehicles have straightened after
   the bend. Aim along the approach at that point, not down at the tarmac in
   front of the pillar.
3. Zoom in until a parked test vehicle's plate at the capture point is 150-250
   pixels wide in a saved 4K JPEG. Do not zoom tighter than needed: at 31
   degrees the corridor is only about 5.5 m wide at 10 m and a moving vehicle
   crosses it in one to two seconds.
4. Save JPEGs at the nearest and farthest expected plate positions before
   enabling FTP uploads for this camera.

## Motion Blur And Exposure

Zoom makes motion blur worse, not better: the same vehicle movement covers more
pixels at a longer focal length. Set **Exposure** to **Manual** where the
firmware exposes it, start the shutter limit at 1/500 second, and test no
slower than 1/250 second with moving vehicles. Cap gain before slowing the
shutter.

At full zoom the aperture is f/3.3, about two stops darker than the RLC-810A.
Night calibration needs the fast shutter and the darker lens together, so
expect more gain or the spotlight to be required than on the RLC-810A. Keep
IR, spotlight, and the existing motion light out of the plate corridor and
follow the RLC-810A night calibration steps for headlight bloom.

## Trigger Line And Detection Zone

Because the zoomed corridor is short, the camera event and FTP snapshot must
land while the plate is inside it:

- Draw the vehicle detection zone around the zoomed corridor only.
- Place the line-crossing line at the near edge of the corridor, where the
  plate enters the framed area, rather than across the whole driveway.
- Keep detection vehicle-only and the FTP alarm upload on this camera's own
  schedule. Do not point both cameras at the watched uploads tree at the same
  time; see the limits below.

The RLC-810A keeps its frozen line-crossing baseline (vehicle-only, sensitivity
80, full-width line) during the comparison window. Change one camera variable
at a time.

## Single-Camera Controller Limits

The controller assumes one camera. Adding a second one does not change that
until per-camera support is built:

- **Bursts.** Every completed JPEG in the watched uploads tree that arrives
  inside one quiet window joins the same burst, whichever camera sent it.
  Content-digest deduplication removes only byte-identical files; it does not
  separate sources. Two cameras uploading together would produce mixed bursts.
- **Webhook correlation.** The correlator attaches the single nearest camera
  event to a burst without a camera key. With two cameras firing, one burst
  can be attributed to the wrong rule and the other event left to attach to a
  later burst.
- **Media and hot stream.** MediaMTX exposes one `camera`/`clear` source pair
  (`MTX_PATHS_CAMERA_SOURCE` and `MTX_PATHS_CLEAR_SOURCE`) and the hot stream
  always reads `rtsp://127.0.0.1:8554/camera`. Whichever camera those point at
  supplies the fluent fallback frames for every FTP trigger, so an RLC-811A
  trigger would be augmented with RLC-810A frames unless the sources are
  repointed.

Commission accordingly, one camera at a time owning the watched tree and the
media paths:

1. **Commissioning.** Upload RLC-811A test JPEGs to a separate FTP directory
   outside `/var/lib/gate-controller/uploads`, or review them on the camera
   itself. Verify plate width, angle, and blur from those files. Leave the
   RLC-810A FTP, webhook, and media configuration untouched.
2. **Cutover.** When the RLC-811A view passes acceptance, in one change:
   disable the RLC-810A alarm FTP upload and its webhook, enable the RLC-811A
   FTP upload into the watched tree and its webhook, and repoint
   `MTX_PATHS_CAMERA_SOURCE` and `MTX_PATHS_CLEAR_SOURCE` in
   `/etc/gate-media-gateway.env` at the RLC-811A. The RLC-810A then provides
   context video only.
3. **Rollback.** Reverse the same three settings together.

Running both cameras into recognition at once requires per-camera burst
grouping, a camera key in webhook correlation, and a second MediaMTX path.
None of those exist yet and they are out of scope for this document.

## Identity And Provenance

- Reserve a LAN address for the RLC-811A before its first FTP or webhook
  configuration. Do not rely on DHCP.
- Give it a distinct camera name; the installed unit reports as `front.station`.
- Configure its webhook with the same controller URL and secret as the RLC-810A
  but a distinct rule name, so trigger provenance identifies which camera fired.
  Enable it only at cutover, when the RLC-810A webhook is disabled.
- Record both camera models and firmware versions when commissioning.
- Camera events never actuate the relay. Recognition, authorisation, claim, and
  relay code are unchanged by adding a camera.

## Acceptance

- A saved production capture from the RLC-811A shows a plate 150-250 pixels wide
  at the capture point, day and night, with no visible motion blur across
  characters.
- Events and traces identify which camera triggered and which camera supplied
  each frame.
- A before/after recognition comparison over comparable passages is recorded
  before any policy or threshold change relies on the new view.
