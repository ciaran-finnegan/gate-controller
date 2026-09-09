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

A cooldown grant is still a grant, so it has to clear the same bar a pulse
would have. `ActuationCoordinator` runs `pre_activation_inhibit` — the same
callable the relay runs under its lock immediately before the GPIO write —
*before* either cooldown short-circuit. If the frame went stale, the plate was
withdrawn from the authorised list, the decision deadline passed, or the
processor closed between the match and the actuation, the event is recorded as
`opened=false` under that reason (`stale_burst`, `authorisation_revoked`,
`decision_timeout`, `processor_closed`) rather than as a cooldown grant. Only an
un-inhibited match in cooldown gets the row above.

The local `events` table carries one extra column the wire does not:
`actuation_outcome`, `"cooldown"` on exactly these records and NULL everywhere
else. It is not in `LocalStore._event_payload`, so it never reaches the
contract. It exists because `LocalStore._was_opened_since` — which decides the
cooldown itself — must count real relay pulses only. Without it, each coalesced
frame of a burst would slide the cooldown window forward and could suppress the
next vehicle's own grant.

### Runbook: rolling back past this release

The `actuation_outcome` column is added by `ALTER TABLE` on open and is left
in place by a downgrade, but the older code does not read it. Its
`_was_opened_since` counts any `opened = 1` row with a null `relay_activated_at`
and a `received_at` inside the window as a relay pulse — which is exactly the
shape of a cooldown grant this release writes.

So after rolling back to a release before this one, **every cooldown row still
inside the window reads as a pulse**: for up to one cooldown window (~20 s, the
`cooldown` given to `GateProcessor` / `ActuationCoordinator`) after the last
such row's `received_at`, a claim that should have been granted can come back
`cooldown` instead. In practice that is
the tail of one burst suppressing the front of the next, so a vehicle arriving
within those 20 seconds may need a second passage or a remote open. It clears
itself once the newest cooldown row falls out of the window; nothing needs to be
deleted. To end it immediately, blank the rows the old code misreads:

```sql
UPDATE events SET opened = 0 WHERE actuation_outcome = 'cooldown';
```

That restores the pre-release recording of those rows (`opened = 0`), which is
the denial this change exists to stop writing — acceptable only as part of a
rollback, and only for rows already sent.

## The reason table

Every recorded reason, and whether it is a refusal at all. The app paints
`opened=false` as "Access Denied", and most of what lands there is not a
refusal. Counts are the 12 hours of 8 September 2026 (131 frames), illustrative
of the mix, not a target; the "before" column is how that day's rows were
recorded, and the nine cooldown rows are the ones this change moves.

| `reason` | Rows | Reader ran? | What it is |
|---|---|---|---|
| `exact_match` / `two_frame_ocr_confusion` | 8 | yes | granted; the relay pulsed |
| `exact_match` **during cooldown** | 9 | yes | **granted**; the gate was already open, so the relay was not pulsed again. Recorded as `denied / cooldown` before this change |
| `no_match` | 80 | yes | a genuine no-read, or a correct refusal of a plate that is not authorised |
| `decision_timeout` | 19 | sometimes | the frame spent its whole decision budget queued behind the Plate Recognizer 1 req/s throttle |
| `queue_coalesced` | 10 | **no** | the controller declined to spend a cloud call on a car it had just let in. Also what the worker's `gate_burst stage=skipped cause=event_already_opened` records |
| `upload_incomplete` | 4 | **no** | the camera's FTP upload was truncated; there is no frame |
| `ocr_error` | 1 | attempted | the reader errored — an error, not a refusal |
| `stale_burst`, `processing_error`, `image_too_large`, `ocr_busy` | — | **no** | the frame was rejected before or instead of a read |

## No reader, no score: `ocr_confidence: null`

A frame that never reached a reader has no score, and since 9 September 2026 it
says so. The contract validates `ocr_confidence` with
`optionalNumber(event.ocr_confidence, 0, 1, 'ocr_confidence')`
(`access-gate-ui` #53, deployed): `null` and a missing key both store no score,
and the Logs page renders that as "—". Anything present and non-null is still
`requireNumber(0, 1)`, so a `NaN` or a `1.5` is a 400 exactly as before.

Until then the controller had to send `0`, which the app rendered as "0%". On 8
September that was 33 of the 123 `opened=false` rows — `queue_coalesced`,
`upload_incomplete`, and the `decision_timeout` frames that never got a slot.

**What the controller sends now.** A frame that produced no observation from
either reader carries `null`; a frame a reader measured carries the real score,
granted or denied:

| Event | `ocr_confidence` |
|---|---|
| `queue_coalesced`, `upload_incomplete`, `ocr_busy`, `stale_burst` before a read | `null` |
| `decision_timeout` with no read behind it | `null` |
| `decision_timeout` after a read that returned a plate | that read's score |
| `no_match` on a plate that was read | that read's score |
| `no_match` where every attempt came back without a plate | `null` |
| `exact_match`, fuzzy grants, and grants skipped by cooldown | that read's score |

The test is the plate, not the reason: `_measured_confidence` in
`gate_controller/processor.py` reports `MatchDecision.confidence` only when the
decision names an `observed_plate`, because that is the one decision shape a
reader is behind. `MatchDecision.confidence` defaults to `0.0`, and a bare
`no_match` — the verdict on a burst whose every attempt came back empty — is
the only decision that carries that default with no plate. A genuinely measured
`0.0` is still sent as `0.0`.

`telemetry.ocr_attempts` remains the fuller account: `[]` for a frame no reader
ran on, and an `ocr_timeout` or `ocr_error` entry for one where a reader ran and
came back with nothing.

Locally, `events.ocr_confidence` was created `NOT NULL DEFAULT 0`. SQLite cannot
relax a column constraint in place, so `LocalStore._relax_ocr_confidence`
rebuilds the table once — copying every row and score across and recreating the
two `events` indexes — guarded by `PRAGMA table_info`. Note that
`gateEventPayloadFingerprint` includes `ocr_confidence`, so an outbox row
written as `0` before this change and resent after it is a different
fingerprint, not a duplicate; nothing rewrites already-queued payloads.
