# Front Gate camera night configuration, 2026-09-06

Work done live against the camera API from the Pi between 22:31 and 22:45 IST
on 2026-09-06, in the dark, on an empty scene. Every number below is measured
from a 4K `Snap` JPEG taken after the change; nothing here is inferred from the
camera UI. Where a conclusion is reasoning rather than measurement it says so.

The camera is a Reolink RLC-810A ("Front Gate", 192.168.0.54, firmware
`v3.1.0.5764_2512171966`). Only image, ISP, IR and — under a later explicit
instruction — the AI vehicle sensitivity were touched. Push and FTP schedules,
the webhook registration, the AI detection *types*, the detection scope mask,
and the stream encoding were not changed; the encoder was read back afterwards
to prove it (`3840*2160` h265, 10 fps, gop 1, main stream; `640*360` h264 sub
stream).

## 1. The failure

At 22:16 the user's car was denied. The two frames that reached OCR had the
number plate sitting inside the car's own headlight blaze, and the whole left
third of the frame was a white blob where the camera's integrated IR
illuminator bounces off the near gate post about 1 m in front of the lens.
Plate Recognizer returned no plate on any frame. Night recognition has been
roughly 0-3% for two years. The controller journalled `clipping=0.09-0.16` with
`brightness≈0.40` on those frames.

The empty scene alone, before any change, already accounted for most of that:

| region | mean brightness | clipped ≥250 | clipped ≥240 |
| --- | --- | --- | --- |
| whole frame | 0.4112 | 0.0745 | 0.0766 |
| plate band (`GATE_PLATE_REGION` 0.05,0.4,0.9,0.6) | 0.4711 | 0.0867 | 0.0879 |
| left third | 0.5351 | **0.2248** | 0.2303 |

Snapshot: `00-before-ir-auto.jpg`. Nearly a quarter of the left third is pure
white with no vehicle present at all. The blob is not the only damage: it
throws a veiling glare — an optical flare inside the lens — that washes a milky
haze across the driveway and hedge and flattens contrast over the whole frame.
A car's headlights then add their own highlight on top of a scene that is
already sitting a fraction of a stop below clipping. That is why the plate
lands inside a blaze.

Two clipping thresholds are reported throughout. `≥240` is what the controller
itself measures (`gate_controller/images.py` sums `histogram[240:]` on a
thumbnailed grayscale), so it is the number comparable with the journal;
`≥250` is the stricter "genuinely blown" count. They track each other closely.

## 2. What the API actually exposes, and what it does not

Read from `GetIsp`, `GetIrLights`, `GetImage`, `GetWhiteLed` before any write.
Three findings shaped everything that followed.

**The IR illuminator has no brightness or zone control.** `GetIrLights` reports
`range.IrLights.state` as exactly `["Auto", "Off"]`. There is no power level,
no per-LED control, no near-field cutback. The blaze cannot be reduced through
the API; it can only be eliminated. That makes IR off the only available API
lever against it.

**There is no white LED, despite the API pretending otherwise.** `GetWhiteLed`
returns `code 0` with a full config block (`mode 1`, `state 0`, `bright 85`, a
18:00-06:00 lighting schedule). Setting `state 1, bright 100` also returned
`rspCode 200` — and produced an image statistically identical to the lamp being
off (brightness 0.0109, darkness 0.9959, both frames). The RLC-810A has no
spotlight hardware; the firmware exposes the command generically. Snapshot:
`probe-whiteled-on.jpg`. The white LED config was restored to its exact
original values immediately after the probe.

**The `shutter` and `gain` limits are inert while `exposure` is `Auto`.** This
is the important one, and it explains two years of fruitless tuning. Tightening
the auto caps from `shutter ≤8, gain ≤16` to `shutter ≤4, gain ≤8` moved
whole-frame clipping from 0.0763 to 0.0738 and left-third clipping from 0.2304
to 0.2225 — nothing, inside measurement noise. Meanwhile *manual* exposure at
the very same nominal numbers (`s8/g16`) produced brightness 0.1362 against
auto's 0.4110: auto was running the sensor about three times hotter than its
own stated ceiling. The camera only honours those fields in `Manual`. Every
past attempt to protect highlights by lowering the auto caps was a no-op.

