# Gate Audio: What Is Built, What Is Measured, What Is Proposed

The camera has a microphone, and the gate it watches is a loud, repeatable
machine a few metres from it. This document records what the controller does
with that today, what the sound actually looks like when measured, and the
design that measurement makes possible.

Three things are deliberately kept apart here, because conflating them is how
a prototype gets mistaken for a system:

1. **Built and running** — the continuous recorder, keeping every segment, and
   the window extractor.
2. **Measured** — four commanded gate cycles, one overnight recording, and a
   pretrained-embedding probe. Real numbers, including the ones that killed
   the first approach.
3. **Proposed** — the gate state machine and occupancy counting. Not built.

## 1. What Is Built

### The recorder

`gate_controller.audio_segments` writes the clear stream's audio to 30-minute
clock-aligned segments and keeps them for 48 hours. It never decodes anything:
`-vn -c:a copy` drops the video before any packet reaches a decoder and remuxes
the camera's own AAC untouched.

```
GATE_AUDIO_SEGMENTS_ENABLED=true
GATE_AUDIO_SEGMENTS_DIR=/var/lib/gate-controller/audio-segments
GATE_AUDIO_SEGMENTS_SOURCE=rtsp://127.0.0.1:8554/clear
GATE_AUDIO_SEGMENTS_SECONDS=300
GATE_AUDIO_SEGMENTS_RETENTION_HOURS=48
GATE_AUDIO_SEGMENTS_KEEP_EVERYTHING=true
GATE_AUDIO_SEGMENTS_MIN_FREE_BYTES=1610612736
```

The filename is the index: `gate-20260916T140836Z.aac` is the segment's start
instant in UTC, so the file covering a moment is found by arithmetic rather
than a manifest that could disagree with what is on the card. The child is
given `TZ=UTC` explicitly, or `-strftime` would name files in host local time
and be an hour out for half the year.

Segments are **five minutes**, and that number is set by the corpus contract
rather than by taste: an artefact payload is capped at 4 MiB and a half-hour
segment is 13.9 MiB.

Cost, measured on the live board rather than estimated: **8.1 KB/s**, so
665 MiB a day and 1.3 GiB at the 48-hour horizon.

### Keeping everything

With `GATE_AUDIO_SEGMENTS_KEEP_EVERYTHING`, every *finished* segment is given a
sidecar, which is the whole mechanism: `TrainingCorpus.pending` offers only
complete pairs, so a segment without one is invisible to the uploader that
already exists. Nothing moves, copies or locks. The segment ffmpeg still has
open is deliberately skipped.

This exists because a threshold rule written from four cycles in one afternoon
broke on the first different condition it met. What the corpus needs is
weather, tractors, road traffic and night — none of which produce a camera
event to cut a window around, and all of which the 48-hour horizon was quietly
discarding.

Pruning a segment that still carries its sidecar means it was queued and never
shipped, which is permanent loss of audio. Filling the card would be worse, so
it still goes — but it is journalled as `stage=pruned_unshipped` and never
happens quietly. For comparison the journal
on the same card writes about 550 MB/day.

Three bounds keep it from ever being the reason something else fails:

* The **pruner runs on its own timer**, not as a step of the extraction job, so
  a job that stops running cannot fill the card.
* A **free-space floor** (1.5 GiB by default) takes the oldest segments first
  and then stops recording altogether, journalling why. The database, the
  evidence images and the corpus buffer all outrank audio.
* **Supervised restart with backoff**, so a stream that refuses every
  connection cannot become a spin.

### Why continuous, and why this is not new

`gate_controller.audio_capture` — the earlier design, still present — opens an
RTSP connection when the relay fires and records 40 s. Every clip therefore
begins *after* the vehicle arrived and after the motor started, and a passage
that produced no actuation leaves no recording at all.

Measured over 30 days at this site: **296 passages, of which 46 fired the
relay**. The other 250 were unrecordable by construction. The per-event
recorder has produced two clips in its lifetime, both starting at the relay,
and no negatives at all.

The continuous approach is also not a new pattern here. `ClearStreamSource`
already keeps a rolling in-memory ring of the *compressed video* and defers
decoding until a camera event asks for a frame. This is the same trick for
audio — and the audio was available to that path all along, since
`record_command` carries `-an` on a connection to this very stream that is
open every second of every day.

