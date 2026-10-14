# Gate may have been left open

On 2026-10-09 the gate was thought to have stood open for 39 minutes with
nobody knowing; the audio re-read on 2026-10-10 cannot confirm it. This page
covers the check that now says so, the measurements behind its rule, and what
it cannot do.

**It only notifies.** Nothing in it, and nothing that reads its output, sends
`open_gate` or touches the relay. There is no automatic close, no retry and no
"recovery" pulse (CLAUDE.md rules 1-3, [invariant 11](invariants.md)). The
only closing action is a person pressing **Close gate** in the app's alert
banner. That uses the app's existing gate command, with its `operator` role
check and its 20 s command cooldown. On this gate one pulse closes a gate that
is fully open and **stops one that is moving**, and the button says so.

## What happened on 2026-10-09

As the scanner's `gate_movements` rows first read it, times in IST. This
reading is **superseded**; see the correction below the table.

| Time | Run | Reading |
| --- | --- | --- |
| 21:41:38 | 17 s | The operator's exit loop (`OPSW`) opens for a departing car. The Pi sent no pulse (`relay_outcome=not_attempted`) and the app sent no command. |
| 21:42:08 | 9 s | The auto-close starts. |
| 21:42:23 | 11 s | The car breaks the photocell beam and the gate reverses open. |
| 21:42:34 to 22:21:47 | (none) | No motor run and no latch for 39 minutes. |
| 22:21:47 | 12 s + 11 s | The owner drives in through the open gate, and it closes. No latch was heard. |

**Re-read on 2026-10-10 at 0.5 s resolution, this table is not what the gate
did** ([reviews/2026-10-10-gate-jam.md](reviews/2026-10-10-gate-jam.md)):

- The 21:42:23 and 22:22:00 "runs" are the camera's own vehicle audio alarm,
  which played through its speaker from 0.2-0.4 s after each camera alarm and
  clipped the microphone.
- The 21:41:38 and 22:21:47 runs are car engines: band +10 to +30 dB, almost
  no 1.2-8 kHz share.
- No motor-band run is visible in either burst. So "reversed by the
  photocell, then open for 39 minutes" is not supported by the audio. It may
  still be what happened; the audio cannot say.
- Overnight, motor-like runs at 23:24, 00:25 and 00:26, and a closing run with
  a latch at 01:32, were heard; the scanner wrote none of them.

