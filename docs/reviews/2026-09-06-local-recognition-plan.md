# Local recognition: plan and dataset inventory (2026-09-06)

Decision: no Plate Recognizer SDK (nowhere to run it, and it bills the same
lookups). Train our own models from the gate's own history and the corpus the
controller now collects, and run them on the Pi 5. Plate Recognizer stays in
parallel as the pseudo-labeller and fallback until the local path is proven.

## What the history holds (D1 + R2, 2026-09-06)

| Slice | Images | With plate label | Notes |
| --- | --- | --- | --- |
| Legacy Hikvision, 2024-09 to 2025-11 | 1,934 | 373 | `legacy/pi-log/*`, 1280x720, different angle |
| Reolink RLC-810A, 2026-08 on | 369 | 10 | `controllers/primary/events/*`, 1280 wide |
| Night, 20:00-07:00 UTC | ~700 | ~4 | effectively unlabelled: the physics gap |
| Distinct plates with images | | 12 | |

Reasons across all events: 1,561 `legacy_no_match`, 373 `legacy_exact_match`,
then this month's failure modes (`no_match` 143, `ocr_error` 108,
`upload_incomplete` 102, `decision_timeout` 44, `authorisation_error` 36).

Export: `scripts/export_training_dataset.py` writes
`~/dev/gate-controller-data/manifest.jsonl` and `images/<sha>.jpg` through
wrangler (about 3 s per object; resumable). The Pi corpus
(`/var/lib/gate-controller/training-corpus`) adds every new OCR upload with
Plate Recognizer's box and candidates as sidecars; sync it with `scp` or
`rsync` into the same tree under `corpus/`.

## Two models, not one

1. **Plate reader** (detector + text recogniser). 383 labelled images across
   12 plates is far too few to learn to read arbitrary plates from scratch,
   so start from pretrained open models (a small YOLO-class plate detector
   and a CRNN/PARSeq-class recogniser trained on public plate datasets) and
   fine-tune on ours. Add the Irish format as a hard prior
   (`\d{2,3}-[A-Z]{1,2}-\d{1,6}`, valid county codes): it rejects nonsense
   reads cheaply and sharpens candidates. Boxes: the new corpus carries
   Plate Recognizer's box for every read; legacy images get boxes from the
   pretrained detector, reviewed in bulk.
2. **Known-vehicle recogniser** (appearance). This is what lets tractors and
   machinery without visible plates through. A small re-identification
   embedding trained on crops of each authorised vehicle (the Audi alone has
   260 legacy events), matched against a gallery per authorised vehicle.
   Policy, not the model, decides what it may open: daytime, confidence
   above a per-vehicle threshold, optional corroboration (a partial plate
   read, the camera's make/model/colour, time of day). Issue #43's rule
   stands: an appearance match is a credential the household chooses to
   accept, not a plate read, and it is never called OCR.

## Phases

- **0 Data (now).** Export history; sync the corpus weekly; build the
  manifest with camera era, day/night, plate label, decision, and vehicle
  identity where known. Split by complete passage/day, never by adjacent
  frames. Legacy and Reolink are separate domains: legacy pretrains, Reolink
  evaluates.
- **1 Labels.** Boxes from the pretrained detector plus corpus sidecars; a
  review pass in the app's existing event-review panel for disagreements,
  low confidence, night, and every Reolink example. Vehicle identity from
  plate matches first, then clustering of unlabelled legacy frames reviewed
  by hand.
- **2 Train on the MacBook.** Fine-tune the plate reader; train the
  embedding. Report accuracy on Reolink-only data and, where sample size
  allows, vehicle-disjoint. Export ONNX, quantise to int8 for ARM64,
  benchmark on the Pi 5 for latency, memory and heat (target: under 150 ms
  per frame on one core; the Pi has no fan).
- **3 Shadow mode on the Pi.** A `LocalRecognizer` runs on every frame
  beside the cloud request, journals its answer and agreement, and never
  reaches the relay. Weeks, not days, of agreement data before promotion.
- **4 Promotion.** Local first; open on a valid, confident local read (or a
  policy-accepted appearance match); the cloud answer still arrives and is
  recorded for the corpus, and it remains the fallback when the local model
  abstains. Kill switch by environment variable.

## What this does not fix

Night. With ~0 labelled night images there is nothing to learn from; the gate
post blazes in infrared and headlights sit on the lens axis. Off-axis
lighting or masking the post-side IR is the prerequisite for any model,
local or cloud, to read plates after dark.