## 3. Measurements

All rows are the same empty scene. Rows 02-13 keep the IR illuminator **on**
deliberately: its specular return off the near post is a bright, small, close
highlight against a dark background — structurally the same problem a
headlight-lit retroreflective plate presents — so it serves as a controlled
bright-source test bench for comparing exposure settings. Rows 01 and 14 have
it off. `s`/`g` are the manual shutter (ms) and gain values.

| # | configuration | frame bright | frame ≥250 | plate band ≥250 | left third ≥250 |
| --- | --- | --- | --- | --- | --- |
| 00 | **as found**: IR Auto, exp Auto, s≤8 g≤16, AF 50HZ | 0.4112 | 0.0745 | 0.0867 | 0.2248 |
| 02 | as found, re-measured through the sweep harness | 0.4110 | 0.0735 | 0.0860 | 0.2221 |
| 03 | exp Auto, antiFlicker Off | 0.4145 | 0.0763 | 0.0894 | 0.2304 |
| 04 | exp Anti-Smearing, antiFlicker Off | 0.3505 | 0.0692 | 0.0852 | 0.2091 |
| 05 | exp Auto, s≤4 g≤8 | 0.4126 | 0.0738 | 0.0858 | 0.2225 |
| 06 | exp Manual s2 g4 | 0.0540 | 0.0053 | 0.0099 | 0.0161 |
| 07 | exp Manual s4 g8 | 0.0942 | 0.0240 | 0.0404 | 0.0724 |
| 08 | exp Manual s6 g12 | 0.1199 | 0.0321 | 0.0509 | 0.0969 |
| 09 | exp Manual s8 g16 | 0.1362 | 0.0369 | 0.0583 | 0.1117 |
| 10 | exp Manual s4 g8 + backLight DRC 200 | 0.0935 | 0.0237 | 0.0398 | 0.0713 |
| 11 | exp Manual s4 g8 + backLight BLC 128 | 0.0948 | 0.0241 | 0.0412 | 0.0726 |
| 12 | exp Manual s4 g24 | 0.1206 | 0.0333 | 0.0523 | 0.1006 |
| 13 | **chosen exposure**, IR still on: Manual s4 g16 | 0.1159 | 0.0289 | 0.0480 | 0.0871 |
| 01 | IR **Off**, exp Auto s≤8 g≤16 | 0.0109 | 0.0000 | 0.0000 | 0.0000 |
| 14 | **deployed**: IR Off, Manual s4 g16 | 0.0180 | 0.0000 | 0.0000 | 0.0000 |

Snapshots are on the Mac in
`~/dev/gate-controller-data/camera-tests/2026-09-06/`, one file per row, named
for the row label.

What the table says:

- **Auto exposure cannot protect a highlight.** Rows 02-05 sit at 0.21-0.23
  left-third clipping no matter what the caps say. Only switching to `Manual`
  moves the number, and it moves it a long way (row 06: 0.0161, a 93%
  reduction). Auto meters the frame average; a retroreflective plate is one to
  two orders of magnitude brighter than the scene average, so auto will always
  blow it. This is the mechanism behind the 22:16 denial.
- **`Anti-Smearing` helps slightly but not enough.** Row 04 is a real 9%
  relative improvement in left-third clipping over row 03, and it is the only
  non-manual mode that moves at all. It does not approach what manual does.
  The user's earlier note that "Anti-Smearing alone did not shrink the blaze"
  is confirmed.
- **DRC and BLC do nothing measurable.** Rows 10 and 11 differ from row 07 by
  0.0011 and 0.0002 in left-third clipping. Reolink's backlight compensation is
  a tone curve applied after capture; it cannot recover a clipped sensor pixel,
  and on this scene it does not usefully lift the shadows either. `backLight`
  stays `Off`, which also keeps the existing house guidance in
  `docs/reolink-rlc-811a.md` intact.
