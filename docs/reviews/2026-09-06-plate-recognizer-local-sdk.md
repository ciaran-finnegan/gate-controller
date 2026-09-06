# Plate Recognizer on-device SDK on a Raspberry Pi 5 (research, 2026-09-06)

Read-only research by a sub-agent from Plate Recognizer's public documentation
and Docker Hub metadata. Nothing was installed or run. Quotes are from the
cited pages as of 2026-09-06.

## 1. Image, tag, hardware requirements

Image: `platerecognizer/alpr-arm` ("for Raspberry Pi ARM-based CPUs or Apple
M1. This image was previously named alpr-raspberry-pi",
https://guides.platerecognizer.com/docs/snapshot/manual-install/). Tags are
`<architecture>:<country>-<version>`. Docker Hub shows `latest` as a
single-arch linux/arm64 manifest, ~392 MB compressed (~1.1 GB on disk),
last pushed 2026-08-05; no 32-bit variant, so a 64-bit OS is required.

Requirements: "At least 500MB of free RAM"; "We also support the Raspberry
Pi3 (inference speed of 1 second)"
(https://guides.platerecognizer.com/docs/snapshot/getting-started/). Disk:
not stated (~1.5 GB inferred).

Raspberry Pi 5 is never mentioned. The supported list stops at "Raspberry Pi
3 & 4 (32 bit, 64 bit)" (https://platerecognizer.com/snapshot/).

## 2. Claimed latency ("Speeds based on HD Image size (1280 x 720)")

| Device | Fast mode | Regular mode |
| --- | --- | --- |
| n2-standard-8 | 21 ms | 41 ms |
| Intel Core i7-8550U | 33 ms | 55 ms |
| LattePanda Alpha | 119 ms | 170 ms |
| Nvidia Jetson Nano | 250 ms | 300 ms |
| Raspberry Pi 3/4 | 1000 ms | 1300 ms |

"Initialization can take up to 10-20 seconds" on Pi. Pi 5: not stated.

## 3. Runtime internals (from image metadata)

Debian bookworm, Python 3.10, `INFERENCE_ENGINE=tflite` (TensorFlow Lite
2.11 CPU / XNNPACK from PINTO0309's aarch64 wheel), OpenCV, Cython `.so`
inference modules, `python3 main.py`, ports 8080/8081. Two-stage
detector + reader (`dscore` = detection confidence, `score` = text
confidence). `config` keys: `mode:fast` ("~30% speed-up ... may result in
lower accuracy when using images with small vehicles"), `detection_rule`,
`detection_mode`, `region:strict`, `threshold_d`/`threshold_o`,
`text_formats`, `plates_per_vehicle`, `zoom_in_vehicles`. `regions` supports
`ie` (Ireland). `mmc=true` for make/model/colour "for an additional fee".
"There is no file size limit on the Snapshot SDK" (cloud: 3 MB).

## 4. Licensing

Needs `-e TOKEN=` and `-e LICENSE_KEY=` with `-v license:/license`.
"An internet connection is needed during installation." Afterwards the SDK
"will 'call home' a few times a month to validate the subscription ...
flakey, intermittent Internet is OK"; failure mode: "Your account status has
not been checked in the past 30 days. An Internet connection is required."
Perpetual licences exist. "Each license can only be used on a single
machine."

Billing: "Pricing below is the same for Cloud and On-Premise SDK"; "A Lookup
is basically any image that is sent over to the Plate Rec Snapshot API Cloud
or SDK. Even if that image does not contain a vehicle plate, it is still
counted." Tiers: FREE 2,500/month (not for production), SMALL $50/month for
50,000, MEDIUM $150 for 250,000, LARGE $250 for 500,000; MMC adds 50%.

## 5. Local API compatibility

`POST http://localhost:8080/v1/plate-reader/`: same path, same multipart
field `upload`, same `regions`/`config`/`mmc`/`camera_id`/`timestamp`, same
`results[]` with `plate`, `score`, `dscore`, `box`, `candidates`, `region`,
`vehicle`, wrapped with `usage`, `camera_id`, `timestamp`, `filename`. No
`Authorization` header; "no maximum calls per second contrary to our Cloud
API"; `GET /info/` reports version and usage. Effectively a base-URL swap.

## 6. Hybrid use, Stream, speed tips

No documented local-first/cloud-fallback pattern. Tips: `-e WORKERS=2` on
4-8 core devices; send BMP/PPM/PNM to skip JPEG decode; `mode:fast`; lower
`zoom_in_vehicles`; plate "must have roughly 100 pixels in width" (reads
down to ~30 px). For gates they steer to Stream ($35/month per camera, no
lookup limit), with `max_prediction_delay` ~3 s for parking gates.

## 7. Pi hardware and thermal notes

Nothing on cooling, throttling or swap. Pi-specific guidance is limited to
the manual install, `alpr-arm`, 10-20 s init, `--restart unless-stopped`,
optional `--network=host`.

## 8. Training and fine-tuning

No self-service fine-tuning, correction API or data feedback endpoint. The
only loop is emailing problem images to support. Customer-side control is
`regions`, `region:strict`, `text_formats` and thresholds.

## Verdict for this gate

It will run: pure arm64 TFLite, no GPU dependency, 500 MB RAM floor. Expect
roughly 300-500 ms regular / 200-350 ms fast on a Pi 5 at 720p, scaled from
the published Pi 3/4 figure, on hardware Plate Recognizer has never listed.
The real risk is thermal: XNNPACK saturates all four cores per inference on
a fanless Pi already near 85 C. Mitigations are on our side: `WORKERS=1`,
`mode:fast`, crop to the plate band and send ~720p, prefer BMP/PPM, active
cooling.

Hybrid local-first with cloud in parallel is straightforward because the API
shapes match: one client, two base URLs, `Authorization` only for the cloud.
Cost is the catch: SDK and cloud lookups draw on the same quota and every
image counts, so mirroring everything doubles consumption (3,000 events ->
6,000 lookups), still inside SMALL at $50/month. The free tier is barred from
production. A self-trained local model remains the only path to zero
per-lookup cost, with Plate Recognizer as the pseudo-labeller and fallback.
