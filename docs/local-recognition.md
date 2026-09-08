# On-device plate recognition

Two small pretrained ONNX graphs read the plate on the Pi itself, from the
exact bytes the controller already uploads to Plate Recognizer. This document
covers what ships today: the modes, the environment variables, the journal
lines, how to read a week of agreement data, and what has to be true on the Pi
before any of it is switched on.

Everything here is **off unless `GATE_LOCAL_OCR_MODE` is set**. With it unset,
nothing is imported, nothing is loaded, no thread is started, and the
controller behaves exactly as it did before.

## The models, and why these ones

| role | model | package | licence | size |
| --- | --- | --- | --- | --- |
| detector | `yolo-v9-t-384-license-plate-end2end` | `open-image-models` 0.6.0 | MIT | ~4 MB ONNX |
| recogniser | `cct-xs-v2-global-model` | `fast-plate-ocr` 1.1.0 | MIT | 3.2 MB ONNX |

Both run on `onnxruntime` 1.29.0 with `CPUExecutionProvider`, batch 1.

The choice rests on two measurement passes, not on reputation:

* **Accuracy** (`gate-controller-data/baseline/report.md`, 2,527 images, 458
  labelled): 90.2% exact reads, 95.5% character accuracy, and **98.2% correct
  gate decisions with zero wrong-plate accepts** once the read is snapped to
  the authorised list. OCR confidence separates right from wrong cleanly:
  **0.998 mean on correct reads against 0.772 on wrong ones**, which is what
  makes a confidence threshold a usable gate rather than a guess. Thresholding
  at 0.95 keeps 415 of 458 reads at 98.1% exact. The model named "European"
  is the *worst* on Irish plates (59.4%); the v2 CCT "global" models win. Do
  not pick by name.
* **Cost on the board** (`gate-controller-data/baseline/pi_benchmark.md`, Pi 5,
  fanless): the 384 detector costs **154 ms mean / 177 ms p95 per frame on one
  core**, against 422 ms for the 640 variant, with no accuracy loss observed on
  the Pi's own frames. Both sessions load in ~135 ms. Peak RSS 133-237 MB.
  All 14 test frames read **byte-identically to the Mac** across four detector
  variants, so there is no arm64 numerical divergence to worry about.

Night is not addressed by any of this. The night problem is IR exposure on
retroreflective plates plus mostly-empty frames; a better model cannot read a
clipped white rectangle.

## The thermal constraint, which is the real constraint

The Pi 5 in this gate has **no fan**. It idles at 71-74 C and the working
ceiling is 80 C, so there are about **6 C of headroom**. Continuous
single-threaded inference heats it at ~0.35 C/s; four threads heat it at
~3.5 C/s and took it from 71.4 C to 78.5 C in under two seconds. In the
benchmark sweep, seven of eight configurations aborted on temperature, and a
governed pass needed **710 s of cooling for 40 s of compute - a 5% duty cycle**.

The design follows directly from that, and none of it is negotiable without
fitting active cooling first:

* `intra_op_num_threads = 1`, `inter_op_num_threads = 1`, `ORT_SEQUENTIAL`.
* Both sessions are loaded **once** at start-up, never per frame.
* Inference runs on a **single worker thread**, so two frames can never be
  inferred at the same time.
* Frames are only read **on an event** - a handful per vehicle passage. There
  is no continuous loop and there must never be one.

At 6-8 frames per passage that is about 1.2 s of CPU on one core, which the
thermal budget absorbs. **Fit the official Pi 5 active cooler before raising
`GATE_LOCAL_OCR_THREADS` above 1**, and re-run `bench2.py`
(left on the Pi at `/home/pi/recog-bench`) rather than assuming a number.

## Modes

`GATE_LOCAL_OCR_MODE` takes one of three values.

### `off` (default)

Nothing runs. This is the default in code, not just in the environment file.

### `shadow`

