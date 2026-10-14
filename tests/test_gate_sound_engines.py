"""A car's engine is not the gate's motor, over audio recorded at the gate.

The classifier (yamnet-linear-v2) wrote every arriving and departing car, an
idling D-Max beside the camera and the camera's own alarm sound into
``gate_movements`` as motor runs (docs/reviews/2026-10-10-gate-jam.md). The
scanner now checks each run for the motor's band before it believes it
(``motor_seconds_heard``, docs/gate-audio.md 2i).

These run the real scan -- ``scan`` -> ``scan_segment`` -> ``movements_from``
-> ``record`` -> the heartbeat -- over clips cut from the recorder's own
segments. One more scanner fix is here too: a run is no longer cut in two
where one of the scan's 60-second chunks ends. Two boundaries are faked, for the reasons the other audio tests
give: ``decode`` (ffmpeg) hands over the clip's PCM, and the classifier, which
is not in the repository, calls every frame of the clip the motor, exactly as
the real one did on each of these moments.

``motor-closing-2026-10-12T182135Z.wav``
    The auto-close of 12 Oct 19:21 IST, 10 s from the middle of its travel.
``engine-idling-dmax-2026-10-10T091020Z.wav``
    The D-Max idling beside the camera at 10:10 IST on the morning of the jam,
    flat at the motor's own level (-47.5 dBFS) with almost nothing in the
    motor's band.
``engine-arriving-car-2026-10-13T140305Z.wav``
    A car at the gate, 13 Oct 15:03 IST, no relay pulse; the scanner wrote
    three runs for it.
``camera-alarm-sound-2026-10-10T100413Z.wav``
    The camera's vehicle alarm through its own speaker, clipping the
    microphone (switched off 2026-10-10 15:27 IST).
"""
import logging
import sqlite3
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import gate_controller.__main__ as gate_main
import gate_controller.gate_sound_scan as gate_sound_scan
from gate_controller.audio_segments import SegmentStore
from gate_controller.gate_audio_detect import (
    MOTOR_MIN_SECONDS_HEARD, analyse_frames, motor_seconds_heard,
)
from gate_controller.gate_left_open import LIKELY, POSSIBLE
from gate_controller.gate_sound_scan import Listening, record, scan
from gate_controller.sound_model import HOP_SECONDS, SAMPLE_RATE, SoundWindow
from gate_controller.store import LocalStore
from tests.test_gate_left_open import Prompt, RelaySpy

FIXTURES = Path(__file__).parent / "fixtures" / "gate-audio"
UTC = timezone.utc
MOTOR = "motor-closing-2026-10-12T182135Z.wav"
IDLING = "engine-idling-dmax-2026-10-10T091020Z.wav"
ARRIVING = "engine-arriving-car-2026-10-13T140305Z.wav"
ALARM = "camera-alarm-sound-2026-10-10T100413Z.wav"
#: A fixed evening, offset from nothing on the wall clock (invariant 10).
EVENING = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)


def recorded(name):
    with wave.open(str(FIXTURES / name), "rb") as clip:
        assert clip.getnchannels() == 1 and clip.getsampwidth() == 2
        assert clip.getframerate() == SAMPLE_RATE
        raw = clip.readframes(clip.getnframes())
    return [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2)]


class EveryFrameTheMotor:
    """The classifier on these moments: every frame over the threshold."""

    available = True
    unavailable_reason = None

    def classify(self, samples, started_at):
        frames = max(0, int((len(samples) / SAMPLE_RATE - 0.96) / HOP_SECONDS) + 1)
        return SoundWindow(started_at, [0.9] * frames)


class HearsWhateverIsNotSilent:
    """A classifier that calls a frame the motor when its patch holds sound."""

    available = True
    unavailable_reason = None

    def classify(self, samples, started_at):
        values = list(samples)
        patch, hop = int(0.96 * SAMPLE_RATE), int(HOP_SECONDS * SAMPLE_RATE)
        probabilities = []
        for start in range(0, len(values) - patch + 1, hop):
            heard = any(value != 0 for value in values[start:start + patch])
            probabilities.append(0.9 if heard else 0.0)
        return SoundWindow(started_at, probabilities)


