# What the controller tells the app about each frame

Every frame the controller looks at becomes one event. The event is written to
the local SQLite `events` table, queued in the outbox, and posted to the
`access-gate-ui` ingest Worker, which validates it against
`worker/contracts/gate-event-ingest/contract.ts` and writes a row in D1. The
Logs page shows **one row per frame**, not one per passage, so a single car
arriving produces four to seven rows of which at most one took the decision.

The Worker answers **400 for any unknown key or out-of-range value**, and the
outbox retries a 400 forever. A payload the contract will not accept is not a
bad row in the app; it is a queue that never drains. `tests/test_event_contract.py`
transcribes the contract's rules and asserts the payloads the controller
actually produces pass them, so drift shows up as a failing test here rather
than as a stuck outbox on the Pi.

## The decision and the actuation are two different answers

The wire carries both, in separate fields, and they must not be conflated:

| Question | Field |
|---|---|
| Was the gate open for this vehicle? | `opened` — the app's Access Granted / Access Denied |
| Why? | `reason` — the match or refusal reason |
| Did **this event** work the relay? | `relay_activated_at`, and `telemetry.actuation` |

`telemetry.actuation` is `{claim, attempted, relay_outcome}`; every value is a
free token as far as the contract is concerned, so it can say exactly what
happened without a contract change.

**A plate matched during the relay cooldown is a grant.** The gate was already
open — the controller had pulsed the relay for the same car a second or two
earlier — so there is nothing to actuate, but the decision was
`granted / exact_match` at the confidence the reader gave it. It is recorded
that way:

```
opened               true
reason               exact_match          (the match reason, not "cooldown")
authorised_plate     131D2696
observed_plate       131D2696
ocr_confidence       0.999
relay_activated_at   null                 (this event did not pulse the relay)
telemetry.actuation  {claim: "cooldown", attempted: false,
                      relay_outcome: "not_attempted"}
```

Until 8 September 2026 `gate_controller/actuation.py` rewrote these events to
`opened=false, reason="cooldown"` while keeping the plate and the confidence.
Nine rows that day read "10-CE-1990 · Access Denied · 99.9%" in the app, for a
car that had just been let in. The journal had always said
`outcome=allowed reason=exact_match relay_outcome=not_attempted`; only the
stored and forwarded event disagreed with it.

The local `events` table carries one extra column the wire does not:
`actuation_outcome`, `"cooldown"` on exactly these records and NULL everywhere
else. It is not in `LocalStore._event_payload`, so it never reaches the
contract. It exists because `LocalStore._was_opened_since` — which decides the
cooldown itself — must count real relay pulses only. Without it, each coalesced
frame of a burst would slide the cooldown window forward and could suppress the
next vehicle's own grant.

## The reason table

What the app calls "Access Denied" is `opened=false`. Not all of it is a
refusal. Counts are from the 12 hours of 8 September 2026 (131 frames, 8
grants, 123 `opened=false`), and are illustrative of the mix, not a target.

| `reason` | Rows | Reader ran? | What it is |
|---|---|---|---|
| `exact_match` / `two_frame_ocr_confusion` | 8 | yes | granted; the relay pulsed |
| `exact_match` **during cooldown** | 9 | yes | **granted**; the gate was already open. Was recorded as `denied / cooldown` before this change |
| `no_match` | 80 | yes | a genuine no-read, or a correct refusal of a plate that is not authorised |
| `decision_timeout` | 19 | sometimes | the frame spent its whole decision budget queued behind the Plate Recognizer 1 req/s throttle |
| `queue_coalesced` | 10 | **no** | the controller declined to spend a cloud call on a car it had just let in. Also what the worker's `gate_burst stage=skipped cause=event_already_opened` records |
| `upload_incomplete` | 4 | **no** | the camera's FTP upload was truncated; there is no frame |
| `ocr_error` | 1 | attempted | the reader errored — an error, not a refusal |
| `stale_burst`, `processing_error`, `image_too_large`, `ocr_busy` | — | **no** | the frame was rejected before or instead of a read |

## Open defect: `ocr_confidence: 0` on frames no reader saw

A frame that never reached a reader has no score. The contract validates
`ocr_confidence` with `requireNumber(value, 0, 1)` — **not** `optionalNumber` —
so `null` is a 400 and so is omitting the key. The controller therefore has to
send `0`, and the app renders it as "0%". On 8 September that was 33 of the 123
`opened=false` rows (`queue_coalesced`, `upload_incomplete`, and the
`decision_timeout` frames that never got a slot).

**This cannot be fixed on the controller side.** The app has to move first, in
this order:

1. `worker/contracts/gate-event-ingest/contract.ts`: change `ocr_confidence` to
   `optionalNumber(event.ocr_confidence, 0, 1, 'ocr_confidence')` and make
   `ParsedGateEvent.ocrConfidence` `number | null`. Note that
   `gateEventPayloadFingerprint` includes `ocr_confidence`, so a resend that
   changes 0 to null is a different fingerprint, not a duplicate.
2. D1: make `gate_events.score` nullable; `score` is currently
   `Math.round(event.ocrConfidence * 10_000) / 100`.
3. The Logs page: render a null score as "—", not "0%".

Once the contract accepts it, the controller change is one line in
`gate_controller/processor.py`'s `_denied_event` and `record_skipped` — send
`None` when no OCR attempt was made. Until then, `telemetry.ocr_attempts == []`
is already on the wire and is the reliable signal that the zero is not a
measurement.
