"""The remote push-to-talk check: its tone detector, and what it calls a pass."""
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import talk_tone_check  # noqa: E402

RATE = talk_tone_check.RATE


def tone(frequency, seconds, amplitude=9000):
    return [int(amplitude * math.sin(2 * math.pi * frequency * index / RATE))
            for index in range(int(seconds * RATE))]


def hiss(seconds, spread=600, seed=7):
    generator = random.Random(seed)
    return [int(generator.gauss(0, spread)) for _ in range(int(seconds * RATE))]


def mix(*signals):
    return [sum(values) for values in zip(*signals)]


class ToneCheckTests(unittest.TestCase):
    def test_a_tone_is_told_from_noise_and_from_the_other_tone(self):
        window = tone(1000, 0.05)
        self.assertGreater(talk_tone_check.tone_share(window, 1000, RATE), 0.95)
        self.assertLess(talk_tone_check.tone_share(window, 1500, RATE), 0.05)
        self.assertLess(talk_tone_check.tone_share(hiss(0.05), 1000, RATE), 0.1)
        self.assertEqual(0.0, talk_tone_check.tone_share([0] * 800, 1000, RATE))
        noisy = mix(tone(1000, 0.05), hiss(0.05))
        self.assertGreater(talk_tone_check.tone_share(noisy, 1000, RATE),
                           talk_tone_check.TONE_SHARE)

    def test_both_tones_played_whole_is_a_pass(self):
        recording = (hiss(1.0) + mix(tone(1000, 1.5), hiss(1.5)) + hiss(0.6)
                     + mix(tone(1500, 1.5), hiss(1.5)) + hiss(1.0))
        rows = talk_tone_check.analyse(recording)
        low, high = (talk_tone_check.heard_seconds(rows, column) for column in (2, 3))
        self.assertAlmostEqual(1.5, low, delta=0.1)
        self.assertAlmostEqual(1.5, high, delta=0.1)

    def test_a_tone_cut_short_by_the_reset_is_a_fail(self):
        # What a reset sent with the last block did to the second tone on the
        # fitted camera: 0.8 s of the 1.5 s survived.
        recording = (hiss(1.0) + tone(1000, 1.5) + hiss(0.6) + tone(1500, 0.8) + hiss(1.7))
        rows = talk_tone_check.analyse(recording)
        self.assertLess(talk_tone_check.heard_seconds(rows, 3), talk_tone_check.HEARD_SECONDS)

    def test_the_check_waits_as_the_service_does_unless_told_otherwise(self):
        from gate_camera_control.talk import TALK_DRAIN_SECONDS
        self.assertEqual(TALK_DRAIN_SECONDS, talk_tone_check.TALK_DRAIN_SECONDS)
        source = Path(talk_tone_check.__file__).read_text(encoding="utf-8")
        self.assertIn("default=TALK_DRAIN_SECONDS", source)
        # Nothing read from the camera's environment file is ever printed.
        for line in source.splitlines():
            if "print(" in line:
                self.assertNotIn("env[", line)


if __name__ == "__main__":
    unittest.main()
