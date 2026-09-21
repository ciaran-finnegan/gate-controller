"""Hearing the gate: motor, clang, wind, and what the gate was left doing.

The signals here are synthesised at the frequencies the real ones were
measured at on 2026-09-16 (events 2927-2930), so a threshold change that would
have broken those four cycles breaks these tests.
"""
from datetime import datetime, timedelta, timezone
import math
import unittest

from gate_controller.gate_audio_detect import (
    CLANG_HIGH_SHARE, MOTOR_HF_SHARE, analyse_frames, find_clangs, find_motor_runs,
    movements, summarise,
)

SR = 16000
START = datetime(2026, 9, 16, 14, 13, 0, tzinfo=timezone.utc)


def tone(seconds, hz, amplitude=0.25):
    """A steady tone: the motor's 1.2-3 kHz energy, measured at 50-90%."""
    return [int(amplitude * 32767 * math.sin(2 * math.pi * hz * i / SR))
            for i in range(int(seconds * SR))]


def quiet(seconds, hz=60, amplitude=0.02):
    """Wind: nearly all of it below 150 Hz, measured at 70-99%."""
    return [int(amplitude * 32767 * math.sin(2 * math.pi * hz * i / SR))
            for i in range(int(seconds * SR))]


def clang(seconds=0.25, amplitude=0.55):
    """A bright transient: energy above 3 kHz, decaying."""
    out = []
    total = int(seconds * SR)
    for i in range(total):
        decay = math.exp(-6.0 * i / total)
        value = (math.sin(2 * math.pi * 4200 * i / SR)
                 + 0.8 * math.sin(2 * math.pi * 6100 * i / SR)
                 + 0.6 * math.sin(2 * math.pi * 5000 * i / SR))
        out.append(int(max(-1.0, min(1.0, amplitude * decay * value / 2.4)) * 32767))
    return out


def frames_of(samples):
    return list(analyse_frames(samples, SR, START))


class BandSeparationTests(unittest.TestCase):
    def test_the_motor_lands_in_the_band_the_rule_watches(self):
        for frame in frames_of(tone(2.0, 2000))[2:-2]:
            self.assertGreaterEqual(frame["hf_share"], MOTOR_HF_SHARE)

    def test_wind_never_reaches_the_motor_threshold(self):
        # 70-99% of real wind energy is below 150 Hz. If this ever fails, the
        # detector has started inventing gate movements out of weather.
        for frame in frames_of(quiet(3.0))[2:-2]:
            self.assertLess(frame["hf_share"], MOTOR_HF_SHARE)
            self.assertGreater(frame["wind_share"], 0.5)

    def test_the_clang_lands_above_three_kilohertz(self):
        peak = max(frames_of(clang()), key=lambda f: f["high_share"])
        self.assertGreaterEqual(peak["high_share"], CLANG_HIGH_SHARE)

    def test_wind_is_never_loud_enough_or_bright_enough_to_be_a_clang(self):
        self.assertEqual(find_clangs(frames_of(quiet(4.0))), [])


class MotorRunTests(unittest.TestCase):
    def test_a_sustained_run_is_found_with_its_duration(self):
        runs = find_motor_runs(frames_of(quiet(1.0) + tone(8.0, 2000) + quiet(1.0)))
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0].seconds, 8.0, delta=0.5)

    def test_a_brief_click_is_not_a_run(self):
        # Shorter than MOTOR_MIN_SECONDS: a relay click, not a gate moving.
        self.assertEqual(find_motor_runs(frames_of(quiet(1.0) + tone(1.0, 2000) + quiet(1.0))), [])

    def test_a_dip_inside_one_run_does_not_split_it(self):
        # The motor's own sound fluctuates as the leaves swing.
        samples = tone(4.0, 2000) + quiet(0.8) + tone(4.0, 2000)
        runs = find_motor_runs(frames_of(samples))
        self.assertEqual(len(runs), 1)

    def test_opening_and_closing_stay_two_runs(self):
        # Forty seconds apart at the real gate; the gap tolerance must not
        # weld them into one movement.
        samples = tone(5.0, 2000) + quiet(10.0) + tone(5.0, 2000)
        self.assertEqual(len(find_motor_runs(frames_of(samples))), 2)

    def test_silence_produces_no_runs(self):
        self.assertEqual(find_motor_runs(frames_of([0] * (SR * 3))), [])


class ClangTests(unittest.TestCase):
    def test_one_impact_is_reported_once_not_once_per_frame(self):
        found = find_clangs(frames_of(quiet(1.0) + clang() + quiet(1.0)))
        self.assertEqual(len(found), 1)

    def test_the_loudest_frame_of_the_impact_is_the_one_kept(self):
        found = find_clangs(frames_of(quiet(0.5) + clang() + quiet(0.5)))
        self.assertGreaterEqual(found[0].peak_dbfs, -38.0)
        self.assertGreaterEqual(found[0].high_share, CLANG_HIGH_SHARE)

    def test_two_impacts_far_apart_are_two_events(self):
        samples = quiet(0.5) + clang() + quiet(3.0) + clang() + quiet(0.5)
        self.assertEqual(len(find_clangs(samples and frames_of(samples))), 2)