Segments on disk rather than a ring in memory because nothing needs the audio
*now*: the analysis is retrospective by definition, and a ring would lose
everything on restart.

### The window extractor

`gate_controller.audio_windows`, run from
`scripts/extract_audio_windows.py` on a timer, reads the controller's own
`events` table read-only, cuts a window around each passage out of the
segments, and writes it into the training corpus as `.aac` plus a `.json`
sidecar.

Nothing downstream changes: `.aac` is already `kind=audio` in the uploader's
media-type table, so the existing corpus uploader ships windows to R2 exactly
as it ships frames.

The default window is **−60 s to +180 s** around the event. The pre-roll is the
whole point — the vehicle and the motor both start before the relay fires.
Overlapping windows merge into one cut, because duplicated audio is not
neutral: it inflates whichever class happens to arrive in bursts.

Labels come free from columns the pipeline already writes:

| Column | What it labels |
| --- | --- |
| `relay_activated_at` | The instant the gate was *commanded* to move |
| `opened`, `actuation_outcome` | Whether it moved at all |
| `source`, `reason` | What noticed the vehicle |
| `observed_plate` | The same vehicle recurring |

## 2. What Is Measured

Four cycles were commanded through the controller's own command path on
2026-09-16 between 14:13 and 14:23 UTC (events 2927–2930), so each actuation
labelled its own recording.

### The bands do not overlap

| Sound | Energy |
| --- | --- |
| **Motor** | 50–90% in **1.2–3 kHz**, sustained for the whole travel |
| **Clang** (leaves meeting) | One transient, **67–79% above 3 kHz**, peak −22 to −26 dBFS |
| **Wind** — the only real distractor at this site | 70–99% **below 150 Hz**, HF share under 5% |

Ambient with nothing happening, measured separately: median −67.2 dBFS, and
3–8 kHz carries only 3% of the energy. The band the clang lands in is
essentially empty the rest of the time.

### Timings

| Event | Opening motor | Closing motor | Clang | 3–8 kHz share | Peak |
| --- | --- | --- | --- | --- | --- |
| 2927 | +1 → +16 s | +40 → +65 s | +64.4 s | 76.1% | −26.4 dB |
| 2928 | +2 → +23 s | +39 → +66 s | +65.4 s | 78.5% | −21.9 dB |
| 2929 | +0 → +25 s | +39 → +65 s | +64.0 s | 74.0% | −26.3 dB |
| 2930 | +2 → +23 s | +40 → +67 s | +65.4 s | 67.5% | −23.5 dB |

All times relative to the relay firing. The clang lands within a
**1.5-second window** across all four. The gate holds open about 17–23 s
before closing itself.

### The detector this implies

Written from the first cycle, correct on all four. Arithmetic on a 32 ms
frame — no model, no training set, nothing to keep current:

```
hf    = energy(1200..8000) / total        # per 1024-sample frame @ 16 kHz
vhigh = energy(3000..8000) / total

MOTOR   hf >= 0.40 sustained >= 3 s
CLANG   vhigh >= 0.25 and peak >= -38 dBFS
WIND    > 70% below 150 Hz, hf < 0.05
```

**This is why the training-set estimate collapsed.** An earlier estimate of
"100 examples, about seven weeks" assumed a learned classifier. The separation
does not need one. What the corpus is still for is *validating thresholds*
across night, rain and wind — 20–30 further cycles — not training.

### What is not measured

Everything below is currently unknown and should not be assumed:

* **Vehicle sound.** Untested. A vehicle is quieter than the gate and competes
  with the same sub-150 Hz wind. Expect it to be materially harder.
* **Night, rain, wind.** All four cycles were within ten minutes of each other,
  in daylight, in the same weather.
* **A cycle with a vehicle in it.** These were clean cycles with nothing
  passing through the gate.
* **A gate that fails.** No jam, stall or obstruction has ever been recorded,
  so the acoustic signature of a failure is unknown.

### The detector, as built

`gate_controller.gate_audio_detect` implements the rules above and turns motor
runs into gate movements. Two things it learned from being run over the real
recordings, both of which an offline threshold check had missed:

