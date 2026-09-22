# Which Way The Vehicle Was Going

Knowing whether a car is arriving or leaving is worth more than tidiness. A
departing car cost three billed cloud lookups on 2026-09-17 at 09:02, reading
its rear plate as `II011T`, `13D2696` and `13D2896` — none of which could ever
match. Roughly half of everything at this gate is a departure. And an access
log that cannot tell them apart is not an access log.

Direction is a **label**. Nothing described here reaches the relay, the admit
decision or the presence session, and nothing here can delay a gate.

## Where it stands, measured

Seven days to 2026-09-21, **78 passages**. A passage is events no more than
10 s apart; scored against the camera's own alarm identity, 10 s reproduces it
exactly (no alarm split in two, no passage holding two alarms).

| | resolved | rate |
| --- | --- | --- |
| live width fit (`box_width`), alone | 7 / 78 | 9.0% |
| photo verdict, best single frame | 56 / 78 | 71.8% |
| either of the two (the baseline) | 57 / 78 | 73.1% |
| **combined (`combined-v1`)** | **63 / 78** | **80.8%** |

Four of the 78 are remote commands with no vehicle in them at all, so of the 74
passages that had something to judge, 63 are resolved (85.1%). Against the
baseline: six gained, none lost, none flipped. Entering 36, exiting 27, unknown
15, conflicts 0.

By signal, over the same 78:

| signal | had an opinion | agreed with the verdict | was the only contributor |
| --- | --- | --- | --- |
| photo (`vision`) | 56 | 56 | 28 |
| the passage next door (`same_arrival`) | 22 | 22 | 6 |
| gate opened after the car was seen | 10 | 10 | 0 |
| gate opened from inside before it was seen | 9 | 9 | 0 |
| width fit (`box_width`) | 7 | **5** | 0 |

The gate's own timing only exists from 2026-09-17 11:00 UTC, when the recorder
began running continuously: 35 of the 78 passages. On those the combined
verdict resolves 33 of 35; on the 43 before it, 30.

**Checked by eye.** Photos of 45 of the 63 resolved passages were fetched back
and looked at — every passage resolved without a photo verdict, every passage
the neighbour rule spoke on, every passage where signals disagreed, every
departure the relay fired for, and the lowest-scoring photo verdicts. **All 45
agreed with the combined verdict** (28 arrivals, 17 departures). The eleven
unresolved vehicle passages were looked at too: nine arrivals and two
telehandlers, one each way.

The two passages where the width fit disagreed (2026-09-17 08:02 and 2026-09-19
18:35) are both departures the fit called `entering`: **the live estimator was
wrong on two of the seven passages it answered**, and has not said `exiting`
once in the week.

Reproduce it with `scripts/scan_direction.py --no-ship --days 8` against a copy
of the database; the per-passage workings land in `passage_directions`.

## What the photos show

The camera is on the gate post, a metre from the gate, looking out along the
approach.

* An **arriving** car comes up the approach toward the camera, **front** first,
  stops at the shut gate, and then drives *past* the lens to go through —
  flank, then a wheel arch a foot away.
* A **departing** car comes from **behind** the camera. It appears suddenly,
  huge and side-on, and then recedes up the approach showing its **rear**. It
  never shows its front.

An earlier version of this page said both directions *grow* toward the camera.
They do not: the photos show a departing car shrinking from its first frame.
That claim was made without looking at a frame, and it is why this page now
says where each statement came from.

## The signals

Implemented in `gate_controller/direction_signals.py`. Every one states a
verdict and what it is worth; `combine` fuses them; `judge` is the single place
a passage's evidence becomes a verdict. None of them knows a pixel — the zoom
is about to change, and every constant is a number of seconds or a weight.

### 1. The photo

`direction_vision` reads front or rear from one frame with CLIP
(`direction-clip-v1`). A passage has several frames and they do not always
agree, so the passage's opinion is the strongest reading on one side less the
strongest on the other, capped at 0.9.

