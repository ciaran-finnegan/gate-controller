"""The camera's alarm sound is not the gate, and only before it was switched off.

Until 2026-10-10 15:27 IST the camera played an alarm through its speaker at
every vehicle alarm, and the sound scanner wrote it into ``gate_movements`` as
a motor run (docs/reviews/2026-10-10-gate-jam.md). Every test here takes the
alarm the way production does -- the camera's webhook into the real sweep,
processor and store, which keep it in the event telemetry -- writes the
scanner's rows with the scanner's own ``record()``, and reads them back
through the heartbeat (``_controller_status``) or the other readers of the
table. None of them builds an alarm row by hand.
"""
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import gate_controller.__main__ as gate_main
from gate_controller import direction_passages
from gate_controller.gate_alarm_sound import (
    ALARM_SOUND_OFF_AT, ALARM_SOUND_WITHIN_SECONDS, alarm_instants, is_alarm_sound,
)
from gate_controller.gate_audio_detect import MotorRun, movements_from
from gate_controller.gate_left_open import LIKELY, POSSIBLE
from gate_controller.gate_sound_scan import Listening, record
from gate_controller.local_sweep import crop_to_region
from tests.test_gate_jam_2026_10_10 import MovableClock
from tests.test_gate_left_open import NINTH_OF_OCTOBER, LAST_HEARD, Prompt, RelaySpy
from tests.test_sweep_pipeline import REGION, CapturedLogs, Gate, digest, frame, wait_for

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
#: The departing Audi's alarm, 9 Oct 21:42:23.774 IST, as the journal has it.
NINTH_ALARM = datetime(2026, 10, 9, 20, 42, 23, 774448, tzinfo=UTC)
#: The plate the alarm's frame reads. What matters here is only that the
#: alarm reaches the store the way every camera alarm does; a read the sweep
#: hands to the pipeline is the shortest way there.
PLATE = "131D2696"
last_state = None


def setUpModule():
    """Import the script when the tests run, not when they are collected.

    ``scripts/scan_gate_sound.py`` puts the repository root at the front of
    ``sys.path`` as it loads, which is right for a script and wrong during
    discovery: the root holds a legacy ``test_relay.py`` that would then
    shadow ``tests/test_relay.py`` (as in test_direction_scan.py).
    """
    global last_state
    from scripts.scan_gate_sound import last_state as function
    last_state = function


def run(start: str, end: str) -> MotorRun:
    return MotorRun(start=datetime.fromisoformat(start), end=datetime.fromisoformat(end))