**A clang is not by itself proof the gate shut.** The leaves meeting is
metallic, but so is the gate reaching its *open* end-stop. On event 2927 the
opening run carried a clang 3.5 s before its motor stopped; the four measured
closing clangs land 1.1–2.5 s before theirs. Those ranges overlap, so timing
alone cannot separate them. What separates them is that a gate alternates —
**only a run that is closing can end shut** — so the detector tracks state and
interprets identical audio differently depending on where the gate was.

**Only an opening can be uncommanded.** The auto-close is never commanded by
anybody. An earlier version charged every closing run to "somebody used a fob",
which made the flag fire on all four *commanded* cycles.

Validated against the real recordings, event 2927 (the one cycle with no
neighbouring cycle overlapping its window) reads exactly right:

```
  +0.3 ->  +24.2s (23.9s) open          <- opening travel; end-stop clang ignored
 +34.6 ->  +66.1s (31.5s) shut   clang +63.6s 63%
 final_state=shut
```

A closing run that ends with **no** clang is reported as `open`, not as shut:
the gate is stuck, was reversed, or the recorder missed it, and the safe
reading is the one that does not claim the property is secured.

## 2b. Why The Rule Was Abandoned

The rule above was written from four cycles recorded within ten minutes of
each other, in one weather, with no vehicle present. Run against the first
overnight recording it reported a **629-second motor run** and called 62% of a
quiet half hour a gate moving.

The cause is that it uses band *share*, and share has a denominator. In
daylight, low-frequency wind dominated the total and held the ratio down.
Overnight that wind is gone, and the ratio saturates on whatever is left.

Two further objections are not fixable by tuning it:

* **Weather is not two conditions.** Ireland does not divide into "day" and
  "night"; it varies continuously, and a threshold set against two samples of
  it is set against nothing.
* **A tractor is a motor.** Agricultural traffic on the road, or a diesel
  idling at the gate waiting for it to open, is loud, sustained and harmonic
  in 1.2-3 kHz — which is exactly how the rule defines the gate. It has no
  defence against the single most likely thing to be happening at the moment
  it matters.

### What replaced it: pretrained embeddings

Measured on 2026-09-17. YAMNet — a MobileNet trained on AudioSet — was run
over the four cycles and the overnight audio with **no training at all**.

Its *class* outputs are weak here, which is expected: this audio is
band-limited to 16 kHz, quiet, and recorded at distance, where AudioSet is
mostly loud full-band YouTube. The gate motor reads as "Silence, Animal,
Snake". What is useful is the 1024-dimensional **embedding**, and a linear
classifier on top of it, trained on three cycles and tested on the fourth:

| Held-out cycle | Motor recall | False positives on night + ambient |
| --- | --- | --- |
| 2927 | 80.0% | 0.15% |
| 2928 | 100.0% | 0.29% |
| 2929 | 91.1% | 0.29% |
| 2930 | 82.0% | 0.44% |

Against the same overnight audio where the threshold rule flagged **62%** of
frames, the learned classifier flags **under half a percent** — on a cycle it
had never seen, from roughly 250 labelled positive frames.

Cost on the live board: **0.68% of one core** run continuously, about ten
core-minutes a day, using the `onnxruntime` already installed for the plate
reader. Decoding adds 0.04%.

That settles the approach. The remaining work is not a better threshold, it is
labelled variety — which is what keeping every segment now collects.

### Is a *vehicle* audible? Measured 2026-09-17

Everything above is about the gate. The question this answers is the other
one: can the microphone hear a vehicle, early enough and distinctly enough to
be worth acting on. Method: YAMNet run over **6.74 hours** of recorded segments
(2026-09-16 14:08 to 2026-09-17 04:23 UTC), no training, scoring every 0.48 s
frame against the fourteen AudioSet classes that describe a road vehicle.

**A vehicle passing the gate is unmistakable, and it lasts about thirteen
seconds.** The clearest example, 2026-09-16 14:57:

