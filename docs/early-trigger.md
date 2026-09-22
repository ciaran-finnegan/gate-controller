# Early Trigger: Noticing A Vehicle Before The Camera Says So

`GATE_EARLY_TRIGGER=off|shadow|on`. The code's default is `off`. The intended
rollout is a day of `shadow` on the Pi, then the report below, then `on`.
Issue #107 is the background; #106 is the audio half of it. The first shadow
day was 2026-09-22; what it showed and what changed because of it is
[below](#the-first-shadow-day-2026-09-22).

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
Default `0.02,0.26,0.32,0.32` (x 0.02-0.34, y 0.26-0.58). Laid over a live
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

`on`: a would-trigger that a second look has **confirmed** (next section)
asks the production `TriggerFrameCapture` for a sweep, through the same
`local_sweep` as a camera alarm, with the origin **stated**
(`SweepPassage.origin = "early"`), never inferred from timing. An unconfirmed
one asks for nothing and is recorded as `skipped_unconfirmed`. Same local
reader, same match policy, same bars, same carried-read path, same cooldown.
Until the camera's alarm arrives, such a sweep:

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

**Caps.** `GATE_EARLY_TRIGGER_CONFIRM_SECONDS` (0.5): how long a would-trigger
waits for a confirming look before it is judged unconfirmed.
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

**Layers, on the decision path.** Each would-trigger of the vision rule is
shown to two second looks, in parallel, on their own threads; each is timed,
and each is `skipped_busy` the moment a vehicle has the controller. Either
one saying "vehicle" confirms the would-trigger; the worker waits for that at
most `GATE_EARLY_TRIGGER_CONFIRM_SECONDS` (0.5 s), and the wait ends at the
first "yes", when both have said "no", or the instant the camera's own alarm
arrives (there is then nothing left to lead). In `on` only a confirmed
would-trigger reaches the capture. The record keeps three things apart: the
vision rule's own evidence (`features`, whose `verdict` is `trigger`), what
the looks said and the decision made on them (`layers`, with a `decision`
block: `confirmed` / `unconfirmed` / `cancelled_camera_alarm` / why the looks
never ran, which layer answered, and the milliseconds waited), and what was
done (`action`). So the raw vision rule stays measurable after the layer is
in the way of it.

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
  `plate_late` and the report can say what a longer wait would have bought.
  In `on` the early sweep's own reads are then the answer.

**The added latency, measured.** On the Pi on 2026-09-22 the CLIP look took
455-530 ms end to end (one clear-stream keyframe decode, about 0.3 s, then the
108 ms embed) and the three plate looks 2.5-2.9 s together, so a single one is
about 0.55 s including its decode. The two run at once, so the first answer is
about half a second away and the bound is half a second: a confirmed
would-trigger costs the lead up to 0.5 s; an unconfirmed one costs nothing but
a sweep that would have been false. The wait is on the detector's own thread,
which misses at most two samples while the detector is `occupied` anyway; it
holds no lock, and the camera's alarm on the webhook thread is never behind
it -- it cancels it. If the looks are refused (rate, busy, running, disabled)
or unavailable, the would-trigger is unconfirmed at once, with no wait.

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
legible that much earlier), and the **layer table**: for vision alone, the
shipped rule (CLIP or plate box), and each combination with CLIP, plate box
and audio, false triggers removed, true ones lost, lead kept. It works on a
private copy and never writes to the record.

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
- false triggers that **pass the confirmation layer** no more than 6 an hour
  by day and 6 by night with the quick abort (about 1.5 core-seconds each:
  0.25% of a core), and no more than the hourly cap of 12 *ever*; and the
  layer costing no true trigger its lead. A false sweep costs CPU only, never a lookup,
  which is why the number can be this generous; it is the farm-machinery model
  and the audio scanner it must not starve, not the bill.

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
- The confirmation bound of 0.5 s is set from eight measured looks on one
  day. A `plate_late` or `clip_late` in the record is the sign it is too
  short.
- Whether the sub stream runs later than the main one. The report measures
  lead against the webhook, which is what matters.
- Whether the first frames of a real early sweep read as "empty scene" and
  trip the quick abort before the camera speaks. Only `on` can show it; the
  cost if so is today's behaviour, not worse.
