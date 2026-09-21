"""The latch rule, over audio actually recorded at the gate.

Every other test of this detector builds its frames from numbers typed into
the test. That is the right way to pin a rule's edges, and it is how a rule
comes to be correct on four cycles from one afternoon and then calls a bird
the gate shutting. These run the shipped functions over PCM cut out of the
recorder's own segments, so what they assert is a fact about this gate.

The clips, and why each is here:

``latch-2026-09-20T100652Z.wav``
    The gate closing itself 71 s after the relay fired at 10:05:41, on a quiet
    morning. The motor band stops and one bright transient follows.

``latch-2026-09-20T181646Z.wav``
    The same event 21 s after the relay at 18:16:25, eight hours later, at a
    different background level -- so the two together are not one condition
    twice.

``loud-noise-not-a-latch-2026-09-19T144148Z.wav``
    A bird calling about four times a second, arriving suddenly and pinning
    the microphone near -7 dBFS. Its frames clear the 3-8 kHz share and the
    -38 dBFS floor as comfortably as a latch does. Before the prominence test
    this produced a latch candidate; a gate that reports itself shut because
    a bird landed near the camera is worse than one that reports nothing.

YAMNet is not in this repository -- it is 16 MB, installed on the board beside
the plate models -- so the motor half of ``scan_segment`` cannot run in CI.
The latch half needs no model at all: ``scan_segment`` looks for a latch by
calling ``find_clangs(analyse_frames(...))``, which is exactly what these
tests call, over exactly the PCM ``decode`` would have produced.
"""
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path

from gate_controller.gate_audio_detect import (
    CLANG_MIN_DBFS, CLANG_HIGH_SHARE, Clang, MotorRun, analyse_frames,
    find_clangs, movements_from,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gate-audio"
SAMPLE_RATE = 16000
MOMENT = datetime(2026, 9, 20, 10, 6, 51, tzinfo=timezone.utc)


def recorded(name):
    """One fixture as signed 16-bit mono, the way ``decode`` returns it."""
    with wave.open(str(FIXTURES / name), "rb") as clip:
        if clip.getnchannels() != 1 or clip.getsampwidth() != 2:
            raise AssertionError(f"{name} is not 16-bit mono")
        if clip.getframerate() != SAMPLE_RATE:
            raise AssertionError(f"{name} is not {SAMPLE_RATE} Hz")
        raw = clip.readframes(clip.getnframes())
    return [int.from_bytes(raw[i:i + 2], "little", signed=True)
            for i in range(0, len(raw), 2)]


def clangs_in(name):
    return find_clangs(analyse_frames(recorded(name), SAMPLE_RATE, MOMENT))


class RecordedLatches(unittest.TestCase):
    def test_a_latch_recorded_at_the_gate_is_found(self):
        found = clangs_in("latch-2026-09-20T100652Z.wav")

        self.assertEqual(len(found), 1, "one impact, reported once")
        self.assertGreaterEqual(found[0].high_share, CLANG_HIGH_SHARE)
        self.assertGreaterEqual(found[0].peak_dbfs, CLANG_MIN_DBFS)

    def test_a_latch_recorded_eight_hours_later_is_also_found(self):
        """Two clips in one condition would only prove the condition."""
        found = clangs_in("latch-2026-09-20T181646Z.wav")

        self.assertTrue(found)
        self.assertGreaterEqual(max(c.peak_dbfs for c in found), CLANG_MIN_DBFS)

    def test_a_bird_that_pins_the_microphone_is_not_the_gate_shutting(self):
        """Loud and bright, and not an impact.

        Its frames pass both of the tests that describe a frame on its own.
        What they fail is standing above the audio around them, because that
        audio is just as loud -- which is the whole of the difference between
        an impact and a noise.
        """
        self.assertEqual(clangs_in("loud-noise-not-a-latch-2026-09-19T144148Z.wav"), [])

    def test_the_bird_really_does_clear_the_share_and_level_tests(self):
        """Otherwise the test above would pass for the wrong reason.

        If some later change made this clip quiet or dull, the assertion that
        it produces no latch would still hold and would have stopped meaning
        anything.
        """
        frames = list(analyse_frames(
            recorded("loud-noise-not-a-latch-2026-09-19T144148Z.wav"),
            SAMPLE_RATE, MOMENT))
        passes = [f for f in frames
                  if f["high_share"] >= CLANG_HIGH_SHARE and f["dbfs"] >= CLANG_MIN_DBFS]

        self.assertTrue(passes, "the clip must still be loud and bright")


class RecordedLatchThroughTheStateMachine(unittest.TestCase):
    """The latch rule is not the last word: the state machine interprets it."""

    def setUp(self):
        self.found = clangs_in("latch-2026-09-20T100652Z.wav")
        self.assertTrue(self.found)
        self.latch = self.found[0]

    def _run(self, seconds=24.0):
        return MotorRun(start=self.latch.at.replace(microsecond=0),
                        end=self.latch.at)

    def test_a_closing_travel_ending_at_this_latch_reads_as_shut(self):
        moves = movements_from([self._run()], self.found, initial_state="open")

        self.assertEqual(moves[0].outcome, "shut")
        self.assertEqual(moves[0].confirmation, "confirmed")

    def test_the_same_latch_on_an_opening_travel_does_not_claim_the_gate_shut(self):
        """The open end-stop is metal too. Only a closing run can end shut."""
        moves = movements_from([self._run()], self.found, initial_state="shut")

        self.assertEqual(moves[0].outcome, "open")

    def test_the_latch_is_recorded_even_when_the_alternation_ignores_it(self):
        """The alternation is the least reliable belief in the system.

        A movement early in a scan that never happened inverts every outcome
        after it, and before this the latch went with them -- the one physical
        observation in the chain was discarded because a model had guessed
        wrong about something else.
        """
        moves = movements_from([self._run()], self.found, initial_state="shut")

        self.assertIsNone(moves[0].clang)
        self.assertIsNotNone(moves[0].latch)
        self.assertEqual(moves[0].latch.at, self.latch.at)
        self.assertEqual(moves[0].confirmation, "confirmed")


class ConfidenceOnRecordedAudio(unittest.TestCase):
    def test_a_run_with_no_latch_and_no_command_is_unconfirmed(self):
        """What 25 to 59 movements a day looked like on the old model."""
        run = MotorRun(start=MOMENT, end=MOMENT.replace(second=30))

        moves = movements_from([run], [])

        self.assertEqual(moves[0].confirmation, "unconfirmed")
        self.assertIsNone(moves[0].latch)

    def test_a_commanded_run_is_confirmed_even_with_nothing_heard(self):
        moves = movements_from([MotorRun(start=MOMENT, end=MOMENT.replace(second=30))],
                               [], commanded_at=[MOMENT])

        self.assertEqual(moves[0].confirmation, "confirmed")

    def test_the_summary_separates_what_is_corroborated_from_what_is_not(self):
        from gate_controller.gate_audio_detect import summarise

        moves = movements_from(
            [MotorRun(start=MOMENT, end=MOMENT.replace(second=30))], [])

        self.assertEqual(summarise(moves)["confirmed"], 0)
        self.assertEqual(summarise(moves)["unconfirmed"], 1)


if __name__ == "__main__":
    unittest.main()
