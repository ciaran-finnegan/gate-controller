import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from gate_controller.local_recognizer import LocalRecognition
from gate_controller.local_sweep import LocalSweepReader, SweepRead, crop_to_region
from gate_controller.models import MatchDecision, ProcessingResult
from gate_controller.plate_region import PlateRegion
from gate_controller.reolink_events import SanitizedCameraEvent
from gate_controller.trigger_capture import (
    TriggerCaptureConfig,
    TriggerFrameCapture,
    load_trigger_capture_config,
)


def jpeg(size=(64, 32), seed=0):
    width, height = size
    image = Image.new("RGB", size)
    image.putdata([
        ((x * 255 + seed) % 256, (y * 255 // max(height - 1, 1)) % 256, (x * 4 + y * 8 + seed) % 256)
        for y in range(height) for x in range(width)
    ])
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def event(event_type="vehicle", rule_id="front_gate"):
    return SanitizedCameraEvent(
        event_id="event-1", event_type=event_type, rule_id=rule_id,
        received_at=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
        event_at=datetime(2026, 9, 16, 11, 59, 59, tzinfo=timezone.utc),
    )


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class Stop:
    """A stop event whose waits advance the fake clock."""

    def __init__(self, clock):
        self._clock = clock
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self._clock.now += seconds
        return self.stopped


class FrameSource:
    """Session frames appear as the clock advances; latest() never repeats one."""

    def __init__(self, clock, frames):
        # (captured_at, jpeg_bytes) in capture order
        self._clock = clock
        self.frames = sorted(frames)
        self.started = 0
        self.stopped = []

    def latest(self, *, after=None):
        available = [
            (at, frame) for at, frame in self.frames
            if at <= self._clock() and (after is None or at > after)
        ]
        if not available:
            return None
        at, frame = available[-1]
        return frame, at

    def start_session(self):
        self.started += 1
        return True

    def stop_session(self, reason):
        self.stopped.append(reason)


class ScriptedSweep:
    """A LocalSweepReader stand-in answering from a script keyed by frame bytes."""

    def __init__(self, answers, ready=True):
        self.answers = answers
        self.ready = ready
        self.reads = []

    def available(self):
        return self.ready

    on_read = None

    def read(self, frame, *, trace_id=None):
        self.reads.append((frame, trace_id))
        if self.on_read is not None:
            self.on_read()
        return self.answers.get(frame, SweepRead(status="no_plate", read_ms=170.0))


class SweepConfigTests(unittest.TestCase):
    def test_defaults_leave_the_sweep_off(self):
        config = load_trigger_capture_config({}, Path("/tmp"), webhook_enabled=True)
        self.assertFalse(config.sweep_enabled)
        self.assertEqual(config.sweep_seconds, 10.0)
        self.assertEqual(config.sweep_max_fps, 5.0)
        self.assertEqual(config.sweep_fallback_frames, 1)

    def test_environment_bounds_are_enforced(self):
        environment = {
            "GATE_LOCAL_SWEEP_ENABLED": "true",
            "GATE_LOCAL_SWEEP_SECONDS": "12.5",
            "GATE_LOCAL_SWEEP_MAX_FPS": "4",
            "GATE_LOCAL_SWEEP_FALLBACK_FRAMES": "0",
        }
        config = load_trigger_capture_config(environment, Path("/tmp"), webhook_enabled=True)
        self.assertTrue(config.sweep_enabled)
        self.assertEqual((config.sweep_seconds, config.sweep_max_fps, config.sweep_fallback_frames), (12.5, 4.0, 0))
        for key, value in (
            ("GATE_LOCAL_SWEEP_SECONDS", "0"),
            ("GATE_LOCAL_SWEEP_SECONDS", "31"),
            ("GATE_LOCAL_SWEEP_MAX_FPS", "11"),
            ("GATE_LOCAL_SWEEP_FALLBACK_FRAMES", "4"),
            ("GATE_LOCAL_SWEEP_ENABLED", "yes"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    load_trigger_capture_config({key: value}, Path("/tmp"), webhook_enabled=True)


class CropTests(unittest.TestCase):
    def test_crop_keeps_only_the_plate_band(self):
        frame = jpeg((200, 100))
        cropped = crop_to_region(frame, PlateRegion(0.25, 0.0, 0.75, 0.6))
        with Image.open(BytesIO(cropped)) as image:
            self.assertEqual(image.size, (150, 60))

    def test_no_region_or_full_frame_returns_the_frame_unchanged(self):
        frame = jpeg((200, 100))
        self.assertIs(crop_to_region(frame, None), frame)
        self.assertIs(crop_to_region(frame, PlateRegion(0.0, 0.0, 1.0, 1.0)), frame)

    def test_undecodable_bytes_pass_through(self):
        self.assertEqual(crop_to_region(b"not a jpeg", PlateRegion(0.25, 0.0, 0.75, 0.6)), b"not a jpeg")


class FakeHandle:
    def __init__(self, recognition):
        self._recognition = recognition

    def result(self, timeout=None):
        return self._recognition


class FakeRecognizer:
    def __init__(self, recognition, *, decides=False, active=True):
        self.recognition = recognition
        self._decides = decides
        self.active = active
        self.begun = []

    def begin(self, image, *, trace_id=None, geometry=None, authorised=None, policy=None):
        self.begun.append((image, trace_id, authorised, policy))
        return FakeHandle(self.recognition)

    def decides(self, handle, recognition):
        return self._decides


class LocalSweepReaderTests(unittest.TestCase):
    def test_read_reports_the_local_answer_and_the_shared_authorisation(self):
        recognizer = FakeRecognizer(
            LocalRecognition(plate="131D2696", score=0.999, status="recognized"), decides=True,
        )
        authorised = lambda: ["131D2696"]
        reader = LocalSweepReader(recognizer, authorised=authorised, match_policy=None)
        read = reader.read(jpeg(), trace_id="sweep-1")
        self.assertEqual((read.plate, read.status, read.authorised), ("131D2696", "recognized", True))
        self.assertAlmostEqual(read.score, 0.999)
        image, trace_id, given_authorised, _policy = recognizer.begun[0]
        self.assertEqual(trace_id, "sweep-1")
        self.assertIs(given_authorised, authorised)

    def test_the_band_is_cropped_before_the_reader_sees_it(self):
        recognizer = FakeRecognizer(LocalRecognition(status="no_plate"))
        reader = LocalSweepReader(recognizer, plate_region=PlateRegion(0.25, 0.0, 0.75, 0.6))
        reader.read(jpeg((200, 100)))
        with Image.open(BytesIO(recognizer.begun[0][0])) as image:
            self.assertEqual(image.size, (150, 60))

    def test_a_busy_or_broken_reader_never_raises(self):
        class Broken:
            active = True

            def begin(self, *args, **kwargs):
                raise RuntimeError("boom")

        self.assertEqual(LocalSweepReader(Broken()).read(jpeg()).status, "error")
        unavailable = FakeRecognizer(LocalRecognition(status="unavailable"))
        self.assertEqual(LocalSweepReader(unavailable).read(jpeg()).status, "unavailable")

    def test_availability_follows_the_recogniser(self):
        self.assertFalse(LocalSweepReader(None).available())
        self.assertFalse(LocalSweepReader(FakeRecognizer(None, active=False)).available())
        self.assertTrue(LocalSweepReader(FakeRecognizer(None, active=True)).available())


class LocalSweepTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = Clock(100.0)
        self.injected = []

    def tearDown(self):
        self.temporary.cleanup()

    def _capture(self, source, sweep, *, seconds=10.0, fallback=1, presence_frames=0,
                 inject=None, max_fps=5.0):
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            sweep_enabled=True, sweep_seconds=seconds, sweep_max_fps=max_fps,
            sweep_fallback_frames=fallback, presence_max_frames=presence_frames,
            empty_scene_threshold=0.0, max_flat_fraction=0.0,
        )
        capture = TriggerFrameCapture(
            config, popen=lambda *a, **k: None, clock=self.clock,
            frame_source=source, sweep=sweep,
        )
        capture.attach(inject or (lambda paths, received_at, trigger: self.injected.append((paths, trigger))))
        return capture

    def test_only_an_authorised_read_is_injected_and_the_sweep_stops_on_open(self):
        plain, winner = jpeg(seed=1), jpeg(seed=2)
        source = FrameSource(self.clock, [(100.0, plain), (100.4, winner)])
        sweep = ScriptedSweep({
            plain: SweepRead(status="recognized", plate="1HYH257", score=0.6, read_ms=170.0),
            winner: SweepRead(status="recognized", plate="131D2696", score=0.999, authorised=True, read_ms=172.0),
        })
        capture = None

        def inject(paths, received_at, trigger):
            self.injected.append((paths, trigger))
            # The processor re-reads the frame and opens the gate.
            capture.note_result(paths, ProcessingResult(True, "exact_match", decision=MatchDecision(
                allowed=True, reason="exact_match", authorised_plate="131D2696",
                observed_plate="131D2696", confidence=0.999,
            )))

        capture = self._capture(source, sweep, inject=inject)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            injected = capture.local_sweep(event(), 100.0, Stop(self.clock))

        self.assertEqual(injected, 1)
        self.assertEqual(len(self.injected), 1)
        paths, trigger = self.injected[0]
        self.assertEqual(paths[0].read_bytes(), winner)
        self.assertEqual(trigger.source, "reolink_webhook")
        self.assertEqual([frame for frame, _ in sweep.reads], [plain, winner])
        output = "\n".join(logs.output)
        self.assertIn("gate_local_sweep outcome=ended reason=opened", output)
        self.assertIn("authorised=1 injected=1 fallback=0", output)
        self.assertIn("source=sweep", output)
        self.assertLess(self.clock.now, 110.0, "an open ends the sweep before its window")

    def test_no_authorised_read_falls_back_to_the_best_frame_at_window_end(self):
        weak, better, last = jpeg(seed=1), jpeg(seed=2), jpeg(seed=3)
        source = FrameSource(self.clock, [(100.0, weak), (101.0, better), (102.0, last)])
        sweep = ScriptedSweep({
            weak: SweepRead(status="recognized", plate="1HYH", score=0.3, read_ms=170.0),
            better: SweepRead(status="recognized", plate="11WH257", score=0.7, read_ms=170.0),
        })
        capture = self._capture(source, sweep, seconds=3.0, fallback=1)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            injected = capture.local_sweep(event(), 100.0, Stop(self.clock))

        self.assertEqual(injected, 1)
        self.assertEqual(self.injected[0][0][0].read_bytes(), better, "the highest local score is the fallback")
        ended = [line for line in logs.output if "gate_local_sweep outcome=ended" in line]
        self.assertEqual(len(ended), 1)
        self.assertIn("reason=window", ended[0])
        self.assertIn("injected=0 fallback=1 best_plate=11WH257 best_score=0.700", ended[0])
        self.assertIn("source=sweep_fallback", "\n".join(logs.output))
        self.assertGreaterEqual(self.clock.now, 103.0)

    def test_zero_fallback_frames_keeps_every_sweep_frame_off_the_pipeline(self):
        frame = jpeg(seed=1)
        source = FrameSource(self.clock, [(100.0, frame)])
        capture = self._capture(source, ScriptedSweep({}), seconds=2.0, fallback=0)
        injected = capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual(injected, 0)
        self.assertEqual(self.injected, [])
        self.assertEqual(capture.status()["sweep"]["reads"], 1)

    def test_a_busy_reader_is_counted_and_the_frame_is_not_injected(self):
        frame = jpeg(seed=1)
        source = FrameSource(self.clock, [(100.0, frame)])
        sweep = ScriptedSweep({frame: SweepRead(status="unavailable", read_ms=1.0)})
        capture = self._capture(source, sweep, seconds=1.0, fallback=0)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        status = capture.status()["sweep"]
        self.assertEqual((status["frames"], status["reads"], status["busy"], status["injected"]), (1, 1, 1, 0))

    def test_the_sweep_never_reads_the_same_frame_twice_and_paces_at_max_fps(self):
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(20)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({})
        capture = self._capture(source, sweep, seconds=2.0, fallback=0, max_fps=5.0)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        read_frames = [frame for frame, _ in sweep.reads]
        self.assertEqual(len(read_frames), len(set(read_frames)))
        self.assertLessEqual(len(read_frames), 11)
        self.assertGreaterEqual(len(read_frames), 5)

    def test_a_clump_of_frames_is_drained_oldest_first_and_capped(self):
        class ClumpSource(FrameSource):
            def frames_since(self, after=None):
                available = [
                    (frame, at) for at, frame in self.frames
                    if at <= self._clock() and (after is None or at > after)
                ]
                return available[-3:]

        frames = [(100.0 + index * 0.05, jpeg(seed=index)) for index in range(5)]
        source = ClumpSource(self.clock, frames)
        sweep = ScriptedSweep({})
        capture = self._capture(source, sweep, seconds=1.0, fallback=0, max_fps=10.0)
        # The whole clump has already been decoded when the sweep looks.
        self.clock.now = 100.3
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        read = [frame for frame, _ in sweep.reads]
        # The three newest of the clump, oldest first, then nothing newer.
        self.assertEqual(read[:3], [frames[2][1], frames[3][1], frames[4][1]])
        self.assertEqual(len(read), 3)

    def test_an_authorised_frame_discards_the_rest_of_its_clump(self):
        winner, later = jpeg(seed=1), jpeg(seed=2)

        class ClumpSource(FrameSource):
            def frames_since(self, after=None):
                return [] if after is not None else [(winner, 100.0), (later, 100.1)]

        source = ClumpSource(self.clock, [])
        sweep = ScriptedSweep({winner: SweepRead(status="recognized", plate="131D2696", score=1.0, authorised=True)})
        capture = None

        def inject(paths, received_at, trigger):
            self.injected.append((paths, trigger))
            capture.note_result(paths, ProcessingResult(True, "exact_match"))

        capture = self._capture(source, sweep, seconds=2.0, fallback=0, inject=inject)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual([frame for frame, _ in sweep.reads], [winner])
        self.assertEqual(len(self.injected), 1)

    def test_run_forever_prefers_the_sweep_only_when_the_reader_is_ready(self):
        frame = jpeg(seed=1)
        source = FrameSource(self.clock, [(100.0, frame)])
        sweep = ScriptedSweep({frame: SweepRead(status="recognized", plate="131D2696", score=1.0, authorised=True)}, ready=False)
        capture = self._capture(source, sweep, seconds=1.0, fallback=0)
        self.assertFalse(capture._sweep_ready())
        self.assertFalse(capture.status()["sweep"]["ready"])
        sweep.ready = True
        self.assertTrue(capture._sweep_ready())
        self.assertTrue(capture.status()["sweep"]["ready"])

    def test_a_newer_camera_event_ends_the_sweep_with_a_fallback(self):
        frame = jpeg(seed=1)
        source = FrameSource(self.clock, [(100.0, frame)])
        sweep = ScriptedSweep({})
        capture = self._capture(source, sweep, seconds=5.0, fallback=1)
        # The next alarm lands while the first frame is being read.
        sweep.on_read = lambda: capture._queue.put_nowait((event(), 100.5))
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            injected = capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual(injected, 1, "the newest frame seen is handed on before the next event")
        self.assertEqual(self.injected[0][0][0].read_bytes(), frame)
        self.assertEqual(capture.status()["sweep"]["fallbacks"], 1)
        self.assertIn("reason=new_event", "\n".join(logs.output))
        self.assertLess(self.clock.now, 105.0)

    def test_an_event_that_arrives_before_any_frame_leaves_nothing_to_fall_back_to(self):
        source = FrameSource(self.clock, [])
        capture = self._capture(source, ScriptedSweep({}), seconds=5.0, fallback=1)
        capture._queue.put_nowait((event(), 100.5))
        self.assertEqual(capture.local_sweep(event(), 100.0, Stop(self.clock)), 0)
        self.assertEqual(self.injected, [])


if __name__ == "__main__":
    unittest.main()