| Time | Level | `Vehicle` | `Motor vehicle (road)` | `Car` |
| --- | --- | --- | --- | --- |
| 14:57:22 | −50.2 dBFS | 0.00 | 0.00 | 0.00 |
| 14:57:24 | −44.6 | 0.05 | 0.01 | 0.01 |
| 14:57:26 | **−30.4** | 0.09 | 0.04 | 0.04 |
| 14:57:28 | −33.6 | **0.30** | **0.24** | **0.21** |
| 14:57:29 | −41.1 | 0.40 | 0.19 | 0.18 |
| 14:57:34 | −46.9 | 0.29 | 0.15 | 0.17 |
| 14:57:37 | −55.5 | 0.01 | 0.00 | 0.00 |

The level leaves the −50 dBFS floor about **six seconds** before closest
approach and the class score is up about **four**. The camera's detection zone
covers the last few metres of that approach; the sound covers all of it. There
is real lead time here, and it is not a rounding error.

**A single-frame threshold is useless, and duration is what fixes it.** The
class head fires weakly and often on nothing: 38 episodes over 0.15 in under
seven hours, most of them one 0.48 s frame at a level indistinguishable from
ambient. Requiring the score to *hold*:

| Score over | Held for | Episodes | Per day |
| --- | --- | --- | --- |
| 0.10 | 0.5 s | 67 | 239 |
| 0.10 | 2 s | 13 | 46 |
| **0.10** | **3 s** | **5** | **18** |
| 0.15 | 3 s | 1 | 3.6 |

Eighteen a day is the right order for a site that sees ten passages plus road
traffic. Four of those five episodes carry `Motor vehicle (road)` and `Car`
together — a coherent read, not a spike.

**It is not merely responding to loudness.** The ten loudest moments in the
whole recording — wind, birds, a voice, −7 to −10 dBFS, far louder than any
vehicle here — score 0.00 to 0.02. Level alone would have flagged every one.

**The gate motor reads as a vehicle, on single frames.** The four commanded
cycles peak at 0.35–0.57 on the vehicle family. None survives the three-second
gate, so duration separates them — but a detector that ignored this would call
every gate cycle a car.

**What is still unknown, and why.** Whether an episode is an arrival, a
departure or a pass-by is unmeasured, and it is the question that matters. The
site had five real passages in the recorded span — 17:42, 18:59, 19:15, 19:19
and 19:23 on 2026-09-16, two of which opened the gate — and **the audio for
every one of them had already been deleted** (see below). So the lead time
above is measured against a vehicle's own closest approach, not against a
camera event, and nothing here yet says which direction anything was going.

Finally, this is the *untrained* class head. On the gate motor the same head
was near-useless where a linear classifier on the embeddings reached 80–100%
recall at under 0.5% false positives. The vehicle numbers above should be read
as a floor, not a ceiling.

### The corpus was deleting the evidence

Found while gathering the audio for the experiment above. Of the 14.1 hours the
recorder had covered, **6.89 hours remained on the card**, and the gaps were not
outages: `TrainingCorpus.discard` deletes each segment the moment R2 confirms
it, by design -- "this is the step that turns the card from an archive into a
buffer".

The buffer is sound. The archive is not readable. The worker exposes
`POST /api/controller/corpus` and **no GET of any kind**, so an artefact in R2
cannot be listed or fetched back by anything. Every consumer of recorded audio
-- `extract_audio_windows`, `label_audio propose`, this experiment -- reads the
card, and the card is emptied within minutes of each upload. That is the
"Known limitation" above, and it is not a limitation of `propose`: it is the
whole corpus being write-only.

Two further faults in the same area:

* **78 `unshippable` refusals.** Segments written under the old 1800 s setting
  are 3.7–4.1 MB, over the 4 MiB payload cap, so they are refused, kept, and
  re-offered on every poll for ever.
* **Every controller restart loses the in-flight segment**, and the controller
  restarted eleven times between 01:05 and 03:28 on 2026-09-17. At the old
  30-minute setting that cost up to half an hour each time.

### The labelling loop

`gate_controller.audio_labels`, driven by `scripts/label_audio.py`. It exists
because the classifier above was trained on four spans typed into a dictionary
by hand, and when that session ended the labels went with it. Eight weeks of
recording without this is eight weeks of *unlabelled* audio.

```sh
# build a queue and cut the audio for each candidate
sudo python3 scripts/label_audio.py propose --detections detections.json

# say what one is
sudo python3 scripts/label_audio.py record <clip_id> gate_motor_opening --by ciaran

# what the corpus holds, and the set to train on
sudo python3 scripts/label_audio.py status
sudo python3 scripts/label_audio.py export --out training.json
```