One rule about order. Because an arriving car passes the lens, its passage
reads front, then flank, then "rear" — the model reads a close flank as a rear,
which is the right call for a departure and the wrong one here. A departing car
never shows a front at all. So once a confident front (≥ 0.5) has been seen,
later rear readings in the same passage are discounted to a quarter. Frames are
taken in the order they were received, which is not always the order they were
captured; when it is wrong the discount is simply not applied and the passage
comes out weaker, never reversed.

The crop stays the centre square. Letterboxing made the model call every
departure an arrival. **Reading three squares (left, centre, right) was tried
on 180 frames on 2026-09-21 and rejected**: the left square read `rear` at
0.5–0.8 on clear arrivals (09-19 08:26, 08:27, 14:41, 18:59) and rescued none
of the frames the centre square misses.

What it misses is systematic: a car stopped at the far end of the approach sits
in the top-left corner, outside the centre square, and reads `empty`. Every
photo-`unknown` car passage looked at was an **arrival**.

### 2. The gate's own timing

The camera cannot see inside the property, so a departing car cannot be seen
until the gate has already opened for it — and if we did not open it, somebody
inside did. An arriving car is the other way round: seen first, at a gate that
has been still, which then moves.

* **`gate_opened_from_inside` → exiting.** A movement that began at least 22 s
  before the first frame, was still running or had ended no more than 45 s
  before it, was not within reach of any relay pulse of ours, and did not belong
  to a vehicle in front. 0.6 when a clang vouches for the cycle; 0.15 when it is
  a motor-only run.
* **`gate_opened_after_seen` → entering.** Nothing moved before the first frame,
  the gate was being listened to, and a movement began within 10 s before to
  30 s after it — on our relay or on somebody's fob. 0.6 with a clang, 0.15
  without.
* **A run that began 10–22 s before the first frame says nothing.** A real
  opening from inside led the first frame by 24–37 s on nine of ten departures
  and by 19 s on the tenth. A car's own engine, which reads as the motor, led it
  by 1–6 s on eight arrivals and by **19 s** on two. In between, the two look
  the same.
* **A farm machine silences it.** On 2026-09-19 at 14:55 a telehandler
  *arriving* had a "gate movement" 45 s before its first frame, clang included:
  a diesel at walking pace is audible for most of a minute and reads as the
  motor throughout. When the photo model's top label is `machine`, the gate
  says nothing. Both telehandler passages that day are left `unknown`.

`gate_movements.uncommanded` is **not** used. It is only ever set on a run the
detector took for an *opening*, and because clangs are missed the detector
believes the gate is open for hours, so 278 of 322 non-clang runs carry
`uncommanded = 0` without having been commanded by anybody. Whether a movement
was ours is worked out here from the relay times in `events`.

### 3. Not the relay

This page used to say *"We fired the relay → the car is coming in. We only fire
for a plate read on the approach."* That is false (gate-controller#171). A
departing car shows its rear plate to the same camera, it is on the same
allow-list, and the relay fires for it. Over the week the relay fired in 24
passages and **five were departures**, each confirmed by eye (09-17 07:34,
09-18 13:47, 09-19 18:35, 09-20 10:47, 09-20 15:11); on the four that had audio
the gate had begun opening 26–77 s before the camera saw anything.

The relay is used only to decide which gate movements were ours. It is never a
verdict, and that signal was never in use: the module that held it was imported
by nothing but its own test.

### 4. The passage next door

An arriving car is seen twice: waiting at the shut gate, and again 24–32 s
later driving through it, side-on and too close for the photo to say anything.
All 22 passages this rule spoke on were looked at by eye and every one was an
arrival. So a passage within 60 s of one judged `entering` **on its own
evidence** is lent that verdict — at 0.6 of its confidence, never above 0.45,
and so never strongly enough to veto what was seen in the passage itself.
Arrivals only: a departing car does not stop in view.

### 5. The width fit

Kept as one vote, halved, capped at 0.45. Its thresholds are deliberately
untouched: the zoom is about to change, and a number re-fitted today would be a
fact about a mount that is about to stop existing.

### 6. Not sound

