# Detection → OCR → opening: where the time goes, and what would make it faster and surer

Data: every passage from 5 Oct 17:00 IST (after the refocus) to 10 Oct 07:30
IST.

- **D1:** `gate_events` / `gate_event_telemetry`, 244 events in 34 passages to 9 Oct 12:35.
- **D1 `controller_health`:** 5-minute buckets, 26 Sep - 9 Oct.
- **The Pi's journal:** 8 Oct 09:00 - 10 Oct 07:37, 23 sweep sessions.
- **`early-trigger.db`:** read with `scripts/early_trigger_report.py`.
- **`gate_movements`:** the audio scanner.
- **Photos:** R2 copies (1280×720, lower quality than the Pi's readers see), plus 4K stills taken from `/camera/snap` after four alarms on 9 Oct.

Passage IDs are the first D1 event id of the passage; times are IST.

Counts are small: 17 relay pulses, 4 of them at night. Anything split
further is marked as resting on fewer than five passages.

## Summary

- **The Pi's own reader is the whole system.**
  - All 29 grants since the refocus were decided on the device (`local_ocr.decision_source=local`).
  - The cloud was billed 83 times and decided nothing. Its only three plates were misreads (`HH257`, `11111111`, `10CE990` at 0.76); 48 lookups timed out and 19 errored.
- **Alarm to relay: median 5.5 s, p90 10.5 s, worst 67.7 s** (17 pulses).
  - The winning frame takes a median 41 ms to decide.
  - The seconds go on waiting for a frame in which the plate is close enough to read, not on computing.
- **Every household arrival opened on the device except the D-Max (`172L66`).**
  - It failed once (4548, 6 Oct 10:05, about 53 s at the gate) and took 67.7 s once (4653, 7 Oct 18:35).
  - Its misreads are all in the last two characters: `172L61` ×5, `172L699`, `J72L69`.
- **Four of the seventeen pulses came from the camera's own 4K FTP still, not the sweep.**
  - A still reaches the Pi 0.6-2.3 s after it is taken and takes 420-890 ms to read on the device.
  - A sweep frame the sweep has already read takes about 40 ms.
- **Three daytime sweeps read nothing at all.** Frames arrived (10-31 each) but every one was skipped as empty or corrupt: 8 Oct 09:15, 8 Oct 10:24 and 9 Oct 11:59. Two were probably departures, where skipping is expected. The 10:24 one was an Audi arriving, and the 4K still let it in at +4.4 s. The sweep did not journal which test skipped the frames; since #203 it does.
- **The waiting phase reads darkness.**
  - At night, after a departure, the floodlight goes off and the sweep spends its whole 80 s cap reading black frames (8 Oct 19:27, 9 Oct 21:42).
  - Two of the four `wait_cap` endings in the journal were these departures, one was a visitor's van waiting, and one is unidentified.
  - Fixed by #204: such a sweep now ends about 5 s after the drive goes dark.
- **The house link is the weakest part of the installation, and it is worst by day.**
  - Pi→router loss averages 5-8 % from 20:00 to 07:00 and 34-40 % from 09:00 to 15:00. On 9 Oct it was 67-100 % for most of the afternoon.
  - Openings do not depend on it (invariant 3). The cloud fallback, the outbox, deploys and remote diagnosis all do.
- **9 Oct 21:42-22:22 the gate stood open for 39 minutes**, after a departure, with no pulse from the Pi (see section 8).

## 1. Latency breakdown per arrival

From D1 (`stage_timestamps`, `trigger.event_at`). The alarm time has 1 s
resolution, so every alarm-relative figure carries up to +1 s.

| Interval | Median | p90 | Worst | n |
| --- | --- | --- | --- | --- |
| Camera alarm → first frame into the pipeline | 1.7 s | 2.5 s | 2.7 s | 34 |
| Camera alarm → winning frame into the pipeline | 4.7 s | 8.1 s | 67.6 s | 17 |
| Winning frame in → decision | 41 ms | 2.5 s | 6.3 s | 17 |
| Decision → relay | 147 ms | 198 ms | 258 ms | 17 |
| **Camera alarm → relay** | **5.5 s** | **10.5 s** | **67.7 s** | 17 |

Day and night (night has fewer than five pulses, so it is not a basis for a day/night difference):

- **Day (n=13):** median 5.2 s.
- **Night (n=4):** 4.7, 5.7, 6.4 and 11.2 s. The 11.2 s is 4513 (5 Oct 21:31), the fast-lane regression fixed by #197.

From the sweep's own journal, 8-9 Oct. The sweep starts when the alarm arrives.

| Passage | First plate read | First authorised read | Plate px at that read |
| --- | --- | --- | --- |
| 8 Oct 10:22 Audi | 1.9 s (`31T0555` 0.16) | 6.3 s (0.78) | not logged before 9 Oct |
| 8 Oct 10:41 Audi | 1.9 s | 4.1 s (0.94) | " |
| 8 Oct 13:50 Audi | 2.0 s | 4.2 s (1.00) | " |
| 9 Oct 12:35 Audi | 9.7 s | 9.7 s (0.99) | 353 |
| 9 Oct 15:05 Audi | 5.0 s (138 px) | 10.6 s (0.98) | 286 |

So on a normal approach the sweep has a plate box about 2 s after the alarm.
The authorised read comes 2-4 s later, when the plate has grown past about
240-280 px (4K-equivalent at zoom 3). The approach is the cost.

Other timings:

- A sweep frame is 0.36-2.1 s old when its read is handed on (median about 1.0 s, n=8); the read itself is 233 ms (p50, p90 277 ms, n=137).
- The 9 Oct 12:35 sweep read only 3 of its 17 frames in 12 s; the rest were skipped as empty or corrupt.

The long "frame in → decision" cases are all 4K FTP stills:

- 4554, 4576, 4610, 4613, 4627, 4628, 4670, 4691, 4692, 4734: `capture_to_burst_ms` 0.6-2.3 s (the upload arriving), plus `ocr_ms` 417-888 ms (reading a 4K JPEG on one core).
- 4515 is the #197 regression (`burst_to_ocr_ms=6242`).

## 2. Early trigger (shadow, 5-9 Oct)

| | Day (77.6 h watched) | Night (36.2 h) |
| --- | --- | --- |
| Camera vehicle alarms | 46 | 7 |
| Led by a would-trigger within 6 s | 12 | 2 |
| Lead over the camera | median 1.2 s (p10 0.3, p90 3.4) | median 3.1 s (2.0-4.2) |
| Missed by the early trigger | 23 | 3 |
| False would-triggers, vision rule alone | 33 (0.43 an hour) | 25 (0.69 an hour; 23 floodlight) |
| False that pass the shipped confirmation rule | **0** | **0** |
| True ones the shipped rule (looks within 0.5 s) also removed | **15 of 15** | **9 of 9** |
| Seconds saved to the first good local read (upper bound) | median 1.8 s, 9 passages | 6.7 s, 3 passages |

Would switching it on have opened the gate sooner?

- **As shipped before 10 Oct, no.** The confirmation rule that removed every false trigger also removed every true one (15 of 15 by day, 9 of 9 at night). The reason is below.
- **With the sweep as the confirmation (#206), yes.** Every true trigger keeps its lead: 1.2 s by day, 3.1 s at night. The saving to the first good read is smaller than the lead (median 1.8 s by day, upper bound), because the plate still has to come close enough to read.

Risk: the early trigger never opens anything. It only starts a sweep, and the
sweep still needs an exact authorised read.

It still holds that nothing goes to the cloud before the camera raises its own
alarm:

- In shadow mode no early sweep runs (every journalled session is camera-origin), so live data cannot exercise the rule.
- The rule is held by `tests/test_early_trigger_pipeline.py`, 19 tests that pass on current `master` (`e136c00`), including `test_an_unread_plate_and_no_camera_event_sends_nothing_and_records_nothing` and `test_one_method_posts_to_the_cloud_reader_and_it_asks_the_permit_first`.

**Why the confirmation removed every true trigger** (measured in #206 over 82 would-triggers, 24 of them true):

- **The deadline is shorter than the looks.** The confirmation waited at most 0.5 s. On this Pi the CLIP look takes 539-773 ms (median 587) and the first plate look 668-1000 ms (median 743). No look ever answered inside the bound, so the shipped rule confirmed none of the 24 true triggers. Earlier drafts of this review said 9 of 15; that figure is the report's unbounded-looks line.
- **Unbounded, CLIP is wrong both ways.** It reads `empty` for a car front at the far fence gap and for every night arrival (headlight blooms), and a vehicle for people walking the lane.
- **The plate look is right but late.** It works from a keyframe up to 1 s old, and it is behind a 1.2 s lead.

**What was done.**

- The owner had it switched on: `GATE_EARLY_TRIGGER=on` since 10 Oct 08:40 IST (env backed up first; restarted with the gate quiet).
- #206 makes the early sweep itself the confirmation (`GATE_EARLY_TRIGGER_CONFIRMATION=sweep`, the default). It keeps 15 of 15 day and 9 of 9 night true triggers with their whole lead.
- **Cost:** about 58 false local-only sweeps in 114 h, about 0.08 % of one core. They send nothing, record no event, and can open nothing without an exact authorised read.
- The old behaviour remains available as `GATE_EARLY_TRIGGER_CONFIRMATION=looks`.

## 3. Reading at the stop, per vehicle

| Vehicle | Arrival passages | Opened | Alarm → relay | Misreads within 2 characters |
| --- | --- | --- | --- | --- |
| Audi Q5 `131D2696` | 9 | 8 (the miss, 4739, was a departure) | 4.4, 4.7, 5.2, 5.7, 7.4, 8.6, 10.5, 11.2 s | `131D2686` ×1 |
| D-Max `172L66` | 4 | 3 | 4.7, 6.4, 67.7 s | `172L61` ×5, `172L699`, `J72L69` |
| Landcruiser `11WH2397` | 3 | 3 | 4.3, 5.5, 8.4 s | none |
| Yeti `10CE1990` | 3 | 3 | 2.7, 4.3, 4.9 s | `10CE990`, `10CE1991`, `10CE1930` |

The D-Max is the only household car that is slow or fails. The earlier
review measured why: its plate is 197-274 px at the stop, under the
~240-280 px where the other cars' reads succeed, and when it stops close the
plate is cut by the right edge of the picture (x 0.94-0.97). Its misreads are
the trailing `66`, the smallest and most edge-cut characters. Every vehicle
except the D-Max has fewer than five passages here.

The other failed passages in this window, identified from the photos, are not
household arrivals and were correctly refused:

- a white Peugeot Partner van (4699, 8 Oct 17:29; it waited out the 80 s cap; and its exit, 4707);
- a dark saloon at night with its plate lost in its own headlights (4580, 6 Oct 20:17);
- Audi departures (4680, 4739, 9 Oct 21:42).

## 4. The cloud's value

| Since 5 Oct 17:00 | Count |
| --- | --- |
| Billed lookups (`billed_lookups`, health buckets) | 83 |
| Openings the cloud decided | **0** |
| Plates the cloud returned | 3, all misreads (`HH257` 0.82, `11111111` 0.85, `10CE990` 0.76) |
| Timeouts / errors | 48 / 19 |
| Slow or failed attempts on departures | 42 of 107 attempts that took 250 ms or more, or failed |
| Hand-overs skipped: breaker open / internet down / no budget (journal, 8-10 Oct) | 30 / 3 / 11 |
| Month to date | 163 of 2,500 |

- The device read every authorised plate that was read at all.
- The one household car the device could not read (the D-Max on 6 Oct) the cloud did not read either: 3 attempts, 10.6 s, timeouts.
- Since #197 the cloud lane cannot hold up a device decision. Its cost is not latency at the gate but link time, quota, and denial rows uploaded over a lossy link.

**Is the fallback still worth having?** Barely. It is a second opinion that has
not changed an outcome since the refocus. With fewer than five cases where it
could have helped, removing it is not justified either. Keep it, and:

- Spend it only on arrivals. 42 attempts went on departures. Neither the live direction verdict (it said `exiting` on 0 of 244 events) nor the audio (there is no real-time motor signal) can tell a departure in time. A receding plate in the sweep's own reads can: #205 ships that in `shadow`.
- Keep the 232 px hold. The 4 departures and 3 junk reads it would have released earlier are exactly the lookups it exists to stop.
- Leave the 7 s budget. Timeouts here are the link, not the budget.

## 5. The waiting phase and its cap

From 23 journalled sweeps, here is how each one ended:

| Ending | Count |
| --- | --- |
| `departed` | 7 |
| `opened` | 6 |
| `final_cooldown` | 3 |
| `new_event` | 3 |
| `wait_cap` | 4 |

While waiting, the sweep reads at 1.4-2.4 frames a second (7 waiting sweeps),
so the 2-a-second target holds.

The four `wait_cap` endings:

- **The van visitor (8 Oct 17:30):** correct, 140 waiting reads, never authorised.
- **Two night departures (8 Oct 19:27, 9 Oct 21:42):** the sweep read 152-157 black frames once the floodlight went off.
- **9 Oct 14:33:** best read `81D2555`; not identified.

The 80 s cap was needed once (D-Max, 7 Oct, +67 s) and is right.

What is wrong is that "a car is waiting" is decided on frames that show
nothing. A black frame was never judged empty against the idle scene, and
152-157 of them were read as a car still present.

The cause (#204): the idle baseline is refreshed only when the drive is quiet,
and after dark the camera's floodlight comes on with motion before the alarm,
so the baseline in force was the lit drive. Measured with the controller's own
thumbnails:
- black against that baseline: difference 0.385;
- black against black: 0.0012, against a threshold of 0.03.

#204 keeps a dark baseline as well. A dark frame is never skipped unread, and
ten dark reads in a row with no characters and no plate box end the sweep as
departed. Expected gain: nothing at the gate; 70-80 s of pointless reads and a
stuck "waiting" status per night departure, now about 5 s.

## 6. Night

Night arrivals by household cars: 4 pulses (4.7, 5.7, 6.4, 11.2 s; the
11.2 s was the #197 regression), and no household night arrival failed. The
only night arrival refused was the visitor's saloon, whose plate was hidden
by its own headlights (4580).

Infrared has been off since 6 Sep. On this evidence neither infrared nor the
floodlight would buy anything measurable for household cars: night reads are
as fast as day reads (fewer than five passages, so this cannot rule a
difference out). Infrared would also not fix headlight glare, which is what
hid the one unread night plate.

The camera's floodlight already comes on with motion: the 9 Oct 21:42 and
22:22 stills at +3 s are lit, and by +25 s they are black. Keeping it on
longer would only help the waiting phase in section 5.

## 7. Thermal and CPU headroom

- **Temperature (health buckets, 26 Sep - 9 Oct):** daily SoC maximum 57-74 °C (73.8 °C on 8 Oct), median 49-58 °C.
- **Throttling:** no bucket reported `currently_throttled` or `arm_capped`. `get_throttled=0xe0000` says the soft limit, a frequency cap and throttling have each happened at some point in the 27 days since boot, not recently.
- **CPU:** the sweep's reader takes 233 ms a frame on one core (p50), read at about 2 a second, so about half a core while a car is present.

The decode-width costs are in Task 1 below.

## 8. Anything else

- **9 Oct 21:42-22:22, the gate stood open for 39 minutes.**
  - The audio scanner (`gate_movements`) heard a 17 s motor run at 21:41:38 as the Audi left, then two more runs (21:42:08, 21:42:23) as it was still in the gateway.
  - Then nothing, and no latch clang, until 22:21:47, when the owner drove through the open gate. The Pi sent no pulse all evening (`relay_outcome=not_attempted`) and there was no app command.
  - Why, from the opener's own manual: it is a TOPENS A5132 with auto-close set to about 14 s after fully open. The TOPENS manual says the board "reverse[s] the gate upon first obstruction and stop[s] upon a second sequential obstruction"; its troubleshooting table lists "the gate stop[s] when on the way of opening or closing" as "two sequential photo beam blocked".
  - That fits the audio exactly. The exit wand opened the gate (21:41:38), auto-close started about 14 s after full open (21:42:08), the departing car broke the beam (reverse, 21:42:23), and broke it again (stop). A board that has stopped waits for a command; auto-close does not re-arm.
  - So the board is behaving as designed; the gate can be left open, possibly part-way, whenever a car lingers in the beam.
  - A notify-only "gate left open" alert now ships (gate-controller#202, access-gate-ui#94). Its one-tap Close requires the user to confirm they can see the gate fully open and not moving (access-gate-ui#95): a pulse into a gate stopped part-way reverses it, and the leaves can cross (2026-09-21).
- **Link loss is diurnal.**
  - Mean Pi→router loss by hour (IST), 5-9 Oct: 00-06 h 5-8 %, 08 h 18 %, 09-14 h 34-40 %, 15-17 h 27-29 %, 19-22 h 7-11 %.
  - A constant load such as the NVR recording the camera around the clock does not by itself explain a daily shape.
  - Daytime electrical noise on the mains the powerline adapters use (for example a solar inverter, or appliances) would explain it, and a constant stream would then fill what is left of a degraded link. That is a hypothesis, not a measurement. The next check is whether loss follows sunshine (a dull day against a bright one).
- **The Pi receives a steady 0.65-0.88 MB/s on eth0:** the camera's clear stream for the keyframe ring, switched inside the gate-end adapter; 1.67 TB since boot.
- **The camera's alarm:** it reaches the Pi as a webhook. The first frame enters the pipeline 1.7 s after it (median), and the early trigger sees the car 1.2 s (day) / 3.1 s (night) before it.
  - The camera raised 10 alarms while the gate was already running un-commanded (departures). Each departure costs a sweep and, until the hold releases, cloud hand-overs.
- **Duplicates:** none (`duplicates=0` in every sweep).

## Recommendations, ranked

| # | Change | Expected gain | Cost / risk | Who | Invariants |
| --- | --- | --- | --- | --- | --- |
| 1 | **More pixels on the D-Max's plate at the stop** (the stop-position re-aim in [reolink-rlc-811a.md](../reolink-rlc-811a.md), or a mark on the drive where it should stop) | The one household failure mode: a 53 s wait and a 67 s wait in 4 passages | Camera aim is a camera write in daylight with the gate quiet; everything else must be re-checked after it | User | 7 |
| 2 | **The TOPENS board stops the gate on a second photocell block and does not re-close** (manual: "reverse the gate upon first obstruction and stop upon a second sequential obstruction"), so ship a notify-only "gate left open" alert | 39 min open on 9 Oct; an alert within ~10 min (1.5 a week on history, about 0.7 false) | The alert never pulses; its Close button needs the user to confirm they can see the gate fully open, because a pulse into a gate stopped part-way can cross the leaves | Done: #202, access-gate-ui#94, #95 | 1, 3, 11 |
| 3 | **Journal skip counts per sweep** (`skipped_empty`, `skipped_corrupt` on `outcome=ended`), and keep one skipped frame per sweep for review | Explains the daytime sweeps that read nothing, one of which a 4K still rescued | Journal and disk only | Done: #203 | none |
| 4 | **End the waiting phase on a dark, plate-less scene.** The idle baseline was captured while the floodlight was on, so no black frame could match it | 70-80 s of pointless reads per night departure, now ~5 s | Invariant 6 kept: a dark frame is never skipped unread, and a plate box restarts the count; new guards listed under invariant 6 | Done: #204 | 6 |
| 5 | **No cloud hand-overs for departures**, judged from the sweep's own reads (a receding plate). On history it judged 0 of 24 arrivals departing and caught 6 of 15 departures | About 40 % of cloud attempts, and their denial rows over the lossy link | Must not touch arrivals; the relay still fires for rear plates (#171). Ships as `shadow` (records only); switch `on` after a week of records shows no arrival judged departing | Done: #205 (shadow) | 1, 2, 5 |
| 6 | **Investigate the daytime link loss.** Check whether loss follows sunshine (powerline noise), and cut the constant load the NVR puts on that link by recording the camera's sub-stream rather than its 4K main stream | Reliable deploys, outbox and diagnosis by day. The NVR is at the house, so its stream crosses the bad link around the clock | Lower-resolution recordings; changing the camera's encoder or the NVR's stream choice is a camera/NVR setting | User | 3 |
| 7 | **Early trigger on, with the sweep as its confirmation** | The 1.2 s (day) / 3.1 s (night) lead on every true trigger, against none before | About 58 false local-only sweeps in 114 h; they open nothing and send nothing | Done: config (10 Oct) + #206 | 1, 5 |
| 8 | Infrared / floodlight: leave as they are | None measured | none | none | none |

Task 1 (decode width) is reported separately below.

## Task 1: decode width

**Not shipped.** The evidence does not justify changing
`GATE_TRIGGER_CAPTURE_FRAME_WIDTH` from 1920.

**What the pipeline really does.**
- The Pi's config has `GATE_TRIGGER_CAPTURE_CROP` off. So the session decoder scales the whole 4K frame to 1920 (the plate ×0.50, not the ×0.556 assumed when this was proposed), and the band is cut from that.
- The detector letterboxes every band to 384 px, so decode width barely changes what it sees.
- The recogniser (cct-xs-v2) resizes every plate crop to a fixed 128×64 with bilinear interpolation.
- At 1920 a plate of 240-290 px (4K-equivalent) is already about 120-145 px when cropped. A wider decode does not give the OCR more pixels; it changes how a larger crop is shrunk to 128, and bilinear shrinking can alias.

**Accuracy, on real stills.**
- Source: 25 4K stills from `/camera/snap` after the 9 Oct 15:05 Audi alarm, read on the Pi through the deployed engine in the Pi's own mode (scale the frame, crop the band, JPEG q90).
- The six with a plate (358-386 px) read `131D2696` at 0.99+ at 1920, 2560 and 3200.
- At 3456 and 3840 one of the six became a misread (`131D26996`, `131D26966`).
- One passage, one car, plates already above the band where reads fail.

**Accuracy, synthetic far plates.** The same six stills, shrunk about the plate's centre so the plate is 200-310 px, then read the same way (5 per cell):

| Plate px | 1920 | 2560 | 3200 | 3456 | 3840 |
| --- | --- | --- | --- | --- | --- |
| 200-275 (30 reads) | 9 | **15** | 7 | 12 | 7 |
| All 200-310 (40 reads) | 16 | **22** | 15 | 19 | 15 |

- 2560 reads more of the band where the D-Max and slow approaches fail.
- The pattern is not monotonic: 3200 and 3840 are no better than 1920. That looks like resampling phase rather than real detail.
- The frames are camera JPEGs, cleaner than stream frames, and all of one car.

That is not evidence enough to flip a default.

**Cost.** Read time on one core: 168 ms at 1920, 181 at 2560, 197 at 3200, 205 at 3456/3840 (medians).

The session decoder's own cost was **not measured**. The attempt (decoding a recorded clip offline on the Pi) ran away to 2.6 GB and put the Pi out of memory from 07:43 to 07:55 IST on 10 Oct. Effects:
- the heartbeat went silent for about 11 minutes;
- the audio transcoder restarted;
- no camera alarm fell in that window, and the controller was not restarted.

Decode experiments belong on a workstation, not the gate's only controller.

**What would settle it.** Real stream frames, not stills, from several cars in the 200-290 px band, read at 1920 and 2560. The NVR at the house records the main stream, so its recordings of real passages are the cleanest source and cost the Pi nothing. If 2560 still wins there, it is a one-line config change, and the things to watch after it are:
- read time (expect about +8 %);
- the Pi's temperature (currently 57-74 °C daily maximum);
- the sweep's `read_fps` while waiting, which must stay at about 2.
