# The gate operator, and how not to break it

The controller pulses a relay wired to a TOPENS swing-gate operator. This page
records what the operator actually does with a pulse, measured on the fitted
gate, and the incident on 2026-09-21 in which an automated test left the gate
jammed for most of a day. Every rule in `CLAUDE.md` about the relay comes from
here.

## The gate

Two swing leaves. They must close in a fixed order so that the road-side leaf
overlaps the other on the correct side; the operator keeps that order by
delaying one leaf. The relay (`gate_controller/relay.py`, RELAY1, 2.0 s dry
contact) is on the operator's step-by-step push-button input, the same input
the fobs and keypad use. The camera faces the approach, not the leaves, so the
controller can only infer gate state from sound (`gate_audio_detect.py`).

## What a pulse does — measured 2026-09-21 on clean cycles with no vehicle

| Gate state at the pulse | What happened | Evidence |
| --- | --- | --- |
| Shut | Motor starts +1.3 s; opening run 18.9-21.5 s | steps 2 and 3 |
| Fully open, holding | Closing starts **1.2 s later** and latches: pulse = close now | step 2 |
| 9 s into closing | Motor **stops dead** 0.3 s later; ~17 s later the gate resumed by itself and closed with a latch | step 3 |
| Stopped mid-travel, then pulsed again | Reverses; the leaves are now out of sequence | recovery pulses |

Undisturbed cycle: opening ~20.5 s, hold 16 s (sometimes 25-27 s), closing
~23 s, latch clang at about +70 s after the opening pulse. Earlier audio of two
real departures (issue #171) had already shown "pulse while open = close now".

## The incident, 2026-09-21

An audio-following test harness was run remotely to establish the table above.
Steps 2 and 3 produced their measurements. Step 3's deliberate mid-travel stop
was followed by the harness's own "recovery" logic: it did not trust the latch
sound, decided the gate was "not provably shut", and sent **three further
pulses** at 45 s intervals, each reversing leaves that were already out of
sequence. The closing at 08:10:40 IST ran a full 21 s but produced **no latch
clang** — that is the moment the road-side leaf closed on the wrong side of the
other. Every pulse after that (08:12, 08:22, 10:12 IST) gave a loud bang and no
travel: the motor pushing against a crossed leaf. Arrivals could not open the
gate until a person freed the leaves by hand at about 18:00 IST; the first
normal cycle after that was 18:59 IST. The controller's diagnosis at the time
("thermal or duty-cycle cut-out") was wrong.

The gate failed shut, not open. That is the only thing that went right.

## The second incident, 2026-10-10

The same failure from the plate reader, with no test involved. An authorised
pickup (`172L66`) waited about nine minutes at the gate, 10:04-10:13 IST. The
camera raised seven vehicle alarms (10:04:11, 10:05:38, 10:06:44, 10:07:34,
10:08:14, 10:12:36, 10:13:01) and the Pi pulsed the relay **four times for
the one car**: 10:04:13.9 (a sweep frame), 10:06:45.8 (the next alarm's sweep
frame), 10:08:18.0 (the camera's 4K still of the 10:08:14 alarm, 92 s after
the previous pulse -- the sweep frame of the same alarm had been refused by
the cooldown at +89.6 s, and the still 2.4 s later passed) and 10:12:37.0 (a
sweep frame). Every read between was refused only by the 90 s cooldown; its
expiry was a clean slate, every alarm started a new passage, and nothing
carried "this plate was already let in and is still here" from one to the
next. Each later pulse landed on a gate whose state nobody knew; the leaves
crossed and the gate jammed. The full review is
[reviews/2026-10-10-gate-jam.md](reviews/2026-10-10-gate-jam.md).

The fix is rule 7 below and [invariant 12](invariants.md): one car, one
automatic pulse, however long it waits and however many alarms it raises --
and a pause switch for automatic opening. Neither adds a pulse; both only
withhold one.

## Rules

1. A pulse is safe only into a gate that is provably shut and still: a
   full-length closing run ended with a latch clang, silence followed, or a
   person has looked. A closing run **without** a clang means the leaves may
   have crossed; stop and get someone to look.
2. Never pulse a moving gate except in a supervised test with a person at the
   gate who will free the leaves by hand.
3. No automatic recovery pulses, ever. Unknown state → stop, listen, and
   report; the operator's auto-close will finish an interrupted closing on its
   own if the leaves are in order, and nothing the controller can send will
   fix them if they are not.
4. At most two full cycles per half hour from any test, one pulse per cycle.
5. Production cooldowns are the guard against the same failure from plate
   reads: automatic actuations wait `GATE_ACTUATION_COOLDOWN_SECONDS` (90 s,
   longer than a full cycle; a waiting car used to be re-pulsed at +29 s, i.e.
   while fully open) and app commands wait `GATE_COMMAND_COOLDOWN_SECONDS`
   (20 s). Neither is to be lowered for a test.
6. The proper fix for pulses landing on an open or moving gate is hardware: if
   the operator board has an open-only input (TOPENS `OPSW`+`COM`), move the
   relay to it. That input cannot stop or close the gate. Until then, the
   direction work (#171) must keep departing cars from pulsing at all.
7. One car, one automatic pulse. Once the relay has pulsed for a plate, no
   plate read pulses for it again until that plate has been out of the record
   for `GATE_REPULSE_UNSEEN_MINUTES` (10 min), whatever the cooldown says: a
   car waiting through alarm after alarm gets exactly one pulse. The refused
   grants are recorded (`actuation_outcome = "repulse_hold"`) and journalled.
   `GATE_AUTOMATIC_OPEN=off` pauses automatic opening altogether. Neither
   touches a person's command from the app. Neither is to be lowered or
   switched off for a test.

## Reading the gate from sound

The continuous recorder (`audio_segments.py`) keeps 300 s AAC segments; the
1.2-3 kHz band level rises ~25 dB over the floor during a motor run. Passing
road traffic gives 4-12 s runs with almost no high-frequency share about once
a minute by day; birdsong gives impulses with a high-frequency share above
0.9; a real latch clang sits at 0.4-0.8. The production scanner
(`gate_sound_scan.py`, yamnet-linear-v2) under-reports run length (it caught
8 s of a 19 s opening) and must not be used to arbitrate gate state during a
test. Full-scale bangs at the moment of a pulse, with no run after, mean the
motor is stalling against something.

Two things mask the motor, and both were found on 2026-10-10
([reviews/2026-10-10-gate-jam.md](reviews/2026-10-10-gate-jam.md)):

- **The camera's own vehicle audio alarm**, which played for 9-15 s from
  0.2-1.9 s after every vehicle alarm and clipped the microphone. It is about
  35 dB louder than the motor, and the scanner recorded it as motor runs. It
  was switched off on 2026-10-10 at 15:27 IST
  ([reolink-rlc-811a.md](reolink-rlc-811a.md#vehicle-audio-alarm-switched-off-2026-10-10)).
  Keep it off.
- **A diesel idling beside the camera** sits at the motor's own 1.2-3 kHz
  level (about -47 dBFS), so a waiting car hides the gate. The motor's
  fingerprint is a line at 1.1-1.5 kHz. On a quiet night it is plain: four
  runs and a latch were heard on 10 Oct 23:24-01:32 that the scanner missed.