The hope was that an arriving car ramps in where a departing one starts cold.
Measured on 2026-09-21 over the eleven by-eye-labelled passages whose audio was
still on the card: the level left its floor 4–20 s before the first frame on six
arrivals and 5–27 s before it on five departures. The ranges overlap almost
entirely, because a departure is preceded by its own gate opening, which is
louder and earlier than any car. Onset lead is left out rather than given a
weight it has not earned. Separating the motor from the engine first might
rescue it; nobody has.

## How they are combined

Each side's support is `1 − ∏(1 − confidence)`, so signals that agree reinforce
each other and none can reach 1. The verdict's confidence is its side's support
less the other side's, capped at 0.95.

Two things make it `unknown` rather than a coin toss:

* **Both sides hold a strong signal (≥ 0.5).** That is a *conflict*: recorded as
  one, verdict `unknown`, confidence 0. A photo of a rear and a gate that says
  arrival is a fact worth counting, not a 0.15 lean.
* **What is left is under 0.2.**

```
verdict:    exiting   0.95   contributing: vision, gate_opened_from_inside
signals:
  vision                   exiting   0.90  rear at 0.99 over 5 frame(s)
  gate_opened_from_inside  exiting   0.60  the gate began moving 28s before anything was seen ...
  box_width                entering  0.00  slope fit at 0.01, halved: fitted on the previous camera
  same_arrival             unknown   0.00  the passage beside this one was not an arrival
```

## Where it runs

**After the fact, in `scripts/scan_direction.py`, on `gate-direction-scan.timer`
every ten minutes.** Not beside the relay, because that is not where the
evidence is: a photo is read after the passage, by fetching it back from the
dashboard, and the gate-sound scan writes a movement five to twenty minutes
after it happened. While a car is at the gate the only signal that exists is
the width fit, so there is nothing to combine.

Each pass:

1. reads the photo of every recent event **the Pi has already delivered** and
   not yet read (see below);
2. `direction_passages.judge_passages` groups events into passages, gathers
   each one's evidence from `events`, `event_telemetry`, `event_directions`,
   `gate_movements` and `gate_sound_scans`, and has `direction_signals.judge`
   decide — once on each passage's own evidence, once more with its neighbour's;
3. the result replaces `passage_directions` for the window, workings included;
4. every event of a passage whose verdict is new or has changed is posted to
   `POST /api/controller/directions`. All of them — including the frames that
   showed nothing by themselves, which is most of them.

Passages are judged **again on every pass**, because a movement can arrive after
the photo. A verdict is only re-sent when it changes or its score moves by 0.05.

This **does not keep frames**. The controller deletes each photo once the
dashboard has it, and the training corpus on this Pi holds audio, not frames;
an earlier version of this page said the corpus "already keeps every frame",
and it does not. Anything that needs a photo after the event fetches it from
`GET /api/controller/events/image`, as the scan does.

### Asking for a photo before it exists

Because the scan fetches the photo *back*, there is a photo to fetch only once
the Pi's own outbox has delivered the event. Asking earlier returns a 404 that
is about the outbox, not about the vehicle — and the scan used to write that
404 down as `no_image`, which then hid the event from the next pass for good.

**That is what happened under the router outage of 2026-09-21.** Event 3161 was
asked for 29 s after it happened, while its outbox item was still failing; it
was delivered on attempt 13, by which time the `no_image` row was permanent.
Events 3162–3168, 3177–3178, 3184 and 3188–3190 went the same way: no photo
verdict ever, so the combined verdict stayed `unknown`, arrivals included.

The rules now:

* **Delivered first.** A photo is asked for only when the event's own row in
  `outbox` has a `completed_at` — the one thing `complete_outbox_item` writes,
  and only after the ingest was acknowledged. One row per event
  (`outbox_one_per_event`), so this is one lookup. An event still in the queue
  is left alone with **no row written**, and the pass says how many are
  waiting: `stage=judged {... 'waiting': 13 ...}` is an outbox that is behind,
  not photos that are missing.
