# Invariants

What the gate controller must always do, why, and the tests that hold it to
that. Read this before changing anything on the decision path: routing,
availability, deadlines, the sweep, uploads, the camera, or the updater.

Every invariant here was written down after it broke, or after it nearly
did. Each one names the tests that guard it; `tests/test_invariants_doc.py`
fails if a test named here no longer exists, so a refactor cannot quietly
delete a guard.

**A guard tests the behaviour, not the code that produces it.** A test that
reads source text and asserts `"and self.cloud_reachable" in needs_cloud` pins
one implementation in place; it passed throughout the 2026-10-05 regression
below while the behaviour it was meant to protect was broken. When you change
how something below is achieved, keep its behavioural test green, and if your
change adds a new way for the invariant to fail -- a state that can change
between two steps, a new thread, a new caller -- add a test that drives that
case through the real path.

## 1. The relay only moves on a verified grant, and never on a moving gate

See [gate-operator.md](gate-operator.md) and the hard rules in `CLAUDE.md`.
Every open needs an exact match against the authorised list under the band
in force, a fresh frame, the cooldown, the one-car-one-pulse hold
(invariant 12), and the actuation claim. Nothing added for speed or spend --
the sweep, the cloud hold, the conclusive-read rule, the early trigger -- may
open the gate on anything else; those only decide what is read and what is
spent.