The alarm sound was switched off on 2026-10-10 at 15:27 IST. Since 2026-10-14
every reader of `gate_movements`, this check included, sets aside the rows
that were that sound, and the scanner no longer writes a car's engine as a
motor run ([below](#replayed-again-2026-10-14)).

## The measurement: what the detector can and cannot see

These figures come from every row the retrained detector (`yamnet-linear-v2`)
wrote between 2026-09-21 and 2026-10-09. That is 198 runs over a scanned span
of 457 h, of which 320 h (70 %) was actually heard. The 45 relay firings in
`events` serve as ground truth. Everything was read on the Pi read-only and
analysed off the board.

**A relay-commanded cycle, whose whole cycle was recorded (22 of 45):**

| Runs heard | Cycles |
| ---: | ---: |
| 0 | 3 |
| 1 | 5 |
| 2 | 10 |
| 3 | 4 |
| Latch heard | 10 of 22 (45 %) |

**After the first run of a burst, which is an opening (122 bursts):**

| Within | Another run | A latch | Either |
| --- | ---: | ---: | ---: |
| 3 min | 42 % | 39 % | 54 % |
| 5 min | 45 % | 41 % | 57 % |
| 10 min | 49 % | 43 % | 61 % |

So **the latch cannot be used to say "shut"**: it is heard on fewer than half
of the closings that certainly happened. A missing closing run cannot be used
to say "open" either, because over a third of real cycles produce one run or
none. The recorder also loses 30 % of the wall clock. The heartbeat's `state:
"open"` already reflects this and has been "open" for most nights. The app has
said "not confirmed shut" rather than "open" after ten minutes for exactly this
reason.

## The rule, and its replay over history

These rules were replayed over the same rows at thresholds of 5 to 30
minutes, with the threshold required to be at least 80 % *heard*:

| Rule | Fires per week at 10 min | 2026-10-09 |
| --- | ---: | --- |
| Last movement had no latch, then N min silence | 8.1 | fired |
| Odd number of runs in the burst, no latch | 5.9 | fired |
| **Three or more runs, odd, no latch on the last** | **1.5** | **fired** |

The third rule fired four times over the 2.72 weeks, at every threshold from
5 to 30 minutes:

| Last heard (IST) | Runs | Silence after | What it was |
| --- | --- | --- | --- |
| 2026-09-21 08:07 | 9, 15, 8 s | 9.9 h | The remote test that crossed the leaves ([gate-operator.md](gate-operator.md)). The gate was not shut properly, and someone needed to look. |
| 2026-10-04 16:51 | 23, 8, 32 s | 10.5 h | Unverified. |
| 2026-10-08 17:30 | 25, 11, 35 s | 1.9 h | Unverified. |
| 2026-10-09 21:42 | 17, 9, 11 s | 39 min | Unverified. Re-read on 2026-10-10: the 11 s run is the camera's alarm sound and the 17 s run a car engine; no motor-band run is visible. |

Only one of the four (2026-09-21) is known to be real, so **up to three of
four may be false: at most about 1.1 false alerts a week** over the 2.72 weeks.
An earlier version of this page counted 2026-10-09 as real and gave 0.7 a week.
Until 2026-10-10 15:27 the camera's own alarm sound was recorded as a motor
run at every vehicle alarm, so any of these bursts may contain one; treat the
rule as unvalidated until it has been replayed on audio recorded with the sound
off. Merging runs that are less than 5 s apart, to remove the detector's split
runs, leaves only the 2026-09-21 and 2026-10-09 bursts. It is not done,
because a fast reversal and a split run can both leave a gap of a second or
two, and merging would hide the reversal the rule depends on.

Physically, an odd burst of three is "open, close, reverse open" with no
closing after it. That is an auto-close interrupted by the photocell that
never ran again. An even burst ends where it started.

### Replayed again, 2026-10-14

The same rule, at the shipped 10-minute threshold, over the same history
(rows from 2026-09-21 to the moment the alarm sound went off), one change at
a time:

| Rows the rule reads | `likely` | Which |
| --- | ---: | --- |
| As the scanner wrote them | 4 | 09-21 08:07, 10-04 16:51, 10-08 17:30, 10-09 21:42 |
| The camera's alarm sound set aside | 3 | 09-21 08:07, 10-04 16:51, **10-05 21:21** |
| ...and runs the scan split at a whole minute joined | **1** | **09-21 08:07** |
| ...and car engines left out, where the audio was available | 1 | 09-21 08:07 |

What happened to each:

* **2026-09-21 08:07** -- the remote test that crossed the leaves, the one
  firing known to be real. All three runs are the motor on the reference
  audio, and it stays `likely` at every step.
* **2026-10-08 17:30** -- its first run started within 2 s of a camera alarm.
  Without it the burst is two runs: `possible`.
* **2026-10-09 21:42** -- the third run is the alarm sound. Two runs remain,
  `possible`; on the audio both are car engines, so with the engine check
  there is no row left at all.
* **2026-10-04 16:51** -- 23 s, then a 40 s run the scan cut in two at
  16:51:00 (8 s + 32 s). Joined, it is two runs: `possible`.
* **2026-10-05 21:21** -- new at the second step. Its silence used to be
  ended by the alarm sound of the next car at 21:31; without that row it
  reached ten minutes. Its first two rows are one run the scan cut at
  21:21:00: it starts 10.6 s *before* the relay fired, so it is the arriving
  car's engine running into the opening. The third is the closing, with no
  latch heard. Joined, it is two runs: `possible`.

So over these 2.72 weeks the rule fired once, and that once was real. At 5,
20 and 30 minutes the second step gives 3, 2 and 2; the joined rows give 1 at
every threshold. Seventeen of the 291 rows yamnet-linear-v2 wrote are the
second half of a run split at a whole minute, and the scanner now joins them
([gate-audio.md 2i](gate-audio.md#2i-engines-the-alarm-sound-and-split-runs)).

How far to trust this: the third and fourth rows are an approximation of
what the new scanner would have written. Rows were joined where one ended
within a second of a whole minute inside a segment and the next began on it,
and the engine check was applied only to the bursts whose audio is still
held (2026-09-21, the night of 9-10 Oct, and 12-14 Oct); the rest of the
history is in R2 and was not re-scanned. Nothing in the database was
rewritten: the rows written before this release keep their splits and their
engine runs, and only the alarm-sound rows are set aside when they are read.

From the sound going off to 2026-10-14 16:00 IST the rule fires twice on the
rows as written (2026-10-10 17:18, five runs, and 20:27, three) and once with
the splits joined (17:18). Neither has its audio left on the board, so
neither is verified.

The heartbeat's `gate.left_open` block reports two levels of confidence:

* **`likely`** means the rule above fired. The app pushes it.
* **`possible`** means the last movement was not confirmed shut and the gate
  has been quiet for N minutes since. This is the ordinary missed-latch case,
  about 6 a week. The app shows it and does not push it.

## Running it

The check is `gate_controller/gate_left_open.py`. It runs on the heartbeat
thread, inside `gate_state()`, using the heartbeat's read-only connection. It
reads at most 12 movement rows and 500 listening rows, then returns a
dictionary. It is not on the decision path, and if it fails the block reads
`unknown`. It never removes the heartbeat.

| Field | Meaning |
| --- | --- |
| `state` | `open`, `clear` (latched, or not yet long enough), `unknown` (not listening, or nothing heard) or `off` |
| `confidence` | `likely` or `possible`, only when `state` is `open` |
| `since` | When the gate was last heard moving. This is the episode key, and a new movement changes it. |
| `burst_started_at`, `runs` | The burst the last movement belongs to |
| `quiet_seconds`, `heard_seconds` | Silence the scan has reached, and how much of the threshold was audio |
| `threshold_minutes`, `config_source` | The threshold in force, and whether it came from `app`, `environment` or `default` |

**Settings.** The app's Settings page (admin only) writes a `gate_left_open`
object, `{"enabled": bool, "threshold_minutes": 3..240}`, into the settings
envelope the controller already polls. `settings_version` stays at 1, so a
controller older than this one ignores the object. A malformed object is
ignored and never touches the plate-matching schedule that shares the
envelope (`test_a_malformed_setting_changes_nothing_about_plate_matching`).
Without app settings the board uses `GATE_LEFT_OPEN_ALERT_ENABLED` (default
`true`) and `GATE_LEFT_OPEN_MINUTES` (default 10).

**Latency.** The scanner runs every 15 minutes and skips the segment still
being written. A movement therefore reaches `gate_movements` 5 to 20 minutes
after it happens. The silence after it only counts once a scan has reached
past the threshold, and the Worker's cron adds up to 5 minutes. With the
10-minute default, 2026-10-09 would have reached the scan at 22:06 and pushed
by about 22:11, ten minutes before the owner arrived at 22:21.

## What it cannot do

* **A gate opened once and never closed, with nothing else heard, is not
  pushed.** It looks exactly like an ordinary cycle whose closing the detector
  missed, which is about a third of them. It is reported as `possible`.
  Telling the two apart needs a better closing detector or a position sensor
  on the leaves, not a threshold.
* **It is deaf when the recorder is.** In 41 of the quiet stretches in history
  less than 80 % of the threshold was heard, and the check says `unknown`
  rather than guessing.
* **Its times run early inside a short segment**, by however much audio the
  camera dropped before the run (gate-audio.md 2g).
* **It reads what the scanner writes.** Until 2026-10-10 15:27 every vehicle
  alarm put the camera's own alarm sound into `gate_movements` as a 9-15 s
  "run". Those rows are set aside when they are read: a row that starts
  before the sound went off and within 2 s of a camera vehicle alarm is not
  the gate (`gate_controller/gate_alarm_sound.py`). That is the only rule
  about alarms: after 15:27 the minutes after an alarm are exactly when the
  real motor runs, and nothing is set aside then. Car engines were also
  written as runs; since 2026-10-14 the scanner checks every run for the
  motor's band and does not write one without it (gate-audio.md 2i). What no
  check can do is hear the motor *under* an engine: an idling diesel beside
  the camera sits at the motor's own level, so an opening for a car that
  waits with its engine running is usually not heard at all, and only its
  closing is.
* **Of the four alerts above, only 2026-09-21 is known to be real**, and it
  is the only one that survives the 2026-10-14 replay. The replay is two
  and a half weeks, and the second half of the table rests on audio for only
  part of it; a season of rows written by the new scanner is what would
  validate the rule.

## The operator's settings

No TOPENS manual or installation record is in the owner's Google Drive. The
public manual for the TOPENS AD3S/AD5S/AD8S dual swing opener has the same
terminal block as this gate (`PHOTO`, `O/S/C`, `OPSW`, `EDGE`). Nobody has yet
checked the fitted board's model. That manual says:

* **Auto-close** is DIP switch 2, off by default. Its pause time is set by
  potentiometer B, from 3 to 120 s.
* **The `O/S/C` (step-by-step) input** steps through open, stop, close, stop,
  open. The manual does not say whether auto-close runs after a stop.
* **Two consecutive photocell blocks stop the gate.** The CODE LED flashes
  quickly for 10 s. The manual does not say that auto-close resumes after
  that.
* TOPENS's own guidance is that **blocking the photocell while the gate is
  open keeps it open**.
* There is no "close immediately after the photocell clears" function.

Whether this happened on 2026-10-09 is unknown: the scanner rows that seemed
to show a reversal are the camera's alarm sound and car engines. The manual's
behaviour still means a gate *can* be left open by a car lingering in the
beam, if the fitted board's auto-close is not re-armed after a photocell
reversal or it stops on a second consecutive block. Someone at the gate needs to read the board's model and DIP
switches to confirm which. That is a hardware question, and no software here
can answer it.