- **`antiFlicker` is neutral here.** Row 03 against row 02 is 0.0763 vs 0.0735
  — if anything marginally worse, inside noise. It was still set to `Off`,
  for a different reason given in section 4.
- **Shutter and gain are near-interchangeable in this range.** Row 12 (s4 g24,
  brightness 0.1206, clipping 0.1006) and row 08 (s6 g12, 0.1199, 0.0969) buy
  the same light. That means shorter shutter plus more gain is free: same
  exposure, 1.5x less motion blur. It is the reason the chosen setting spends
  its budget on gain rather than shutter time.
- **Turning IR off removes the blaze completely** — 0.0745 to 0.0000
  whole-frame, 0.2248 to 0.0000 in the left third — **and removes the scene
  with it.** Row 01: brightness 0.0109, 99.6% of pixels below 33. There is
  effectively no ambient light at the gate. Everything visible in the "before"
  image was IR.

The single most useful image is `13-irON-manual-s4-g16.jpg`. At a third of the
original exposure the veiling haze across the driveway is gone and the fence
and trees are clean — but the near post is *still* a fully saturated white blob
throwing a local flare. A source that close and that specular saturates at any
exposure that leaves the rest of the scene usable. No ISP setting fixes the
near-post bounce. Only killing the illuminator or moving the post out of the
beam does.

## 4. What changed

### IR illuminator

| | before | after |
| --- | --- | --- |
| `IrLights.state` | `Auto` | `Off` |

```sh
# on the Pi
python3 /root/camtool.py raw '[{"cmd":"SetIrLights","action":0,"param":{"IrLights":{"state":"Off"}}}]'
python3 /root/camtool.py get GetIrLights      # confirm
```

Confirmed by `GetIrLights` before (`{"IrLights": {"state": "Auto"}}`) and after
(`{"IrLights": {"state": "Off"}}`).

### ISP

Read-modify-write of the whole `Isp` block so no field is dropped. Fields not
listed were read back and written unchanged.

| field | before | after | why |
| --- | --- | --- | --- |
| `exposure` | `Auto` | `Manual` | the only mode in which shutter and gain are honoured at all (section 2) |
| `shutter` | `{min 0, max 8}` | `{min 4, max 4}` | 4 ms = 1/250 s, the shutter already specified for the stop position in `docs/reolink-rlc-811a.md`; freezes any residual movement and caps how much light a retroreflective plate can accumulate |
| `gain` | `{min 1, max 16}` | `{min 16, max 16}` | gain adds no motion blur, so it is the right place to spend the exposure budget; 16 is the ceiling the system has run under for two years, so it is the least surprising value |
| `antiFlicker` | `50HZ` | `Off` | measured: with IR off the scene reads 0.0109 brightness, i.e. there is provably no mains-powered light anywhere in the field of view, so there is no flicker to cancel. `Off` is also the camera's own factory default, and it removes any risk of the firmware quantising the fixed 4 ms shutter up to a 10 ms mains multiple |
| `backLight` | `Off` | `Off` (unchanged) | rows 10-11 show DRC and BLC do nothing here |
| `drc` | `128` | `128` (unchanged) | inert while `backLight` is `Off` |
| `blc` | `128` | `128` (unchanged) | inert while `backLight` is `Off` |
| `hdr` | `0` | `0` (unchanged) | HDR double-exposure artefacts on a retroreflective plate |
| `dayNight` | `Auto` | `Auto` (unchanged) | keeps the IR-cut filter swinging out at night, which is what makes the sensor most sensitive to what headlights do put out |
| `dayNightThreshold` | `50` | `50` (unchanged) | |
| `nr3d` | `1` | `1` (unchanged) | noise reduction is worth keeping while gain is the main light source; the vehicle is stopped at the capture point so temporal smearing is not the risk it would be on a moving target |
| `whiteBalance`, `redGain`, `blueGain`, `mirroring`, `rotation`, `corridorMode`, `bd_day`, `bd_night`, `encType`, `constantFrameRate` | — | unchanged | echoed back verbatim |

