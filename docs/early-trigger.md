# Early Trigger: Noticing A Vehicle Before The Camera Says So

`GATE_EARLY_TRIGGER=off|shadow|on`. The code's default is `off`. The intended
rollout is a day of `shadow` on the Pi, then the report below, then `on`.
Issue #107 is the background; #106 is the audio half of it.

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
time it is seen (`sudden`: light arrives, vehicles enter); held for 2 samples.
Each cell also learns its own noise, so a hedge in the wind desensitises the
cells it lives in and nowhere else.

**Night.** The patch is black (real frames measured a mean of 0.1-1.8). The
signal is a source at least 60 levels over the background once the median rise
(diffuse light) is taken off, and at least 150 in itself; held for 4 samples
(0.75 s); not fading; and not crossing the patch faster than 0.9 patch-widths a
second. A car passing on the road beyond either lights the scene diffusely
(removed with the median), or sweeps a beam across and is gone in well under a
second (persistence, speed). **Honestly:** at 1-2 fps those two could not be
told from an arrival at all, which is another reason for 4; and even at 4 the
speed gate is a guess, because no night arrival has been recorded on this view.
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

`on`: a would-trigger asks the production `TriggerFrameCapture` for a sweep,
through the same `local_sweep` as a camera alarm, with the origin **stated**
(`SweepPassage.origin = "early"`), never inferred from timing. Same local
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

**Caps.** `GATE_EARLY_TRIGGER_MIN_INTERVAL_SECONDS` (20, the camera's own
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
action=… blob=… changed=… scatter=… luma=… luma_jump=… peak=… persistence=…`,
plus `stage=paused`, `stage=backoff`, and `stage=status` every ten minutes.
Nothing is added to the heartbeat.

**Pictures.** Each row keeps a before/after pair of the patch, 320x90 grey
JPEG, about 6 KB, in `early-trigger-thumbnails/`. At most 500 files
(`GATE_EARLY_TRIGGER_THUMBNAIL_MAX`) and none older than 7 days
(`GATE_EARLY_TRIGGER_THUMBNAIL_DAYS`): **about 3 MB**. They go nowhere.

**Layers, evaluated in shadow** (each timed; each `skipped_busy` the moment a
vehicle has the controller):

- *CLIP look.* The farm-machinery image tower, already loaded, shown a
  **square around the lane** (`lane_square`), not the frame: the tower looks
  at the centre square of what it is given and arrivals appear at the far
  left. Checked on the Pi on real frames: for a night frame with a car in the
  lane the whole frame says `empty` 0.68 and the lane square says
  `pickup` 0.68 / `car` 0.26 / `empty` 0.03; two dusk arrival frames read as
  vehicles either way. The *empty* lane by day was not put through it, so the
  negative is unchecked. Needs `GATE_AGRI_ADMIT=shadow|on`; otherwise
  `unavailable`.
- *Plate look.* Would the local detector find a plate box in the first three
  looks? In `on` the early sweep's own reads are the answer.

## The report, and what would justify `on`

```sh
scripts/early_trigger_report.py --database /var/lib/gate-controller/early-trigger.db \
    --events-database /var/lib/gate-controller/gate-controller.db \
    --audio-segments /var/lib/gate-controller/training-corpus/audio-segments
```

By day and by night: passages, lead (median/p10/p90), misses (no would-trigger
in the 6 s before the alarm, which is as long as an unconfirmed sweep runs; and
how many of those were while paused), false triggers an hour, the false ones
bucketed by their evidence (whole patch lit; fast source; small source;
scattered change; brightness change; large region; compact region), seconds
saved to the first good read (an upper bound: it assumes the plate was legible
that much earlier), and the **layer table**: for vision alone and each
combination with CLIP, plate box and audio, false triggers removed, true ones
lost, lead kept. It works on a private copy and never writes to the record.

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
- false triggers **no more than 6 an hour by day and 6 by night** with the
  quick abort (about 1.5 core-seconds each: 0.25% of a core), and no more than
  the hourly cap of 12 *ever*. A false sweep costs CPU only, never a lookup,
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

- No real frame of a vehicle *entering* the re-aimed view: the day detector is
  proven on synthetic scenes, on the real empty drive (fixtures and 90 live
  seconds), and on a vehicle painted onto the real drive.
- No real night arrival: the night thresholds, and above all the speed gate,
  are reasoned, not measured. The real night frames they were tried on are not
  in the repository.
- Whether the sub stream runs later than the main one. The report measures
  lead against the webhook, which is what matters.
- Whether the first frames of a real early sweep read as "empty scene" and
  trip the quick abort before the camera speaks. Only `on` can show it; the
  cost if so is today's behaviour, not worse.
