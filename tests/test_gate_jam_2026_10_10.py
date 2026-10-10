"""2026-10-10, 10:04-10:13 IST: one car, seven alarms, four pulses, a jammed gate.

A silver D-Max (`172L66`, authorised) waited about nine minutes at the gate.
The camera raised vehicle alarms at 10:04:11, 10:05:38, 10:06:44, 10:07:34,
10:08:14, 10:12:36 and 10:13:01. The Pi pulsed the relay four times for that
one car -- 10:04:13.9 (sweep frame), 10:06:45.8 (the next alarm's sweep
frame), 10:08:18.0 (the camera's 4K FTP still of the 10:08:14 alarm, 92 s
after the previous pulse; the sweep frame of the same alarm had been refused
by the cooldown at +89.6 s, and the still 2.4 s later passed) and 10:12:37.0
(sweep frame). Every read between was refused only by the 90 s cooldown. The
relay is on the operator's step-by-step input, so a pulse into a gate whose
state is unknown stops or reverses it; the leaves crossed and the gate jammed
(docs/reviews/2026-10-10-gate-jam.md, docs/invariants.md 12).

Replayed here the way the alarm goes, on the harness
``tests/test_sweep_pipeline.py`` built for it:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

inside the real ``run_worker``, with the real sweep reader, on-device
recogniser, OCR client, processor, store and coordinator, and the camera's
own FTP still of each alarm landing in the watched directory. The fakes sit
at the true boundaries: the frame source, the ONNX engine, the HTTP session,
the relay -- and the clock, which every component reads and the test moves
between alarms so nine minutes pass in a few seconds. Nothing here depends on
today's date (invariant 10): the clock is a fixed morning, offset from now.
"""
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep

from gate_controller.actuation import AUTOMATIC_PAUSED, REPULSE_HOLD
from gate_controller.automatic_open import AutomaticOpenConfig
from gate_controller.command_server import DirectCommandExecutor
from gate_controller.local_sweep import crop_to_region
from tests.test_early_trigger_pipeline import SpySession, early_gate
from tests.test_sweep_pipeline import (
    REGION, CapturedLogs, Gate, digest, frame, wait_for,
)

DMAX = "172L66"
#: The first alarm, 10:04:11 IST.
FIRST_ALARM = datetime(2026, 10, 10, 9, 4, 11, tzinfo=timezone.utc)
#: Every alarm the camera raised that morning, in seconds after the first.
ALARMS = (0.0, 87.0, 153.0, 203.0, 243.0, 505.0, 530.0)


class MovableClock:
    """One clock for the whole controller, that the test moves between passages.

    Inside a passage it runs at real speed, so the sweep's windows, the
    processor's freshness rule and the relay's timings all behave; between
    passages it is jumped to the next alarm's instant.
    """

    def __init__(self, start: datetime):
        self._start = start
        self._offset = 0.0
        self.jump_to(0.0)

    def jump_to(self, seconds_after_start: float) -> None:
        target = self._start + timedelta(seconds=seconds_after_start)
        self._offset = (target - datetime.now(timezone.utc)).total_seconds()

    def wall(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self._offset)

    def monotonic(self) -> float:
        return monotonic() + self._offset