Every frame that goes to the cloud is also read locally, on the single
background worker, and the two answers are journalled together. The local
answer **cannot reach the relay**: in shadow mode the observation the processor
receives is always the cloud's.

The local inference never extends the decision. It is submitted before the
cloud request is posted and the decision proceeds the moment the cloud answers.
Whichever of the two lands second emits the frame's journal line; if the local
read finishes first it waits for the cloud before logging, and if the cloud
finishes first the decision has already gone ahead.

Run this for **weeks, not days**, before considering promotion.

### `active`

The local read runs first on the same crop. It may answer for the frame - and
so open the gate - only when all three of these hold:

1. The recogniser's confidence is at or above `GATE_LOCAL_OCR_MIN_CONFIDENCE`.
   This is the **only** local-specific gate. The statistic compared against it
   is the **minimum** per-character probability of the read, not the mean -
   see [The confidence gate](#the-confidence-gate-a-minimum-not-a-mean) below.
2. The controller's own
   [`decide_access`](../gate_controller/matching.py) authorises the local
   observations of that event. Not a copy of it, not a stricter variant of it:
   the same function, the same per-level confidence bars
   (`LevelRule.min_exact_confidence` / `min_fuzzy_confidence`, see
   [plate matching](plate-matching.md#confidence-bars)), the same exact-first
   rule, and the same two-frame `two_frame_ocr_confusion` rule, under the same
   time-of-day matching policy the processor will apply to the very same
   frame. A local read is subject to exactly the scrutiny a cloud read has
   always been subject to.
3. The plate that decision rests on is **this frame's own read**. `decide_access`
   weighs every observation of the event, so asking only whether it allows
   would let a frame that read something unrelated answer on an earlier
   frame's credit: it would spend the frame, skip its cloud lookup, and be
   journalled as the local match. The processor would still refuse to open on
   the wrong plate - it re-runs `decide_access` on what it was handed - but
   the frame and the evidence would both be wasted.

When both hold, the plate and its confidence are handed to the processor as a
`PlateObservation` with `source="local"`, and the processor makes the decision
through its normal path - so `reason` comes out as `exact_match` or
`two_frame_ocr_confusion` exactly as it would for a cloud read, the cooldown,
the idempotency key, the authorisation re-check before activation and the
telemetry are all unchanged. Only the event's `source` column says `local`
instead of `ocr`.

Anything else - no read, a read below the threshold, a read the shared matching
does not authorise, a local failure, or models that never loaded - falls
through to the cloud path exactly as today.

Note the consequence of reusing the two-frame fuzzy rule rather than
duplicating it: a fuzzy local open needs two confident observations of the same
misread within one event, held by the processor. In practice that is the first
frame's cloud read agreeing with the second frame's local read, which is the
same situation the rule was written for.

Say that consequence plainly, because it changes how the shadow numbers should
be read: **the processor's two-frame rule can now be satisfied by one cloud
read plus one local read.** The processor accumulates observations per event
and does not care which reader produced each one. Before this change a fuzzy
open required two cloud frames to agree; in active mode it can be one of each.
Shadow-mode agreement counts are per frame and say nothing about that pairing,
so a week of `agreement=match` does not by itself tell you how often a mixed
pair would have opened the gate. What does is `authorised=`, which is computed
over the event's accumulated local observations.

### The confidence gate: a minimum, not a mean

`GATE_LOCAL_OCR_MIN_CONFIDENCE` is compared against the **weakest character**
of the read. The recogniser returns one softmax maximum per character slot
(`char_probs`); the gate takes their minimum, and the mean is kept beside it as
`mean_score` for the journal, the corpus sidecar and the promotion decision.

The reason is arithmetic. On a seven-character Irish plate, six characters at
1.00 and one at 0.65 average 0.96 - clearing a 0.95 mean gate - and that one
weak character is precisely the one deciding whether the read is the authorised
plate or a different vehicle. The measured separation between right and wrong
reads (0.998 against 0.772) is wide enough that a per-character minimum costs
very little of the 415-in-458 retention the 0.95 threshold was chosen for.
(That 0.95 was the *measurement* threshold; what ships as the admission gate is
0.5 -- see the environment table below.)

Two details that follow from the same place:

* The per-character probabilities are paired with the read **positionally**,
  keeping only the slots whose character survives normalisation. The library
  strips trailing padding only, so slicing `char_probs` by the length of the
  normalised plate would shift by one for any read with an interior pad or
  separator, and score the wrong character.
* A non-finite probability counts as 0.0, and a non-finite score never clears
  the gate. `nan < 0.95` is `False`, so a `<` test would have passed a NaN
  straight through into `GateEvent.ocr_confidence` and out to the outbox as a
  bare `NaN` literal that no strict JSON reader accepts.

### Off the cloud slot

The controller has exactly one serial slot for *cloud* OCR requests
(`GateProcessor._ocr_slot`, a `BoundedSemaphore(1)`). Until 2026-09-08 the
local read ran inside that slot, so a 170 ms inference queued behind whatever
cloud call was in flight for an older frame. The measured cost, 2026-09-08
11:13:50: `local inference 170 ms` but `burst_to_ocr_ms=3654`, and the gate
opened 11.5 s after the webhook.

The local read now runs **before** the slot is taken, on the processor's own
burst thread (`GateProcessor._recognise` -> `PlateRecognizerClient.local_pass`).
Only frames that fall through to the cloud queue for the slot. A local grant
while a cloud call for an older frame is still in flight opens the gate
immediately; that in-flight call completes into the normal cooldown, which
refuses the second activation exactly as it always has.

The prepared upload travels with the frame: `local_pass` decodes, crops and
re-encodes the JPEG once and hands the bytes to the cloud request that
follows, so splitting the work does not double it on a board that cannot spare
the cycles.

`GATE_LOCAL_OCR_CLOUD=always` is the exception and stays exactly as it was -
its whole purpose is to keep the cloud request on the decision path so every
frame is labelled, and splitting it would change what it collects.

### What the guard may spend

The local read still runs inside the burst's decision budget, so a stalled
inference is not free. The wait is the smallest of three bounds:

| bound | value | why |
| --- | --- | --- |
| stuck-engine ceiling | `LOCAL_DECISION_TIMEOUT_SECONDS` = 2.0 s | 154 ms mean / 177 ms p95 measured, so this is a guard, not a normal outcome |
| what the cloud still needs | budget left less `LOCAL_DECISION_CLOUD_RESERVE_SECONDS` = 2.0 s | a cold handshake is 0.4-0.8 s and the read another second; a guard that ate this would time out the burst it was meant to accelerate |
| what the event has left | `LOCAL_EVENT_WAIT_BUDGET_SECONDS` = 2.0 s per event | admission is bounded per frame, so without this a three-frame burst could pay the guard three times inside one budget |

The processor passes the remaining budget to each call, and the cloud request's
connect/read split is re-sized **after** the local wait, from what is actually
left - otherwise the request would be sized for a budget the guard had already
spent. If nothing is left to spend, the frame takes a local read only if it has
already landed, and otherwise goes straight to the cloud.

And if the budget left cannot cover a cloud call at all, the frame does not
make one. A lookup is charged whether or not the answer arrives in time, and
on 2026-09-08 three frames of one visitor passage waited 3.4-5.6 s in the queue
and then each spent a paid lookup that timed out. `GATE_OCR_MIN_REQUEST_SECONDS`
(default 1.0 s, just under the measured p50 of every attempt that completed
3-6 September) is the floor; below it the frame is journalled
`gate_ocr stage=cloud_skipped reason=insufficient_budget` and decided as
`decision_timeout`, with **no** `ocr_timeout` attempt recorded, because
nothing was sent and nothing was billed.

### Corroborating the cloud read

A confident local read is kept for its event, keyed by trace id, and offered to
the processor's own `decide_access` as a *corroboration*
(`PlateRecognizerClient.local_observations`). It takes no part in the exact or
fuzzy rules; it exists so the
[agreement rule](plate-matching.md#when-both-readers-agree) can see that both
readers produced the same string and lower each one's bar. A read below
`GATE_LOCAL_OCR_MIN_CONFIDENCE` never enters the pool, so it corroborates
nothing.

That makes `GATE_LOCAL_OCR_MIN_CONFIDENCE` a **floor under**
`GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE_*`, not an independent knob: the
effective local agreement bar is the larger of the two. This is why the
shipped default is 0.5 rather than the 0.95 the shadow-mode measurement used.
At 0.95 the 2026-09-08 11:13:48 event -- local `10CE1990` at 0.566, cloud the
same plate at 0.806 -- was dropped before the 0.50 agreement bar written for
it could see it, and the agreement rule could not run at all on shipped
defaults.

### The time-of-day matching policy

`__main__` hands the one `MatchPolicyCache.get` to
`PlateRecognizerClient(match_policy=...)` and to `GateProcessor(match_policy=...)`,
so both consumers read the same schedule and a refresh in the background
reaches the next frame on both sides. See [plate matching](plate-matching.md)
for the schedule itself; only `strict` and `standard` ship.

The local path resolves the provider **once per frame**, at admission, and
applies that answer both to the admission gate and to the frame's journal
label, so the two speak about the same instant and the same band as the
processor's own decision on that frame. A provider that raises is not silently
replaced with a laxer band: the frame falls back to `decide_access`'s own
default, which is exactly what the processor falls back to for the same frame.

Without it, a fuzzy local read at 02:00 would be admitted under `standard`,
skip that frame's cloud lookup, and then be refused by the processor applying
`strict` - fail-closed at the relay, but it burns the frame the cloud would
have read exactly, making the overnight band *less* likely to open for a
legitimate car. Under a `strict` band, threading the policy means the local
path can still answer on an **exact** read (which `strict` allows and the
processor will honour) and never on a fuzzy one.

## Environment variables

| variable | default | meaning |
| --- | --- | --- |
| `GATE_LOCAL_OCR_MODE` | `off` | `off`, `shadow` or `active`. |
| `GATE_LOCAL_OCR_CLOUD` | `fallback` | `fallback`: the cloud request is skipped only for a frame the local read actually answered - one that cleared the confidence gate and authorised. Frames the local reader declined, read below the threshold, failed on, or could not take still go to the cloud exactly as today. `always`: the cloud request still runs, so the frame is labelled for the training corpus, but the local read is still what decides. |
| `GATE_LOCAL_OCR_DETECTOR` | `yolo-v9-t-384-license-plate-end2end` | Any detector registered in `open-image-models`. |
| `GATE_LOCAL_OCR_RECOGNISER` | `cct-xs-v2-global-model` | Any OCR model registered in `fast-plate-ocr`. |
| `GATE_LOCAL_OCR_THREADS` | `1` | `intra_op_num_threads`. Leave at 1 on a fanless board. |
| `GATE_LOCAL_OCR_MIN_CONFIDENCE` | `0.5` | The confidence gate, applied to the **weakest character** of the read (not the mean - see above). In shadow mode it only classifies the journal's `authorised=` field; in active mode it also gates the decision *and* admission to the corroboration pool, so it is the floor under `GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE_*` too. 0.95 is the shadow-mode measurement threshold and is too high to be the admission gate: it would keep the agreement rule from ever running on the shipped 0.50 agreement bar. |
| `GATE_LOCAL_OCR_MODEL_DIR` | `/var/lib/gate-controller/models` | Where the ONNX weights are cached. |
| `GATE_OCR_MIN_REQUEST_SECONDS` | `1.0` | The decision budget a **cloud** lookup must still have before it is worth billing. Below it the frame is skipped unbilled. Re-derive it if the uplink changes. |

The confidence bars the shared matching applies - including the ones that
lower when both readers agree - live with the matching levels, not here. See
[plate matching](plate-matching.md#confidence-bars) for the `GATE_MATCH_*`
keys.

`GATE_LOCAL_OCR_CLOUD=always` keeps the labelling but gives up the latency win:
the cloud request runs on the decision path exactly as it does today, and the
frame is only answered once it returns.

Be blunt about what that combination costs. **`active` with `always` is
strictly slower than the controller is today** - it pays the local read *and*
the full cloud request, in series, on the decision thread - and it can never be
faster. It is a labelling mode: it proves the local decision against a cloud
answer on the same frame while the corpus keeps growing. The mode that makes
the gate faster is `fallback`, and `always` is a stage on the way to it, never
a steady state.

That is deliberate, and it is the one place where the obvious design was the
wrong one. Moving the label request to a background thread would have it share
this client's one-request-per-second pacing window and its upload geometry with
a gate-critical request on the *next* vehicle; a stray 429 on the request that
opens the gate is a far worse outcome than the latency this mode gives up. So
`always` exists to keep collecting pseudo-labels while active mode is being
proven, not as a steady state - the mode that actually makes the gate faster is
`fallback`. If the cloud request fails here, the local read still decides, and
only the label is lost.

## The model directory

The service runs with `ProtectHome=true`, so the `~/.cache` directory both
packages default to is invisible to it. `GATE_LOCAL_OCR_MODEL_DIR` must be a
path the `gate-controller` user can write, which in practice means under
`/var/lib/gate-controller` - the unit's only `ReadWritePaths` entry.
`deployment/install.sh` creates `/var/lib/gate-controller/models` owned by
`gate-controller` with mode 0700.

The weights are **fetched on first use**, not shipped: the recogniser downloads
them into that directory during its start-up warm-up. That one-off needs
**outbound HTTPS to `github.com` and `objects.githubusercontent.com`** (the
release assets of `ankandrew/open-image-models` and `ankandrew/cnn-ocr-lp`).
About 8 MB, once. After that the directory is self-sufficient and the download
is skipped; the Pi does not need outbound access to those hosts again unless
the model names change. If the fetch fails, the recogniser reports
`stage=unavailable` once and the controller runs exactly as today.

To pre-seed the directory without waiting for a first run - useful when the Pi
is behind a proxy - copy the cached weights across from a machine that already
has them, keeping the same layout:

```
/var/lib/gate-controller/models/
  yolo-v9-t-384-license-plate-end2end/yolo-v9-t-384-license-plates-end2end.onnx
  cct-xs-v2-global-model/cct-xs-v2-global-model.onnx
  cct-xs-v2-global-model/cct-xs-v2-global-model_config.yaml
```

Then `chown -R gate-controller:gate-controller` and `chmod 0700` the directory.

## Deployment

The three packages are in `requirements.txt`, so both `deployment/install.sh`
and the CI-gated updater (`deployment/gate_controller_updater.py`, which builds
each release's `.venv` from that file) install them the same way as everything
else.

They are marked
`(platform_machine == "aarch64" or platform_machine == "arm64") and
python_version >= "3.11"`, extending the existing `rpi-lgpio` precedent. The
architecture half covers the Pi and the development Mac - the only two
platforms these graphs have been measured on - and deliberately excludes x86-64
CI, so the unit suite keeps running with no onnxruntime present. That is not an
accident of convenience: it is a standing test that the local recogniser
degrades correctly when the wheels are missing.

The Python half is not optional. `onnxruntime` 1.29.0 declares
`Requires-Python >=3.11`; on an arm64 host still running 3.10 a marker that
asserted only the architecture would fail the **whole**
`pip install -r requirements.txt`, and that install is exactly what the
updater's `verify_release` runs before accepting a release. One old interpreter
would reject every update rather than merely leave the local recogniser
uninstalled.

The transitive closure (numpy, opencv-python-headless, protobuf, flatbuffers,
packaging, PyYAML, rich, tqdm and rich's own three) is pinned beside them, the
way `requests`' closure already is, and `--only-binary` names that stack so a
missing wheel fails the install instead of quietly starting an opencv source
build - which could not finish inside the updater's 900 s timeout.

That option names each package rather than using `--only-binary=:all:`, which
would apply to the entire resolution. `lgpio`, which `rpi-lgpio` pulls in,
publishes its aarch64 wheel as `manylinux_2_34`: installable against
Bookworm's glibc 2.36 and not against an older image, where pip falls back to
the sdist and builds it in seconds. `:all:` would turn that working fallback
into a hard install failure and make `verify_release` reject every release -
the same failure mode the `python_version` marker above exists to prevent.

**Size.** A fresh `.venv` is built per release, so this is paid on every
update, not once: about **80 MB of wheels downloaded** (86 MB for the whole
requirements file) and about **264 MB installed**, most of it onnxruntime,
opencv and numpy. Check the free space on `/opt` before enabling this on a
controller that keeps several releases. That is separate from - and much larger
than - the ~8 MB of ONNX weights fetched once into the model directory.

`MemoryMax` in `file-monitor.service` rises from 512M to 1G. Two ONNX sessions
plus the decoded frame measured 133-237 MB RSS on the Pi, on top of the
controller's own footprint, and an OOM kill of the gate daemon would be a far
worse outcome than a slightly looser cap.

## What gets logged

### Start-up

```
gate_local_ocr stage=ready mode=shadow detector=yolo-v9-t-384-license-plate-end2end \
  recogniser=cct-xs-v2-global-model threads=1 load_ms=137 warmup_ms=168 \
  model_dir=/var/lib/gate-controller/models
```

or, once and only once:

```
gate_local_ocr stage=unavailable reason=import_failed
```

`reason` is one of `import_failed`, `model_dir_unwritable`, `unknown_detector`,
`detector_load_failed`, `recogniser_load_failed`, `load_failed`,
`warmup_failed`.

Loading and the synthetic warm-up inference both happen on a background thread
at start-up, so the first real frame is not the one that pays for them.

### Per frame

One line per frame, emitted when both answers are in:

```
gate_local_ocr stage=shadow trace_id=4af8401d-... local_plate=131D2696 \
  local_score=0.999 local_mean=0.999 local_ms=163 cloud_plate=131D2696 \
  cloud_score=0.910 agreement=match authorised=both decision_source=cloud
```

* `stage` is the mode: `shadow` or `active`.
* `local_score` is the weakest character of the read - the statistic the
  threshold gates on - and `local_mean` is the mean of the same characters.
  A wide gap between them is a read with one bad character.
* `agreement` is `match`, `mismatch`, `local_only` (only the local reader got a
  plate), `cloud_only`, or `both_none`. Plates are compared normalised -
  uppercased, non-alphanumerics stripped - so `12-D-3456` and `12d3456` agree.
* `authorised` is `both`, `local_match`, `cloud_match` or `none`: whether each
  side's read authorises under the shared matching, **on the strength of that
  frame's own plate** and under the policy band in force when the frame was
  read. The local side additionally has to clear
  `GATE_LOCAL_OCR_MIN_CONFIDENCE`. A frame that read something unrelated is
  never `local_match`, however its siblings read.
* `decision_source` is `local`, `cloud` or `none` - which reader's answer went
  to the processor.
* `local_ms` is the whole local cost for the frame: decode plus detect plus OCR.

A frame where the local reader was skipped or failed still produces a line, with
`local_plate=-`.

`authorised=` still labels each reader against the **full** bar for its band,
not the agreement bar. So a passage the agreement rule opened is journalled
`authorised=none` with `decision_source=cloud`, and the line that says what
actually happened is the `gate_match` one below. Keeping the label at the full
bar is deliberate: it is the evidence the promotion decision rests on, and it
has to keep meaning what it has always meant.

### Per decision

Two lines were added on 2026-09-08. Both are journal-only: nothing about the
event payload or the telemetry envelope changed, and the Worker's allowlists
were not touched.

```
gate_match stage=agreement_grant plate=10CE1990 local_score=0.566 \
  cloud_score=0.806 match_rule=exact level=standard band=08:00-22:00 \
  min_local=0.50 min_cloud=0.70
```

The gate opened because both readers produced the same plate, at a bar neither
would have cleared alone. `match_rule` is `exact` or `ocr_confusion`; the event
itself carries the reason it would otherwise have carried.

```
gate_ocr stage=cloud_skipped reason=insufficient_budget remaining_ms=487 \
  required_ms=1000
```

A queued frame whose remaining decision budget could not cover a cloud lookup,
so none was posted and none was billed. Count them against the monthly
allowance the same way you count `authorised=`:

```
journalctl -u file-monitor.service --since '-7 days' \
  | grep -c 'stage=cloud_skipped'
```

Two more lines changed rather than appeared:

* `gate_trigger_capture outcome=presence_ended reason=plate_read` is **gone**.
  A read no longer ends a presence session; only a grant (`reason=opened`), a
  confident read of a plate nothing authorised is near (`reason=plate_denied`),
  or a final pipeline answer does.
* `gate_burst stage=skipped reason=event_already_opened` marks a queued frame
  whose own passage had already opened the gate, dropped before it could buy a
  lookup that could not change anything.

## Reading a week of agreement

```
journalctl -u file-monitor.service --since '-7 days' \
  | grep -o 'agreement=[a-z_]*' | sort | uniq -c | sort -rn
```

Whether the local reader would have opened the gate correctly:

```
journalctl -u file-monitor.service --since '-7 days' \
  | grep -o 'authorised=[a-z_]*' | sort | uniq -c | sort -rn
```

The two together are the promotion criterion. `authorised=cloud_match` is the
count that matters most: those are passages the cloud let in and the local
reader would not have. `authorised=local_match` without `cloud_match` is the
opposite - and, per the baseline, is as likely to be the local reader
*correcting* a cloud misread as making one of its own.

Latency, to confirm the board is not being pushed:

```
journalctl -u file-monitor.service --since '-7 days' \
  | grep -o 'local_ms=[0-9]*' | cut -d= -f2 | sort -n | awk '
    {v[NR]=$1} END {printf "n=%d p50=%d p95=%d max=%d\n", NR, v[int(NR*0.5)], v[int(NR*0.95)], v[NR]}'
```

Every mismatch is worth looking at by hand. The corpus keeps the frame.

## Where the answers are kept

**Event telemetry.** A compact `local_ocr` block travels with the event, beside
`frames` and `trigger`, so it reaches D1 and the app:

```json
"local_ocr": {
  "mode": "shadow", "frames": 2, "plate": "131D2696", "score": 0.999,
  "latency_ms": 163, "agreement": "match", "authorised": "both",
  "decision_source": "cloud", "status": "recognized"
}
```

Every value is a bounded token, a bounded duration or a plate string, and
unknown tokens are replaced with their safe default before the block is
serialised. In shadow mode the block is best-effort by construction: a frame
whose local read is still running when the cloud has already decided is simply
not counted.

> **Two app-side prerequisites, not one.** The ingest Worker validates the
> telemetry payload against a key allowlist and rejects the whole event for an
> unknown key, so `local_ocr` must be on that allowlist. Active mode
> additionally sends events with `source="local"`, a value the app has never
> seen - today's are `ocr`, `remote_command`, `reolink_webhook` and
> `camera_ftp`. If `source` is validated as an enum, an active-mode open is
> rejected at ingest: **the gate opens and the event never reaches the app**,
> which is the worst of the two failure modes because nothing on the gate looks
> wrong. Both changes are safe to make in advance, and both are inert while the
> mode is `off`: nothing is emitted and no event carries `source="local"`.
>
> Both landed in access-gate-ui **PR #42**, which must be deployed before this
> controller release. Its accepted shape is exactly the nine keys above -
> `mode`, `frames` (0-8), `plate` (`[A-Z0-9]{1,32}` or null), `score` (0-1 or
> null), `latency_ms`, `agreement`, `authorised`, `decision_source`, `status` -
> with **no `box` on the wire**; the box stays in the corpus sidecar, where it
> is useful, and never travels with the event.

**One cross-field rule.** PR #42 also rejects the whole event when
`decision_source` is `"local"` and `mode` is not `"active"`. Nothing in the
controller can produce that pairing - only the active path calls
`decided_locally()` - but `LocalOcrTelemetry.to_wire` enforces it as a backstop
and downgrades such a `decision_source` to `none` rather than emitting an event
that would be refused whole at ingest.

**Bounded admission.** Only one local read is ever outstanding. A frame that
arrives while another is still being read is answered immediately as
`unavailable` and goes to the cloud, rather than queueing behind it: a stalled
inference must not make every later frame spend its guard timeout waiting, and
a queue would pin one JPEG per waiting frame. The `busy` counter in the status
block says how often that happened.

**Training corpus.** Each corpus sidecar gains a `local` block alongside `ocr`,
with the local plate, its score, the box in whole-frame fractions, the
per-stage latencies and the candidate reads, plus `extra.local_ocr` (the mode)
and `extra.cloud` (`requested` or `skipped`). Frames where the cloud was
skipped are recorded with `source: "local_recognizer"` and no `ocr` results, so
a later review pass can tell a pseudo-label from a local read.

**Status / heartbeat.** `recognition.local_shadow` in the controller status
carries the counters: frames, agreements, mismatches, local_only, cloud_only,
both_none, local_decisions, errors, unavailable, and mean/p95 latency, plus the
load and warm-up timings and the state.

## Rolling out

1. Fit the active cooler. Everything below is measured without one and the
   benchmark says that is the binding constraint.
2. Prepare the app for both new things, not one: add `local_ocr` to the ingest
   Worker's telemetry allowlist, **and** accept `"local"` as an event `source`
   value. The second only matters for active mode, but an event rejected at
   ingest after the gate has already opened is the failure that looks like
   nothing at all from the gate's side.
3. `GATE_LOCAL_OCR_MODE=shadow`. Confirm `stage=ready`, then leave it for
   weeks. Watch `local_ms` p95 and the SoC temperature under real load - the
   benchmark was taken with the controller idle and says nothing about
   behaviour beside a live event session.
4. Read the agreement and authorisation counts. Look at every mismatch.
5. Only then `GATE_LOCAL_OCR_MODE=active`, first with
   `GATE_LOCAL_OCR_CLOUD=always` so the corpus keeps growing while the local
   path decides - that stage proves the decision, not the latency - then
   `fallback`, where the lookup and the wait for it go away for every frame
   the local reader answers - and only those.
6. The kill switch is `GATE_LOCAL_OCR_MODE=off` and a restart.

## What is deliberately not here

* **A local-only mode.** The cloud remains the fallback for everything the
  local reader does not answer. Removing it is a separate decision that needs
  the shadow and active data first.
* **The Irish format prior.** Measured, and it *costs* accuracy: 90.2% to
  89.7% exact, 95.5% to 93.3% character accuracy, because it discards
  partially-correct reads without replacing them. It may still be useful as a
  tie-break or a confidence signal; it must not be shipped as a filter.
* **Any appearance / known-vehicle recogniser.** Issue #43's rule stands: an
  appearance match is a credential the household chooses to accept, it is not a
  plate read, and it is never called OCR.
* **Fine-tuning.** The pretrained pair is good enough to run on. The dominant
  error is plate pixel width - misreads average 78 px against 134 px for
  correct reads - so framing and camera zoom are worth more than training. Fix
  `GATE_PLATE_REGION` and the zoom before spending effort on a model.
