"""The departure rule at the coordinator, and the judgement it reads (#171).

The coordinator half goes through the path production uses from the
processor on, as ``tests/test_one_pulse_per_car.py`` does: the real
``GateProcessor`` and ``DirectCommandExecutor`` sharing one real
``ActuationCoordinator`` over a real ``RelayController`` on a fake GPIO and a
real ``LocalStore``. The clocks, the plate reader and the passage's reads are
the fakes: what the passage says is scripted, so each rule can be driven to
its edge without real seconds passing. The alarm-to-relay replays of the
passages the rule was measured on are in ``tests/test_departing_withhold.py``.
"""
import logging
import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from threading import Thread
from time import monotonic, sleep as real_sleep

from PIL import Image

from gate_controller.__main__ import (
    _automatic_open_config, _departure_reading, actuation_cooldowns, repulse_unseen,
)
from gate_controller.actuation import DEPARTING, WITHHELD_OUTCOMES, ActuationCoordinator
from gate_controller.automatic_open import load_config as load_automatic_open
from gate_controller.command_server import DirectCommandExecutor
from gate_controller.departure import (
    APPROACHING, DEFAULT_HOLD_SECONDS, DEFAULT_MODE, FAR, STANDING, UNDECIDED,
    DepartureConfig, DirectionReading, load_config, settle,
)
from gate_controller.processor import GateProcessor
from gate_controller.relay import RelayController
from gate_controller.store import _telemetry_payload
from gate_controller.trigger_capture import DepartingPlate
from tests.test_event_contract import (
    assert_ingest_accepts_event, assert_ingest_accepts_telemetry,
)
from tests.test_one_pulse_per_car import AUDI, DMAX, START, OnePulsePerCarHarness


def reading(state, peak=380, latest=260, reads=4):
    return DirectionReading(state, peak_px=peak, latest_px=latest, reads=reads)


