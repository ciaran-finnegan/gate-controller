# Early Trigger: Noticing A Vehicle Before The Camera Says So

`GATE_EARLY_TRIGGER=off|shadow|on`. The code's default is `off`. The intended
rollout is a day of `shadow` on the Pi, then the report below, then `on`.
Issue #107 is the background; #106 is the audio half of it. The first shadow
day was 2026-09-22; what it showed and what changed because of it is
[below](#the-first-shadow-day-2026-09-22). The second shadow fortnight,
2026-09-23 to 10-10, took the confirming looks off the decision path:
`GATE_EARLY_TRIGGER_CONFIRMATION=sweep` (the default) makes the sweep itself
the second look; the measurements are
[below](#the-second-shadow-fortnight-2026-09-23-to-10-10).

## The geometry that motivates it

After the re-aim of 2026-09-20 (see
[reolink-rlc-811a.md](reolink-rlc-811a.md#re-aim-2026-09-20-and-the-band-re-fitted-to-it))
an arriving vehicle is in the picture for about three seconds. It comes out
from behind the foreground fence at the far left (x 0.05-0.15 of the frame,
y 0.30-0.45, plate 120-190 px wide) and stops at the gate at x 0.65. On the
one arrival measured since, the camera's own vehicle alarm fired less than a
second before the car *stopped*: the camera's detection latency ate about two
of the three seconds. Re-drawing the camera's detection zone gains 0.3 s at
most, and the camera cannot push two webhooks within 20 s. A clean 192 px
plate reads at 0.99 on the device, so the frames of the first second are
usable. They are simply never looked at, because nothing has started yet. The
gate then takes about 20.5 s to open, so every second here is a second less at
a shut gate.

## What it watches, and what that costs

Measured on the Pi, 2026-09-21, each option for 20 s, niced, decoding to the
same cropped 160 px grey picture:

| Option | CPU, share of one core |
| --- | --- |
| **Sub stream (640x360 HEVC, 10 fps), software, sampled at 2 fps** | **1.8%** |
| Sub stream, software, sampled at 5 fps | 1.9% |
| 4K main stream, hardware decode (`-hwaccel drm`), 2 fps | 4.8% |
| 4K main stream, hardware decode, 1 fps | 3.8% |
| 4K main stream, software, 2 fps (every frame is decoded) | 56.2% |
| One 4K frame in software / through the hardware decoder | 101 ms / 18 ms of CPU |
| Spawning ffmpeg for one 4K frame (hardware / software) | 0.31 s / 0.45 s of CPU |
| Packet copy only, main / sub (for reference) | 2.1% / 0.7% |

The sub stream wins, and it leaves the hardware decoder to the sweep, which
needs it. MediaMTX already pulls that stream for the live view, so this is one
more local reader. Its GOP measured 4 s (2 keyframes in 82 frames), so
keyframes alone would be a picture every four seconds: every frame is decoded,
which is why sampling at 5 fps costs the same as at 2. **The rate is therefore
set by latency, not cost: 4 samples a second.** Two consecutive samples confirm
a daytime vehicle; at 2 fps that is up to a second after it appears, half of
the two seconds being fought for, and at 4 fps half a second.

The whole worker, as shipped, against the live stream:

| When | ffmpeg | Python | Total |
| --- | --- | --- | --- |
| Night (black picture), 40 s at 4 fps | 1.66% | 0.38% | **2.04%** |
| Night, 30 s at 2 fps | 1.63% | 0.18% | 1.81% |
| **Day, 90 s at 4 fps** | 2.35% | 0.44% | **2.79%** |

The budget is 5%. While a sweep, a presence session or a burst has the
controller, the child is *stopped*, not ignored: the cost is then nothing.
After a pause nothing is looked at for 5 s (longer than the GOP, because a
decoder that joins mid-GOP shows rubbish until the next keyframe) and the
background re-bases for 2 s more. The camera cannot alarm again inside that.

The confirmation layers run only on a would-trigger, on their own thread, at
most `GATE_EARLY_TRIGGER_LAYERS_PER_MINUTE` (4) a minute: one hardware decode
of a clear-stream keyframe (0.31 CPU-s) and one CLIP embed (108 ms measured)
for the CLIP look, three keyframe decodes and three local reads (about 1.5
CPU-s) for the plate look.

## The patch

`GATE_EARLY_TRIGGER_PATCH=x,y,w,h`, frame fractions like `GATE_PLATE_REGION`.
Default `0,0.248,0.338,0.338` since the zoom step of 2026-10-09 (the original
`0.02,0.26,0.32,0.32` mapped through the measured x1.056 about 0.501, 0.480,
kept square and started at the picture's left edge; see
[the camera notes](reolink-rlc-811a.md#zoom-step-to-position-3-2026-10-09)). Laid over a live
daytime frame (2026-09-21 06:27 UTC), the measured first-appearance box is the
bright gap where the drive comes in, at about (0.09, 0.37), and the gravel
below and to the right of it. The patch runs from the foreground fence to
x 0.34 so the first second of travel along the lane stays inside; down to
y 0.58 because gravel is the steadiest background in the picture; and only up
to the top of the far fence, because everything above that is trees, which are
the least steady. It clears the camera's clock and watermark. Ninety seconds of
the live daytime scene through the shipped worker: every armed sample `clear`,
no changed cell at all.

A car is much larger in this view than its plate width suggests: in the first
frame the camera's alarm produced (2026-09-20 18:15:43) it already *fills*
this corner of the picture. That is fine, and it is why the rule is about
**entry**: a front standing the full height of the patch takes about 14% of it
per sample at the measured 0.18 frame-widths a second, so it is a candidate on
its first sample and a would-trigger on its second, long before it fills
anything.

## Two detectors

Each 160x90 grey picture is averaged (Pillow's box filter) to a 24x16 grid,
and the grid is compared with a slowly adapting background in pure Python:
0.24 ms a sample on a laptop. Which detector runs is decided by the **measured
luma of the background** (night under 28/255, with hysteresis). That is not the
match policy's day/night, which is a clock schedule of how strict matching is;
here the only question is whether the patch is black. Every row records which
detector made it, and the report splits on that.

**Day.** Log-domain contrast with the *median* change removed, so an exposure
step, dusk, or the sun going in is no change at all (`global` when the median
itself jumps: the background is re-based and nothing triggers). What is left
must be one 4-connected blob of 6-80% of the patch; with little change outside
it (`scattered`: foliage, rain, dappled shade); no bigger than 45% the first
time it is seen (`sudden`: light arrives, vehicles enter); held for 2 samples;
and -- since the first shadow day -- it must have **replaced** what was there,
not re-lit it (`illumination`). Cloud shade advancing over part of the patch
passes every rule above: it is connected, vehicle-sized, enters from one side
and holds. But it multiplies the gravel's brightness rather than covering it,
so inside the blob the log ratio to the background is nearly one number. The
detector records that number's mean over the blob (`blob_contrast`), its
standard deviation (`blob_spread`) and the share of cells of the minority sign
(`blob_mixed`), and refuses the blob as light when the spread is under 0.30
*and* the contrast is under 0.55 in magnitude. On the seven shade triggers of
2026-09-22 the spread was 0.00-0.17 and the contrast -0.22 to -0.40 (the
gravel 20-33% darker), nothing mixed; on the three real vehicle frames of the
same day (a dark front tiny at the far fence gap; a pale flank filling the
patch, twice) the spread was 0.53-0.61 and the front's contrast -0.74. A
refused blob re-bases the background quickly, as `sudden` does, so a car that
then drives into the shade is seen against it. Each cell also learns its own
noise, so a hedge in the wind desensitises the cells it lives in and nowhere
else.

Two rules that were considered and are *not* used, because the day's data
said no: "the blob must be darker than its surround" -- all seven false blobs
were darker (shade), and the true flank was lighter and darker at once -- and
"penalise a clipped frame (`peak=255`)" -- the sunlit gravel clips in nearly
every daytime row, and the white bodywork of the real car clipped a fifth of
its blob's cells, more than any shade frame. The contrast bound is the one
place the rule could fail open: a hard noon shadow deeper than 42% would be
taken for an object. That is what the confirmation layer is for.

**Night.** The patch is black (real frames measured a mean of 0.1-1.8). The
signal is a source at least 60 levels over the background once the median rise
(diffuse light) is taken off, and at least 150 in itself; at least 4 cells
(1% of the patch); held for 4 samples (0.75 s); not fading; not crossing the
patch faster than 0.9 patch-widths a second; and **lighting the ground around
it**: the median rise itself must be at least 3 levels (`unlit` otherwise;
the run is kept, so spill that arrives a sample after the lamps still
counts). A car passing on the road beyond either lights the scene diffusely
with no source in it (removed with the median), or sweeps a beam across and is
gone in well under a second (persistence, speed). The size and spill floors
come from the first shadow night: at 04:29 UTC a two-cell source (0.5% of the
patch, peak 181, speed 0, median rise 0.0) held for four samples and was a
would-trigger -- eyeshine or a droplet, with nothing lit around it -- while
the one real floodlit frame with a car in the lane had a median rise of 44.
**Honestly:** at 1-2 fps a passing beam could not be told from an arrival at
all, which is another reason for 4; and even at 4 the speed gate and the spill
floor are reasoned, not measured, because no night arrival has been recorded
on this view.
One real finding already: a floodlit night frame with a departing car in the
lane, run through the detector over real black frames, became a would-trigger
at its fourth sample with a median rise of 44 levels, and the blob it chose was
the lit fence post as much as the car. The record carries `shift` (the median
rise) so those can be counted rather than argued about, and
`GATE_EARLY_TRIGGER_HOURS` exists so `on` can be daylight-only if night is bad.

**Both.** One would-trigger per appearance: after it the detector is
`occupied` until the patch has been clear for 3 s, and a thing that stays is
absorbed into the background in about 90 s, so a car parked in the patch
triggers once, not for ever (its leaving is at most one more). Nothing can
trigger for 8 s after a start.

Known weakness, by construction: a connected region of 6% or more that changes
by 20% or more and holds for half a second *is* a would-trigger. A gust that
moves a whole hedge that much will be one. The shadow day counts them.

This reuses `scene.thumbnail_difference` for `scene_difference` and
`stillness`, so those two numbers mean what `empty_scene_threshold` and the
sweep's stillness mean. It does not reuse `SceneBaseline` itself: that is one
whole-frame JPEG thumbnail refreshed every 30 s while idle, which answers "is
the drive empty" and cannot answer "did something just enter this corner".

**An option not built: the spotlight.** The camera's white spotlight lights the
whole drive well at night, and a night would-trigger could switch it on so the
sweep has a lit plate to read. It is only written down here. It is a camera
setting, which this change does not touch; it throws a strong diagonal flare
across the left-centre of the frame, which is where this patch is; and it
would make every passing headlight visible from the road.

## What it may do: the invariant

> **No picture of a passage reaches the cloud plate reader unless the camera
> has raised a vehicle event for that passage.**

`shadow` journals and records. It reaches nothing: the capture's
`on_early_trigger` refuses (`disabled`) unless the mode is `on`, whoever calls.

`on`: a would-trigger that passes confirmation asks the production
`TriggerFrameCapture` for a sweep, through the same `local_sweep` as a camera
alarm, with the origin **stated** (`SweepPassage.origin = "early"`), never
inferred from timing. With `GATE_EARLY_TRIGGER_CONFIRMATION=sweep` (the
default) every would-trigger of the vision rule passes and asks at once, and
the sweep is the confirmation; with `looks` only a would-trigger a second
look has confirmed inside the bound asks, and an unconfirmed one asks for
nothing and is recorded as `skipped_unconfirmed` (see
[Layers](#what-is-recorded)). Either way the ask is subject to the hours,
the minimum interval and the hourly cap under **Caps** below. Same local reader,
same match policy, same bars, same carried-read path, same cooldown. Until
the camera's alarm arrives, such a sweep:

- makes no cloud hand-over and no fallback hand-over (`hand_over` refuses any
  source but `sweep`, and the two call sites are gated as well);
- injects only a frame its own on-device read already authorises, and that
  frame carries `origin` and a `cloud_permit` on its `BurstIdentity`; an
  injector that cannot carry the permit is not given the frame;
- in the pipeline, `PreparedBurst.needs_cloud` is false while the permit says
  no, so the frame never enters the cloud lane; `_recognise` passes the permit
  to the recogniser, or answers "no plate" itself if the recogniser cannot take
  one; and `PlateRecognizerClient._recognise_once` asks the permit immediately
  before `session.post`, the one place a request leaves from. In
  `GATE_LOCAL_OCR_CLOUD=always` that is also where the on-device answer is
  taken, so the gate still opens and the label request is simply not sent;
- never gets a presence session, which exists to hand frames to the cloud.

The permit is *asked*, not remembered: a frame that waited while the alarm
arrived is allowed the moment it has. FTP stills are the camera's own and are
untouched. `tests/test_early_trigger_pipeline.py` enumerates these routes and
fails when a new one appears.

**The camera's alarm mid-sweep upgrades it in place.** The sweep takes the
alarm off the queue itself: one sweep, one decoder session, the frames already
read still in hand, cloud hand-overs from that moment, and the read window
re-measured *from the alarm*, so the head start is never charged to the car.
A queued early trigger gives way to a camera alarm; an early trigger takes no
part in the camera's rate limit, so it can never be why an alarm is
`skipped_interval`.

**The camera never speaks.** The sweep ends quietly after
`GATE_EARLY_TRIGGER_MAX_SECONDS` (6: the alarm has been measured about 2 s
after first appearance, so this is that with a wide margin). Nothing is sent
anywhere. It is journalled and recorded in the early-trigger table only: no
access-log event, no upload, nothing in the owner's Activity list. The one
exception is deliberate: a frame the sweep *authorised* and the pipeline then
refused is recorded as the denial it is, because that is an anomaly worth
seeing.

**Quick abort.** `GATE_EARLY_TRIGGER_ABORT_FRAMES` (5) frames in a row with no
plate box and an empty scene end an unconfirmed sweep. Empty frames are not
even read, so that costs a decoder start and five thumbnails, about a second,
whatever caused the trigger. A frame with something in it and no plate *yet*
does not count: that is a vehicle still turning in.

**Caps.** `GATE_EARLY_TRIGGER_CONFIRM_SECONDS` (0.5): with
`GATE_EARLY_TRIGGER_CONFIRMATION=looks`, how long a would-trigger waits for a
confirming look before it is judged unconfirmed; with `sweep` nothing waits.
`GATE_EARLY_TRIGGER_MIN_INTERVAL_SECONDS` (20, the camera's own
floor). `GATE_EARLY_TRIGGER_MAX_PER_HOUR` (12) *unconfirmed* sweeps in any
hour, then a stand-down of `GATE_EARLY_TRIGGER_BACKOFF_SECONDS` (1800) that
doubles each time, to four hours; a camera alarm forgives the count, because
the triggers were true. Twelve of the worst kind (six full seconds each, about
9 core-seconds) is 108 core-seconds an hour, 3% of one core, and **no cloud
lookup at all**. It still records while it is stood down.
`GATE_EARLY_TRIGGER_HOURS` (`HH:MM-HH:MM`, the farm-machinery policy's format,
default all day) and `GATE_EARLY_TRIGGER_TIMEZONE` restrict *acting*; recording
goes on outside them.

**Farm machinery.** The policy runs on undecided frames in `prepare`, and an
unconfirmed early sweep hands on no undecided frame (that would be an
access-log event per false trigger). So before the alarm a tractor gains what
every vehicle gains: the decoder already warm and the sweep already reading
when the alarm lands. From the alarm on, its frames reach the policy exactly as
today. The policy is local and sends nothing; its own rate cap is untouched.
Assessing machinery *before* the alarm is left for later.

## What is recorded

`early-trigger.db`, beside the controller's database and deliberately not in
it: these writes come from a background thread the moment something moves, and
the controller's database is where the relay's claim takes its lock. One table,
`early_trigger_observations`, a row per would-trigger **and per camera alarm**
(so misses can be analysed too): time, source (`vision`; `audio` is reserved),
mode, light, detector state, the evidence (`blob_fraction`, `changed_fraction`,
`scatter`, `mean_luma`, `bg_luma`, `luma_jump`, `peak`, `shift`, `persistence`,
centroid, `track_dx/dy`, `growth`, `speed`, `scene_difference`, `stillness`),
what was done, the layers, the early sweep's own report, and, filled in a
minute later by the correlator, the camera alarm it belongs to, the lead, and
the passage's first local read of 0.75 or better (read from the controller's
database, opened read-only).

Journal: `gate_early_trigger stage=would_trigger source=vision mode=… light=…
action=… confirmation=… by=… waited_ms=… blob=… changed=… scatter=… luma=…
luma_jump=… peak=… persistence=… contrast=… spread=…`; `stage=refused
reason=illumination|unlit …` once per appearance the vision rule itself turned
away; plus `stage=paused`, `stage=backoff`, and `stage=status` every ten
minutes (with cumulative `samples`, `confirmed`, `unconfirmed` and `refused`).
Nothing is added to the heartbeat.

**Pictures.** Each row keeps a before/after pair of the patch, 320x90 grey
JPEG, about 6 KB, in `early-trigger-thumbnails/`. At most 500 files
(`GATE_EARLY_TRIGGER_THUMBNAIL_MAX`) and none older than 7 days
(`GATE_EARLY_TRIGGER_THUMBNAIL_DAYS`): **about 3 MB**. They go nowhere.

**Layers, and the setting that puts them on the decision path.**
`GATE_EARLY_TRIGGER_CONFIRMATION=sweep|looks`, default `sweep` since
2026-10-10 ([why](#the-second-shadow-fortnight-2026-09-23-to-10-10)). With
`sweep`, a would-trigger of the vision rule reaches the capture at once in
`on`: the decision block says `confirmed` / `by: sweep` / `waited_ms: 0`, the
looks are `not_asked` (the sweep owns the clear-stream decoder and the plate
reader from the moment it starts), and the sweep's own report -- reads, plate
boxes, how it ended -- is annotated onto the row when it ends. The sweep is a
better second look than either layer: it reads live frames at 5 fps rather
than a keyframe up to a second old, it costs the lead nothing, the quick abort
ends it in about a second on an empty lane, and it sends nothing. In `shadow`
the looks still run for the record, with nobody waiting on them, and the
decision block says what `on` would have done.

With `looks`, each would-trigger of the vision rule is shown to two second
looks, in parallel, on their own threads; each is timed, and each is
`skipped_busy` the moment a vehicle has the controller. Either one saying
"vehicle" confirms the would-trigger; the worker waits for that at most
`GATE_EARLY_TRIGGER_CONFIRM_SECONDS` (0.5 s), and the wait ends at the first
"yes", when both have said "no", or the instant the camera's own alarm
arrives (there is then nothing left to lead). In `on` only a confirmed
would-trigger reaches the capture. Either way the record keeps three things
apart: the vision rule's own evidence (`features`, whose `verdict` is
`trigger`), what the looks said and the decision made on them (`layers`, with
a `decision` block: `confirmed` / `unconfirmed` / `cancelled_camera_alarm` /
why the looks never ran, which layer answered, and the milliseconds waited),
and what was done (`action`). So the raw vision rule stays measurable
whatever is in the way of it.

- *CLIP look.* The farm-machinery image tower, already loaded, shown a
  **square around the lane** (`lane_square`), not the frame: the tower looks
  at the centre square of what it is given and arrivals appear at the far
  left. "Vehicle" is an `empty` share of 0.5 or under. Checked on the Pi on
  real frames: for a night frame with a car in the lane the whole frame says
  `empty` 0.68 and the lane square says `pickup` 0.68 / `car` 0.26 /
  `empty` 0.03; two dusk arrival frames read as vehicles either way; and on
  the first shadow day the empty lane under moving shade read `empty`
  0.9974-1.0 on all eight looks. Needs `GATE_AGRI_ADMIT=shadow|on`; otherwise
  `unavailable`. The look has a lock of its own and never takes the readings'
  lock, so it can never make a real burst's machinery reading answer `busy`;
  and the looks are capped at `GATE_EARLY_TRIGGER_LAYERS_PER_MINUTE` (4),
  well under the policy's own rate, so the early trigger can never starve it.
- *Plate look.* Would the local detector find a plate box? The first look is
  what can confirm inside the bound; the second and third (0.4 s apart)
  carry on for the record, so a "yes" that came late is written down as
  `plate_late` (or `clip_late`) in the row's decision block and the report
  can say what a longer wait would have bought. Until 2026-10-10 the late
  "yes" reached the `Confirmation` but not the row, which is why the second
  fortnight's record has none although four true triggers earned one.
  In `on` the early sweep's own reads are then the answer.

**The added latency, measured.** On the Pi on 2026-09-22 the CLIP look took
455-530 ms end to end (one clear-stream keyframe decode, about 0.3 s, then the
108 ms embed) and the three plate looks 2.5-2.9 s together, so a single one is
about 0.55 s including its decode. The two run at once, so the first answer
was taken to be about half a second away and the bound was set at half a
second. **Over the second fortnight it was not:** on 81 would-triggers the
CLIP look took 539-773 ms (median 587) and the first plate look 668-1000 ms
(median 743), so at 0.5 s neither ever answered in time and the `looks` rule
confirmed nothing. With `looks` a confirmed would-trigger costs the lead the
wait; an unconfirmed one costs nothing but a sweep that would have been
false. The wait is on the detector's own thread, which misses at most two
samples while the detector is `occupied` anyway; it holds no lock, and the
camera's alarm on the webhook thread is never behind it -- it cancels it. If
the looks are refused (rate, busy, running, disabled) or unavailable, the
would-trigger is unconfirmed at once, with no wait.

## The first shadow day (2026-09-22)

Watched 5.1 h by day and 6.5 h by night (from the journal's status lines).
Vision alone: **7 false day would-triggers in 13 minutes** (08:38-08:51 IST):
the empty gravel lane, sun and cloud coming and going on a gusty morning.
Blobs of 8.6-10.7% of the patch, `luma_jump` -21 to +3.7, `peak` 255 (the
sunlit gravel clips), scatter at most 0.068, `track_dx` at most 0.054: every
rule of the day detector as first shipped was met, and 7 in 13 minutes is
over half the hourly cap. Both second looks cleared all seven at no cost to
the one true trigger (which they were `skipped_busy` for: a sweep had the
controller). One false night would-trigger, the two-cell point source above.
1.36 false an hour by day over the hours actually watched (the first cut of
the report said 3.18: it had estimated the hours from the rows alone, 2.2 h,
with every quiet stretch left out).

Misses: the one real daylight arrival was only a `candidate` (persistence 1)
at the instant of the camera's alarm, whose thumbnail shows the car's front
tiny at the far fence gap. The camera fired at first appearance; there was
nothing to lead. The would-trigger came 0.10 s after the *second* alarm, with
the car's flank filling the patch. One arrival is not a lead measurement.

What changed: the `illumination` rule, the night size and spill floors, the
layers on the decision path, and the report's split. The seven pairs, the two
vehicle pairs and the point source are in `tests/fixtures/early_trigger/` and
the detector is run over them.

## The second shadow fortnight (2026-09-23 to 10-10)

The report was run over 2026-10-05 14:00 to 10-10 06:30 UTC, the stretch after
the Pi caught up with `master` on 5 Oct: 77.6 h of day and 36.2 h of night from
the journal's status lines, 82 would-triggers, 24 of them followed by the
camera's alarm within a minute (15 by day, 9 by night) and 58 not (33 by day,
25 by night: **0.43 and 0.69 an hour** for the vision rule alone). Lead over
the camera, median: **1.2 s by day** (p10 0.3, p90 3.4), **3.1 s by night**
(p10 2.0, p90 4.2). Those 82 rows, with the vision rule's features and the two
looks' recorded answers and timings, are
`tests/fixtures/early_trigger/shadow-2026-10-05-to-10.json`;
`tests/test_early_trigger.py::ShadowRecordTests` replays them through the
worker's own decision path, and the numbers below are what reached the
capture.

**What the looks did on the decision path: nothing.** Of the 24 true
would-triggers the rule as shipped (CLIP or a plate box within 0.5 s)
confirmed none; of the 58 false, none. Three causes, in order of weight:

1. *The bound is under the looks' latency on this Pi.* The CLIP look took
   539-773 ms (median 587) and the first plate look 668-1000 ms (median 743),
   on all 81 would-triggers the looks ran for; the 2026-09-22 figure of
   455-530 ms was eight looks on a quiet day. One confirmation in the whole
   record since 22 Sep (2026-10-02 11:25, CLIP in 432 ms). 22 of the 24 true
   triggers were `unconfirmed` at 500 ms and 2 `cancelled_camera_alarm`
   (the alarm arrived 183 and 283 ms into the wait). The record's decision
   block also never says `plate_late` or `clip_late`, although four true day
   triggers got a plate box at 1.8-2.9 s: the decision was written when the
   wait ended and a yes after it reached the `Confirmation` but not the row.
   Fixed with this change, for the `looks` setting.
2. *Unbounded, the CLIP look is wrong both ways.* It says `empty` (0.98-1.0)
   to a car front coming through the far fence gap -- 11 of the 15 day true
   triggers -- and to all 9 night ones, where a headlight bloom in a black
   lane square is nothing it knows; and it says a vehicle class to people in
   the lane: 9 of the 33 day false triggers passed it, six of them people
   walking (hi-vis and all), the others a car the camera did not alarm on.
3. *The plate look is right but late, and it looks at a stale picture.* It
   boxed no plate on any false trigger but one (a car the camera ignored,
   10-06 07:29). On the true ones its first look (0.7-1.0 s) found a box in 5
   of 15 by day: a car front at the gap is 120-190 px of plate at the edge of
   the band, and the look decodes the newest clear-stream *keyframe*, which
   at the camera's 1 s interval is anywhere up to a second old, so the first
   look can be seeing the lane as it was before the trigger. By the third
   look (2.7-3.0 s) 8 of 15; 8 of the 15 were `skipped_busy`, cut short
   because the camera's own alarm had arrived and a real sweep took the
   reader. By night it boxed nothing in 9: the headlights bloom, the plate is
   unlit. A plate box that comes at 2 s is behind a 1.2 s lead.

What the pictures show. The 9 true night triggers are headlight blooms at the
gap with the lane lit by the car (4), or the lane lit by the PIR floodlight
with the fence posts' shadows thrown across it as the car trips it (5). The
25 false: the same floodlight coming on with nothing in the lane (10 --
identical rows: a 3-5% blob at the lit near fence post, x 0.05-0.07, speed 0,
median rise 50-68), passing cars' beams through the gap (9, sweeping right to
left, `track_dx` -0.10 to -0.17), and a few small sources. The 33 day false:
beams through the gap at dawn and dusk (13: a blob uniformly *brighter* than
the gravel with no darker cell in it), people (6), cars the camera did not
alarm on or the same car twice (4), two flat grey decoder frames, shade, and a
handful of small dark blobs at the gap that look exactly like a car front and
are most likely cars passing on the road beyond.

**Every rule measured, on this record.** False passed / true kept, by light;
the lead column is the median left to the true triggers that were within 6 s
of the alarm (10 by day, 4 by night), after the wait the rule needs:

| Rule | Day: false passed, true kept | Night: false passed, true kept | Useful lead left |
| --- | --- | --- | --- |
| Looks within 0.5 s (as shipped) | 0 / 33, 0 / 15 | 0 / 25, 0 / 9 | nothing kept |
| Looks within 1.0 s | 9 / 33, 6 / 15 | 3 / 25, 0 / 9 | -0.5 s |
| Looks within 3.0 s (the setting's maximum) | 9 / 33, 9 / 15 | 3 / 25, 0 / 9 | -0.3 s |
| CLIP alone, unbounded | 9 / 33, 4 / 15 | 3 / 25, 0 / 9 | 0.1 s |
| A plate box in any look, unbounded | 1 / 33, 8 / 15 | 0 / 25, 0 / 9 | -0.7 s |
| **Vision alone; the sweep confirms (the default now)** | **33 / 33, 15 / 15** | **25 / 25, 9 / 9** | **1.2 s day, 3.1 s night: all of it** |
| Vision + refuse a blob uniformly brighter than the gravel (contrast > 0.4, no darker cell) | 20 / 33, 15 / 15 | 25 / 25, 9 / 9 | all |
| Vision + night: the source must move or grow (speed >= 0.02 or growth >= 1.5) | 33 / 33, 15 / 15 | 12 / 25, 8 / 9 | all |
| Vision + night: refuse whole patch lit (`shift` >= 20) | -- | 2 / 25, 2 / 9 | none useful |
| Vision + night: refuse a leftward sweep (`track_dx` <= -0.1) | -- | 17 / 25, 9 / 9 | all |

No rule that waits for a second look keeps a night arrival, and by day the
wait any look needs is longer than the lead it is there to protect. Nothing
measurable at the moment of the trigger keeps 0 false *and* the true ones: a
person and a car front at the gap are the same size, darkness and stillness
in one sample; what tells them apart is the next second of frames, which is
exactly what the sweep reads.

**So the sweep is the confirmation.** `GATE_EARLY_TRIGGER_CONFIRMATION=sweep`
is the default: `on` asks for the local-only sweep the moment the vision rule
fires, subject only to the hours, the 20 s minimum interval and the hourly
cap, and the bar of "0 false passed" is met where it matters --
nothing false reaches the cloud, the owner's Activity list or the relay --
rather than at the sweep. What a false sweep costs, on this record:

- *CPU.* A sweep of an empty lane (the beams, the glitches, the shade: about
  25 of the 58) hits the quick abort in about a second, ~1.5 core-seconds.
  One with something in the lane (a person, the floodlit lane, a car the
  camera did not alarm on: about 33) runs its `GATE_EARLY_TRIGGER_MAX_SECONDS`
  (6 s) reading on the device, ~9 core-seconds. About 330 core-seconds in
  113.8 h, **0.08% of one core**; worst case all of them long, 0.13%. The
  worst rolling hour in the window held 5 false by day and 4 by night, against
  the cap of 12.
- *Blind time.* The detector is stopped while a sweep runs and for 7 s after
  it (5 s settle, 2 s re-base): 8-13 s per false sweep, about 10 minutes in
  113.8 h, 0.15% of the time. A real arrival in that window is detected by the
  camera as today, and an arrival *during* a false sweep upgrades it in place,
  with the decoder already warm.
- *Lookups, events, notifications, the relay:* none. The sweep's cloud routes
  are refused until the camera's alarm (`tests/test_early_trigger_pipeline.py`),
  an unconfirmed sweep writes no access-log event, and the relay moves only on
  an exact authorised on-device read -- which an empty lane, a beam, a person
  or the floodlight cannot produce. A departing car's rear plate is the one
  thing in the lane that can, and that is the camera-alarm path's question as
  much as this one's (#171): the early sweep's frames go through the same
  `local_sweep`, processor and match policy, and under `looks` the plate look
  would have confirmed a rear plate just the same.
- *The caps still hold.* `GATE_EARLY_TRIGGER_MIN_INTERVAL_SECONDS` (20) and
  the hourly cap of 12 unconfirmed sweeps with its doubling backoff are
  unchanged. On this record no true trigger had a false one in the 20 s before
  it. A bad hour -- the first shadow day's 7 in 13 minutes of shade before the
  `illumination` rule; 3 Oct's 24 in an hour of sun patches on the gravel,
  which that rule does not catch because they are brighter, not darker --
  stands the feature down for half an hour, which is today's behaviour.

**Not shipped, and what to watch.** The two detector rules in the table that
cost nothing here -- "a blob uniformly brighter than the gravel with no darker
cell is light" (removes 13 of 33 day false; the whole record since 22 Sep has
one true trigger it would refuse, a pale flank filling the patch at
2026-09-23 14:28 with the plate already boxed) and the night move-or-grow rule
(removes 13 of 25, costs 1 of 9) -- are fitted to 15 and 9 true triggers and
are not in the code; the sweep aborts the beams in a second anyway. If `on`
shows the floodlight's ten sweeps a night or the dusk beams filling the hourly
cap, the brighter-blob rule is the one to add first, with
`GATE_EARLY_TRIGGER_HOURS` as the blunt tool. Watch, in the report: `false
that PASSED it` is now the vision rule's own rate (the bar below is 6 an
hour); the `sweep` column's `reason` (`early_abort` against
`early_unconfirmed`) says which kind of false sweep is being paid for and
`first_plate_ms` what the sweep sees that the looks did not; and
`stage=backoff` in the journal says the cap was hit.

## The report, and what would justify `on`

```sh
scripts/early_trigger_report.py --database /var/lib/gate-controller/early-trigger.db \
    --events-database /var/lib/gate-controller/gate-controller.db \
    --audio-segments /var/lib/gate-controller/training-corpus/audio-segments
```

By day and by night: passages, lead (median/p10/p90), misses (no would-trigger
in the 6 s before the alarm, which is as long as an unconfirmed sweep runs; and
how many of those were while paused), false triggers an hour for the vision
rule alone and then **split**: removed by the confirmation layer, *passed* it
(what `on` would have swept, as a rate), unjudged, and true ones it cost; the
false ones bucketed by their evidence (whole patch lit; fast source; small
source; scattered change; brightness change; large region; compact region),
seconds saved to the first good read (an upper bound: it assumes the plate was
legible that much earlier), and the **layer table**: for vision alone (which
is what the `sweep` setting acts on), the `looks` rule (CLIP or plate box),
and each combination with CLIP, plate box and audio, false triggers removed,
true ones lost, lead kept. The split and the `PASSED` rate follow the
worker's own decision block, so under `sweep` they are the vision rule's own
numbers and under `looks` the looks'. It works on a private copy and never
writes to the record.

The hours a rate is over: `--journal /path/to/journal.txt` reads the
`stage=status` lines' cumulative sample counts (at the `fps=` of the
`stage=configured` line, or `--fps`) and credits each interval to the light
the detector was in, so quiet hours count. `--hours-day` / `--hours-night`
override it. With neither, the hours are estimated from the rows alone,
which leaves out every quiet stretch, and the report says so.

Audio is **offline**: from the recorder's 300 s AAC segments it finds whether a
vehicle-like sound (150-1200 Hz) was already rising 6 dB over the quiet half of
the 30 s before, held 2 s, and not mostly wind (under 150 Hz) or hiss (over
3 kHz, which is what rain is). It also says whether the gate was heard running
un-commanded in the 90 s before (somebody leaving). `--dump-audio-features`
writes the band levels of the 10 s before every camera alarm, and of quiet
moments, as JSONL, for a later "on the gravel or on the road" model. Labels are
free; nothing is trained. The thresholds are a heuristic, fitted to nothing.
YAMNet embeddings are not dumped because the scanner does not keep them.

Proposed bar for `on`, per light, over at least 20 passages:

- median lead **1.5 s or more** (under that the decoder's one-second start
  eats it);
- misses **no more than 1 in 20** while armed;
- false triggers that **pass the confirmation** no more than 6 an hour
  by day and 6 by night with the quick abort (about 1.5 core-seconds each:
  0.25% of a core), and no more than the hourly cap of 12 *ever*; and the
  confirmation costing no true trigger its lead. A false sweep costs CPU only, never a lookup,
  which is why the number can be this generous; it is the farm-machinery model
  and the audio scanner it must not starve, not the bill. Under `sweep` this
  is the vision rule's own false rate: 0.43 an hour by day and 0.69 by night
  on the second fortnight, worst hour 5 and 4.

If night fails and day passes: `GATE_EARLY_TRIGGER_HOURS=07:00-19:00`.

## A live audio pre-arm: not built

No live audio tap exists outside the segment recorder, and a second continuous
ffmpeg for one was out of scope. It would take a second output on the
recorder's existing child (`-map 0:a -f s16le -ar 16000 pipe:3`), a reader
thread, and a rule. The repo's own measurement says level alone is useless
(the ten loudest moments in 6.7 h were wind, birds and a voice), so the rule
would be YAMNet's vehicle classes held for 3 s, about 1% of a core. The table's
`source` column already takes `audio`.

## Not established

- No real frame of a vehicle *entering* the re-aimed view with a lead: the
  day detector is proven on synthetic scenes, on the real empty drive
  (fixtures and 90 live seconds), on a vehicle painted onto the real drive,
  and on the first shadow day's own pairs (seven shade, two vehicle). The one
  real arrival was at the camera's own alarm.
- No real night arrival: the night thresholds -- the speed gate and the spill
  floor above all -- are reasoned, not measured. The point source that set
  the size floor is in the fixtures; the floodlit frame is not.
- The `looks` bound of 0.5 s was set from eight measured looks on one day and
  is now known to be under this Pi's look latency (539-1000 ms over 81). If
  the looks are ever put back on the decision path the bound needs a second
  at least, which is most of the day lead; a `plate_late` or `clip_late` in
  the record is the sign the bound in force is too short.
- Whether the sub stream runs later than the main one. The report measures
  lead against the webhook, which is what matters.
- What a real early sweep reads in its first second: whether the first frames
  read as "empty scene" and trip the quick abort before the camera speaks, and
  whether a car front at the gap gives the sweep a plate box the plate look's
  stale keyframe never had. Only `on` can show it; the record's `sweep` column
  (`reason`, `plate_reads`, `first_plate_ms`) is where it will appear, and the
  cost if the abort fires early is today's behaviour, not worse.
