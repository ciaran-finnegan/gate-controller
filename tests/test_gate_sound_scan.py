"""The scanner, and the model that decides what a motor sounds like."""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.gate_sound_scan import DETECTOR, already_scanned, record
from gate_controller.sound_model import (
    GateSoundModel, HOP_SECONDS, SoundWindow, motor_runs,
)
from gate_controller.gate_audio_detect import Clang, MotorRun, movements_from
from gate_controller.store import LocalStore

MOMENT = datetime(2026, 9, 17, 11, 8, tzinfo=timezone.utc)


def window(*probabilities):
    return SoundWindow(MOMENT, list(probabilities))


class MotorRuns(unittest.TestCase):
    def test_a_short_blip_is_not_a_gate(self):
        """Measured travel is 15-27 s; two frames of anything is not that."""
        self.assertEqual(motor_runs(window(*([0.1] * 3 + [0.9] * 2 + [0.1] * 3))), [])

    def test_a_dropped_frame_mid_travel_does_not_split_the_movement(self):
        """One movement reported as five makes duration meaningless, and
        duration is how a completed travel is told from a stall.

        Either half alone is under the eight-second floor, so a split would
        also lose the movement entirely rather than merely mis-measuring it.
        """
        runs = motor_runs(window(*([0.9] * 15 + [0.2] + [0.9] * 15)))

        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0].seconds, 31 * HOP_SECONDS, places=3)

    def test_a_real_gap_does_end_a_movement(self):
        runs = motor_runs(window(*([0.9] * 20 + [0.1] * 8 + [0.9] * 20)))

        self.assertEqual(len(runs), 2)

    def test_a_run_still_going_at_the_end_of_the_window_is_reported(self):
        runs = motor_runs(window(*([0.1] * 2 + [0.9] * 20)))

        self.assertEqual(len(runs), 1)

    def test_a_run_shorter_than_the_gate_takes_to_travel_is_not_one(self):
        """Measured travel is 15-27 s. At a three-second floor a real scan
        found 88 openings against 7 closings, and a gate that opens 88 times
        shuts 88 times."""
        self.assertEqual(motor_runs(window(*([0.9] * 12))), [])


class ModelAvailability(unittest.TestCase):
    def test_a_missing_model_is_reported_not_raised(self):
        """A gate controller must not stop opening gates over an analysis model."""
        model = GateSoundModel(model_dir=Path("/nonexistent"))

        self.assertFalse(model.available)
        self.assertIn("not installed", model.unavailable_reason or "")
        self.assertEqual(model.classify([], MOMENT).probabilities, [])


class Recording(unittest.TestCase):
    def setUp(self):
        # The real store, so these tests prove the migration creates the
        # tables rather than proving a copy of the DDL matches itself.
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        store = LocalStore(Path(self.directory.name) / "gate.db")
        self.connection = sqlite3.connect(store.path)
        self.addCleanup(self.connection.close)

    def movement(self, *, uncommanded=False, outcome="open"):
        return movements_from(
            [MotorRun(start=MOMENT, end=MOMENT + timedelta(seconds=20))],
            [Clang(at=MOMENT + timedelta(seconds=19), high_share=0.7, peak_dbfs=-24.0)]
            if outcome == "shut" else [],
            commanded_at=() if uncommanded else (MOMENT,),
            initial_state="open" if outcome == "shut" else "shut",
        )

    def test_a_movement_is_written_once_however_often_it_is_rescanned(self):
        """A restart re-reads the segment; the gate did not open twice."""
        moves = self.movement()
        record(self.connection, moves, [("gate-a.aac", 100)])
        record(self.connection, moves, [("gate-a.aac", 100)])

        rows = self.connection.execute("SELECT started_at, detector FROM gate_movements").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], DETECTOR)

    def test_an_opening_nobody_commanded_is_recorded_as_such(self):
        """84% of passages here never fire the relay. That is the denominator."""
        record(self.connection, self.movement(uncommanded=True), [("gate-a.aac", 100)])

        flag = self.connection.execute("SELECT uncommanded FROM gate_movements").fetchone()[0]
        self.assertEqual(flag, 1)

    def test_a_scanned_segment_is_not_scanned_again(self):
        record(self.connection, [], [("gate-a.aac", 100)])

        self.assertEqual(already_scanned(self.connection), {"gate-a.aac"})

    def test_a_database_without_the_table_scans_everything_rather_than_failing(self):
        self.assertEqual(already_scanned(sqlite3.connect(":memory:")), set())


if __name__ == "__main__":
    unittest.main()