A frame whose cloud request was skipped answers "no plate"; the device's
refused read rides along only as `PlateObservation.review_plate`, which
matching reads after every rule has refused, to name a near miss, and never
to grant (#201). That holds for every skip reason, the departure skip
included.

- `tests/test_matching.py::test_rejects_a_low_confidence_exact_authorised_plate`
- `tests/test_matching.py::test_an_exact_plate_still_opens_overnight`
- `tests/test_actuation.py::test_persisted_cooldown_records_the_grant_rather_than_a_denial`
- `tests/test_near_miss_when_cloud_skipped.py::test_an_exact_authorised_plate_at_full_confidence_is_never_a_grant`
- `tests/test_near_miss_when_cloud_skipped.py::test_a_confident_misread_is_still_only_a_near_miss`
- `tests/test_near_miss_when_cloud_skipped.py::test_the_departing_frame_is_denied_and_names_the_dmax`
- `tests/test_processor.py::test_a_departing_verdict_never_touches_a_frame_the_device_decided`

## 2. The burst thread never waits on the network

A frame the Pi's own reader can decide is decided on the burst thread at
once; only frames it cannot decide go to the cloud lane, and nothing on the
burst thread may take the cloud OCR slot or post.

- **2026-09-10 21:52** -- a perfect local read waited 4.15 s behind two cloud
  lookups on the one burst thread. Fixed by the fast lane (#165).
- **2026-10-05 21:31** -- regressed. #183 kept bursts on the burst thread when
  the cloud looked unreachable; #189 put the circuit breaker inside "reachable",
  which flips within a second. The camera's still was routed as unreachable,
  finished after the breaker closed, posted from the burst thread, and held it
  to its 7 s deadline: a 0.999 read of the Audi waited 6.2 s and the gate
  opened 10.0 s after the alarm. Fixed by #197: the route is decided once, and
  a burst kept on the burst thread is answered without the network.

Guards:

- `tests/test_fast_lane.py::test_a_frame_the_device_can_read_is_decided_while_an_older_cloud_call_waits`
- `tests/test_fast_lane.py::test_a_burst_kept_off_the_lane_never_waits_on_the_cloud_when_the_breaker_closes`
- `tests/test_processor.py::test_a_burst_routed_off_the_lane_is_finished_without_the_cloud_after_the_link_returns`
- `tests/test_fast_lane.py::test_a_departing_burst_is_finished_on_the_burst_thread_without_the_cloud`
- `tests/test_processor.py::test_a_burst_skipped_as_departing_is_finished_without_the_cloud_when_the_car_turns_back`
  (the departure verdict is not sticky; the route taken at `prepare` is the
  route the burst is finished on, whatever the verdict says by then)

## 3. The gate opens on the Pi's own read with no link to the house

The camera, the Pi and the relay are all at the gate; the cloud reader, the
dashboard and the NVR are not. A locally authorised plate opens the gate with
the internet down, the breaker open, or the powerline link to the house lossy.
Nothing the cloud or the house link does may delay or block that open.

- `tests/test_internet_down_pipeline.py::test_a_locally_authorised_plate_still_opens_once_with_the_internet_down`
- `tests/test_fast_lane.py::test_a_burst_kept_off_the_lane_never_waits_on_the_cloud_when_the_breaker_closes`

## 4. No frame opens the gate once it is older than `GATE_MAX_IMAGE_AGE_SECONDS`

Measured from the moment the frame was first seen -- for an FTP upload, when
the upload started, including bytes that arrived before a restart. A slow
upload is waited for and decided (so the passage is on record with its
photo), but a late frame is refused as `stale_burst`, never acted on.

- **2026-10-01 to 03** -- the camera's uploads slowed to 3-40 KB/s; every still
  was thrown away unread as `upload_incomplete` and no photo was kept. Fixed
  by #192, which waits for an upload while it is still arriving and keeps
  the freshness rule above.

Guards:

- `tests/test_slow_upload_pipeline.py::test_a_still_that_takes_40_s_to_arrive_is_decided_and_refused_as_stale`
- `tests/test_slow_upload_pipeline.py::test_a_still_already_half_arrived_when_the_controller_restarts_is_not_fresh`

## 5. Far frames and departing cars are not sent to the cloud

The cloud reader is billed, one request a second, and slow over the gate's
link. A frame whose plate is too small to read is not worth a lookup: the
sweep holds its hand-overs until the plate is big enough, has stopped growing,
or it is the last chance, and the camera's own alarm still (taken when the car
is furthest away) is not sent while a sweep is reading. The Pi's own reader
still reads every frame -- it is free.

A departing car is not worth a lookup either: it is let out by the gate's
exit mechanism, not by the Pi, and since 2026-10-05 the cloud had decided no
opening while 42 of its 107 slow or failed lookups were on departures. The
sweep's own reads say which it is -- a departing car's plate shrinks as it
recedes, an arriving car's only grows until it stops -- and while they say so
the passage's frames and still are decided on the device and not sent
(`cloud_skipped reason=departing`) once `GATE_LOCAL_SWEEP_DEPARTING_SKIP=on`;
shipped as `shadow`, which journals what `on` would have kept off the cloud
and sends exactly as before. An arriving car keeps its cloud fallback, and an
authorised rear plate the device reads still opens the gate (#171).

- `tests/test_fast_lane.py::test_the_cameras_alarm_still_is_not_sent_to_the_cloud_while_a_sweep_is_reading`
- `tests/test_fast_lane.py::test_a_sweep_frame_is_never_treated_as_the_cameras_still`
- `tests/test_departing_pipeline.py::test_a_departing_cars_frames_and_still_post_nothing`
- `tests/test_departing_pipeline.py::test_an_arriving_car_still_gets_its_cloud_fallback`
- `tests/test_departing_pipeline.py::test_a_departing_cars_authorised_rear_plate_still_opens_on_the_device`
- `tests/test_trigger_capture.py::DepartingPlateTests::test_the_2026_10_09_arrival_never_reads_as_receding`

## 6. A waiting car keeps being read

A car stopped at the gate is read until it is let in, leaves, or the cap
(80 s of waiting at two reads a second). One confident misread does not end
the passage: a different car is concluded only from a plausible registration
read on two frames.

- **2026-10-05** -- a car read 54 times over 40 s was then not read again; a
  cloud misread (`1SU2U` at 0.811) could end a passage. Fixed by #196.
- **2026-10-07 18:35** -- a pickup stopped too close for its plate to be found
  was let in at +66 s when it moved; the old 40 s cap would have missed it.
- **2026-10-09 21:42** -- the other direction: an Audi departed under the
  camera's spotlight, the light went off, and the sweep read 152 black frames
  to the cap because the idle baseline had been refreshed under the spotlight
  and black never matched it (8 Oct 19:27: 157). The dark-drive rule (#204)
  ends the waiting when a dark frame matches the newest *dark* idle frame
  **and** the reader found neither characters nor a plate box in it, ten
  looks running. It is a second way to say "gone", so it is also a new way
  for this invariant to fail: a car in the dark whose plate is lit at all
  gives the reader a box, and that must keep it being read. The same PR
  closed the older hole on a quiet night, where the baseline is itself black
  and three *unread* black looks used to end the wait, or the window: a dark
  frame always goes to the reader now, in the window and while waiting.

Guards:

- `tests/test_local_sweep.py::LocalSweepTests::test_with_the_shipped_defaults_a_car_still_at_the_gate_at_65_s_is_still_read`
- `tests/test_trigger_capture.py::test_a_confident_read_in_no_registration_shape_never_ends_the_passage`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_car_in_the_dark_whose_plate_is_boxed_is_read_to_the_cap`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_boxed_plate_on_a_quiet_night_is_read_to_the_cap_not_skipped_as_empty`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_black_drive_with_no_dark_idle_frame_on_record_is_read_to_the_cap`
- `tests/test_local_sweep.py::LocalSweepTests::test_a_boxed_plate_in_the_dark_starts_the_run_again`
- `tests/test_local_sweep.py::LocalSweepTests::test_a_failed_read_in_the_dark_is_not_an_empty_drive`

## 7. The camera lens is never left changed

Only one thing on the Pi moves the lens: the refocus nudge (#195), which steps
zoom one position and back to exactly where it was, and puts it back first if
the service restarted part-way. Nothing writes a focus position. The setup
script (`scripts/reolink/configure-rlc811a.py`) sends no zoom or focus command
at all.

- **2026-10-04** -- the camera's autofocus settled out of focus overnight;
  every daytime frame was soft for a day and a half until the zoom was nudged.

Guards:

- `tests/test_camera_refocus.py::test_a_restart_part_way_through_a_nudge_puts_the_zoom_back_first`
- `tests/test_camera_refocus.py::test_a_restart_with_no_record_never_moves_the_lens`

## 8. The camera's clock is UTC, and the Pi owns it

The camera displays UTC (zone 0, DST off), and the Pi corrects it hourly if it
drifts more than 5 s. Camera alarms are matched to frames by time, so a camera
clock that is wrong makes every alarm look stale. The camera's overlay is
therefore an hour behind Irish summer time, on purpose.

- **2026-09-16** -- the NVR pushed the camera a clock two hours out; every
  camera alarm was refused for four days. Fixed by #147.

Guards:

- `tests/test_camera_clock.py::test_a_clock_two_hours_ahead_is_written_back_to_utc`
- `tests/test_camera_clock.py::test_a_zone_somebody_chose_is_reported_but_never_written`

## 9. The Pi only runs a release whose exact commit passed CI on master

The updater installs `master`'s head only after a successful push (or manual)
CI run for that exact SHA. If GitHub drops a push, nothing deploys until CI is
re-run: `gh workflow run ci.yml --ref master`.

- **2026-09-23 to 10-05** -- GitHub never ran CI on #190's merge; the Pi
  stayed on the 22 Sep release for twelve days. Fixed by #191.

Guards:

- `tests/test_updater.py::test_accepts_only_completed_successful_protected_branch_push_for_exact_sha`
- `tests/test_updater.py::test_accepts_a_manual_run_on_the_protected_branch`

## 10. The suite does not depend on today's date

A test that saves fixtures with fixed timestamps and then prunes, ages or
compares them against the wall clock passes until the calendar catches up,
then fails every run -- and a failing suite blocks every deploy (invariant 9).

- **2026-09-23 onward** -- the early-trigger thumbnail test started failing on
  every run, including every Renovate PR. Fixed by #191; #187 fixed the same
  shape in the audio recorder tests.

Guards:

- `tests/test_early_trigger.py::test_pictures_are_aged_by_when_they_were_taken_not_by_the_wall_clock`

## 11. Nothing that watches the gate can move it

The sound scanner and the left-open check (`gate_left_open.py`) report what
the gate seems to be doing; they never act on it. Neither imports the relay,
the actuation coordinator or the command server. Neither sends a command or
retries one, and nothing they report reaches the decision path. The only
closing action the left-open alert offers is a person pressing the app's
existing gate button. That goes through the app's command route, its
`operator` role check and the 20 s command cooldown, exactly as a manual open
does.

- **2026-09-21** -- an audio-following harness decided the gate was "not
  provably shut" and sent three recovery pulses, crossing the leaves; the gate
  could not open until someone freed them by hand ([gate-operator.md](gate-operator.md)).
- **2026-10-09** -- the gate stood open for 39 minutes after an interrupted
  auto-close. The alert built for this (docs/gate-left-open.md) is notify-only
  for the reason above.

Guards:

- `tests/test_gate_left_open.py::LeftOpenThroughTheHeartbeat::test_it_never_reaches_the_relay_or_the_actuation_coordinator`
- `tests/test_gate_left_open.py::LeftOpenThroughTheHeartbeat::test_the_check_imports_nothing_that_can_move_the_gate`
- `tests/test_gate_left_open.py::LeftOpenThroughTheHeartbeat::test_a_malformed_setting_changes_nothing_about_plate_matching`

## 12. One car, one automatic pulse

Once the relay has pulsed for a plate, no automatic grant pulses for it again
while that plate stays in the record: it is held until the plate has been
unseen for `GATE_REPULSE_UNSEEN_MINUTES` (10). "Seen" is any event naming the
plate -- the plate a grant matched, the plate a reader observed, or the
authorised plate a refused read was nearest to -- including the grants the
hold itself and the cooldown refuse, so a car waiting through alarm after
alarm gets exactly one pulse, and a car that has gone and come back gets
another. Different plates are independent. The hold sits in the
`ActuationCoordinator`, the one object every automatic path -- sweep frame,
FTP still, presence frame, early sweep, appearance grant -- goes through, and
it is derived from the store on every grant, so a restart keeps it. It is in
addition to the 90 s cooldown, which stands as it was.

The owner can also pause automatic opening altogether: `GATE_AUTOMATIC_OPEN=off`,
or `automatic_open: {"enabled": false}` in the app's settings envelope, refuses
every automatic grant at the same point and records it as `automatic_paused`.
A malformed setting changes nothing.

Neither rule touches a person's command from the app, which keeps its
`operator` role check and its 20 s cooldown (someone watching the camera can
see what the gate is doing; the plate reader cannot), and neither can add,
retry or hurry a pulse: each can only withhold one. Nothing here listens to
the gate: until 2026-10-10 15:27 the camera's own siren sounded at every
vehicle alarm (now switched off), and an idling diesel still masks the motor,
so sound cannot yet say whether the gate is open.

- **2026-10-10 10:04-10:13 IST** -- an authorised pickup (`172L66`) waited
  nine minutes at the gate. The camera raised seven vehicle alarms; the Pi
  pulsed the relay four times for the one car (10:04:13.9, 10:06:45.8,
  10:08:18.0 -- the camera's still, 2.4 s after its sweep frame had been
  refused by the cooldown at +89.6 s -- and 10:12:37.0). Every read between
  was refused only by the cooldown, whose expiry was a clean slate; every
  alarm started a new passage, and nothing carried "this plate was already let
  in and is still here" from one to the next. A pulse into a gate whose state
  is unknown stops or reverses it (invariant 1, [gate-operator.md](gate-operator.md));
  the leaves crossed and the gate jammed ([the review](reviews/2026-10-10-gate-jam.md)).

Guards:

- `tests/test_gate_jam_2026_10_10.py::JamReplayTests::test_the_waiting_dmax_gets_one_pulse_for_its_seven_alarms`
  (the morning replayed through alarm → sweep → processor → coordinator →
  relay, with the camera's still of every alarm)
- `tests/test_gate_jam_2026_10_10.py::JamReplayTests::test_the_same_car_back_after_the_window_is_let_in_again`
- `tests/test_gate_jam_2026_10_10.py::JamReplayTests::test_a_person_can_still_open_the_gate_while_the_car_is_held`
- `tests/test_one_pulse_per_car.py::OnePulsePerCarTests::test_a_car_that_stays_in_view_gets_exactly_one_automatic_pulse`
- `tests/test_one_pulse_per_car.py::OnePulsePerCarTests::test_refused_reads_keep_the_hold_alive_for_as_long_as_the_car_stays`
- `tests/test_one_pulse_per_car.py::OnePulsePerCarTests::test_a_different_authorised_plate_arriving_during_the_hold_gets_its_own_pulse`
- `tests/test_one_pulse_per_car.py::OnePulsePerCarTests::test_a_near_miss_read_of_the_plate_counts_as_the_car_still_being_there`
- `tests/test_one_pulse_per_car.py::OnePulsePerCarTests::test_a_controller_restart_mid_hold_keeps_the_hold`
- `tests/test_one_pulse_per_car.py::OnePulsePerCarTests::test_an_app_command_during_the_hold_still_pulses`
- `tests/test_gate_jam_2026_10_10.py::AutomaticOpenPausedReplayTests::test_a_sweep_frame_and_the_cameras_still_are_both_refused_but_a_person_is_not`
- `tests/test_gate_jam_2026_10_10.py::AutomaticOpenPausedReplayTests::test_an_early_sweeps_frame_is_refused_too`
- `tests/test_gate_jam_2026_10_10.py::AutomaticOpenPausedReplayTests::test_a_presence_frame_is_refused_too`
- `tests/test_one_pulse_per_car.py::AutomaticOpenPauseTests::test_every_automatic_source_is_refused_at_the_coordinator`
- `tests/test_one_pulse_per_car.py::AutomaticOpenPauseTests::test_a_malformed_app_setting_changes_nothing`
