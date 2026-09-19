# Which Way The Vehicle Was Going

Knowing whether a car is arriving or leaving is worth more than tidiness. A
departing car cost three billed cloud lookups on 2026-09-17 at 09:02, reading
its rear plate as `II011T`, `13D2696` and `13D2896` — none of which could ever
match. Roughly half of everything at this gate is a departure. And an access
log that cannot tell them apart is not an access log.

## What is there now, measured

`gate_controller.direction` fits the least-squares slope of `log(box width)`
against time. Over **991 passages with telemetry** it has produced:

| Verdict | Passages |
| --- | --- |
| no estimate at all | 671 |
| `unknown` | 302 |
| `entering` | 9 |
| `exiting` | 8 |
| `stationary` | 1 |

**It answers 1.8% of the time.** The gate is three boxed frames spanning two
seconds, and a passage here is one to three events — the 11:08 arrival on
2026-09-17 reached it with one frame and came back `unknown`.

And when it does answer it can be wrong. The 09:02 departure read `entering`
at 0.26 on a slope of **+0.135**, above the top of the +0.010..+0.098 band the
rule was fitted on.

### Why width stopped working

The camera sits about a metre from the gate, facing the approach. A car
arriving drives toward it from the road. A car leaving drives toward it from
inside the property, and past it. **Both grow.**

On the RLC-810A, aimed further out along the approach, a departing car was
already past the lens and receding — which is why exiting measured
−0.663..−0.093 there and does not here. The rule did not degrade; the geometry
it encodes was replaced.

That is the general lesson for this file: a threshold fitted to one mount is a
fact about that mount. Re-aiming the camera, which is pending, will invalidate
any number re-derived today.

## The signals actually available

### 1. The gate itself — causal, free, strongest

`gate_movements` now records when the gate moved, for how long, whether the
leaves met, and whether anybody commanded it.

* **We fired the relay** → the car is coming in. We only fire for a plate read
  on the approach.
* **The gate was already moving before the camera saw anything, uncommanded** →
  somebody opened it from inside and is driving out. This is why a departing
  car is seen late and from behind: it comes from where the lens cannot see.
* **The gate moved after the car was seen** → leans arriving, but weakly: an
  exit noticed early looks the same.

No model, no training, no geometry. Implemented in
`gate_controller.direction_signals`.

### 2. A gate that opened with nobody in frame

The strongest case of all, and the one the current design cannot even
represent: the gate moves, no camera event occurs at all. The car was never in
view because it came from behind the camera. That is a **departure that leaves
no passage**, so the access log has no row to classify — and the count of
passages is wrong by however many of these there are.

This needs a passage that begins life as a gate movement rather than as a
camera event. It is the largest change here and the one that most improves the
denominator.

### 3. Where the car crosses the frame

An arrival enters from the road side; a departure from the property side.
`DirectionTracker` was handed the whole box on every frame and kept only the
width, so this was thrown away on every passage ever recorded. It is kept now
(`DirectionTracker.track`) and **recorded only** — which side is which is a
fact about this mount that nobody has measured, and a sign guessed today would
be a second rule fitted to no data.

The 09:02 departure ran 0.915 → 0.553 → 0.478: right to left. One sample.

### 4. Front or back of the car — vision

An arriving car shows its front; a departing car shows its rear. This is the
signal a person uses instantly, it is trivially learnable, and crucially it
works **on a single frame** — which dissolves the "three frames over two
seconds" gate that silences the current estimator on 98% of passages.

It needs labelled frames. Signal 1 provides them free, at roughly ten a day,
and the corpus already keeps every frame.

### 5. The approach, in sound

Measured 2026-09-17: an arriving car is audible for about thirteen seconds,
leaving the −52.9 dBFS floor about twelve seconds before the camera fires and
reading on the vehicle classes about five seconds before.

A departing car has no such ramp. It starts inside the property, close to the
microphone, and is loud immediately. **The shape of the approach is the
discriminator**, not the loudness: a long rise from the road versus a cold
start at short range.

Two cautions from the same measurement: the microphone saturates from about a
second out, so the usable signal is the approach rather than the arrival; and
the gate motor scores as a vehicle, so the gate's own movement has to be
subtracted before anything is read from engine sound.

This is also the only signal that arrives **before** the camera event, so it is
the one that could ever make direction *fast* rather than merely correct.

## How they should fit together

One verdict, every signal's own opinion kept beside it:

```
verdict:    exiting
confidence: 0.59
signals:
  gate_already_moving  exiting   0.85  gate moving 20s before the camera, uncommanded
  box_width            entering  0.13  slope fit at 0.26, halved: fitted on the previous camera
```

Confidence is the strongest supporting signal less the strongest opposing one,
so disagreement produces a weak answer rather than a loud one — and the
disagreement stays on the record, which is what lets the signals be scored
against each other over a season instead of argued about.

The width fit is kept as a vote and halved. It worked completely on the old
aim and may again after re-aiming; it is not trusted to decide alone here.

## The loop that makes it improve

1. Signal 1 labels passages for free, from the relay and the gate's own sound.
2. Those labels train signals 4 and 5, on frames and audio the corpus already
   keeps.
3. Every signal's opinion is recorded per passage, so each can be scored
   against the others and against human review.
4. A signal that stops agreeing — after a camera is re-aimed, say — shows up as
   a disagreement rate rather than as a silent wrong answer.

That last point is the thing missing today. The width rule did not announce
that it had stopped working; it was found by hand, six days after the camera
was changed, because a departing car appeared in an access log.

## Order of work

1. **Retain the geometry.** Done — nothing can be re-fitted from a history that
   was not kept.
2. **Signal 1, and record every opinion.** Done as a module; not yet joined to
   the live path or backfilled over history.
3. **Gate movements with no passage** — the departures nothing currently counts.
4. **Front-or-back from a single frame**, trained on signal 1's labels.
5. **The audio approach ramp**, which is what makes it fast.
6. **Re-derive the width bands** after the camera is re-aimed, or retire the
   signal if the others carry it.
