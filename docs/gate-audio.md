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

## Where each part runs

Three machines, and each does the one thing it is suited to.

```
   the gate                      Cloudflare                   a laptop
   ---------                     ----------                   --------
   record the sound      ---->   keep all of it       <----   fetch a day of it
   run the finished              (R2, the archive)            try things out
   detector, live                                             train a model
        |                                                          |
        +---------------- deploy the finished model <--------------+
                              (a few kilobytes)
```

**The gate controller records and detects. It does not store or learn.** It
keeps a short rolling window on the card -- enough to survive a broken
internet connection, nothing more -- and ships everything to R2. The only
model it runs is one that has already been shown to work.

**R2 keeps everything.** Storage is cheap and the recording is 8 KB/s, so a
year of continuous audio is about 250 GB and no decision has to be made in
advance about which hour might turn out to matter.

**The laptop does the thinking.** Fetching a day of audio, listening to it,
labelling it, training a classifier and testing it against a season of weather
are all things to do somewhere with a screen, a fast disk and no gate
depending on it.

**What gets deployed is tiny.** The gate-motor classifier trained above is a
list of 1024 numbers: 4 KB. Deploying it is copying a file. The heavy part --
YAMNet, which turns sound into those numbers -- is 16 MB and never changes.

This is why the archive needed a way to be read (see below): without it the
laptop had nothing to work from, and the only copy of anything was a rolling
window on an SD card in a cabinet.

## 1. What Is Built

### The recorder

`gate_controller.audio_segments` writes the clear stream's audio to 30-minute
clock-aligned segments and keeps them for 48 hours. It never decodes anything:
`-vn -c:a copy` drops the video before any packet reaches a decoder and remuxes
the camera's own AAC untouched.

```
GATE_AUDIO_SEGMENTS_ENABLED=true
GATE_AUDIO_SEGMENTS_DIR=/var/lib/gate-controller/audio-segments
GATE_AUDIO_SEGMENTS_SOURCE=rtsp://127.0.0.1:8554/camera   # see 2g: not `clear`
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

## 2c. The Detector, Running

Built 2026-09-17. `gate_controller.gate_sound_scan`, driven by
`scripts/scan_gate_sound.py` on a quarter-hourly timer, reads finished
segments and writes what the gate did into `gate_movements`.

Nothing here is in the path of opening a gate. It runs after the fact, over
recordings, niced to the back of the queue, and if it never runs at all
recognition is unaffected.

### What it found on the first real scan

Forty-eight segments, four hours of recorded audio:

| | |
| --- | --- |
| Movements | 13 |
| Ending shut (a clang) | 5 |
| Ending open | 8 |
| **Openings nobody commanded** | **1** |

The four commanded cycles are all there with their travel times -- 24.0 s,
25.4 s, 28.3 s, 32.2 s against a measured 15-27 s -- and the clangs land at
-21.9 to -28.1 dBFS with 54-79% of their energy above 3 kHz, which is the band
profile measured by hand in section 2.

The uncommanded opening is **2026-09-17 11:47:16, twelve seconds**, with no
relay firing anywhere near it. Somebody used a fob or the keypad, and until
this table nothing in the system would ever have known.

### Two numbers that had to be measured, not guessed

**The minimum run is eight seconds.** At three it found 88 openings against 7
closings over the same audio -- and a gate that opens 88 times shuts 88 times,
so eighty of those were transients the classifier fired on for a frame or two.
Eight is well under the shortest real travel and well over anything that is
not the motor; the count fell to 13, and open and shut came into balance.

**A relay firing claims a run from 12 s before it to 75 s after.** Late,
because the auto-close follows about forty seconds behind. Early, because the
detector hears the motor start *before* the relay row is written: at 11:08 the
run was timed 3.2 s ahead of the firing. Erring wide is the safe direction --
a commanded opening mistaken for a fob inflates the one number this exists to
produce, and the opposite error only understates it.

### What it costs

Fifty-nine seconds for twenty minutes of audio, measured on the board. It was
six and a half minutes before the band analysis was restricted to the few
seconds around each motor run: YAMNet is a MobileNet and cheap, but
`analyse_frames` is a pure-Python FFT over 9,375 windows per segment. A clang
is only ever *interpreted* beside the end of a motor run, so that is the only
place it is looked for now.

### What is not yet true of it

* **A vehicle's engine reads much like the motor.** It is what put the 11:08
  run 3.2 s early, and it is unresolved: separating them needs labelled engine
  audio, which the retention fix has only just started collecting.
* **The alternation is only as good as its first run.** A movement means
  "shut" because the gate was open, and a false positive early in a scan
  inverts every outcome after it.
* **One travel can still be reported as two.** A dropout longer than 1.5 s
  splits a run; the 14:20 closing came out as 15.4 s plus 9.6 s.
* **The gate-motor classifier was trained on four cycles** from one
  afternoon. Replaced 2026-09-21 by one trained on 49.2 hours — see section
  2d, including what that retrain still does not cover.
* **The recorder drops five percent of the wall clock**, in gaps of up to
  three minutes, from repeated restarts against the clear stream. It is the
  largest single cause of a gate movement going unrecorded and nothing in
  this document's detectors can compensate for it.

### Installing it on a board

The classifier ships in this repository as JSON. YAMNet does not -- it is
16 MB and never changes:

```sh
sudo install -o gate-controller -g gate-controller -m 644 \
  yamnet.onnx /var/lib/gate-controller/models/yamnet.onnx
