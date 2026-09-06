# Decision timing and the presence window (2026-09-06)

The user's target: the gate opens within 5 s of the car stopping, and the
controller does not sit retrying for most of a minute. This note records the
measurements behind the Pi settings changed on the evening of 2026-09-06 and
what the pipeline can and cannot achieve with cloud OCR on this network.

## What the 22:16 miss showed

The camera's AI vehicle alarm fired at 22:16:41 (webhook received 1.6 s
after the camera's FTP still had already landed). Seven frames were
processed in 34 s and all were denied: two `no_plate` (the plate sat inside
the headlight blaze, see `2026-09-06-camera-night-configuration.md`), two
`ocr_timeout` at 4.4-5.0 s, two `ocr_busy`, two `queue_coalesced`. The
presence session then took no extra frame for the rest of its 45 s window
because one of its own frames had been coalesced out of the burst queue and
the drop path never reported back (#88). The user drove through at 22:17:05
on the remote.

## Network measurements from the Pi

Plate Recognizer's own processing time on tonight's frames was 27-38 ms
(corpus sidecars). Everything else in `ocr_ms` is network.

| Measurement (from the Pi) | 22:21, degraded | 23:00, healthy |
| --- | --- | --- |
| Ping to the router 192.168.0.1 (avg) | 209 ms, 0% loss | 13 ms, 0% loss |
| Ping to 1.1.1.1 (avg) | 245 ms | 24 ms |
| TLS handshake to api.platerecognizer.com | 0.50-0.76 s (measured 13:12) | 0.21-0.26 s |
| First byte from api.platerecognizer.com (unauthenticated GET) | | 0.30-0.36 s |
| 220 KB upload to speed.cloudflare.com | | 0.44-0.57 s (about 3-4 Mbit/s) |

So a complete OCR round trip is about 1 s when the powerline hop between
the gate switch and the router is healthy, and 3-5 s when it degrades. The
degradation is intermittent (it was present at 22:16-22:21 and gone by
23:00). The camera-to-Pi hop is 0.3 ms and never involved.

## OCR attempt durations, 3-6 September (journal `ocr_attempts`)

| status | n | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- |
| no_plate | 156 | 1074 | 1672 | 2201 | 2910 | 4685 |
| recognized | 5 | 2415 | 2501 | 2687 | 2687 | 2687 |
| ocr_timeout | 29 | 3225 | 4411 | 5229 | 5977 | 5985 |
| ocr_busy | 15 | 0 | 0 | 0 | 0 | 0 |

(`ocr_error` 40 attempts carry no duration.) Attempts that complete do so
within 2.9 s at the 95th percentile; the only plate reads this month took
2.4-2.7 s. The 6 s decision timeout therefore rescued almost nothing: the
attempts that ran past 3 s were `no_plate` or never answered.

The only automatic open this weekend (2026-09-05 18:26:04) took 5.1 s end
to end: 2.5 s of OCR, 0.4 s capture to burst, 0.15 s decision to relay, and
about 2 s between the camera event and the frame being processed. With a
healthy network the same passage would have been about 3.5 s. Under 5 s is
achievable when the first frame reads; nothing in the controller can make
a second frame arrive faster than the first one failed.

Plate size is the reason the upload width stays at 1920: the four
`plate_box` lines since 3 September put the plate at 0.021-0.072 of the
1920x720 upload width (40-140 px wide, 16-41 px high). Shrinking the upload
to save 0.2 s of transfer would push the plate under Plate Recognizer's
usable size.

## Settings changed on the Pi (`/etc/gate-controller.env`)

| Variable | Was | Now | Basis |
| --- | --- | --- | --- |
| `GATE_DECISION_TIMEOUT_SECONDS` | 6 | 4 | p95 of completing attempts (2.9 s) plus ingress and capture overhead (about 0.5 s) plus margin |
| `GATE_PRESENCE_WINDOW_SECONDS` | 45 | 12 | one full retry after a timed-out first attempt: 2 x decision timeout + spacing, rounded up |
| `GATE_PRESENCE_SPACING_SECONDS` | 5 | 3 | code default; a frame every 3 s is enough on a 5 fps session |
| `GATE_PRESENCE_MAX_FRAMES` | 6 | 3 | bounds Plate Recognizer spend per passage (free tier: 2,500 lookups/month) |

Backups: `/etc/gate-controller.env.pre-presence-window-20260906T214126Z`
and `/etc/gate-controller.env.pre-decision-timeout-20260906T220622Z`. The
service was restarted after each change and came up with the webhook
listener on 8766.

None of these values is tuned to one night's timings; each is derived from
the measured distribution above or from another setting. Re-derive them if
the network changes (for example when the powerline bridge is replaced).

## Plate Recognizer spend

Every camera FTP still and every webhook-triggered frame is one lookup; the
empty-scene gate only filters session captures. Tonight's camera
reconfiguration (IR off, manual exposure) itself triggered 14 alarms
between 22:32 and 22:41, each costing a lookup on a black frame. The camera's
FTP and push schedules are being reduced to AI_VEHICLE and AI_CROSSLINE_1/2
only (see the camera note), and the vehicle AI sensitivity is 80.

## What to watch on the next real entry

- `gate_pipeline stage=processing_finished ... outcome=allowed` within about
  3.5 s of `filesystem_ingress` when the plate is readable.
- `ocr_attempts` durations: a healthy night is 1-1.5 s; if they sit at 3 s+
  the powerline is degraded again (check `ping 192.168.0.1` from the Pi).
- `presence_retry` after a `queue_coalesced` session frame (once #88 is
  live), and `presence_ended` no later than 12 s after `outcome=scheduled`.
- No `gate_trigger_capture outcome=scheduled` on an empty drive after the
  camera schedule change.
