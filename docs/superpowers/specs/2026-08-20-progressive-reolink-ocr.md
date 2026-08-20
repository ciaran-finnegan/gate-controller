# Progressive Reolink OCR Design

## Goal

Reduce time-to-authorisation and improve plate recognition reliability by adding
two fresh 4K frames to the existing OCR pipeline without delaying the first FTP
image or allowing a camera event to actuate the relay directly.

## Measured Camera Baseline

The installed camera reports model `RLC-810A`, hardware `IPC_56064M8MP`, and a
fixed 4 mm lens. Its configured streams are:

- Clear: 3840x2160, 10 fps, H.265, 6144 Kbit/s.
- Fluent: 640x360, 10 fps, H.264, 256 Kbit/s.

On 2026-08-20, two sequential authenticated HTTPS `Snap` requests from the Pi
returned complete 4K JPEGs in 625 ms and 677 ms. Earlier on-demand ffmpeg reads
from the RTSP/MediaMTX path took 4.88-5.44 seconds for three frames because a new
reader waited for the camera keyframe. Therefore HTTPS snapshots are the
high-resolution OCR source until a measured RTSP configuration beats them.

## Runtime Architecture

The first completed Reolink FTP JPEG continues into the existing 200 ms burst,
quality ranking, OCR, authorisation, durable actuation, and cooldown path. At
that same first-completed boundary, a best-effort background sampler starts a
single authenticated camera session and captures two 4K JPEGs sequentially
under one 2.25-second deadline.

The additional images form a separate progressive burst carrying the original
FTP receipt time. They enter the same bounded candidate ranking and maximum
three-frame OCR policy as every other image. They cannot open the gate except
through an exact or otherwise policy-approved authorised-plate decision.

Fluent RTSP through MediaMTX remains the low-cost continuous video path and is
the input for the separate local vehicle-detection shadow benchmark. Local
detection must not gain relay authority until its latency, recall, and false
trigger rate have been measured against Reolink detection.

## Safety and Resource Bounds

- Snapshot augmentation is optional and fail-open only with respect to the
  original FTP recognition attempt; all access decisions still fail closed.
- Camera configuration accepts only a private or loopback literal HTTPS origin,
  rejects redirects, and never logs credentials or tokens.
- Capture count is limited to 1-4, the global deadline to at most 3 seconds, and
  each response to the controller's configured image-byte ceiling.
- Only one augmentation request may be active or queued.
- Generated files live in an owner-only ignored directory, are validated as
  complete JPEGs, and are removed after processing or shutdown.
- The existing relay ordering, four-second decision deadline, persisted
  cooldown, queue coalescing, and image freshness checks remain authoritative.

## Deployment

Enable the sampler through root-readable `/etc/gate-controller.env` after the
release is active. Reuse the already configured camera account initially, then
replace it with a dedicated least-privilege viewing account if the camera
firmware permits snapshots for that role. The deployment must be verified with
the relay held inactive before normal service is restored.