sudo systemctl enable --now gate-sound-scan.timer
```

Without it the detector reports itself unavailable and does nothing, which is
the correct behaviour for an analysis model on a gate controller.

## 2d. The Retrain, And What 49 Hours Of Recording Said

Measured 2026-09-21 over everything the recorder had kept: **662 segments,
49.2 hours of audio inside 51.5 hours of wall clock**, from 2026-09-18 22:50
to 2026-09-21 02:20 UTC. All analysis was done off the board; the Pi was only
read.

### What the corpus actually contains

| | |
| --- | --- |
| Recorded audio | 49.2 h (95.5% of the wall clock it spans) |
| Full days | **two** — 2026-09-19 (22.1 h) and 2026-09-20 (23.7 h) |
| Part days | 2026-09-18 (1.2 h), 2026-09-21 (2.3 h) |
| Relay-commanded cycles inside it | **ten** |
| Human labels in existence | **none** |

Conditions, read off the audio itself because the site has no weather station
— a minute is `impulsive` when a loud frame occurs more than five percent of
the time, which is rain on the housing, and `windy` when the sub-150 Hz band
carries it:

| Condition | Minutes |
| --- | --- |
| Quiet daylight | 1317 |
| Quiet night | 987 |
| Impulsive (rain) | 447 |
| Windy | 218 |
| Loud | 8 |

Plus one unplanned gift: **about forty-five minutes of farm machinery** on
2026-09-20 between 15:00 and 15:45, a sustained low harmonic drone. That is
the tractor case this document predicted and had never recorded.

**The recorder loses audio, and it is the largest single failure found here.**
121 gaps over one second, 71 over twenty, 57 over a minute, totalling 2.4
hours — five percent of the wall clock. The journal shows the segment recorder
restarting 46 times over the two days, mostly `method DESCRIBE failed: 404`
against the clear stream, and each restart loses the segment in flight. One
commanded cycle on 2026-09-20 at 10:47 had **97 consecutive seconds missing**,
covering the whole of its auto-close. No detector can be measured on audio
that does not exist, and no threshold can recover it.

### How the labels were made, with no labels to start from

The labelling loop in section 2b has never been run: the store is empty. So
ground truth was built out of signals the motor model does not produce.

* **Positives** — motor runs bracketed by a relay firing. 27 runs, 1203
  frames, 577 seconds of motor, over two days.
* **Negatives** — runs with no relay firing, no latch and no camera event
  within four minutes either side. 81 runs, 2935 frames.
* **Easy negatives** — 120,000 frames sampled from stretches with no evidence
  of any kind, across every condition above.

**Thirty-six of each were rendered as spectrograms and looked at.**

| | Sampled | Confirmed by eye | Overturned |
| --- | --- | --- | --- |
| Claimed negatives | 36 | 36 | **0** |
| Claimed positives | 36 | 24 | **12** |

Every claimed negative was wind swell, a road vehicle, rain or the farm
machinery. The overturned positives are the useful result: broken out by what
corroborated them,

| Corroborated by | n | Really the gate |
| --- | --- | --- |
| A relay firing | 19 | 17 (89%) |
| A latch and nothing else | 17 | **7 (41%)** |

so **latch corroboration alone is not good enough to train on** — it would
have taught the model that rain is the motor. Only relay-corroborated runs
were used as positives.

### The numbers, by held-out day

Leave-one-*day*-out, as issue #155 requires. The held-out day contributes
nothing to training — no positives, no negatives, no threshold choice. The
decision threshold stays at the shipped 0.5; it was not tuned.

| Held-out day | | 2026-09-19 | 2026-09-20 |
| --- | --- | --- | --- |
| Recorded audio | | 22.1 h | 23.7 h |
| Motor runs per day | v1 | 79.4 | 107.5 |
| | **v2** | **25.0** | **19.3** |
| Uncorroborated runs per day | v1 | 25.0 | 58.8 |
| | **v2** | **0.0** | **0.0** |
| Relay openings found | v1 | 4/4 | 6/6 |
| | **v2** | 4/4 | **5/6** |
| Runs confirmed by eye, found | v1 | 8/8 | 15/15 |
| | **v2** | 8/8 | **13/15** |
| Movements reading `shut` | v1 | 13% | 10% |
| | **v2** | **39%** | **37%** |

**This is a real improvement and a real cost.** The false positives go to
zero on both held-out days; two of fifteen confirmed movements and one of six
commanded openings are lost on 2026-09-20. The shut fraction — a gate that
opens n times shuts n times, so a healthy detector should approach 50% —
roughly triples.

### What this result is not

* **It is two days.** Issue #155 asks for a week, and this is not one. The
  positive class is 27 runs.
* **No snow, no jam, no stall, no tractor driven through the gate.** The
  machinery on 2026-09-20 was nearby, not passing through.
* **The confirmation split is not independent of the training signal.**
  "Uncorroborated" is both what the negatives were selected by and what the
  held-out day is scored on. The recall figures — relay openings, and runs
  confirmed by a person looking at a spectrogram — are not, and they are the
  ones that carry the cost side of the trade.
* At a threshold of 0.35 rather than 0.5, 2026-09-20 keeps 14 of 15 confirmed
  runs at the same zero uncorroborated runs. That was measured **on the
  held-out day**, so it is not a number to ship on; it is a thing to check on
  the next retrain, with more days.

### `MIN_RUN_SECONDS` was left alone

Raising the eight-second floor is the obvious way to cut the count and it is
wrong. Of the 54 clang-confirmed closings recorded to date, **22 are shorter
than twelve seconds** — 41% — so a twelve-second floor would discard two
fifths of the closings the system can actually confirm. The inflation was
never in the duration rule.

## 2e. The Latch: Why Closings Looked Unreliable

The complaint that the gate's shutting is not detected reliably is right
about the effect and wrong about the cause. Measured on the ten commanded
cycles in the
retained audio — the only closings whose existence needs no detector, because
the auto-close is not optional:

| | |
| --- | --- |
| Commanded cycles | 10 |
| Auto-close window actually recorded | 9 |
| **Latch found by the band rule** | **8** |
| Recall on recordable closings | **8/9 = 89%** |

All eight were confirmed by eye: the motor band stops, one bright broadband
transient follows, often with one or two smaller ones behind it as the leaves
settle. The two misses are **one lost to a 97-second recording gap** and
**one masked by wind** (2026-09-19 10:30, where the window was itself only
60% recorded).

**So the latch detector is not the problem.** What made closings look
unreliable is the denominator: with 107 movements a day and 11 of them
reading `shut`, the dashboard showed a gate that apparently moved a hundred
times and closed eleven. Removing the false movements moved that ratio from
10-13% to 37-39% without touching the latch rule at all.

Three things were measured and **not** changed, because the data refused them:

* **Lowering the −38 dBFS floor.** At −42 the raw candidate count over the
  same audio goes from 403 to 678 a day; at −46, to 1126. The floor is
  holding back a flood, not hiding latches.
* **Widening the ±4 s search window.** Widening it to ±8, ±12 and ±20 s
  changed *nothing at all* on either held-out day. The binding constraint is
  the alternation — a clang is only interpreted when the state machine
  already believes the gate is open — and not the window.
* **The camera's re-aiming.** The clang peak level has drifted down: median
  −24.9 dBFS on 2026-09-16, −30.2 and −30.3 on 19 and 20 September, with 17%
  of detected latches now within 3 dB of the floor. Real, worth watching, and
  with 54 latches over five days there is not enough to place a discontinuity
  at a timestamp. It is not currently causing misses.

### What did change: prominence

The rule tested `high_share` and `dbfs`, both of which describe a frame on
its own. A frame of continuous loud noise satisfies them as readily as an
impact. Measured against the two seconds either side of each candidate:

| Candidate | Local median | Peak | Prominence |
| --- | --- | --- | --- |
| Latch 09-19 08:28 | −51.5 | −24.2 | 27.3 dB |
| Latch 09-20 09:58 | −58.3 | −26.2 | 32.1 dB |
| Latch 09-20 18:16 | −36.5 | −15.0 | 21.5 dB |
| A bird, 09-20 12:29 | −6.4 | −5.4 | **1.0 dB** |
| A bird, 09-20 16:58 | −6.8 | −5.3 | **1.5 dB** |

A bird calling four times a second pins the microphone near −7 dBFS, and
every frame of it clears both tests. In those three windows the clustering
rule happened to return the real latch anyway, so this is a near miss being
closed rather than a reported error being corrected — which is exactly why it
is worth closing before the day it does not happen to work.
`CLANG_PROMINENCE_DB = 12.0` sits in the empty space between 3.5 and 21.5. Prominence is a *difference* of levels, so
unlike a band share it has no denominator to saturate — the fault that killed
the first motor rule.

Its measured effect is honest and modest: **75 of 795 raw candidates removed
over 49.2 hours, with all eight commanded latches kept.** It is shipped
because the failure it closes is demonstrated in this site's own audio, not
because it moved the headline number.

### The latch is now written down even when the alternation ignores it

`clang_at` is only ever populated for a run the state machine already thought
was a closing. One false movement early in a scan inverts every outcome after
it, and the latch went with them — the one physical observation in the chain
discarded because a model guessed wrong about something else. `latch_at` and
`latch_peak_dbfs` record the impact regardless, and `outcome` keeps exactly
its old meaning.

## 2f. Saying How Sure It Is

Every movement now carries a `confirmation`:

* **`confirmed`** — a relay firing brackets it, or a latch was heard beside
  its end. Something outside the motor model agrees.
* **`unconfirmed`** — only the classifier says so. It may be real; a fob
  opening fires no relay and a gate left standing open has no latch to hear.

The heartbeat's `gate` block gains `confirmed_24h`, `unconfirmed_24h`,
`latches_heard_24h` and `state_confirmation` beside the existing
`movements_24h`. Nothing was removed. **`movements_24h` is a ceiling, not a
count**, and a dashboard should be showing the confirmed figure with the rest
available behind it.

## 2g. Listening, And Not Listening

The complaint was that closings were not heard reliably. 2e found the latch
detector hears 8 of the 9 closings that *were recorded*. This section is about
the ones that were not, measured on the Pi on 2026-09-21 over every segment on
the card (627 of them, 2026-09-19 04:00 to 2026-09-21 04:00 UTC) against the
recorder's, MediaMTX's and systemd's journals for the same hours.

**45.73 h of audio in 48.08 h of wall clock: 8,483 s (4.9%) not heard.** The
journals cover the last 44.2 h of that, and within them the loss divides like
this:

| Cause | Events | Seconds lost | Of which avoidable here |
| --- | ---: | ---: | ---: |
| The camera delivered less audio than real time; recorder running throughout | 108 segments | 7,356 | 0 -- but see "the source" below |
| MediaMTX reset the 4K session (`buffer length exceeds 64`, `TCP timeout`, a garbled reply) | 24 | 619 | 457 (backoff) |
| The controller was restarted (deploys) | 11 | 247 | 229 (ffmpeg's buffer) + 18 |
| The camera rebooted (03:00 IST daily, and once at 08:50) | 2 | 240 | 113 (backoff) |
| MediaMTX restarted: the TURN credential refresh every 4 h (10), a deploy's media publish (2), undetermined (1) | 13 | 77 | 77 |
| Unexplained | 1 | 5 | 5 |
| Before the journal's horizon, unclassifiable | -- | 133 | -- |

So 86% of what was lost was lost with the recorder up and nothing in any
journal, and of the 1,188 s the recorder *was* down, the stream was genuinely
unavailable for only 289 s. The other 899 s were the recorder's own doing.

What was checked and found **not** to be a cause:

* **`clear` is not an on-demand path.** `pathDefaults.sourceOnDemand: false`;
  MediaMTX holds the camera session permanently. The `DESCRIBE failed: 404` in
  the recorder's journal is MediaMTX's "no stream is available on path" during
  the ~5 s it takes to re-dial a camera session it has just dropped. There
  were 7 such failed starts, in 5 of the 51 outages, each a retry that landed
  inside that window -- and each then cost the better part of a minute.
* **The per-alarm session decoder and the recorder do not kill each other.**
  Sessions open and are torn down by their own client throughout
  (`destroyed: torn down by 127.0.0.1:...`) with the recorder's session
  untouched. Every reader is terminated together only when the *source* goes.
* **Ordinary rotation loses nothing.** 444 clock-aligned boundaries with no
  restart near them: mean +0.041 s, under one 64 ms frame. The 1.0 s "gaps" and
  the -1 to -4 s "overlaps" in a naive reading are file names, which are whole
  seconds and are sometimes stamped `...1459Z` for the `15:00` boundary. Of 126
  apparent gaps over 1 s, 51 were restarts and the rest were this or the
  shortfall below.
* **LAN loss.** `gate_net_probe` reported `lan_loss_pct=0` and p95 under 5 ms
  through the worst hour.

### The shortfall: segments shorter than the time they cover

On 2026-09-20 from 10:45 to 11:40 UTC every five-minute segment held 153-208 s
of audio. The recorder did not restart (its journal is silent from 10:39:43 to
14:40:44), ffmpeg logged no error, MediaMTX logged *no* packet loss, and the
files start on the clock. This is the hour that contains the relay-commanded
cycle at 10:47 whose whole auto-close was missing: not a restart, as first
reported, but 141 s absent from a segment that was being written throughout.

Two things place the loss upstream of the recorder:

* The per-event clip recorder -- a different ffmpeg, on its own RTSP session --
  captured that same passage: `gate_audio_capture outcome=captured
  label=actuated bytes=137535 seconds=40.3` at 11:47:39 IST. Every other clip
  that day is 324,375-325,413 bytes for the same 40.3 s. It lost 58% of its
  audio; the segment beside it lost 47%.
* The Pi's receive rate from the LAN, steady at ~900 kB/s all day, fell to
  545-850 kB/s for exactly those minutes (`gate_net_probe rx_bytes_per_s`).
  Across 396 clean five-minute windows, 12 of the 25 short ones had a mean
  receive rate under 850 kB/s, against 2 of the 371 full ones.

The camera was sending less than it was encoding. On 2026-09-19 09:00-12:30 UTC
the same thing happened with MediaMTX counting it: 3,000-37,000 RTP packets an
hour lost *on a TCP session*, which can only be the camera discarding at its
own socket. That morning is 5,774 of the 7,356 s; the hour on 09-20 is 1,336.

**Where in a short segment the missing audio falls is not knowable** from an
ADTS file, which carries no timestamps. The scanner places every event at
`segment start + samples decoded`, so inside a short segment its times run
early by however much had been dropped before them. Nothing here corrects for
that; it is now at least flagged.

### The source: `camera`, not `clear`

MediaMTX holds two sessions to the camera: `clear` (4K main stream) and
`camera` (sub-stream). Over the 44 hours:

| | `clear` | `camera` |
| --- | ---: | ---: |
| RTP packets lost | 86,977 | 0 |
| Source resets (`buffer length exceeds 64`, garbled reply) | 29 | 0 |
| Resets shared by both (camera reboot, `TCP timeout`) | 5 | 5 |

They carry the same audio. Read side by side for eight seconds on 2026-09-21,
**126 of 126 AAC frames were byte-identical** (16 kHz, mono, AAC-LC, 65,394
bytes each): it is one encoder feeding two sessions, so the motor classifier
and the latch rule see exactly the bytes they were trained on.

Recording from `camera` would have avoided all 24 session resets and every
packet-loss hour on 09-19. **What it would have done on 09-20 is not known**:
MediaMTX counted no loss on either path that hour, so the sub-stream's
cleanliness then is inferred, not measured. This is a change to
`/etc/gate-controller.env` on the Pi and is *not* made by a release; see
Operations.

### What the recorder now does about the part that was its own

* **Retry follows what the child did, not how many children there have been.**
  The old delay was `5 s x (1 + restarts)` capped at 60, and `restarts` never
  went down: from the twelfth restart of a process's life, any child that had
  run under a minute waited a full minute. 57 gaps were over 60 s. Now a child
  that recorded and lost its stream is retried after 0.5 s; one that never
  wrote a byte backs off 1, 2, 5, 10, 30 s.
* **No ffmpeg is spawned to be told 404.** While MediaMTX answers DESCRIBE with
  "no stream", one loopback socket a second asks again
  (`stage=waiting reason=no_stream`). Recording resumes within ~2.5 s of the
  stream's return. Anything other than an outright 404 -- including no answer
  at all -- does not hold a spawn back.
* **Every frame is written as it arrives** (`-fflags +flush_packets`). ffmpeg's
  `file` protocol buffers 256 KiB, which is 32 s of this audio, and a child
  that is stopped loses it: every segment a deploy cut short was an exact
  multiple of 262,144 bytes ending mid-frame (262144, 524288, 1048576, 1572864,
  2097152). 229 s over 11 restarts, and the open segment is also now readable
  to the second rather than half a minute late. `-flush_packets 1` does *not*
  work here -- it is not passed to the muxer the segmenter opens. Both were
  tried on the Pi's ffmpeg 5.1.5: 0 bytes on the card after 6 s with it,
  48,267 with `-fflags`.
* **Only the audio is asked for** (`-allowed_media_types audio`). `-vn` discards
  video after MediaMTX has sent it; the recorder was pulling 900 kB/s of 4K to
  throw away. MediaMTX now reports `1 track (MPEG-4 Audio)` for this reader.
* **Release and pruning run on their own timer.** They ran only *between*
  children, so on the gate 49 segments were released and 49 pruned at once,
  every four hours, whenever the TURN refresh happened to restart MediaMTX --
  and a recorder that never restarted would never have pruned. They also ran
  before the first child was started; the child now comes first.
* **A stall guard.** A child that writes nothing for 60 s is stopped and
  replaced. None was seen in 44 hours. It is deliberately longer than the 32 s
  an unflushed ffmpeg goes between writes, so a build that ignored the flag
  would be slow to notice a stall rather than killed before its first write.

What is left is what cannot be avoided from here: ~5 s per MediaMTX restart
(six a day from the TURN refresh alone), ~70 s per camera reboot, and ~5 s of
process start per deploy.

### Not listening, as data

Silence in `gate_movements` has meant one thing until now: the gate did not
move. It now has to be read beside two tables the scanner fills in the same
transaction as the movements:

* `gate_listening` -- one row per scanned segment: the span it covers (to the
  next segment's start), the audio in it measured from its frames, and the
  difference.
* `gate_listening_gaps` -- `started_at`, `ended_at`, `missing_seconds`, `cause`,
  `detail`. The recorder writes its own gaps, with exact times, to
  `listening-gaps.jsonl` beside the segments (`service_restart`,
  `recorder_off`, `source_unavailable`, `stream_ended`, `stalled`, `low_disk`,
  `spawn_failed`); the scanner copies the ones inside the segments it has just
  read. Whatever a segment is still missing beyond those is a
  `stream_shortfall` row against the *whole span*, because that is all that is
  known about where it fell. For audio recorded before this release there is
  no ledger, so restarts from then appear as shortfall too.

The scanner journals each one as `gate_sound stage=not_listening from=... to=...
missing_seconds=... cause=...`, and the recorder journals
`gate_audio_segments stage=resumed not_listening_from=... to=... seconds=...
cause=...` when it comes back.

The heartbeat's `capabilities.gate` gains, additively:

| Field | Meaning |
| --- | --- |
| `listening_24h_seconds` | Audio actually on the card for segments started in the window |
| `not_listening_24h_seconds` | The span those segments cover, less that audio |
| `listening_gaps_24h` | Rows in `gate_listening_gaps` in the window |
| `longest_gap_24h_seconds` | The largest `missing_seconds` among them |
| `last_gap_at`, `last_gap_until`, `last_gap_cause` | The most recent one |

The first two add up to what has been *scanned*, not to 86,400, so a scanner
two hours old does not read as 92% deaf. All are absent until something has
been measured, which must render as unknown. The dashboard's ingest
(`narrowedGateCapabilities` in access-gate-ui) keeps only the numeric keys
named in its `GATE_CEILINGS` and drops the rest without rejecting the
heartbeat, so these are safe to send today and invisible there until that list
names them (suggested ceilings: 86,400 for the three durations, 10,000 for the
count).

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

**Moving the recorder to the sub-stream (2g).** Not done by a release: the Pi's
`/etc/gate-controller.env` is the operator's file and the updater never writes
it. `.env.example` carries the new value for a fresh install.

```sh
# change: GATE_AUDIO_SEGMENTS_SOURCE=rtsp://127.0.0.1:8554/clear  ->  .../camera
sudo sed -i.bak-audio-source \
  's#^GATE_AUDIO_SEGMENTS_SOURCE=rtsp://127.0.0.1:8554/clear$#GATE_AUDIO_SEGMENTS_SOURCE=rtsp://127.0.0.1:8554/camera#' \
  /etc/gate-controller.env
