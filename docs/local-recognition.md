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
so open the gate - only when both of these hold:

1. The recogniser's confidence is at or above `GATE_LOCAL_OCR_MIN_CONFIDENCE`.
   This is the **only** local-specific gate.
2. The controller's own
   [`decide_access`](../gate_controller/matching.py) authorises the local
   observations of that event. Not a copy of it, not a stricter variant of it:
   the same function, the same `MIN_EXACT_CONFIDENCE`/`MIN_FUZZY_CONFIDENCE`
   thresholds, the same exact-first rule, and the same two-frame
   `two_frame_ocr_confusion` rule. A local read is subject to exactly the
   scrutiny a cloud read has always been subject to.

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

## Environment variables

| variable | default | meaning |
| --- | --- | --- |
| `GATE_LOCAL_OCR_MODE` | `off` | `off`, `shadow` or `active`. |
| `GATE_LOCAL_OCR_CLOUD` | `fallback` | `fallback`: in active mode the cloud request is skipped entirely once the local read has answered - the lookup is not spent, the uplink is not used, and the frame's latency drops to the local read. `always`: the cloud request still runs, so the frame is labelled for the training corpus, but the local read is still what decides. |
| `GATE_LOCAL_OCR_DETECTOR` | `yolo-v9-t-384-license-plate-end2end` | Any detector registered in `open-image-models`. |
| `GATE_LOCAL_OCR_RECOGNISER` | `cct-xs-v2-global-model` | Any OCR model registered in `fast-plate-ocr`. |
| `GATE_LOCAL_OCR_THREADS` | `1` | `intra_op_num_threads`. Leave at 1 on a fanless board. |
| `GATE_LOCAL_OCR_MIN_CONFIDENCE` | `0.95` | The confidence gate. In shadow mode it only classifies the journal's `authorised=` field; in active mode it also gates the decision. |
| `GATE_LOCAL_OCR_MODEL_DIR` | `/var/lib/gate-controller/models` | Where the ONNX weights are cached. |

`GATE_LOCAL_OCR_CLOUD=always` keeps the labelling but gives up the latency win:
the cloud request runs on the decision path exactly as it does today, and the
frame is only answered once it returns.

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

They are marked `platform_machine == "aarch64" or platform_machine == "arm64"`,
matching the existing `rpi-lgpio` precedent. That covers the Pi and the
development Mac - the only two platforms these graphs have been measured on -
and deliberately excludes x86-64 CI, so the unit suite keeps running with no
onnxruntime present. That is not an accident of convenience: it is a standing
test that the local recogniser degrades correctly when the wheels are missing.

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
  local_score=0.999 local_ms=163 cloud_plate=131D2696 cloud_score=0.910 \
  agreement=match authorised=both decision_source=cloud
```

* `stage` is the mode: `shadow` or `active`.
* `agreement` is `match`, `mismatch`, `local_only` (only the local reader got a
  plate), `cloud_only`, or `both_none`. Plates are compared normalised -
  uppercased, non-alphanumerics stripped - so `12-D-3456` and `12d3456` agree.
* `authorised` is `both`, `local_match`, `cloud_match` or `none`: whether each
  side's read authorises under the shared matching. The local side additionally
  has to clear `GATE_LOCAL_OCR_MIN_CONFIDENCE`.
* `decision_source` is `local`, `cloud` or `none` - which reader's answer went
  to the processor.
* `local_ms` is the whole local cost for the frame: decode plus detect plus OCR.

A frame where the local reader was skipped or failed still produces a line, with
`local_plate=-`.

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

> The ingest Worker validates the telemetry payload against a key allowlist and
> rejects the whole event for an unknown key. **Add `local_ocr` to the Worker's
> allowlist before enabling any mode on a controller that delivers to
> Cloudflare.** Nothing is emitted while the mode is `off`.

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
2. Add `local_ocr` to the ingest Worker's telemetry allowlist.
3. `GATE_LOCAL_OCR_MODE=shadow`. Confirm `stage=ready`, then leave it for
   weeks. Watch `local_ms` p95 and the SoC temperature under real load - the
   benchmark was taken with the controller idle and says nothing about
   behaviour beside a live event session.
4. Read the agreement and authorisation counts. Look at every mismatch.
5. Only then `GATE_LOCAL_OCR_MODE=active`, first with
   `GATE_LOCAL_OCR_CLOUD=always` so the corpus keeps growing while the local
   path decides - that stage proves the decision, not the latency - then
   `fallback`, which is where the lookup and the wait for it both go away.
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
