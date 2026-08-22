# Hot Stream Recognition Design

## Goal

Minimise FTP-trigger-to-gate latency while improving OCR robustness. The Pi must
have recent clear-stream frames ready before an FTP event arrives and submit the
best frames in the event's first cloud OCR burst. Local recognition remains
disabled until a separate laptop evaluation establishes that it is accurate
enough to justify a Pi shadow deployment.

## Measured baseline

The installed RLC-810A provides clear 3840x2160 H.265 at 10 fps and fluent
640x360 H.264 at 10 fps. A newly attached reader can wait about five seconds for
an upstream keyframe, so opening a stream after an event is not a viable fast
path. Continuous clear-stream decoding is feasible on the four-core Pi 5.

FastALPR 0.4.0 with the pinned detector and OCR models processed 90 private
production images on the laptop at 36.632 ms p50, 41.033 ms p95 and 43.133 ms
p99. It emitted 29 predictions and deliberately abstained on 61 images. These
figures establish latency only; agreement and labelled accuracy remain promotion
gates.

## Runtime architecture

MediaMTX owns two continuously pulled camera paths:

- `camera` is the existing fluent/sub stream used for browser video.
- `clear` is the clear/main stream used by the recognition buffer.

The controller starts one bounded `ffmpeg` child at boot and keeps it attached
to loopback `clear`. It decodes continuously and emits JPEGs at 5 fps into
an in-memory ring. Five fps limits normal frame age to about 200 ms while avoiding
the cost of re-encoding all ten clear frames each second. The ring contains at
most eight validated JPEGs and is capped by both per-frame and total bytes.

When the first completed FTP JPEG for an event is observed, the controller
atomically materialises the three newest distinct buffered clear frames into an
owner-only ignored directory. They are added to the same 200 ms collection
window as the FTP image, quality-ranked together, and the existing maximum-three
OCR policy submits the best candidates. There is no on-trigger connection,
network request, keyframe wait, cooldown, or second OCR pass.

The HTTPS `Snap` sampler, its configuration, and its progressive fallback queue
are removed.

## Local recognition boundary

This release reports local recognition as disabled and does not install or load
a model on the Pi. The private laptop corpus shows adequate inference latency
but insufficient agreement with the existing cloud observations, which are
themselves only pseudo-labels. Enabling even a telemetry-only Pi worker before a
representative labelled evaluation would add resource load without producing a
trustworthy comparison.

Promotion to a local opening fast path requires a separate reviewed change with
a representative labelled day/night/rain evaluation, explicit false-accept and
false-reject bounds, and the existing durable authorisation/claim/cooldown path.

## Configuration and status

Root-managed media configuration contains separate fluent and clear RTSP source
URLs. Camera credentials remain outside the application database and UI. The
controller exposes only non-secret effective profile and health fields: enabled,
ready, stream name, configured source resolution/fps/codec, sampling fps, latest
frame age, restart count, local-recognition mode and model readiness.

The web UI displays these effective values and health. It does not accept
arbitrary URLs or camera credentials, and this release keeps the root-managed
preset read-only in the browser. The deployed preset is RLC-810A clear
3840x2160 H.265 10 fps, fluent 640x360 H.264 10 fps, and clear sampling at 5
fps.

## Safety and failure behaviour

- FTP remains the trigger and remains usable if either stream or local model is
  unavailable.
- Buffered frames never actuate except through existing cloud OCR,
  authorisation, durable claim, relay and cooldown safeguards.
- The buffer accepts only loopback RTSP input, rejects oversized or malformed
  JPEGs, never logs URLs or credentials, and restarts with bounded backoff.
- Frame storage is volatile, owner-only and bounded. Selected files are removed
  by the normal burst cleanup path.
- The ffmpeg child receives no inherited environment secrets and is terminated
  on shutdown.
- Local recognition is non-actuating until separately promoted.

## Deployment acceptance

Before live activation, verify all repository tests, media configuration
validation, loopback authorization, clear-path readiness, buffer age below 500
ms, bounded Pi CPU/memory, and a non-actuating triggered OCR event. Production
must roll back automatically to the FTP/cloud-only path if the hot buffer is not
ready.