```sh
python3 /root/camtool.py raw '[{"cmd":"SetIsp","action":0,"param":{"Isp":{
  "channel":0,"exposure":"Manual","shutter":{"min":4,"max":4},
  "gain":{"min":16,"max":16},"antiFlicker":"Off","backLight":"Off",
  "drc":128,"blc":128,"hdr":0,"dayNight":"Auto","dayNightThreshold":50,"nr3d":1}}}]'
python3 /root/camtool.py get GetIsp
```

One wrinkle worth recording: a write of `drc` while `backLight` is `Off` is
silently ignored — the field held the previous sweep's value of 200 until
`backLight` was momentarily set to `DynamicRangeControl`, `drc` written, and
`backLight` set back to `Off`. Verified afterwards as `drc: 128`.

### Image

Untouched. `bright`, `contrast`, `saturation`, `hue`, `sharpen` all remain at
`128`, confirmed by `GetImage` after the work. Nothing in the measurements
justified moving them: brightness and contrast are post-capture tone controls
and cannot un-clip a blown plate, and raising `sharpen` on a high-gain night
frame amplifies noise into the OCR crop.

### AI vehicle sensitivity

Changed under a later explicit instruction, which supersedes the original
"do not touch AI detection settings" constraint for this one field. At
sensitivity 100 the vehicle AI raised a false alarm on the empty scene at 20:27
(`gate_trigger_capture outcome=captured event_type=vehicle clipping=0.07` — the
IR blob), and each false alarm costs up to 6 OCR requests against a 1 req/s
cloud budget. Section 9 separates this from the motion-driven uploads that
accounted for the rest of tonight's traffic; only the 20:27 event was the
vehicle AI.

| | before | after |
| --- | --- | --- |
| `AiAlarm.sensitivity` (`ai_type` `vehicle`) | `100` | `80` |

The detection scope mask (60x33 grid), `min_target_width/height`,
`max_target_width/height` and `stay_time` were read back and written unchanged;
the script asserted equality afterwards and both assertions passed.

```sh
python3 /root/camtool.py raw '[{"cmd":"GetAiAlarm","action":1,"param":{"channel":0,"ai_type":"vehicle"}}]'
# then re-send the whole AiAlarm block with only "sensitivity" edited
```

`GetAiCfg` was read before and after: `AiDetectType` remains
`{people 1, vehicle 1, dog_cat 1, face 0}`. Detection types were not changed.

## 5. The trade-off, stated plainly

The instruction was to disable the IR illuminator *and* keep the scene
detectable. The measurement shows those two are in direct conflict at this
site, and the conflict is total rather than marginal: with IR off the empty
scene reads brightness 0.0180 and 99.6% of pixels below level 33. There is no
street lighting, no house light and no useful skyglow in the field of view.
Everything the camera saw at night was its own IR.

So the configuration now deployed is honest about what it is: **the camera has
no night illumination of its own, and both the trigger and the plate light now
come from the approaching vehicle's headlights.** The manual 1/250 s exposure
is set so that a headlight-lit plate lands in range rather than blown.

The reasoning behind `s4 g16` specifically — and this part is reasoning, not
measurement, because no car could be tested tonight: the IR return off the near
post at ~1 m is the only retroreflective-grade highlight available as a
reference. At `s4 g16` that post still clips (left third 0.0871). A plate at
the stop position is roughly 4 m away, so by inverse square it returns on the
order of sixteen times less light than the post, which should place it well
inside range and around mid-grey rather than clipped. If the next real entry
shows the plate still blown, the dial to turn is gain (16 → 8), which costs
sensitivity but no sharpness. If it shows the plate too dark, raise gain
(16 → 24), never the shutter — the shutter is what keeps the characters sharp.

