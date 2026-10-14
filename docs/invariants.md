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
(invariant 12), a car that is not leaving (invariant 13, under
`GATE_DEPARTING_WITHHOLD=on`), and the actuation claim. Nothing added for speed or spend --
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

Nor may anything done on the Pi for the baseline. The on-device reader has one
worker, and the scene baseline's detector probe (invariant 6) runs on it. A
probe is not a read: a frame that lands while a probe is on the worker -- the
camera's still, the sweep's first frame -- is admitted behind it and read the
moment the probe's inference ends, never refused as busy. The probe itself is
declined behind any read, and the one-outstanding-read rule stands for reads.
(Found in review of #211 before it shipped: the probe reserved the worker
exactly as a frame did, and a still landing in that second was declined; with
the internet down and no later frame, an authorised plate could not open.)

- `tests/test_internet_down_pipeline.py::test_a_locally_authorised_plate_still_opens_once_with_the_internet_down`
- `tests/test_fast_lane.py::test_a_burst_kept_off_the_lane_never_waits_on_the_cloud_when_the_breaker_closes`
- `tests/test_internet_down_pipeline.py::InternetDownPassageTests::test_the_cameras_still_landing_during_a_baseline_probe_is_read_on_the_device`
- `tests/test_local_recognizer.py::BaselineProbeTests::test_a_frame_arriving_during_a_probe_is_read_the_moment_the_probe_ends`

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
authorised rear plate the device reads is still decided as the grant it is:
the skip is about spending. Whether that grant's pulse goes is invariant 13.

The judge is shared with invariant 13, and was hardened for it on 2026-10-14:
"receding" now takes **three** shrunk reads in a row, not two, and a plate
standing still (five reads within 8 % across two seconds) is never receding.
On 2026-10-13 at 13:37 UTC the two-read rule judged an authorised arrival
departing from the detector's box jumping 205 <-> 322 px; under `on` its
later frames would have been kept off the cloud (nothing was lost: the car
had been let in 1.1 s earlier on the device). Every departure the journal
holds from 2026-10-10 to 10-14 is still judged, 0.3-0.6 s later. Under `off`
the judge is still fed (invariant 13 reads it), but nothing here consults it.

- `tests/test_fast_lane.py::test_the_cameras_alarm_still_is_not_sent_to_the_cloud_while_a_sweep_is_reading`
- `tests/test_fast_lane.py::test_a_sweep_frame_is_never_treated_as_the_cameras_still`
- `tests/test_departing_pipeline.py::test_a_departing_cars_frames_and_still_post_nothing`
- `tests/test_departing_pipeline.py::test_an_arriving_car_still_gets_its_cloud_fallback`
- `tests/test_departing_pipeline.py::test_a_departing_cars_authorised_rear_plate_still_opens_on_the_device`
- `tests/test_trigger_capture.py::DepartingPlateTests::test_the_2026_10_09_arrival_never_reads_as_receding`
- `tests/test_trigger_capture.py::DepartingPlateTests::test_the_2026_10_13_arrival_whose_box_jumped_never_reads_as_receding`
- `tests/test_trigger_capture.py::DepartingPlateTests::test_every_departure_the_journal_holds_from_10_to_14_october_is_still_caught`

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
- **2026-10-10 10:05:38 and 10:07:34** -- a car that waits raises no alarm,
  and "idle" was measured from the last alarm. The D-Max `172-L-66` sat at
  the stop, half the picture, through the 60 s of quiet after its alarm; the
  keyframe decoder's 30 s refresh adopted it as the idle drive, and the next
  alarm's sweep skipped its frames unread as "empty" (the samples #203 kept
  show the car, judged empty; 9 Oct 12:35 -- 17 frames, 3 reads, the first
  at +9.7 s -- and 8 Oct 10:24 -- 10 frames, 0 reads -- went the same way).
  A frame is now shown to the on-device plate detector before it becomes the
  baseline; one with a plate box in it is refused and the old baseline kept,
  and so is one the detector cannot answer for (not ready, busy with a sweep
  frame, failed), because a stale picture of the *empty* drive makes a car's
  frames differ, which is to say read. An alarm that lands while the
  detector is looking refuses it too: the idle check is made again before
  the baseline is installed. A sweep read that boxes a plate also
  counts as activity, so the idle clock runs from the last sight of the car,
  not the last alarm. Without a local recogniser there is no detector and
  the baseline refreshes as before. The probe runs on the reader's one
  worker, so the sources offer nothing while a capture session is reading,
  and the baseline asks once more, at the last moment before the detector,
  that no session has started since the frame was offered (the compressed
  source decodes a keyframe in between, for up to 3 s). Neither holds the
  session lock across the probe: `start_session` is called on the sweep's
  thread, and the sweep never waits. A probe already on the worker when a
  session starts cannot be taken back; the sweep's read is admitted behind
  it and runs the moment it ends (invariant 3), never refused.