class JamReplayTests(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _gate(self, **options):
        self.clock = MovableClock(FIRST_ALARM)
        options.setdefault("coordinator_options", {})
        self.gate = Gate(
            self, answers={}, sweep_seconds=1.0, authorised={DMAX},
            clock=self.clock.wall, monotonic_clock=self.clock.monotonic, **options,
        )
        return self.gate

    def _passage(self, gate, logs, index: int, *, at: float, expect_results: int):
        """One camera alarm at ``at`` s: its sweep, then the camera's own still."""
        self.clock.jump_to(at)
        frames = [frame(1000 + index * 10 + offset) for offset in range(4)]
        for data in frames:
            gate.engine.answers[digest(crop_to_region(data, REGION))] = (DMAX, 0.95)
        still = frame(2000 + index)
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = (DMAX, 0.954)
        gate.frames.script([(0.05 + offset * 0.12, data) for offset, data in enumerate(frames)])
        ended_before = logs.text().count("gate_local_sweep outcome=ended")
        gate.alarm()
        self.assertTrue(
            wait_for(lambda: logs.text().count("gate_local_sweep outcome=ended") > ended_before,
                     10.0),
            f"the sweep of alarm {index} never ended:\n{logs.text()}",
        )
        # The camera's 4K still of the same alarm arrives after the sweep's
        # first frame, as it did on the day (0.6-2.3 s behind the alarm).
        gate.ftp_still(still, name=f"still-{index}.jpg")
        self.assertTrue(
            wait_for(lambda: len(gate.outcomes()) >= expect_results, 10.0),
            f"alarm {index} was not decided: {gate.outcomes()}\n{logs.text()}",
        )
        # Give anything else that was going to happen the chance to.
        sleep(0.3)

    def test_the_waiting_dmax_gets_one_pulse_for_its_seven_alarms(self):
        gate = self._gate()
        with CapturedLogs() as logs:
            decided = 0
            for index, at in enumerate(ALARMS):
                with self.subTest(alarm=index, seconds_after_first=at):
                    decided += 2  # the sweep's authorised frame, and the still
                    self._passage(gate, logs, index, at=at, expect_results=decided)
                    self.assertEqual(gate.relay_calls, ["relay"],
                                     f"a second pulse after alarm {index}: {gate.outcomes()}")

        outcomes = gate.outcomes()
        self.assertEqual(gate.relay_calls, ["relay"], "exactly one pulse for the whole visit")
        self.assertEqual(outcomes[0], (True, "exact_match"), "the first sweep frame opens")
        self.assertEqual(
            {outcome for outcome in outcomes[1:]}, {(False, "cooldown"), (False, REPULSE_HOLD)},
            f"every later read is withheld, never let through: {outcomes}",
        )
        # The three pulses the day produced -- the 10:06:44 sweep frame, the
        # 10:08:14 still and the 10:12:36 sweep frame -- are all held, as is
        # everything else more than a cycle after the one pulse.
        held = [outcome for outcome in outcomes if outcome == (False, REPULSE_HOLD)]
        self.assertGreaterEqual(len(held), 10, outcomes)
        # Every withheld read is on record as the grant it was, with no pulse:
        # one row worked the relay, every other names what withheld it.
        with closing(sqlite3.connect(gate.store.path)) as connection:
            rows = connection.execute(
                "SELECT opened, reason, authorised_plate, actuation_outcome FROM events"
            ).fetchall()
        self.assertEqual(len(rows), len(outcomes))
        self.assertEqual([row[3] for row in rows].count(None), 1, rows)
        for opened, reason, plate, outcome in rows:
            self.assertEqual(opened, 1, rows)
            self.assertEqual(reason, "exact_match")
            self.assertEqual(plate, DMAX)
            self.assertIn(outcome, (None, "cooldown", REPULSE_HOLD))
        text = logs.text()
        self.assertIn("gate_actuation outcome=repulse_hold plate=172L66 "
                      "pulsed_at=2026-10-10T09:04:1", text)
        self.assertIn("gate_local_sweep outcome=ended reason=final_repulse_hold", text)
        self.assertNotIn("outcome=ended reason=opened", text.split("outcome=ended", 2)[2],
                         "a later sweep believed it had opened the gate")

    def test_the_same_car_back_after_the_window_is_let_in_again(self):
        """The hold lapses once the plate has been out of the record for the window."""
        gate = self._gate()
        with CapturedLogs() as logs:
            self._passage(gate, logs, 0, at=0.0, expect_results=2)
            self._passage(gate, logs, 1, at=200.0, expect_results=4)
            self.assertEqual(gate.relay_calls, ["relay"])
            # Gone for eleven minutes, then back.
            self._passage(gate, logs, 2, at=200.0 + 11 * 60, expect_results=6)

        self.assertEqual(gate.relay_calls, ["relay", "relay"])
        self.assertEqual(gate.outcomes()[4], (True, "exact_match"))

    def test_a_person_can_still_open_the_gate_while_the_car_is_held(self):
        gate = self._gate()
        with CapturedLogs() as logs:
            self._passage(gate, logs, 0, at=0.0, expect_results=2)
            self._passage(gate, logs, 1, at=200.0, expect_results=4)
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertEqual(gate.outcomes()[-1], (False, REPULSE_HOLD))

        executor = DirectCommandExecutor("primary", gate.coordinator, gate.store,
                                         clock=self.clock.wall)
        response = executor.execute({
            "controller_id": "primary", "command": "open_gate",
            "idempotency_key": "owner-watching-the-camera",
            "expires_at": (self.clock.wall() + timedelta(seconds=5)).isoformat(),
        })

        self.assertEqual(response, {"status": "completed"})
        self.assertEqual(gate.relay_calls, ["relay", "relay"])


class AutomaticOpenPausedReplayTests(unittest.TestCase):
    """GATE_AUTOMATIC_OPEN=off, through every automatic path there is."""

    PAUSED = {"automatic_open": lambda: AutomaticOpenConfig(enabled=False, source="environment")}

    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _authorised_frames(self, gate, seeds):
        frames = [frame(seed) for seed in seeds]
        for data in frames:
            gate.engine.answers[digest(crop_to_region(data, REGION))] = (DMAX, 0.95)
        return frames

    def test_a_sweep_frame_and_the_cameras_still_are_both_refused_but_a_person_is_not(self):
        self.gate = gate = Gate(self, answers={}, authorised={DMAX}, sweep_seconds=1.0,
                                coordinator_options=self.PAUSED)
        frames = self._authorised_frames(gate, range(3000, 3004))
        still = frame(3100)
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = (DMAX, 0.954)
        gate.frames.script([(0.05 + index * 0.12, data) for index, data in enumerate(frames)])

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 10.0))
            gate.ftp_still(still)
            self.assertTrue(wait_for(lambda: len(gate.outcomes()) >= 2, 10.0),
                            f"the still was never decided: {gate.outcomes()}\n{logs.text()}")
            sleep(0.3)

        self.assertEqual(gate.relay_calls, [], "a paused gate pulsed")
        self.assertEqual(set(gate.outcomes()), {(False, AUTOMATIC_PAUSED)})
        with gate._lock:
            results = [result for _at, _paths, result in gate.results]
        for result in results:
            stored = gate.stored(result)
            self.assertFalse(stored["opened"], "the gate did not open for this car")
            self.assertEqual(stored["reason"], AUTOMATIC_PAUSED)
            self.assertEqual(stored["authorised_plate"], DMAX)
            self.assertIsNone(stored["relay_activated_at"])
        self.assertIn("gate_actuation outcome=automatic_paused", logs.text())
        self.assertIn("gate_local_sweep outcome=ended reason=final_automatic_paused", logs.text())

        executor = DirectCommandExecutor("primary", gate.coordinator, gate.store)
        response = executor.execute({
            "controller_id": "primary", "command": "open_gate",
            "idempotency_key": "owner-at-the-app",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
        })
        self.assertEqual(response, {"status": "completed"})
        self.assertEqual(gate.relay_calls, ["relay"])

    def test_an_early_sweeps_frame_is_refused_too(self):
        session = SpySession()
        self.gate = gate = early_gate(self, answers={}, cloud=session, authorised={DMAX},
                                      early_max_seconds=3.0, coordinator_options=self.PAUSED)
        frames = self._authorised_frames(gate, range(3200, 3204))
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])

        with CapturedLogs() as logs:
            self.assertEqual(gate.capture.on_early_trigger({"blob_fraction": 0.1}), "scheduled")
            self.assertTrue(wait_for(lambda: gate.outcomes(), 10.0),
                            f"the early frame was never decided:\n{logs.text()}")
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 10.0))

        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(set(gate.outcomes()), {(False, AUTOMATIC_PAUSED)})
        self.assertEqual(session.posted_at, [], "an early-origin passage asked the cloud reader")

    def test_a_presence_frame_is_refused_too(self):
        """The sweep reads nothing it can authorise; the presence session's own
        frame is read by the pipeline, matched, and refused at the coordinator."""
        self.gate = gate = Gate(self, answers={}, authorised={DMAX}, sweep_seconds=0.6,
                                presence_frames=2, presence_seconds=4.0, presence_spacing=0.1,
                                coordinator_options=self.PAUSED)
        unread = [frame(seed) for seed in range(3300, 3303)]
        for data in unread:
            gate.engine.answers[digest(crop_to_region(data, REGION))] = (None, 0.0)
        # Frames for the presence session to find whenever it starts: the
        # sweep cannot read them, the pipeline's own read of them is authorised.
        later = [frame(seed) for seed in range(3350, 3354)]
        for data in later:
            gate.engine.answers[digest(crop_to_region(data, REGION))] = (None, 0.0)
            gate.engine.answers[digest(gate.pipeline_bytes(data))] = (DMAX, 0.95)
        gate.frames.script(
            [(0.05 + index * 0.12, data) for index, data in enumerate(unread)]
            + [(0.9 + index * 0.8, data) for index, data in enumerate(later)]
        )

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: gate.outcomes(), 10.0),
                            f"no presence frame was decided:\n{logs.text()}")
            sleep(0.3)

        self.assertEqual(gate.relay_calls, [])
        self.assertIn((False, AUTOMATIC_PAUSED), gate.outcomes())
        self.assertIn("outcome=presence_retry", logs.text())
        self.assertNotIn((True, "exact_match"), gate.outcomes())


if __name__ == "__main__":
    unittest.main()