**The risk this accepts, and it is a real one:** the camera's AI is the only
trigger in the whole pipeline. On a black scene it has nothing to classify
until headlights arrive. A vehicle with headlights on floods the driveway and
should trigger both the vehicle rule and the crossline rules; a vehicle
arriving on sidelights or DRLs alone may not. This could not be tested tonight.
If the next night entry produces no webhook and no FTP upload at all, the
illuminator is the cause and the one-line rollback in section 6 restores the
previous behaviour immediately. That failure mode — nothing happens — is worse
than tonight's, where the pipeline at least triggered and then failed to read.
It is the reason section 8 treats the white light as the blocking fix rather
than a nice-to-have.

The competing option, kept on the record: leaving IR on and running `Manual
s4 g16` anyway (row 13) contains the blaze from 0.2248 to 0.0871 — a 61%
reduction — while leaving a dim but non-empty scene at brightness 0.1159 for
the AI. That is the configuration to fall back to if IR off proves to kill the
trigger and the white light is not yet installed. It is strictly better than
the state found tonight in both respects.

## 6. Rollback

Every change is one command. To restore the exact state found at 22:31 on
2026-09-06:

```sh
# on the Pi, as root
# 1. IR illuminator back to Auto
python3 /root/camtool.py raw '[{"cmd":"SetIrLights","action":0,"param":{"IrLights":{"state":"Auto"}}}]'

# 2. ISP back to auto exposure with the original caps
python3 /root/camtool.py raw '[{"cmd":"SetIsp","action":0,"param":{"Isp":{
  "channel":0,"exposure":"Auto","shutter":{"min":0,"max":8},
  "gain":{"min":1,"max":16},"antiFlicker":"50HZ","backLight":"Off",
  "drc":128,"blc":128,"hdr":0,"dayNight":"Auto","dayNightThreshold":50,"nr3d":1}}}]'

# 3. AI vehicle sensitivity back to 100
#    re-send the AiAlarm block from the backup with sensitivity 100
```

To fall back only as far as "IR on, exposure fixed" (the section 5 compromise),
run step 1 and leave the ISP as deployed.

`/root/camtool.py` is a small root-only helper left on the Pi for this work. It
reads the camera credentials out of `/etc/gate-media-gateway.env`, caches one
login token in `/root/.camtool-token.json` and reuses it, because this firmware
returns 502 for about a minute if you log in repeatedly. The rollback does not
depend on it: the same calls work with `curl -k` against
`https://192.168.0.54/cgi-bin/api.cgi?cmd=<Cmd>&token=<token>`, POSTing the same
JSON array shown above. Log in once with `cmd=Login` to get the token and reuse
it for the whole session; do not put the credentials in a shell history — read
them from the env file in a script, as the helper does. Nothing in the pipeline
depends on the helper existing; deleting it costs only the convenience.

Full `Get` output captured before any write is on the Pi, root-readable only:

| area | file |
| --- | --- |
| ISP | `/root/camera-isp-before-2026-09-06.json` |
| image | `/root/camera-image-before-2026-09-06.json` |
| IR lights | `/root/camera-irlights-before-2026-09-06.json` |
| white LED | `/root/camera-whiteled-before-2026-09-06.json` |
| AI vehicle alarm | `/root/camera-aialarm-before-2026-09-06.json` |

The same five files are committed beside this document in
`2026-09-06-camera-config-backup/`. They contain camera settings only — the
camera credentials live solely in `/etc/gate-media-gateway.env` on the Pi and
appear in no file here. The yesterday backup `/root/camera-isp-before-2026-09-05.json`
is untouched.

## 7. Timing budget