* **`no_image` is retryable.** A row that says `no_image` is asked for again on
  later passes, while the event is under two days old and has since been
  delivered. A photo that comes back replaces the row, and because every
  passage is judged again on every pass the verdict then ships like any other
  change. The rows the outage left behind need no migration and no hand-edited
  database: those events were delivered long ago and are inside the two days,
  so the first pass after this release picks them up — provided the release is
  on the Pi before 2026-09-23 17:59Z, when the oldest of them turns two days
  old and the rule stops offering them. They are a day old as this is written.
* **Two days, then the wait ends** (`UNDELIVERED_DAYS`). The outbox sets no
  deadline of its own — `OutboxWorker` backs off to a 300 s ceiling and then
  retries the same item for as long as the row exists — so the giving up is
  here. Two days is about 570 attempts at that ceiling, it is inside this
  scan's own three-day window so it happens on an ordinary timer pass rather
  than only under a backfill, and it is far longer than any outage measured
  here. The event is then recorded `unshippable` and left, which is also what
  becomes of an event that was never queued for the dashboard at all.
* **Bounded per pass.** `--limit` (120) is the cap on fetches, retries
  included, oldest first, with retries taking at most a quarter of it so a
  backlog cannot starve the events arriving now. A three-day outage cannot be
  followed by a fetch storm.

### Gate cycles nobody was seen at

A gate that opened, not on our command, and was confirmed shut by a clang, with
no camera event within two minutes: a car that left without ever being in view.
These are kept in `passage_directions` with `kind = 'gate_only'`, verdict
`exiting` at 0.4 — twelve of them in the four days of audio, about three a day.
Motor-only runs are not counted. **They are not sent**: there is no event to
hang them on and the dashboard has no row for them. They are also unverified —
by definition there is no photo.

## The dashboard contract

`POST /api/controller/directions` reads `event_id`, `idempotency_key`,
`direction` and `score` from each entry and ignores everything else, so the
entries sent today — those four plus `method: "combined-v1"` and `signals`, a
list of contributing method names — are accepted by the worker as deployed
(`access-gate-ui` `worker/routes/controller.ts`, `ingestDirections`). Nothing
new is sent through event ingest, which rejects unknown keys.

Two things need a worker change, specified here for a separate
`access-gate-ui` PR. The Pi side already tolerates the worker not having either.

1. **Retraction.** The worker skips `direction: "unknown"`, deliberately. But a
   passage can *become* unknown, when a late movement contradicts the photo.
   The Pi sends such entries in their own request as
   `{event_id, idempotency_key, direction: "unknown", score: 0, method, retract: true}`.
   Wanted: when `retract === true` and `direction === "unknown"`, set
   `direction = null, direction_score = null` for the matching keys, and add
   `retracted: <integer count>` to the response beside `updated` and `skipped`.
   Until the response carries `retracted`, the Pi treats the retraction as
   unsent and offers it again each pass while the passage is inside the window.
2. **Keeping the workings.** Optional: store `method` (string, ≤ 32) and
   `signals` (array of ≤ 8 strings, each ≤ 40) per event, so the dashboard can
   say *why*. Already being sent.
3. **Gate-only departures** need a row of their own —
   `{started_at, ended_at, direction, score, method}` keyed on `started_at` —
   and are not sent until there is somewhere to put them.

## What is still wrong

* **Eleven vehicle passages are unknown**, and nine of them are arrivals from
  09-14 to 09-16 that the centre crop missed, before there was any gate audio
  to fall back on. With audio, that shape is now caught (09-20 09:57).
* **The gate signal has barely been tested alone.** It agreed with the photo on
  all 18 passages where both spoke, and settled one the photo missed (09-20
  09:57, with the width fit agreeing, right by eye). But the other two passages
  it would have settled by itself were farm machines — one right, one wrong —
  which is why it is switched off for them. Its first solo verdicts on cars
  should be looked at.
* **`direction.source` is still journalled and not shipped**, and the journal
  keeps under two days.
* **The width thresholds are stale** and are left so until the zoom settles.