class EnginesAreNotTheGate(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.segments = SegmentStore(self.root / "audio-segments")
        self.segments.prepare()
        self.clips = {}
        store = LocalStore(self.root / "gate.db")
        self.store = store
        self.connection = sqlite3.connect(store.path)
        self.addCleanup(self.connection.close)

    def recorder(self, *clips):
        """Segments as the recorder names them, each holding one recorded clip.

        ``clips`` is ``(seconds after EVENING, fixture)``. A last, empty
        segment stands for the one still being written, which the scan skips.
        """
        for offset, name in clips + ((3600, None),):
            at = EVENING + timedelta(seconds=offset)
            path = self.segments.directory / f"gate-{at:%Y%m%dT%H%M%S}Z.aac"
            path.write_bytes(b"\0")
            self.clips[path.name] = recorded(name) if name else None

    def recorder_with(self, offset, samples):
        at = EVENING + timedelta(seconds=offset)
        path = self.segments.directory / f"gate-{at:%Y%m%dT%H%M%S}Z.aac"
        path.write_bytes(b"\0")
        self.clips[path.name] = samples
        last = self.segments.directory / f"gate-{EVENING + timedelta(hours=1):%Y%m%dT%H%M%S}Z.aac"
        last.write_bytes(b"\0")
        self.clips[last.name] = None

    def run_scan(self, *, commanded_at=(), model=None):
        def decode(path, **_):
            return self.clips[Path(path).name]

        with patch.object(gate_sound_scan, "decode", decode), \
                self.assertLogs("gate_controller.gate_sound_scan", logging.INFO) as logs:
            result, moves, scanned = scan(self.segments, model or EveryFrameTheMotor(),
                                          commanded_at=commanded_at)
            record(self.connection, moves, scanned)
        self.logs = "\n".join(logs.output)
        return moves

    def heartbeat(self, now):
        return gate_main._controller_status(
            self.store, Prompt(), {}, relay=RelaySpy(), clock=lambda: now,
        )["gate"]

    # -- the recorded sounds, one at a time ----------------------------------------

    def test_the_motor_is_still_a_gate_movement(self):
        self.recorder((0, MOTOR))
        moves = self.run_scan()
        self.assertEqual(len(moves), 1)
        self.assertGreaterEqual(moves[0].seconds, 8.0)

    def test_an_idling_diesel_beside_the_camera_is_not_a_gate_movement(self):
        self.recorder((0, IDLING))
        self.assertEqual(self.run_scan(), [])
        self.assertIn("gate_sound stage=not_the_motor", self.logs)
        self.assertIn("not_the_motor=1", self.logs)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM gate_movements").fetchone()[0], 0)

    def test_an_arriving_cars_engine_is_not_a_gate_movement(self):
        self.recorder((0, ARRIVING))
        self.assertEqual(self.run_scan(), [])

    def test_the_cameras_alarm_sound_is_not_a_gate_movement(self):
        self.recorder((0, ALARM))
        self.assertEqual(self.run_scan(), [])

    def test_a_motor_run_across_a_minute_boundary_is_one_run(self):
        """The scan reads a minute at a time; the gate does not move a minute at a time.

        Five seconds either side of the boundary are each under the eight a run
        needs, so a run found chunk by chunk was lost altogether -- and a
        longer one was written as two, which the left-open check counts as two
        movements. 17 of the 291 rows yamnet-linear-v2 wrote to 2026-10-10
        were the second half of such a split.
        """
        silence = [0] * (55 * SAMPLE_RATE)
        self.recorder_with(0, silence + recorded(MOTOR) + [0] * (10 * SAMPLE_RATE))

        moves = self.run_scan(model=HearsWhateverIsNotSilent())

        self.assertEqual(len(moves), 1, moves)
        boundary = EVENING + timedelta(seconds=60)
        self.assertLess(moves[0].start, boundary)
        self.assertGreater(moves[0].end, boundary)
        self.assertGreaterEqual(moves[0].seconds, 9.0)

    # -- what the rest of the system reads ----------------------------------------

    def test_a_closing_heard_after_a_car_is_a_closing_not_an_uncommanded_opening(self):
        """The car's engine hid the opening; the closing is still the closing.

        On 12-14 Oct five of the nine motor runs kept were closings whose
        opening was under the engine of the car it opened for.
        """
        self.recorder((0, ARRIVING), (40, MOTOR))
        moves = self.run_scan()

        self.assertEqual(len(moves), 1)
        self.assertFalse(moves[0].uncommanded, "a closing is never somebody's fob")
        gate = self.heartbeat(EVENING + timedelta(minutes=2))
        self.assertEqual(gate["movements_24h"], 1)
        self.assertEqual(gate["uncommanded_24h"], 0)

    def test_the_same_closing_with_no_car_before_it_is_read_as_an_opening(self):
        """The alternation is unchanged where no car was heard."""
        self.recorder((40, MOTOR))
        moves = self.run_scan()
        self.assertTrue(moves[0].uncommanded)

    def test_a_car_and_its_idling_no_longer_make_the_left_open_alerts_odd_burst(self):
        """Engine, engine, motor: three "runs" and no latch was the `likely` shape.

        5 Oct 21:20 IST read that way: an arriving car 10.6 s before the relay,
        the opening, and the auto-close with no latch heard. With the engines
        set aside one run is left, which is the ordinary missed latch.
        """
        self.recorder((0, ARRIVING), (20, IDLING), (60, MOTOR))
        moves = self.run_scan()
        self.assertEqual(len(moves), 1)
        coverage = [{
            "segment": f"gate-{EVENING + timedelta(minutes=5 * index):%Y%m%dT%H%M%S}Z.aac",
            "started_at": EVENING + timedelta(minutes=5 * index),
            "span_seconds": 300.0, "audio_seconds": 300.0, "missing_seconds": 0.0,
        } for index in range(6)]
        record(self.connection, [], [], listening=Listening(tuple(coverage), ()))

        left_open = self.heartbeat(EVENING + timedelta(minutes=20))["left_open"]

        self.assertEqual(left_open["runs"], 1)
        self.assertNotEqual(left_open.get("confidence"), LIKELY)
        self.assertEqual(left_open["confidence"], POSSIBLE)


class TheBandCheckOnItsOwn(unittest.TestCase):
    """The numbers behind the threshold, on the same clips."""

    def heard(self, name):
        return motor_seconds_heard(analyse_frames(recorded(name), SAMPLE_RATE, EVENING))

    def test_the_motor_has_its_band_and_the_engines_do_not(self):
        self.assertGreaterEqual(self.heard(MOTOR), MOTOR_MIN_SECONDS_HEARD)
        for name in (IDLING, ARRIVING, ALARM):
            with self.subTest(clip=name):
                self.assertLess(self.heard(name), MOTOR_MIN_SECONDS_HEARD / 2)


if __name__ == "__main__":
    unittest.main()