The target is the gate opening within 5 s of the car stopping. The only
successful open this weekend, 2026-09-05 18:26, took 5.1 s end to end, of which
2.5 s was the Plate Recognizer cloud round trip. That leaves about 2.5 s for
everything else, and it means **there is no budget for a second look**: the
camera work has to make the *first* frame readable, not the third. A frame that
arrives blown is not a frame that costs a retry, it is a frame that costs the
whole 5 s target.

This reframes the exposure choice. Auto exposure's failure is not that it reads
plates badly on average — it is that it fails on the first frame and then needs
a retry the budget cannot afford. A fixed manual exposure produces the same
result on every frame, which is what a one-shot budget needs; it either works
or it is wrong by a known, correctable amount that section 5 says how to dial
out. The presence retry window is separately being cut on the Pi from 45 s to
12 s (one full retry after a timed-out first attempt, derived from the 6 s
decision timeout) by the main session — not by this work, and no controller
configuration was touched here.

Trigger hygiene feeds this directly. Every false alarm spends up to 6 OCR
requests against a 1 req/s budget, which can leave a real arrival queued behind
requests for an empty driveway. Lowering vehicle sensitivity from 100 to 80
removes one class of those; taking generic motion off the FTP table (section 9)
removes by far the larger class.

## 8. What still needs a physical fix

The API work has taken the blaze out and made the exposure predictable. It
cannot create light, and that is now the binding constraint.

1. **White light on the stop position.** This is the blocking item, not an
   optional improvement. The measurements prove the site has no night
   illumination whatsoever once the camera's own IR is off (brightness 0.0180,
   99.6% dark), and the RLC-810A has no spotlight to fall back on — the
   `GetWhiteLed` probe in section 2 settles that. A modest warm white lamp
   aimed at the stop position, not at the camera, makes the AI trigger reliable
   again, lets the plate be lit by something other than the car's own
   headlights, and would allow gain to come back down for a cleaner crop. Until
   it exists, the system depends on every arriving vehicle having its headlights
   on.
2. **Get the near gate post out of the IR beam.** Shielding, a hood, or
   re-aiming so the post about 1 m from the lens is no longer in the
   illuminator's cone. Row 13's image is the evidence for why this is a
   physical job: at a third of the original exposure the post is still fully
   saturated and still flaring. If this is fixed, IR `Auto` becomes usable
   again and item 1 becomes less urgent. A privacy mask over the blob was
   considered and rejected — it would hide the white pixels but not the veiling
   glare, which is generated optically inside the lens and degrades the whole
   frame.
3. **Camera angle off the headlight axis.** The camera currently looks close to
   straight down the approach, so headlights point into it. A few degrees of
   offset, or a slightly higher mount looking down at the plate, moves the
   plate out of the headlight cone and is standard ANPR practice.
4. **The planned RLC-811A swap.** Covered in `docs/reolink-rlc-811a.md`. The
   zoom lets the plate fill more of the frame at the stop position, which is
   worth more than any ISP setting — but note the 811A is about 1.4 stops
   darker at the long end, so it makes item 1 more necessary, not less.

## 9. Follow-up: generic motion was driving every OCR request

Added at 22:50 after the user reported Plate Recognizer quota being consumed.
Between 22:32 and 22:41, while the settings above were being changed, the app
logged a run of black-frame "Access Denied" events. Each one cost a cloud OCR
request. This section is the diagnosis and the fix.

### What was actually firing

The camera's upload and notification triggers live in three independent
schedule tables, each a 168-character week grid (one character per hour) per
alarm type. Read with `GetFtpV20`, `GetPushV20` and `GetRecV20`. A warning for
anyone repeating this: `action: 1` returns `initial`, `range` **and** `value`,
and the `initial` block is the factory default, not the live setting. The live
state is in `value` — reading `initial` by mistake gives a completely different
and wrong answer.

Live state before this change, showing only the types that were enabled (all of
them enabled for the full 168 hours):

