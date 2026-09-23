# Letting Farm Machinery In

This is a working farm. A tractor or a telehandler has no plate the camera can
read, and on 2026-09-07 one was in front of the camera for 57 seconds — eight
frames: three timeouts, an OCR error, a coalesced burst and three `no_match` —
before somebody let it in. Policy: **recognised agricultural machinery is
admitted regardless of plate, and the access log says that is why the gate
opened.**

It is an *appearance credential*, not a plate read (gate-controller #96, #63).
The event says `source="appearance"`, `reason="farm_machinery"` and names no
authorised plate. Nothing in it is called OCR.

`gate_controller/agricultural.py` is the whole of it; `GateProcessor` consults
it. It is **off** unless the environment says otherwise.

> **Every number below was measured on the old camera aim.** The camera was
> re-aimed on 2026-09-20 between 16:58 and 18:15 UTC and the crop band is now
> `GATE_PLATE_REGION=0.05,0.10,0.90,0.85` (widened again on 2026-09-23). All six machinery passages, and all
> but 19 of the 929 frames, were taken before that; **no machine has been seen
> on the new aim at all.** The thresholds are therefore a starting point, not a
> calibration. **A shadow run on the new aim is the validation that gates
> `on`**: `on` should not be set until shadow has recorded real machinery
> arriving and leaving on this aim and every `would_admit` has been looked at.

## The flag

| `GATE_AGRI_ADMIT` | What happens |
| --- | --- |
| `off` (default, and anything unrecognised) | Nothing is built, no model is loaded, the processor is handed `None`. |
| `shadow` | Every undecided burst is assessed, journalled (`gate_agri ...`) and kept in the local `event_appearance` table with its scores and `would_admit`. The relay is never asked, the event sent to the app is exactly what it was, and no plate decision waits for it. |
| `on` | A `would_admit` opens the gate, through the same `ActuationCoordinator` claim, cooldown and pre-activation checks as a plate match. |

| Setting | Default | |
| --- | --- | --- |
| `GATE_AGRI_ADMIT_MIN_MACHINE` | `0.75` | share of probability on the machinery prompts |
| `GATE_AGRI_ADMIT_MIN_MARGIN` | `0.60` | machinery less the strongest other class |
| `GATE_AGRI_ADMIT_STILL_COSINE` | `0.95` | how alike two frames must be to count as standing still |
| `GATE_AGRI_ADMIT_HOURS` | `06:00-22:00` | local hours in which it may admit; `00:00-24:00` for always |
| `GATE_AGRI_ADMIT_TIMEZONE` | `Europe/Dublin` | |

None of the three numbers can be set below 0.5; a value that does not parse
keeps the default and says so at start-up.

The model is CLIP's image tower, `clip-vit-b32-visual-int8.onnx` in
`GATE_MODEL_DIR` — the file `direction_vision` already uses, installed beside
the plate models and not kept in the repository. The prompts are frozen as
numbers in `gate_controller/models/agri-clip-v1.json`
(`scripts/export_agri_prompts.py` regenerates it, on a laptop). With the model
missing the policy answers `model_unavailable` to everything.

## The rule

A burst is admitted only when **all** of this holds.

1. **Clearly machinery, in every frame read.** The machinery prompts hold at
   least `min_machine` of the probability *and* lead the strongest other class
   by `min_margin`. "Other" is every class separately — car, van, lorry,
   pickup/4x4, vehicle-with-trailer, construction plant, people and animals,
   the empty driveway — so machinery has to beat each of them, not their
   average. Winning the argmax is not enough.
2. **More than one frame agrees** when the burst has more than one (two are
   read). One frame decides only when it is the only one there is.
3. **Not leaving.** No frame reads `exiting` under `direction_vision`'s rule.
4. **Standing still.** An earlier clear frame of the same machine, taken
   between 1 and 15 seconds before this one, looks the same (cosine ≥ 0.95
   between the two image embeddings). See "Unknown direction" below: this is
   what stands in for a direction the model cannot give.
5. **Inside the hours.**

Anything else — unsure, a road vehicle, frames that disagree, model missing, a
library missing, an unreadable frame, an inference that overruns, a second
reading requested while one is running, an exception anywhere — is a verdict of
"no". The plate path's own answer stands and the gate does what it would have
done had this module not existed. It never blocks a plate that would have
opened, because it is not asked about one.

### Unknown direction — decided, and why

The brief was: never open for a vehicle judged to be leaving; for unknown
direction, decide and justify.

**Measured: for machinery, direction is almost always unknown.** The direction
prompts are about cars ("the rear of a car", "tail lights visible"). Of the 28
frames in the stored photos that are clearly machinery they read `unknown` on
25. The three they called `exiting` are one distant departure (09-01) and two
arriving tractors passing the lens side-on. They called **none** of the six
clear frames of the other two departures on record — the tractor leaving at
16:32 on 2026-09-08 and the telehandler leaving at 14:58 on 2026-09-19.
Seventeen machinery-specific prompts ("a tractor seen from behind", "a
telehandler's rear counterweight", …) were tried against the same frames and
separated nothing: "rear of a tractor" outscored "front of a tractor" on every
frame, arriving or leaving.

So "unknown → admit" would have pulsed the relay for **two of the three
departures** in the data — and a pulse is not harmless. The relay drives the
operator's step-by-step input: measured on 2026-09-21 (#171,
`docs/deployment.md`), a pulse on a fully open gate starts it **closing** two
seconds later, and a pulse on a moving gate stops it dead. For a machine
driving out through an open gate that is the gate closing on it. "Unknown →
refuse" would never admit anything. Neither is acceptable, so the question was
changed to one a camera can answer:

> A machine that is waiting to come in is **standing still in front of a closed
> gate**. One that is leaving has the gate behind it, and keeps going.

Measured on clear machinery frames 1–15 s apart, embedding against embedding:

| | pairs | cosine |
| --- | --- | --- |
| arriving, and stopped (09-07 tractor, 09-08 tractor, 09-19 telehandler) | 9 | **0.962 – 0.977** |
| arriving, and still moving | 21 | 0.769 – 0.931 |
| driving away (09-08 tractor; the telehandler's two clear frames were 0.8 s apart) | 5 | **0.813 – 0.922** |

The threshold, 0.95, sits in the gap. The one-second floor matters: two frames
of the departing tractor 0.7 s apart scored 0.964.

This is two departures and three arrivals. It is a rule with a physical reason
and a small sample, and it says so; `stillness` and `still_seconds` are
recorded for every clear burst in shadow precisely so the number can be checked
against a season rather than a fortnight. What it cannot catch is a departing
machine that stops dead in view of the camera for more than a second. Then the
relay is pulsed for a vehicle that is already out — which is what the plate
path does today for a departing car whose rear plate reads, and is the reason
to read a shadow run before turning this on.

Nothing else available live does better, and `docs/vehicle-direction.md`
reaches the same place from the other side. `DirectionTracker`'s box-width fit
is fitted on plate boxes, resolved 7 of 78 passages there and was wrong on two
of the seven. The gate's own movement is not available live — `gate_sound_scan`
reads finished audio segments on a timer — and a diesel at walking pace reads
as the gate motor for most of a minute, so that page's combined verdict says
nothing when the photo's top label is `machine`: it leaves **both telehandler
passages of 2026-09-19 `unknown`**, one each way, exactly as here. It also
records that a pulse for a departure is not hypothetical: the relay fired for
five departing cars that week, on their rear plates.

## What was measured

**The photos.** Every event photo the dashboard still holds from 2026-09-01 to
2026-09-20: **929 frames in 231 passages** (630 on the RLC-810A, 299 on the
RLC-811A fitted 2026-09-11), fetched back with the controller's own service
token. Only the last 19 frames (events 3133–3151, no machinery among them, top
machinery share 0.001) follow the re-aim of 2026-09-20; everything that says
anything about machinery is old-aim. The image tower was run **on the Pi** — those are the numbers production
will produce — and the shipped policy was then replayed over the embeddings in
capture order. (The Mac agrees to a mean of 0.001 in the machinery share, with
one frame of 542 differing by 0.18 and crossing the threshold. Quantised
kernels differ between the two; calibrate on the Pi.)

Six passages contain machinery; every frame that scored as machinery was
looked at, and so was every would-admit.

| Passage | What it is | Frames clear | Would admit |
| --- | --- | --- | --- |
| 09-01 18:47 | red tractor with a mower, **leaving** (640×360, one frame) | 1 of 1 (0.776) | **no** — `leaving`, rear 0.58 |
| 09-07 16:59 | Case tractor with a loader, **arriving**, in view 57 s | 7 of 8 (0.822–0.993) | **yes**, 1.3 s after first sight |
| 09-08 16:32 | red tractor with a mower, **leaving** | 4 of 5 (0.992–1.000) | **no** — never still (≤ 0.922) |
| 09-08 18:03 | the same tractor **arriving**, stopped well back from the gate, in view 37 s | 5 of 9 (0.781–1.000) | **yes**, 4.9 s after first sight |
| 09-19 14:55 | yellow telehandler **arriving** | 7 of 7 (all ≥ 0.9999) | **yes**, 4.0 s after first sight |
| 09-19 14:58 | the telehandler **leaving** | 2 of 8 (0.986, 1.000) | **no** — no second look at it |

**Would-admit frames: 6 of 929 — events 2507, 2509, 2635, 3064, 3065, 3066.
True positives 6, false positives 0**, all six looked at. (3065 and 3066 would
have met the relay cooldown.) Arrivals admitted 3 of 3; departures admitted 0
of 3.

**The other 890 frames, 225 passages, none admitted.** The highest machinery
share on any of them:

| Frame | What it is | machinery | strongest class | margin |
| --- | --- | --- | --- | --- |
| 2473 | a corrupted frame (green) | 0.164 | car 0.688 | −0.524 |
| 2451 | a corrupted frame (green) | 0.103 | car 0.808 | −0.705 |
| 2253 | Isuzu D-Max pickup, side-on at the gate | 0.072 | pickup 0.757 | −0.685 |
| 3021 | pickup and livestock trailer, close | 0.005 | trailer 0.813 | −0.808 |
| 2553 | jeep and horse box passing the lens | 0.003 | lorry 0.957 | −0.955 |
| 2530 / 2605 / 2531 / 2606 | jeep and horse box on the lane | ≤ 0.001 | lorry / trailer 0.57–0.91 | ≤ −0.567 |
| 2555 / 2556 / 2558 | DPD Sprinter van | 0.000 | van 0.87–1.00 | ≤ −0.867 |

Three of the 890 reach 0.05 and twenty reach 0.01. The 86 frames taken between
21:00 and 05:00 UTC top out at 0.103. **Nothing that was not a machine came
within 0.58 of the threshold**, and everything between 0.17 and 0.75 *is* a
machine, seen from further off.

**What the threshold costs.** Zero of 225 other passages is a 95% upper bound
of about 1.3% per passage, not zero. On these photos any `min_machine` above
0.17 gives the same zero false positives, so the choice is about missed
tractors:

| `min_machine` / `min_margin` | Arrivals admitted | First admit after first sight |
| --- | --- | --- |
| 0.90 / 0.80 | 2 of 3 — misses 09-08, which waited too far back to score 0.9 | 5.0 s, —, 4.0 s |
| **0.75 / 0.60** (shipped) | **3 of 3** | 1.3 s, 4.9 s, 4.0 s |
| 0.60 / 0.40 | 3 of 3 | 1.3 s, 3.3 s, 4.0 s |

Whatever the threshold, the standing-still rule costs the wait: nothing is
admitted on first sight, so a machine waits at least a second and in practice
the four or five it takes the next frame to arrive. A machine that stops too
far back to score 0.75 (frames 2632 and 2633, at 0.42 and 0.61) is not
admitted until it pulls forward.

**What has not been measured.** **The current camera aim** — see the note at
the top. The model reads the *centre square* of the frame (the crop
`direction_vision` measured; a letterbox shrinks the vehicle until the model
stops seeing it), so on a 16:9 frame the outer ~22% on each side is never
looked at. On the old aim machines stood inside it. On the new aim, frame 3140
shows a car waiting at the far left edge that the model called `empty`; whether
a tractor stops inside the square on this aim is exactly what shadow has to
show. Night: there is not one photo of a machine
after dark, which is why the hours default to #63's window and why
`outside_hours` is still assessed and recorded. Rain, low sun, and any machine
other than these three. A lorry: `lorry` was the top class on three frames, all
of a horse box. Real tractors that are not the household's — the policy admits
*any* machinery, by design.

**What it costs the Pi.** 139 ms median, 152 ms p95, per frame, one thread,
measured on the Pi over all 929 photos at `nice 19` (8 ms decode and crop,
106 ms in the tower, the rest reading the file and handing the embedding
over; scoring it against the prompts, in plain Python, adds 1.3 ms). A standalone
process holding the model peaks at 182 MB; the controller is at 180 MB today
under a `MemoryMax` of 512 MB on the Pi's unit.

## How many frames it reads

One burst from the sweep is one frame, so one reading. A camera alarm puts a
bounded number of frames into the pipeline (`trigger_capture.py`):

| | shipped | configuration ceiling |
| --- | --- | --- |
| cloud handovers (`GATE_LOCAL_SWEEP_CLOUD_FRAMES`) | 5 | 10 |
| authorised injections that then failed to decide (`MAX_SWEEP_AUTHORISED_INJECTIONS`) | 3 | 3 |
| fallback frames when the window closes | 1 | 3 |
| **undecided frames, so readings, per alarm — worst case** | **9** | **16** |

The sweep's waiting phase (#175: up to 30 s at one read a second) reads on the
device and injects nothing beyond those caps — its handovers share the same
five, and while waiting only a frame with a plate in it is handed over. A
machine with no plate is the measured case: five blind handovers and a
fallback, **six readings an alarm, which is what the telehandler produced on
2026-09-19 before #175 as well**. Nine readings is 1.25 s of one core spread
over a 40 s sweep (139 ms each), beside a sweep that itself uses about 90% of a
core for its first 10 s. An authorised injection that *does* decide is never
read at all.

Two things bound it regardless. Readings are strictly one at a time — a burst
arriving while one runs is answered `busy`, never queued — and no more than
`MAX_READINGS_PER_MINUTE` = 30 are begun in any rolling minute (4.2 s of one
core in sixty); past that a burst is answered `rate_limited` and the plate path
carries on alone. Thirty is three worst-case alarms a minute as shipped. A
camera's own FTP still, which can carry two frames, costs two.

## Where it sits, and what it costs a plate

Production reaches `GateProcessor` through the worker's fast lane:
`prepare` on the burst thread, then `process` there or on the cloud lane.

* `prepare` takes the on-device plate read — the read the sweep carried in with
  the frame (#175), adopted first and judged exactly as #175 judges it, or
  else the ordinary local pass. **A frame either one decides is never shown to
  the model** — a known plate the device reads opens exactly as it did. A
  carried read that does *not* decide (unlisted plate, under the bar, refused
  for a digest that is not this file's) leaves an undecided frame like any
  other, and the sweep's own `authorised` flag is consulted by nothing here.
* For an undecided frame `prepare` *begins* the reading on its own thread and
  returns. In `shadow` no plate decision waits for it: `process` collects it
  after the plate path has finished with the burst, by which time — a cloud
  call later — it is done. It competes for one of four cores for ~140 ms. The
  one wait there is: a burst decided on the burst thread *without* a cloud call
  (device saw no plate, frame still moving) holds that thread until its own
  reading is in, so the next frame's on-device read can start up to ~140 ms
  late. The sweep capture the Pi runs today does not produce such bursts.
* In `on` the answer decides where the burst goes, so `prepare` waits for it —
  the ~140 ms above, bounded at 0.6 s — for undecided frames only. A burst it
  admits goes straight to the relay: no plate is looked for and **no cloud
  lookup is bought** for a vehicle that carries none (the telehandler's two
  passages handed ten frames to the cloud lane, and eleven of its fifteen
  events ended `decision_timeout`). A plate the cloud reads is therefore
  decided ~140 ms later than today.
* A burst with no fast lane ahead of it (the FTP path) is assessed in `process`
  after the plate path has declined, within what is left of the decision
  budget, and never if that is under 150 ms.
* A reading still running when the next burst arrives is answered `busy`, not
  queued. A wedged model holds a thread, not a lane.

An appearance grant clears the same bars as any other: fresh frame, processor
open, decision deadline, one actuation call, and the **automatic** relay
cooldown — `GATE_ACTUATION_COOLDOWN_SECONDS`, 90 s from the last pulse by any
source, because `appearance` is not a person's command. The one check it
skips is "is the matched plate still authorised" — it matched none.

## What the access log is told

| Field | Value |
| --- | --- |
| `source` | `appearance` |
| `reason` | `farm_machinery` |
| `opened` | `true` |
| `authorised_plate` | `null` — always |
| `observed_plate`, `ocr_confidence` | what a plate reader saw, if one ran and saw anything; else `null` |
| `telemetry.decision` | `{outcome: "allowed", reason: "farm_machinery"}` |

Both tokens pass the ingest contract as it stands (`EVENT_TOKEN`), and D1
stores `event_source` and `reason` as unconstrained text, so nothing is
rejected and no outbox sticks. The scores do **not** go on the wire: the
contract's `TELEMETRY_KEYS` is an allow-list and an unknown block is a 400 the
outbox retries forever. They stay in `event_appearance`.

Until the app is taught the tokens it shows "Opened" (the default arm of
`describeOpener`) rather than "Plate on the list", sends "Access granted — A
vehicle was seen at the gate", and drops `appearance` from `log.source`
(`eventSource()` maps unknown values to null). To render "Farm machinery —
admitted" it needs `appearance` in `EVENT_SOURCES`
(`src/lib/accessLogQuery.ts`), a `case 'appearance'` in `describeOpener`
(`src/lib/accessPassages.ts`), and a `farm_machinery` entry in
`REASON_SENTENCE` (`src/lib/passage.ts`).

In `shadow` the app is told nothing new at all.

## Reading a shadow run

```sql
SELECT e.received_at, e.reason, a.verdict, a.would_admit, a.machine, a.margin,
       a.rival, a.direction, json_extract(a.detail, '$.stillness') AS stillness
FROM event_appearance a JOIN events e ON e.id = a.event_id
WHERE a.verdict IN ('machine_clear', 'not_standing_still', 'leaving', 'outside_hours')
   OR a.machine >= 0.05
ORDER BY e.received_at;
```

Every `would_admit = 1` wants its photo looked at. `machine`, `margin`,
`rival` and `direction` are the weakest frame's. `gate_agri` in the journal
carries the same line per burst, with `elapsed_ms`; `verdict=timeout`, `busy`,
`rate_limited` and `no_budget` are the ones that say the Pi is short of time.

The camera has been re-aimed since these photos were taken (2026-09-20), which
changes what every frame looks like. What to look for in the shadow run, before
`on`: machinery arriving scores `machine_clear` and reaches `would_admit = 1`
once it has stopped; machinery leaving never does; nothing else comes near
`machine >= 0.75`; and `verdict` is rarely `timeout`, `busy` or `rate_limited`.