Candidates come from three places in descending order of cost: the **relay**,
which labels its own actuations for free; a **detector** run, which proposes
and is often wrong; and a **person** who heard something. Only a person's
verdict clears a clip from the queue — the detector proposing it is why it is
there, so its own opinion cannot be what removes it.

Three decisions worth stating:

* **Append-only.** A label is an observation, not a setting. Somebody deciding
  in November that a September `gate_motor` was really a `tractor` is new
  information *about both*, and overwriting would destroy the evidence that
  the two are confusable — the most useful thing that disagreement could say.
  Latest verdict stands; a machine never overturns a person.
* **A fixed vocabulary.** Free text would fill with `gate`, `Gate`,
  `gate motor` and `motor?` inside a week. `tractor` is its own label because
  it is the confusion most likely to matter, and `nothing` is a label rather
  than an absence, because a confirmed negative is worth as much as a positive.
* **Human verdicts only, by default.** A model trained on its own detector's
  proposals learns to agree with itself, which is the failure this loop exists
  to prevent. Including the cheap sources has to be a deliberate argument.

**Known limitation:** clips are cut from segments still on the card. Once a
segment ships to R2 and is discarded, `propose` reports `no audio` for
anything inside it — four of eight actuations on the first real run. Either
propose often, or teach it to fetch from R2.

## 3. What Is Proposed

The detector above exists and is tested. What does **not** exist yet is the
path from it to anything a person sees: a scheduled job that runs it over each
day's segments, somewhere to persist the movements, and a dashboard that shows
them. Until that is built, the gate state below is computable but not
displayed anywhere.

### The gate state machine

The measurement supports a state machine the controller currently has no way
to populate. Its load-bearing insight is that **a motor run that ends without a
clang is the gate stopping open**:

| State | Entered when | What it means |
| --- | --- | --- |
| `opening` | Motor starts | If no relay command preceded it, somebody used a key fob |
| `open` | Motor stops, **no clang** | The gate is standing open; a vehicle is about to pass |
| `closing` | Motor runs again | Typically about 40 s after opening |
| `shut` | **Clang** | The leaves have met — positive confirmation the gate closed |

The last row is the one the system cannot assert today. The controller fires
the relay and then assumes the gate moved; if the motor failed, the relay stuck
or the gate jammed, the event log still reads `activated`.

### Fob openings become visible

At this site **84% of passages never fire the relay** — people use key fobs
because recognition is slow or wrong. Those openings are invisible to the
controller today. A motor detected with no preceding relay command is a fob,
a keypad, or a hand, and is worth recording as an event in its own right.

This also solves the corpus bootstrap: once the detector runs, roughly ten
gate cycles a day label themselves with no hand-labelling at all.

### Occupancy counting

Counting vehicles on site needs **direction**, not just presence, and direction
is the one part that genuinely needs a trained model. The vision-side estimate
in `direction.py` already runs in shadow mode and refuses to fit a series that
does not meet its gate, which makes it a usable source of free labels for
audio — but only when it is confident.

Estimated cost: several hundred examples per direction, so 6–8 weeks at the
measured 9.9 passages/day. This is the long pole and nothing above shortens it.

### Exit prediction

`open` with no clang, corroborated by vehicle sound and by the camera's own
detections, is a strong prior that a vehicle is about to exit. That is worth
having because the camera is aimed at the approach and reads arriving plates;
a vehicle leaving is currently only ever seen as a rear plate, motion-blurred,
outside the crop band.

## Operations

```sh
# Is the recorder running, and what has it got?
systemctl is-active gate-audio-segments.service
sudo du -sh /var/lib/gate-controller/audio-segments

# Cut yesterday's windows into the corpus
sudo python3 scripts/extract_audio_windows.py --hours 24

# See what it would cut without writing anything
sudo python3 scripts/extract_audio_windows.py --hours 24 --dry-run
```

The recorder's counters appear in the heartbeat under `audio_segments`:
segment count, bytes, oldest and newest, restarts, low-disk refusals and free
space. A recorder that stopped four days ago must not look identical to a quiet
week — the same lesson the trigger-capture counters were added for.