class MovementTests(unittest.TestCase):
    """The part that matters: what the gate was left doing."""

    #: The motor is *quiet*. Measured over 49.2 hours of recording at this
    #: gate, a closing travel sits at -49 to -58 dBFS and the latch that ends
    #: it peaks at -15 to -31: the impact is twenty to thirty decibels above
    #: the motor that preceded it. This fixture used to make them the same
    #: loudness, which is not a gate and which is why the detector had no
    #: reason to notice that a bird pinning the microphone at -7 dBFS looks
    #: nothing like an impact.
    MOTOR_AMPLITUDE = 0.01

    def _cycle(self, *, with_clang: bool):
        # The measured shape: motor, then a pause, then motor, ending with the
        # leaves meeting -- or not.
        motor = lambda: tone(6.0, 2000, amplitude=self.MOTOR_AMPLITUDE)
        samples = quiet(1.0) + motor() + quiet(4.0) + motor()
        samples += clang() if with_clang else quiet(1.0)
        return frames_of(samples + quiet(1.0))

    def test_a_run_that_ends_in_a_clang_shut_the_gate(self):
        moves = movements(self._cycle(with_clang=True))
        self.assertEqual(moves[-1].outcome, "shut")
        self.assertIsNotNone(moves[-1].clang)

    def test_a_run_that_ends_without_a_clang_left_the_gate_open(self):
        # The load-bearing case: this is the moment a vehicle is about to
        # drive out, and the only way the system can know it.
        moves = movements(self._cycle(with_clang=False))
        self.assertEqual(moves[-1].outcome, "open")
        self.assertIsNone(moves[-1].clang)

    def test_an_opening_with_no_relay_command_is_flagged_as_somebody_else(self):
        moves = movements(self._cycle(with_clang=True), commanded_at=())
        self.assertTrue(moves[0].uncommanded)

    def test_a_closing_is_never_charged_to_somebody_else(self):
        # The auto-close is not commanded by anybody. An earlier version
        # charged every closing run to "somebody used a fob", which made the
        # flag fire on all four of the commanded cycles it was tested against.
        frames = self._cycle(with_clang=True)
        closing = movements(frames, commanded_at=[START + timedelta(seconds=0.5)])[-1]
        self.assertFalse(closing.uncommanded)

    def test_a_clang_on_an_opening_run_does_not_mean_the_gate_shut(self):
        # The open end-stop is metallic too. On event 2927 the opening run
        # carried a clang 3.5 s before the motor stopped; only a run that is
        # closing can end shut.
        moves = movements(self._cycle(with_clang=True), initial_state="shut")
        self.assertEqual(moves[0].outcome, "open")
        self.assertIsNone(moves[0].clang)

    def test_a_gate_already_open_closes_on_its_next_movement(self):
        # One run, ending in the leaves meeting, on a gate that was standing
        # open: the movement can only be a close, and it completed.
        frames = frames_of(quiet(1.0) + tone(6.0, 2000, amplitude=self.MOTOR_AMPLITUDE)
                           + clang() + quiet(1.0))
        moves = movements(frames, initial_state="open")
        self.assertEqual(moves[0].outcome, "shut")
        self.assertIsNotNone(moves[0].clang)

    def test_the_same_sound_on_a_shut_gate_is_an_opening(self):
        # Identical audio, opposite starting state, opposite meaning. This is
        # exactly why alternation carries the interpretation and the clang
        # alone does not.
        frames = frames_of(quiet(1.0) + tone(6.0, 2000, amplitude=self.MOTOR_AMPLITUDE)
                           + clang() + quiet(1.0))
        moves = movements(frames, initial_state="shut")
        self.assertEqual(moves[0].outcome, "open")

    def test_a_closing_run_with_no_clang_is_read_as_still_open(self):
        # Stuck, reversed, or the recorder missed it. The safe reading is the
        # one that does not claim the property is secured.
        moves = movements(self._cycle(with_clang=False), initial_state="open")
        self.assertEqual(moves[0].outcome, "open")

    def test_a_movement_the_controller_commanded_is_not_flagged(self):
        frames = self._cycle(with_clang=True)
        moves = movements(frames, commanded_at=[START + timedelta(seconds=0.5)])
        self.assertFalse(moves[0].uncommanded)

    def test_a_command_long_before_the_movement_does_not_claim_it(self):
        frames = self._cycle(with_clang=True)
        moves = movements(frames, commanded_at=[START - timedelta(minutes=10)])
        self.assertTrue(moves[0].uncommanded)

    def test_the_summary_reports_the_state_the_gate_was_left_in(self):
        shut = summarise(movements(self._cycle(with_clang=True)))
        self.assertEqual(shut["final_state"], "shut")
        self.assertGreaterEqual(shut["shut"], 1)
        standing_open = summarise(movements(self._cycle(with_clang=False)))
        self.assertEqual(standing_open["final_state"], "open")
        self.assertGreaterEqual(standing_open["left_open"], 1)

    def test_nothing_heard_is_reported_as_unknown_not_as_shut(self):
        # A recorder that was down must never be read as "the gate is closed".
        self.assertEqual(summarise([])["final_state"], "unknown")

    def test_a_movement_serialises_for_the_wire(self):
        move = movements(self._cycle(with_clang=True))[-1]
        document = move.as_dict()
        self.assertEqual(document["outcome"], "shut")
        self.assertIn("peak_dbfs", document["clang"])
        self.assertIsInstance(document["seconds"], float)


if __name__ == "__main__":
    unittest.main()