class AlarmSoundThroughThePipeline(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _alarm_at(self, moment: datetime, *, seed: int = 1):
        """One camera alarm at ``moment``: the webhook, its sweep, one read."""
        if self.gate is None:
            self.clock = MovableClock(moment)
            self.gate = Gate(self, answers={}, sweep_seconds=0.6, authorised={PLATE},
                             clock=self.clock.wall, monotonic_clock=self.clock.monotonic)
            self.start = moment
        self.clock.jump_to((moment - self.start).total_seconds())
        before = len(self.gate.outcomes())
        data = frame(seed)
        self.gate.engine.answers[digest(crop_to_region(data, REGION))] = (PLATE, 0.95)
        self.gate.frames.script([(0.05, data)])
        with CapturedLogs() as logs:
            self.gate.alarm()
            self.assertTrue(wait_for(lambda: len(self.gate.outcomes()) > before, 10.0),
                            f"the alarm's frame was never decided:\n{logs.text()}")
        return self.gate.store

    def _connection(self, store):
        connection = sqlite3.connect(store.path)
        self.addCleanup(connection.close)
        return connection

    def _scan(self, connection, runs, *, segment="gate-a.aac"):
        """Rows exactly as ``scripts/scan_gate_sound.py`` writes them."""
        moves = movements_from([run(a, b) for a, b in runs], [])
        record(connection, moves, [(segment, 100)])

    def _listened(self, connection, start: datetime, minutes: int):
        coverage = []
        at = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % 5)
        while at < start + timedelta(minutes=minutes):
            coverage.append({
                "segment": f"gate-{at:%Y%m%dT%H%M%S}Z.aac", "started_at": at,
                "span_seconds": 300.0, "audio_seconds": 300.0, "missing_seconds": 0.0,
            })
            at += timedelta(minutes=5)
        record(connection, [], [], listening=Listening(tuple(coverage), ()))

    def _heartbeat(self, store, now: datetime):
        return gate_main._controller_status(
            store, Prompt(), {}, relay=RelaySpy(), clock=lambda: now,
        )

    # -- the case it exists for ---------------------------------------------------

    def test_the_ninth_of_october_burst_is_two_runs_once_the_alarm_sound_is_set_aside(self):
        """9 Oct 21:42 IST: the third "run" started 0.07 s after the camera's alarm.

        On the scanner's rows alone this was the left-open check's `likely`
        signature (open, close, reverse). The 11 s run at 21:42:23.84 is the
        camera's own alarm sound, clipping the microphone; without it the
        burst is two runs, which is an ordinary unconfirmed cycle.
        """
        store = self._alarm_at(NINTH_ALARM)
        connection = self._connection(store)
        self._scan(connection, NINTH_OF_OCTOBER)
        self._listened(connection, LAST_HEARD - timedelta(minutes=2), 15)

        left_open = self._heartbeat(store, LAST_HEARD + timedelta(minutes=12))["gate"]["left_open"]

        self.assertEqual(left_open["state"], "open")
        self.assertEqual(left_open["confidence"], POSSIBLE)
        self.assertEqual(left_open["runs"], 2)
        self.assertNotEqual(left_open["confidence"], LIKELY)
        self.assertEqual(datetime.fromisoformat(left_open["since"]),
                         datetime.fromisoformat(NINTH_OF_OCTOBER[1][1]))
        # The row is still there: the classification is applied when it is
        # read, never by deleting what the scanner heard.
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM gate_movements").fetchone()[0], 3)

    def test_the_alarm_instant_is_the_moment_the_pi_received_the_webhook(self):
        store = self._alarm_at(NINTH_ALARM)
        connection = self._connection(store)
        instants = alarm_instants(connection, NINTH_ALARM - timedelta(seconds=30),
                                  NINTH_ALARM + timedelta(seconds=30))
        self.assertTrue(instants, "the alarm left nothing in the event telemetry")
        for instant in instants:
            self.assertLess(abs((instant - NINTH_ALARM).total_seconds()), 0.5)

    def test_the_heartbeat_counts_and_state_leave_the_alarm_sound_out(self):
        store = self._alarm_at(NINTH_ALARM)
        connection = self._connection(store)
        self._scan(connection, NINTH_OF_OCTOBER)

        gate = self._heartbeat(store, LAST_HEARD + timedelta(minutes=1))["gate"]

        self.assertEqual(gate["movements_24h"], 2)
        self.assertEqual(datetime.fromisoformat(gate["since"]),
                         datetime.fromisoformat(NINTH_OF_OCTOBER[1][1]))
        # The scanner's next run starts from where the gate was left, which is
        # not where the alarm sound "left" it.
        self.assertEqual(last_state(connection), "open")
        self.assertTrue(is_alarm_sound(connection, NINTH_OF_OCTOBER[2][0]))

    def test_a_run_that_starts_after_the_alarm_sound_is_not_set_aside(self):
        """Before 15:27 too, the commanded opening a few seconds later is the gate."""
        store = self._alarm_at(NINTH_ALARM)
        connection = self._connection(store)
        # The Pi pulses ~2-5 s after an alarm and the motor starts 1.3 s later.
        opening = NINTH_ALARM + timedelta(seconds=ALARM_SOUND_WITHIN_SECONDS + 3.0)
        self._scan(connection, [(opening.isoformat(),
                                 (opening + timedelta(seconds=20)).isoformat())])

        self.assertFalse(is_alarm_sound(connection, opening.isoformat()))
        gate = self._heartbeat(store, opening + timedelta(minutes=1))["gate"]
        self.assertEqual(gate["movements_24h"], 1)

    def test_after_the_sound_was_switched_off_a_run_at_the_alarm_is_the_gate(self):
        """Not a blanket post-alarm exclusion: from 15:27 the run is the motor."""
        alarm = ALARM_SOUND_OFF_AT + timedelta(days=2, hours=1, seconds=17.25)
        store = self._alarm_at(alarm)
        connection = self._connection(store)
        start = alarm + timedelta(seconds=0.5)
        self._scan(connection, [(start.isoformat(), (start + timedelta(seconds=20)).isoformat())])

        self.assertTrue(alarm_instants(connection, alarm - timedelta(seconds=2),
                                       alarm + timedelta(seconds=2)))
        self.assertFalse(is_alarm_sound(connection, start.isoformat()))
        gate = self._heartbeat(store, start + timedelta(minutes=1))["gate"]
        self.assertEqual(gate["movements_24h"], 1)
        self.assertEqual(datetime.fromisoformat(gate["since"]), start + timedelta(seconds=20))

    def test_the_direction_scan_does_not_see_the_alarm_sound_as_the_gate(self):
        store = self._alarm_at(NINTH_ALARM)
        connection = self._connection(store)
        self._scan(connection, NINTH_OF_OCTOBER)

        seen = []
        judge = direction_passages.judge

        def spy(evidence, **kwargs):
            seen.append(evidence)
            return judge(evidence, **kwargs)

        with patch.object(direction_passages, "judge", spy):
            direction_passages.judge_passages(
                connection, (NINTH_ALARM - timedelta(minutes=5)).isoformat())

        self.assertTrue(seen, "the alarm's passage was never judged")
        heard = {movement.started_at for evidence in seen for movement in evidence.movements}
        self.assertIn(datetime.fromisoformat(NINTH_OF_OCTOBER[1][0]).timestamp(), heard)
        siren = datetime.fromisoformat(NINTH_OF_OCTOBER[2][0]).timestamp()
        self.assertNotIn(siren, heard)


class NothingThatReadsTheRowsCanMoveTheGate(unittest.TestCase):
    def test_the_classification_and_the_scanner_import_nothing_that_can_move_the_gate(self):
        """Invariant 11: a fresh interpreter, so nothing else loaded the relay first."""
        loaded = subprocess.run(
            [sys.executable, "-c",
             "import sys, gate_controller.gate_alarm_sound, gate_controller.gate_sound_scan;"
             "print(' '.join(sorted(m for m in sys.modules if m.startswith('gate_controller'))))"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertIn("gate_controller.gate_alarm_sound", loaded)
        for forbidden in ("relay", "relay_safe", "actuation", "command_server", "processor",
                          "worker", "control_plane"):
            self.assertNotIn(f"gate_controller.{forbidden}", loaded)


class AlarmSoundWithoutAnAlarmOnRecord(unittest.TestCase):
    def test_a_database_with_no_event_tables_sets_nothing_aside(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.assertFalse(is_alarm_sound(connection, NINTH_OF_OCTOBER[2][0]))
        self.assertFalse(is_alarm_sound(connection, "not a time"))


if __name__ == "__main__":
    unittest.main()