class ScriptedPassage:
    """What the capture's current passage says, one answer per question.

    The last answer repeats. ``None`` is a passage with nothing to judge by.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    def __call__(self):
        self.asked += 1
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0] if self.answers else None


class DepartureHarness(OnePulsePerCarHarness):
    def build(self, environment, *, boot_id="boot-1", uptime_at_start=1000.0,
              authorised=None, mode="on", hold=DEFAULT_HOLD_SECONDS, passage=None,
              spawned=None, sleep=None):
        """Wire the controller the way ``main`` does, the departure rule included.

        ``sleep`` is the hold's own wait; by default it moves the controller's
        clocks and nothing else, so a hold costs the test no real seconds.
        """
        self.uptime_at_start = uptime_at_start
        self.passage = passage or ScriptedPassage(None)
        self.slept = []
        automatic, command = actuation_cooldowns(environment)
        defaults = load_automatic_open(environment)
        relay = RelayController(
            self.gpio, sleeper=lambda _seconds: None, clock=self.wall_clock,
        )

        def fake_sleep(seconds):
            # The hold's own wait: the controller's clocks move, nothing else.
            self.slept.append(seconds)
            self.elapsed += seconds

        sleep = sleep or fake_sleep
        self.coordinator = ActuationCoordinator(
            self.store, relay, automatic, clock=self.wall_clock,
            monotonic_clock=self.monotonic_clock, boot_id=boot_id,
            command_cooldown=command, repulse_unseen=repulse_unseen(environment),
            automatic_open=lambda: _automatic_open_config(self.settings, defaults),
            departure=DepartureConfig(mode=mode, hold_seconds=hold),
            departure_reading=lambda: self.passage(),
            sleep=sleep,
            spawn=(spawned.append if spawned is not None else None),
        )
        self.processor = GateProcessor(
            recognizer=self.reader, store=self.store, relay=relay,
            authorised=authorised or self.AUTHORISED, cooldown=automatic,
            coordinator=self.coordinator, clock=self.wall_clock,
        )
        self.addCleanup(self.processor.close)
        self.executor = DirectCommandExecutor(
            "primary", self.coordinator, self.store, clock=self.wall_clock,
        )

    def assert_departing(self, result, plate=DMAX):
        """No pulse, and on record as the denial it is."""
        self.assertFalse(result.opened)
        self.assertEqual(result.reason, DEPARTING)
        row = self.event(result.event_id)
        self.assertEqual(row["opened"], 0, "the gate was not opened for a leaving car")
        self.assertEqual(row["reason"], DEPARTING)
        self.assertEqual(row["actuation_outcome"], DEPARTING)
        self.assertEqual(row["authorised_plate"], plate)
        self.assertIsNone(row["relay_activated_at"])


class CoordinatorTests(DepartureHarness):
    def test_a_grant_for_a_car_judged_departing_is_withheld_and_recorded_as_departing(self):
        self.build({}, passage=ScriptedPassage(reading("departing", latest=181)))
        with self.assertLogs("gate_controller.actuation", level="WARNING") as logs:
            result = self.plate_read(3.7)
        self.assert_departing(result)
        self.assertEqual(self.gpio.pulses, 0)
        self.assertEqual(self.slept, [], "a decided passage is not held")
        self.assertIn(
            "gate_actuation departure=on outcome=withheld state=departing peak_px=380 "
            "latest_px=181 reads=4 first=departing held_ms=0 plate=172L66",
            "\n".join(logs.output),
        )
        # What the app is told is a denial named for what it was, and the
        # ingest contract takes it as it stands.
        payload = self.store.event_payload(result.event_id)
        payload.setdefault("schema_version", 3)
        payload.setdefault("controller_id", "primary")
        payload.setdefault("event_id", result.event_id)
        self.assertEqual((payload["opened"], payload["reason"]), (False, DEPARTING))
        assert_ingest_accepts_event(payload)
        telemetry = _telemetry_payload(result.telemetry)
        assert_ingest_accepts_telemetry(telemetry)
        self.assertEqual(telemetry["decision"], {"outcome": "denied", "reason": DEPARTING})
        self.assertEqual(telemetry["actuation"]["claim"], DEPARTING)
        self.assertFalse(telemetry["actuation"]["attempted"])
        self.assertIn(DEPARTING, WITHHELD_OUTCOMES)

    def test_an_undecided_passage_is_held_until_its_reads_say_departing(self):
        passage = ScriptedPassage(
            reading(UNDECIDED, latest=409), reading(UNDECIDED, latest=288),
            reading(UNDECIDED, latest=245), reading("departing", latest=210),
        )
        self.build({}, passage=passage)
        result = self.plate_read(3.34)
        self.assert_departing(result)
        self.assertEqual(self.gpio.pulses, 0)
        self.assertLessEqual(sum(self.slept), DEFAULT_HOLD_SECONDS)
        self.assertGreater(sum(self.slept), 0)

    def test_an_undecided_passage_that_turns_out_to_approach_is_pulsed_the_moment_it_does(self):
        passage = ScriptedPassage(
            reading(UNDECIDED, peak=241, latest=241), reading(APPROACHING, peak=322, latest=322),
        )
        self.build({}, passage=passage)
        with self.assertLogs("gate_controller.actuation", level="WARNING") as logs:
            result = self.plate_read(2.7)
        self.assert_pulsed(result)
        self.assertEqual(self.slept, [0.05], "held one look, not the whole hold")
        self.assertIn("departure=on outcome=released state=approaching", "\n".join(logs.output))

    def test_a_passage_still_undecided_when_the_hold_runs_out_is_pulsed(self):
        # Withholding is on evidence only: a hold that ends undecided pulses,
        # exactly as the gate always did.
        self.build({}, passage=ScriptedPassage(reading(UNDECIDED)))
        result = self.plate_read(3.0)
        self.assert_pulsed(result)
        self.assertAlmostEqual(sum(self.slept), DEFAULT_HOLD_SECONDS, places=6)

    def test_the_hold_never_outlasts_the_frames_freshness(self):
        # A frame already 6.9 s old has 1.1 s of its 8 s left. The hold stops
        # short of it, and the pulse still lands on a fresh frame.
        self.build({}, passage=ScriptedPassage(reading(UNDECIDED)))
        self.elapsed = 10.0
        result = self.plate_read_received(10.0, received_seconds_ago=6.9)
        self.assert_pulsed(result)
        self.assertLess(sum(self.slept), 1.1 - 0.2)
        self.assertGreater(sum(self.slept), 0.5)

    def test_nothing_to_judge_by_is_pulsed_with_no_hold(self):
        answers = (None, reading(FAR, peak=134, latest=134, reads=1),
                   reading(APPROACHING), reading(STANDING))
        for index, answer in enumerate(answers):
            with self.subTest(answer=answer):
                self.build({}, passage=ScriptedPassage(answer))
                # Each visit more than the one-pulse hold's ten minutes apart.
                self.assert_pulsed(self.plate_read(3.0 + 800.0 * index))
                self.assertEqual(self.slept, [])

    def test_a_passage_that_cannot_be_read_withholds_nothing(self):
        def broken():
            raise RuntimeError("the capture fell over")

        self.build({})
        self.passage = broken
        with self.assertLogs("gate_controller.departure", level="WARNING"):
            result = self.plate_read(3.0)
        self.assert_pulsed(result)

    def test_a_person_pressing_the_app_during_a_hold_ends_it_and_is_not_kept_waiting(self):
        """The hold sits under the coordinator's lock, which a person's command
        from the app needs too. The command is never judged, and it is not
        made to wait out the hold either: it ends the hold the moment it
        arrives, the held grant is judged on what the reads have said by
        then (here: nothing, so it pulses as it always did), and the person's
        command then meets the pulse it would always have met."""
        def sleep(seconds):
            # The hold's wait, with enough real time in it for the press.
            self.slept.append(seconds)
            self.elapsed += seconds
            real_sleep(0.02)

        self.build({}, passage=ScriptedPassage(reading(UNDECIDED)), sleep=sleep)
        results = {}

        def grant():
            with self.assertLogs("gate_controller.actuation", level="WARNING") as logs:
                results["grant"] = self.plate_read(3.0)
            results["log"] = "\n".join(logs.output)

        reader = Thread(target=grant)
        reader.start()
        deadline = monotonic() + 5.0
        while not self.slept and monotonic() < deadline:
            real_sleep(0.001)
        self.assertTrue(self.slept, "the grant was never held")
        pressed = monotonic()
        response = self.human_command(self.elapsed)
        waited = monotonic() - pressed
        reader.join(5.0)
        self.assertFalse(reader.is_alive())

        self.assert_pulsed(results["grant"])
        self.assertEqual(self.gpio.pulses, 1)
        self.assertLess(sum(self.slept), DEFAULT_HOLD_SECONDS / 2, "the hold ran on")
        self.assertIn("outcome=released", results["log"])
        self.assertIn("cut_short=command", results["log"])
        # The command waited for the pulse it was always going to meet (the
        # relay's own contact, here instantaneous), not for the hold.
        self.assertLess(waited, 0.5)
        self.assertEqual(response, {"status": "failed", "detail": "cooldown"})
        # And the next grant holds as before: the press is spent.
        self.build({}, passage=ScriptedPassage(reading(UNDECIDED)))
        self.assert_pulsed(self.plate_read(900.0, plate=AUDI), plate=AUDI)
        self.assertAlmostEqual(sum(self.slept), DEFAULT_HOLD_SECONDS, places=6)

    def test_a_held_pulse_is_recorded_at_the_moment_it_went_not_at_the_grant(self):
        """The cooldowns are measured from the pulse. A grant held for two
        seconds pulses two seconds after it was granted, and the record the
        store keeps of the attempt (what the cooldown reads after a restart,
        when the relay reported no instant) must say so, or the 90 s window
        would run from the grant and end two seconds early."""
        self.build({}, passage=ScriptedPassage(reading(UNDECIDED)))
        result = self.plate_read(3.0)
        self.assert_pulsed(result)
        self.assertAlmostEqual(sum(self.slept), DEFAULT_HOLD_SECONDS, places=6)
        with closing(sqlite3.connect(self.database)) as connection:
            attempt_at, attempt_monotonic = connection.execute(
                "SELECT activation_attempt_at, activation_monotonic FROM actuation_claims "
                "WHERE event_id = ?", (result.event_id,)
            ).fetchone()
        pulsed_at = START + timedelta(seconds=3.0 + DEFAULT_HOLD_SECONDS)
        self.assertEqual(attempt_at, pulsed_at.isoformat())
        self.assertAlmostEqual(
            attempt_monotonic, self.uptime_at_start + 3.0 + DEFAULT_HOLD_SECONDS, places=6,
        )
        row = self.event(result.event_id)
        self.assertEqual(row["relay_activated_at"], pulsed_at.isoformat())

    def test_a_person_at_the_app_is_never_asked(self):
        self.build({}, passage=ScriptedPassage(reading("departing")))
        self.assertEqual(self.human_command(1.0), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 1)
        self.assertEqual(self.passage.asked, 0)

    def test_the_cooldown_and_the_one_pulse_hold_are_asked_first(self):
        # A read inside the gate's own cycle says cooldown, the truth it always
        # was: departing names only pulses the other rules would have let go.
        self.build({}, passage=ScriptedPassage(None))
        self.assert_pulsed(self.plate_read(2.0))
        self.passage = ScriptedPassage(reading("departing"))
        self.assert_withheld(self.plate_read(30.0), "cooldown")
        self.assert_withheld(self.plate_read(200.0), "repulse_hold")
        self.assertEqual(self.passage.asked, 0)

    def test_a_departure_never_starts_a_one_pulse_hold(self):
        # Withheld at 13:11, back as an arrival eleven minutes later: no pulse
        # was ever sent for the departure, so there is nothing to hold.
        self.build({}, passage=ScriptedPassage(reading("departing")))
        self.assert_departing(self.plate_read(5.9))
        self.passage = ScriptedPassage(reading(APPROACHING))
        self.assert_pulsed(self.plate_read(60.0))

    def test_shadow_pulses_and_says_what_on_would_have_done(self):
        spawned = []
        cases = ((reading("departing"), "withhold"), (reading(UNDECIDED), "hold"),
                 (reading(APPROACHING), "pulse"), (None, "pulse"))
        for index, (answer, would) in enumerate(cases):
            with self.subTest(would=would):
                spawned.clear()
                self.build({}, mode="shadow", passage=ScriptedPassage(answer), spawned=spawned)
                with self.assertLogs("gate_controller.actuation", level="INFO") as logs:
                    # Each visit more than the one-pulse hold's ten minutes apart.
                    result = self.plate_read(3.0 + 800.0 * index)
                self.assertTrue(result.opened, result.reason)
                self.assertIn(f"gate_actuation departure=shadow would={would}",
                              "\n".join(logs.output))
                self.assertEqual(self.slept, [], "shadow held a grant")
                self.assertEqual(len(spawned), 1 if would == "hold" else 0)

    def test_shadow_follows_the_hold_it_did_not_make(self):
        spawned = []
        passage = ScriptedPassage(reading(UNDECIDED), reading(UNDECIDED), reading("departing"))
        self.build({}, mode="shadow", passage=passage, spawned=spawned)
        self.assertTrue(self.plate_read(3.0).opened)
        with self.assertLogs("gate_controller.actuation", level="INFO") as logs:
            spawned[0]()
        self.assertIn("gate_actuation departure=shadow stage=settled would=withhold "
                      "state=departing", "\n".join(logs.output))

    def test_off_asks_nothing(self):
        self.build({}, mode="off", passage=ScriptedPassage(reading("departing")))
        self.assert_pulsed(self.plate_read(3.0))
        self.assertEqual(self.passage.asked, 0)

    def test_the_heartbeat_reports_the_rule(self):
        self.build({}, mode="shadow", hold=1.5)
        status = self.coordinator.status()
        self.assertEqual(status["departing_withhold"], "shadow")
        self.assertEqual(status["departing_hold_seconds"], 1.5)

    def plate_read_received(self, at, *, received_seconds_ago):
        self.elapsed = at
        self.frames += 1
        frame = self.directory / f"frame-{self.frames}.jpg"
        Image.new("L", (16, 8), color=self.frames % 256).save(frame, format="JPEG")
        return self.processor.process(
            (frame,), received_at=self.wall_clock() - timedelta(seconds=received_seconds_ago),
        )


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def test_it_waits_only_while_undecided(self):
        passage = ScriptedPassage(reading(UNDECIDED), reading(UNDECIDED), reading(APPROACHING))
        final, held = settle(passage, hold_seconds=2.0, clock=self.clock, sleep=self.sleep)
        self.assertEqual(final.state, APPROACHING)
        self.assertAlmostEqual(held, 0.1)

    def test_it_is_bounded_by_the_budget_less_a_margin(self):
        final, held = settle(ScriptedPassage(reading(UNDECIDED)), hold_seconds=2.0,
                             budget=lambda: 1.0 - self.now, clock=self.clock, sleep=self.sleep)
        self.assertEqual(final.state, UNDECIDED)
        self.assertLessEqual(held, 0.75 + 1e-9)
        self.assertGreater(held, 0.6)

    def test_a_person_waiting_for_the_lock_ends_it_at_once(self):
        asked = []

        def person_waiting():
            asked.append(True)
            return len(asked) >= 3

        final, held = settle(ScriptedPassage(reading(UNDECIDED)), hold_seconds=2.0,
                             clock=self.clock, sleep=self.sleep, stop=person_waiting)
        self.assertEqual(final.state, UNDECIDED)
        self.assertAlmostEqual(held, 0.1)

    def test_a_failing_clock_or_budget_ends_the_wait(self):
        def broken():
            raise RuntimeError

        final, held = settle(ScriptedPassage(reading(UNDECIDED)), hold_seconds=2.0,
                             budget=broken, clock=self.clock, sleep=self.sleep)
        self.assertEqual((final.state, held), (UNDECIDED, 0.0))


class ConfigTests(unittest.TestCase):
    def test_the_shipped_default_is_shadow_with_a_two_second_hold(self):
        self.assertEqual(load_config({}), DepartureConfig("shadow", 2.0))
        self.assertEqual(DEFAULT_MODE, "shadow")

    def test_each_mode_and_hold_is_read(self):
        for mode in ("off", "shadow", "on", " ON "):
            with self.subTest(mode=mode):
                self.assertEqual(load_config({"GATE_DEPARTING_WITHHOLD": mode}).mode,
                                 mode.strip().lower())
        self.assertEqual(load_config({"GATE_DEPARTING_HOLD_SECONDS": "1.5"}).hold_seconds, 1.5)
        self.assertEqual(load_config({"GATE_DEPARTING_HOLD_SECONDS": "0"}).hold_seconds, 0.0)

    def test_a_typo_neither_raises_nor_switches_the_rule_on(self):
        with self.assertLogs("gate_controller.departure", level=logging.ERROR):
            self.assertEqual(load_config({"GATE_DEPARTING_WITHHOLD": "yes"}).mode, "shadow")
        for hold in ("soon", "-1", "9", "nan", "inf"):
            with self.subTest(hold=hold), self.assertLogs("gate_controller.departure",
                                                          level=logging.ERROR):
                self.assertEqual(
                    load_config({"GATE_DEPARTING_HOLD_SECONDS": hold}).hold_seconds, 2.0,
                )

    def test_main_reads_the_capture_and_answers_none_without_one(self):
        self.assertIsNone(_departure_reading(None))

        class Capture:
            def departure_reading(self):
                return reading(APPROACHING)

        class Broken:
            def departure_reading(self):
                raise RuntimeError

        self.assertEqual(_departure_reading(Capture()).state, APPROACHING)
        self.assertIsNone(_departure_reading(Broken()))


class DirectionJudgementTests(unittest.TestCase):
    """`DepartingPlate.direction_at`, the one judgement both rules read."""

    def feed(self, widths, *, start=100.0, step=0.3):
        judge = DepartingPlate()
        judge.alarm(start - 1.0)
        at = start
        for width in widths:
            judge.note(width, at)
            at += step
        return judge, at - step

    def state(self, widths, **kwargs):
        judge, last = self.feed(widths, **kwargs)
        found = judge.direction_at(last, since_alarm=False)
        return None if found is None else found.state

    def test_states_on_the_journals_own_series(self):
        # 10-13 10:49, the cloud's grant two reads in: undecided.
        self.assertEqual(self.state([416, 409]), UNDECIDED)
        # ...and three reads under 70 % of 416 later: departing.
        self.assertEqual(self.state([416, 409, 288, 293, 245, 229, 210]), "departing")
        # 10-13 13:37, the arrival granted at 205 px after 110: approaching.
        self.assertEqual(self.state([110, 205]), APPROACHING)
        # ...and in its box's jump back to 205 from 322: undecided, never departing.
        self.assertEqual(self.state([110, 205, 322, 205, 205, 241]), UNDECIDED)
        # 10-11 12:36, the camera's still granted with one 134 px plate boxed.
        self.assertEqual(self.state([134]), FAR)

    def test_nothing_to_judge_by(self):
        judge = DepartingPlate()
        self.assertIsNone(judge.direction_at(100.0, since_alarm=False))
        judge, last = self.feed([416, 409])
        self.assertIsNone(judge.direction_at(last + 10.1, since_alarm=False), "stale")
        judge.alarm(last + 0.5)
        self.assertIsNone(judge.direction_at(last + 0.6, since_alarm=True),
                          "the next car's still judged by the last car's reads")
        self.assertEqual(judge.direction_at(last + 0.6, since_alarm=False).state, UNDECIDED,
                         "the last car's own frames keep their passage")

    def test_a_car_standing_at_the_gate_is_not_leaving_however_big_the_car_before_it(self):
        # One alarm, two cars: a big plate leaves, a smaller one comes up and
        # stops at 370 px -- under 70 % of 583, so its reads count as a shrink.
        leaving = [583, 520, 470, 400]
        standing = [370, 372] * 5
        judge, last = self.feed(leaving + standing[:3], step=0.25)
        self.assertEqual(judge.direction_at(last, since_alarm=False).state, "departing")
        judge, last = self.feed(leaving + standing, step=0.25)
        self.assertTrue(judge.standing())
        self.assertFalse(judge.receding)
        self.assertEqual(judge.direction_at(last, since_alarm=False).state, STANDING)

    def test_a_departing_car_that_wavers_on_its_way_out_is_not_standing(self):
        # 10-12 12:59: 259, 241, 259 over 1.4 s, then on out.
        judge, _last = self.feed([366, 259, 241, 259, 212, 205, 181], step=0.4)
        self.assertFalse(judge.standing())
        self.assertTrue(judge.receding)


if __name__ == "__main__":
    unittest.main()