Guards:

- `tests/test_local_sweep.py::LocalSweepTests::test_with_the_shipped_defaults_a_car_still_at_the_gate_at_65_s_is_still_read`
- `tests/test_trigger_capture.py::test_a_confident_read_in_no_registration_shape_never_ends_the_passage`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_car_in_the_dark_whose_plate_is_boxed_is_read_to_the_cap`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_boxed_plate_on_a_quiet_night_is_read_to_the_cap_not_skipped_as_empty`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_black_drive_with_no_dark_idle_frame_on_record_is_read_to_the_cap`
- `tests/test_local_sweep.py::LocalSweepTests::test_a_boxed_plate_in_the_dark_starts_the_run_again`
- `tests/test_local_sweep.py::LocalSweepTests::test_a_failed_read_in_the_dark_is_not_an_empty_drive`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_a_car_waiting_at_the_gate_through_the_idle_refresh_is_read_on_the_next_alarm`
  (the 10 Oct morning: alarm → sweep of misreads → 90 s of quiet with the
  car offered as the idle drive → second alarm, read and let in once)
- `tests/test_scene.py::WaitingCarTests::test_a_keyframe_the_detector_boxes_a_plate_in_is_refused_and_the_old_baseline_kept`
- `tests/test_scene.py::WaitingCarTests::test_a_detector_that_cannot_answer_keeps_the_old_baseline`
- `tests/test_scene.py::WaitingCarTests::test_an_empty_frame_is_still_adopted_when_the_detector_finds_nothing`
- `tests/test_scene.py::WaitingCarTests::test_an_alarm_on_another_thread_during_a_slow_probe_refuses_the_frame`
- `tests/test_clear_stream_source.py::ClearStreamSourceTests::test_a_keyframe_the_detector_boxes_a_plate_in_is_never_adopted_as_the_idle_drive`
- `tests/test_main.py::BaselineVehicleCheckWiringTests::test_a_capture_the_decoder_already_cropped_is_not_cropped_again`
- `tests/test_sweep_pipeline.py::SweepPipelineTests::test_an_alarm_whose_sweep_lands_during_a_baseline_probe_is_still_read_and_let_in`
  (alarm → sweep → processor → relay while the probe holds the reader's
  one worker: the sweep's first frames are read behind it, none refused)
- `tests/test_trigger_capture.py::HotKeyframeCaptureTests::test_a_session_that_starts_between_the_check_and_the_probe_does_not_take_the_detector`
- `tests/test_clear_stream_source.py::ClearStreamSourceTests::test_a_session_that_starts_during_the_keyframe_decode_does_not_take_the_detector`
- `tests/test_scene.py::WaitingCarTests::test_a_session_that_started_since_the_offer_keeps_the_detector_unasked`

## 7. The camera lens is never left changed, and neither is a picture setting a lease changed

Only one thing on the Pi moves the lens: the refocus nudge (#195), which steps
zoom one position and back to exactly where it was, and puts it back first if
the service restarted part-way. Nothing writes a focus position. The setup
script (`scripts/reolink/configure-rlc811a.py`) sends no zoom or focus command
at all.

The same discipline holds for any camera setting a lease changes. The
night-vision lease switches `Isp.dayNight` to `Black&White` so the IR LEDs
light; it reads the whole `Isp` block first and writes it to durable storage
before anything changes, writes back **exactly** that block when the lease ends
-- expiry, an Off from the app, or the next start after a crash -- and only
then switches IR off. The only ISP value it ever writes that was not in the
snapshot is `dayNight` itself; the exposure the plate reader is tuned to
(Manual 1/250 s, gain 16, [the review](reviews/2026-09-06-camera-night-configuration.md))
is never a value the service holds. A camera whose ISP cannot be read gets no
change at all, a restore that fails is retried and journaled at error priority
and reported in the heartbeat, and an unreadable snapshot is never written back
([camera-control.md, Night vision](camera-control.md#night-vision-the-isp-half-of-the-ir-lease)).

- **2026-10-04** -- the camera's autofocus settled out of focus overnight;
  every daytime frame was soft for a day and a half until the zoom was nudged.
- **2026-10-11** -- the app's Night vision tile leased IR on and the picture
  stayed black: the ISP had been pinned to `Color` since 2026-09-07, in which
  the camera never lights its IR LEDs. Making the lease switch the mode made it
  a lease over a measured plate-reading setting, which is why it is held to
  the lens's rule.

Guards:

- `tests/test_camera_refocus.py::test_a_restart_part_way_through_a_nudge_puts_the_zoom_back_first`
- `tests/test_camera_refocus.py::test_a_restart_with_no_record_never_moves_the_lens`
- `tests/test_camera_night_vision.py::NightVisionLeaseTests::test_expiry_restores_the_isp_exactly_as_it_was_read_then_turns_ir_off`
- `tests/test_camera_night_vision.py::NightVisionLeaseTests::test_nothing_is_ever_written_to_the_isp_that_was_not_in_the_snapshot`
- `tests/test_camera_night_vision.py::NightVisionLeaseTests::test_a_restart_mid_lease_restores_the_isp_before_anything_else`
- `tests/test_camera_night_vision.py::NightVisionLeaseTests::test_an_isp_that_cannot_be_read_is_never_changed`
- `tests/test_camera_night_vision.py::NightVisionLeaseTests::test_a_restore_that_fails_is_retried_reported_and_leaves_ir_on_until_it_takes`
- `tests/test_camera_night_vision.py::NightVisionLeaseTests::test_a_record_that_cannot_be_read_is_never_written_back_and_is_said_loudly`

## 8. The camera's clock is UTC, and the Pi owns it

The camera displays UTC (zone 0, DST off), and the Pi checks it hourly and
corrects it if it drifts more than 5 s or shows any other zone or DST setting;
a correction, an unanswering camera or an unsynced Pi is looked at again after
5 min. Camera alarms are stamped `+0000` whatever the camera displays and are
matched to frames by time, so a camera clock that is wrong makes every alarm
look stale. The camera's overlay is therefore an hour behind Irish summer
time, on purpose.

Nothing is written to the camera -- time, zone or NTP -- while the Pi's own
clock is not synchronised (`pi_unsynced`): the Pi has no RTC, and a pass
before timesyncd has stepped the clock would write its boot-time clock into a
camera that was right. A correction is `corrected` only when the read-back
shows UTC within tolerance; otherwise it is `correction_failed`, never
`synced`.

- **2026-09-16** -- the NVR pushed the camera a clock two hours out; every
  camera alarm was refused for four days. Fixed by #147.
- **2026-10** -- a zone other than 0 was reported as the operator's choice and
  left alone, so one phone "sync with phone time" or NVR push could stop
  recognition for good. Fixed by #198, which also stopped writes from an
  unsynced Pi and turns the camera's NTP back on when it is found off.

Guards:

- `tests/test_camera_clock.py::test_a_clock_two_hours_ahead_is_written_back_to_utc`
- `tests/test_camera_clock.py::test_a_zone_somebody_chose_is_corrected_too`
- `tests/test_camera_clock.py::test_a_correction_that_will_not_hold_is_reported_not_claimed`
- `tests/test_camera_clock.py::test_nothing_is_written_while_the_pi_clock_is_unsynced`

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
the actuation coordinator or the command server, and nor does the rule every
reader of `gate_movements` applies to the camera's old alarm sound
(`gate_alarm_sound.py`), nor the scanner's engine check: both can only leave a
row out of what is reported. Neither sends a command or
retries one, and nothing they report reaches the decision path. The only
closing action the left-open alert offers is a person pressing the app's
existing gate button. That goes through the app's command route, its
`operator` role check and the 20 s command cooldown, exactly as a manual open
does.

- **2026-09-21** -- an audio-following harness decided the gate was "not
  provably shut" and sent three recovery pulses, crossing the leaves; the gate
  could not open until someone freed them by hand ([gate-operator.md](gate-operator.md)).
- **2026-10-09** -- the gate was thought to have stood open for 39 minutes
  after an interrupted auto-close. The audio re-read on 2026-10-10 cannot
  confirm it ([the review](reviews/2026-10-10-gate-jam.md)). The alert built for
  this case (docs/gate-left-open.md) is notify-only for the reason above.

Guards:

- `tests/test_gate_left_open.py::LeftOpenThroughTheHeartbeat::test_it_never_reaches_the_relay_or_the_actuation_coordinator`
- `tests/test_gate_left_open.py::LeftOpenThroughTheHeartbeat::test_the_check_imports_nothing_that_can_move_the_gate`
- `tests/test_gate_left_open.py::LeftOpenThroughTheHeartbeat::test_a_malformed_setting_changes_nothing_about_plate_matching`
- `tests/test_gate_alarm_sound.py::NothingThatReadsTheRowsCanMoveTheGate::test_the_classification_and_the_scanner_import_nothing_that_can_move_the_gate`

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

## 13. No automatic pulse for a car that is leaving

A departing car comes from behind the camera and shows it a rear plate that is
on the same list. It is let out by the exit wand or the auto-open, so a pulse
for it lands on a gate that is open or opening -- on the step-by-step input
that closes it behind the car or stops it dead, and stopped leaves pulsed
again can cross (invariant 1, [gate-operator.md](gate-operator.md)). Under
`GATE_DEPARTING_WITHHOLD=on` the `ActuationCoordinator` -- the one place every
automatic path goes through, after the cooldown and the one-pulse hold --
withholds the pulse from a grant whose passage reads as a car leaving, and
records it as the denial it is (`reason`/`actuation_outcome` `departing`). A
sweep frame, or a cloud answer on one, is judged by its own passage; the
camera's still by the current passage's reads since the latest alarm, never
by the car before it.

The judgement is the sweep's plate widths (`DepartingPlate.direction_at`, the
same one the cloud skip of invariant 5 reads): three boxed reads in a row at
most 70 % of the passage's widest plate. A plate standing still is never
leaving, so a car waiting at the gate is not, however big the car before it in
the same alarm. A grant whose passage has boxed a plate it cannot yet place is
**held** -- the one place a decided grant waits on anything -- for at most
`GATE_DEPARTING_HOLD_SECONDS` (2.0), never past its own decision deadline or
its frame's freshness, while the sweep reads on; a hold that ends undecided
pulses. A passage with nothing to judge by is never held. Nothing here waits on
the network, and nothing here can add, retry or hurry a pulse.

Where the hold waits: inside `ActuationCoordinator.actuate`, under the
coordinator's lock, on the thread that decided the frame -- the burst thread
for a frame the device read (invariant 2 is about the network, and the hold
polls a passage in memory and sleeps), the cloud lane for a cloud answer. The
sweep is a thread of its own and is never made to wait: its hand-over is a
queue put, and its reads during the hold are what settle it (invariant 6).
What a hold keeps waiting is another grant of the same passage, which its
outcome settles anyway. A pulse that follows a hold is recorded at the
moment it went, so both cooldowns run their full length from the pulse. The
judge is read without a lock; a read torn between the sweep's write of one
width and its verdict can only answer `undecided` or the verdict the whole
write gives, never a release the finished write would not.

A car withheld that was arriving is not stuck: no pulse was sent, so invariant
12 has nothing to hold it on; the sweep keeps reading (`departing` is a retry
reason) and lets it in the moment its plate grows back or stands still. A
person's command from the app is never asked, and never waits on the rule: a
command on its way to the lock ends a hold at once, the held grant is judged
on what the reads have said by then, and the command meets only what it
always met (the pulse's own two seconds, and its 20 s cooldown if that grant
pulsed). Shipped as `shadow`, which pulses exactly as before and journals
what `on` would have done; the criterion for `on` is in
[vehicle-direction.md](vehicle-direction.md#withholding-the-pulse).

What it cannot see: a departing car that stops beside the camera (its plate
grows), and the camera's still of one car decided after the next car's alarm
is judged by the next car's reads -- both bounded by the still being decided
on the device within a second or two of landing.

- **2026-10-10 18:26 to 10-14 15:30 UTC** -- eight of eighteen vehicle pulses
  went to departing cars. On 10-14 the gate began opening at 13:11:00 for a
  car leaving; the Pi pulsed for its rear plate at 13:11:13.85 and a closing
  run began three seconds later. Replayed, seven of the eight are withheld
  (three after a hold of 1.16-1.45 s); the eighth was the camera's still,
  granted before any plate was boxed, of a car that then stood beside the
  camera. None of the ten arrivals is withheld or held.
- **2026-10-13 13:37 UTC** -- the plate-shrink rule as #205 shipped it (two
  reads) judged an authorised arrival departing, 1.1 s after its pulse: the
  detector's box jumped 205 <-> 322 px on a car coming closer. Three reads now.

Guards:

- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_14_13_11_the_pulse_that_closed_an_opening_gate`
  (each of the eight replayed alarm -> sweep -> processor -> coordinator ->
  relay, the grant arriving as it did: sweep frame, cloud answer, or still)
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_11_16_19_a_cloud_grant_on_a_plate_already_receding`
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_12_12_59_a_cloud_grant_before_the_plate_had_receded_is_held_then_withheld`
  (held `undecided` at the grant and withheld `departing` after: the reads
  that settled it were the sweep's, taken on its own thread while the
  coordinator held the grant)
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_12_15_48_a_sweep_frame_long_after_the_verdict`
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_13_10_49_a_cloud_grant_two_reads_in_is_held_then_withheld`
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_13_16_08_a_sweep_frame_after_the_verdict`
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_14_15_17_a_sweep_frame_before_the_verdict_is_held_then_withheld`
- `tests/test_departing_withhold.py::DepartingPulseReplayTests::test_10_11_10_47_a_departure_with_no_plate_to_measure_still_pulses`
  (the rule withholds only on evidence)
- `tests/test_departing_withhold.py::ArrivalReplayTests::test_every_arrival_of_10_to_14_october_is_let_in_with_no_hold`
- `tests/test_departing_withhold.py::ArrivalReplayTests::test_the_10_13_13_37_arrival_is_never_judged_departing`
- `tests/test_departing_withhold.py::WithheldArrivalStillGetsInTests::test_a_car_wrongly_judged_departing_that_stands_at_the_gate_is_let_in`
- `tests/test_departing_withhold.py::WithheldArrivalStillGetsInTests::test_a_person_can_open_the_gate_for_a_car_judged_departing`
- `tests/test_departing_withhold.py::CameraStillTests::test_the_cameras_still_of_a_departing_car_is_withheld_by_the_passage_it_belongs_to`
- `tests/test_departing_withhold.py::CameraStillTests::test_the_next_cars_still_is_not_judged_by_the_car_before_it`
- `tests/test_departing_withhold.py::ModeTests::test_shadow_pulses_exactly_as_before_and_journals_what_on_would_have_done`
- `tests/test_departure.py::CoordinatorTests::test_the_hold_never_outlasts_the_frames_freshness`
- `tests/test_departure.py::CoordinatorTests::test_a_passage_still_undecided_when_the_hold_runs_out_is_pulsed`
- `tests/test_departure.py::CoordinatorTests::test_a_passage_that_cannot_be_read_withholds_nothing`
- `tests/test_departure.py::CoordinatorTests::test_the_cooldown_and_the_one_pulse_hold_are_asked_first`
- `tests/test_departure.py::CoordinatorTests::test_a_departure_never_starts_a_one_pulse_hold`
- `tests/test_departure.py::CoordinatorTests::test_a_person_at_the_app_is_never_asked`
- `tests/test_departure.py::CoordinatorTests::test_a_person_pressing_the_app_during_a_hold_ends_it_and_is_not_kept_waiting`
- `tests/test_departure.py::CoordinatorTests::test_a_held_pulse_is_recorded_at_the_moment_it_went_not_at_the_grant`
- `tests/test_departure.py::CoordinatorTests::test_shadow_pulses_and_says_what_on_would_have_done`
- `tests/test_trigger_capture.py::DepartingPlateTests::test_the_2026_10_13_arrival_whose_box_jumped_never_reads_as_receding`