| table | enabled alarm types before |
| --- | --- |
| **FTP** | `MD`, `AI_PEOPLE`, `AI_DOG_CAT`, `AI_VEHICLE`, `AI_CROSSLINE_1`, `AI_CROSSLINE_2` |
| **Push** | `AI_VEHICLE`, `AI_CROSSLINE_1`, `AI_CROSSLINE_2` |
| **Record** | `MD`, `AI_PEOPLE`, `AI_DOG_CAT`, `AI_VEHICLE`, `AI_CROSSLINE_1`, `AI_CROSSLINE_2` |

`MD` is generic motion detection, and on the FTP table it was enabled around the
clock. That is the whole explanation. The controller sends **every** camera FTP
still to cloud OCR, and the empty-scene gate only covers session captures, not
FTP stills. So every motion event — a gust in the hedge, rain, a headlight
sweep on the road, or an abrupt exposure change made over the API — became a 4K
JPEG on the Pi and a paid Plate Recognizer request. `AI_PEOPLE` and
`AI_DOG_CAT` were uploading too, which nothing in this pipeline uses.

Push was already correct, and had been all along: AI vehicle and the two
crossline rules only, no motion. That asymmetry is why the journal for
22:32-22:41 shows a long run of `filesystem_ingress` lines and **not one**
`gate_trigger_capture`. The webhook path never fired. Only the FTP-on-motion
path did.

### Was it my changes, or is the camera firing on the black scene?

It was the changes. Every `SetIsp` and `SetIrLights` call rewrites the whole
frame's brightness instantly, which is about as unambiguous a motion event as
can be manufactured. The FTP timestamps line up one-for-one with the sweep:
22:32:35/37/39 with the IR illuminator going off, 22:35:43 with the white LED
probe being restored, 22:36:40-47 and 22:37:52-57 and 22:38:34 through the
first candidate sweep, 22:40:53-58 through the second. Nothing fired between
22:41 and the follow-up work.

The camera is **not** raising vehicle or crossline alarms on the black scene:
zero `gate_trigger_capture` lines in that entire window, and Push carries only
AI types. Earlier tonight is a different story and worth separating out:

- **20:27** — a genuine AI vehicle false alarm on an empty scene,
  `gate_trigger_capture outcome=captured event_type=vehicle clipping=0.07`.
  This is the event that justified dropping vehicle sensitivity 100 → 80 in
  section 4. Its cause was the IR blob, which is now switched off.
- **21:00 and 22:32-22:41** — `filesystem_ingress` only, no capture line.
  Motion-driven FTP, not AI.

### What changed

Only the FTP schedule table. Every other field in the `Ftp` block — including
the FTP server address, user name and password — was read back and written
unchanged; the script asserted that afterwards and confirmed the credentials
are still populated.

| FTP alarm type | before | after |
| --- | --- | --- |
| `MD` | 168/168 | **0/168** |
| `AI_PEOPLE` | 168/168 | **0/168** |
| `AI_DOG_CAT` | 168/168 | **0/168** |
| `AI_VEHICLE` | 168/168 | 168/168 (kept) |
| `AI_CROSSLINE_1` | 168/168 | 168/168 (kept) |
| `AI_CROSSLINE_2` | 168/168 | 168/168 (kept) |
| all other types | 0/168 | 0/168 |

`AI_CROSSLINE_0` stays disabled: Push has it at 0 while `_1` and `_2` are at
168, which identifies `_1` and `_2` as the two rules actually in use.

**Push was not changed** — it was already exactly the wanted set. **Record was
not changed.** Recording is governed by its own table and writes to the SD card;
it does not produce FTP uploads, so leaving `MD` enabled there costs no OCR
requests and preserves the user's recorded footage of motion events.

Vehicle sensitivity stays at **80**. It is not being lowered further, and the
reason matters: with the IR illuminator off and `MD` no longer uploading,
`AI_VEHICLE` and the two crossline rules are now the *only* things that can
trigger the pipeline at all. The single AI false alarm tonight (20:27) was
caused by the IR blob, which no longer exists. Cutting sensitivity further
would trade a problem that is already solved against the one failure mode there
is no recovery from.