sudo systemctl restart file-monitor.service     # at a quiet moment: ~7 s not listening

# verify: the recorder names the new source, and MediaMTX serves it one track
sudo journalctl -u file-monitor.service -o cat --since -1min | grep 'stage=recording'
sudo journalctl -u gate-media-gateway.service -o cat --since -1min | grep "path 'camera'.*1 track"
# ...and after the next scan, the heartbeat's gate.not_listening_24h_seconds stops climbing

# roll back
sudo mv /etc/gate-controller.env.bak-audio-source /etc/gate-controller.env
sudo systemctl restart file-monitor.service
```

```sh
# Is the recorder running, and what has it got?
systemctl is-active gate-audio-segments.service
sudo du -sh /var/lib/gate-controller/audio-segments

# Pull a day of recorded audio down to this machine, from R2
export GATE_CLOUDFLARE_API_URL=https://gate-mate.example.workers.dev
export GATE_CLOUDFLARE_ACCESS_CLIENT_ID=...      # the controller's service token
export GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET=...
python3 scripts/fetch_corpus.py list  --kind audio --since 2026-09-16
python3 scripts/fetch_corpus.py fetch --kind audio --since 2026-09-16 --out ./audio

# Cut yesterday's windows into the corpus
sudo python3 scripts/extract_audio_windows.py --hours 24

# See what it would cut without writing anything
sudo python3 scripts/extract_audio_windows.py --hours 24 --dry-run
```

The recorder's counters appear in the heartbeat under `audio_segments`:
segment count, bytes, oldest and newest, restarts, low-disk refusals and free
space. A recorder that stopped four days ago must not look identical to a quiet
week — the same lesson the trigger-capture counters were added for.
