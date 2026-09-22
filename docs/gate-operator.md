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