### Effect

Before, on this scene, six alarm types could raise an FTP still at any hour, and
three of them (`MD`, `AI_PEOPLE`, `AI_DOG_CAT`) fire on things the gate does not
care about. After, only a vehicle or a crossline event uploads. The 22:32-22:41
burst that prompted this section would not have produced a single OCR request
under the new table.

Verified on the empty scene afterwards, watching
`journalctl -u file-monitor.service`:

| window | FTP stills (`filesystem_ingress`) | OCR requests (`gate_ocr`) |
| --- | --- | --- |
| 22:32-22:41, before the change, during the ISP sweep | 13 | 13 |
| 22:50-23:19, after the change, scene untouched | **0** | **0** |

including a dedicated 2.5-minute continuous watch from 23:07:37 to 23:10:07
that recorded zero events of any kind.

There is a real cost, and it compounds the section 5 trade-off rather than
sitting beside it. `MD` was a crude but genuinely functional night trigger: a
headlit car sweeping the driveway is trivially detectable as motion, whether or
not the AI classifies it as a vehicle. Removing it, on the same night the IR
illuminator was switched off, means the night pipeline now rests entirely on
AI vehicle and crossline detection against a scene lit only by the arriving
car. If the next night entry does not trigger, restore `MD` on the FTP table as
the first diagnostic step — it is the cheaper half of the rollback and it will
distinguish "the AI cannot see the car" from "nothing is reaching the Pi".

### Rollback

Full `Get` output captured before the change is on the Pi as
`/root/camera-{ftp,push,rec}-before-2026-09-06b.json` (root-readable only,
contains the FTP credentials), with credential-redacted copies committed beside
this document as `camera-*-before-2026-09-06b.redacted.json`. The redacted
copies are enough to rebuild the schedule tables; the credentials in the live
block are never overwritten by a schedule-only edit.

To restore motion-driven FTP uploads, read the current `Ftp` block, set the
`MD` row of `schedule.table` back to 168 ones, and send the whole block back
with `SetFtpV20`. Do not hand-write the `Ftp` block from scratch — round-trip
the live one, or the FTP password will be blanked and uploads will stop
entirely.

## 10. What the next real night entry should show

Nothing here has been tested against a car. The next night arrival is the
experiment, and these are the things to read off it.

- **Did anything trigger at all?** A webhook line or an FTP upload. If neither
  appears, IR off has blinded the AI; roll back step 1 in section 6 and treat
  the white light as urgent.
- **`gate_trigger_capture ... clipping=`** should be well below tonight's
  0.09-0.16. The empty-scene plate band is now 0.0000 against 0.0867, so
  whatever the number is, it is now attributable to the car rather than to the
  IR bounce. Above about 0.05 means the plate is still blown: drop gain to 8.
- **`gate_ocr plate_box=`** appearing at all means Plate Recognizer found a
  plate region, which it did not manage once tonight.
- **Whether the plate is legible but the surroundings are black.** That is the
  expected and acceptable outcome, not a fault. A correctly exposed
  retroreflective plate against a dark background is what an ANPR frame looks
  like.
- **Time from stop to relay**, against the 5 s target and the 5.1 s baseline.
- **A `gate_trigger_capture` line should now accompany the FTP still.** Before
  section 9, FTP fired on motion and the webhook fired on AI, so the two paths
  ran independently. Now both are driven by the same AI events, so a real
  arrival should produce a `filesystem_ingress` *and* a
  `gate_trigger_capture outcome=captured event_type=vehicle`. An
  `filesystem_ingress` on its own would mean the FTP table change did not take.
- **Overnight OCR volume should fall to near zero on an empty scene.** Any
  `filesystem_ingress` with no vehicle present now means a genuine AI false
  positive rather than a motion event, which is a different and more
  interesting problem.
- **False alarms overnight** at sensitivity 80: tonight's 20:27 vehicle-AI
  firing should not recur, its cause (the IR blob) being switched off.
