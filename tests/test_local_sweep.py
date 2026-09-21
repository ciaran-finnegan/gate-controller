import tempfile
import unittest
from datetime import datetime, timezone
from hashlib import sha256
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
        self.assertEqual(config.sweep_cloud_frames, 5)
        self.assertEqual(config.sweep_cloud_spacing_seconds, 1.0)
        # Once the sweep is on, a waiting vehicle is looked at by default.
        self.assertEqual((config.sweep_waiting_seconds, config.sweep_waiting_fps), (30.0, 1.0))

    def test_the_waiting_phase_is_bounded_from_the_environment(self):
        config = load_trigger_capture_config(
            {"GATE_LOCAL_SWEEP_WAITING_SECONDS": "0", "GATE_LOCAL_SWEEP_WAITING_FPS": "2"},
            Path("/tmp"), webhook_enabled=True,
        )
        self.assertEqual((config.sweep_waiting_seconds, config.sweep_waiting_fps), (0.0, 2.0))
        for key, value in (
            ("GATE_LOCAL_SWEEP_WAITING_SECONDS", "-1"),
            ("GATE_LOCAL_SWEEP_WAITING_SECONDS", "61"),
            ("GATE_LOCAL_SWEEP_WAITING_FPS", "0.1"),
            ("GATE_LOCAL_SWEEP_WAITING_FPS", "2.5"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    load_trigger_capture_config({key: value}, Path("/tmp"), webhook_enabled=True)

    def test_environment_bounds_are_enforced(self):
        environment = {
            "GATE_LOCAL_SWEEP_ENABLED": "true",
            "GATE_LOCAL_SWEEP_SECONDS": "12.5",
            "GATE_LOCAL_SWEEP_MAX_FPS": "4",
            "GATE_LOCAL_SWEEP_FALLBACK_FRAMES": "0",
        }
        environment["GATE_LOCAL_SWEEP_CLOUD_FRAMES"] = "3"
        environment["GATE_LOCAL_SWEEP_CLOUD_SPACING_SECONDS"] = "2"
        config = load_trigger_capture_config(environment, Path("/tmp"), webhook_enabled=True)
        self.assertTrue(config.sweep_enabled)
        self.assertEqual((config.sweep_seconds, config.sweep_max_fps, config.sweep_fallback_frames), (12.5, 4.0, 0))
        self.assertEqual((config.sweep_cloud_frames, config.sweep_cloud_spacing_seconds), (3, 2.0))
        for key, value in (
            ("GATE_LOCAL_SWEEP_SECONDS", "0"),
            ("GATE_LOCAL_SWEEP_SECONDS", "31"),
            ("GATE_LOCAL_SWEEP_MAX_FPS", "11"),
            ("GATE_LOCAL_SWEEP_FALLBACK_FRAMES", "4"),
            ("GATE_LOCAL_SWEEP_CLOUD_FRAMES", "11"),
            ("GATE_LOCAL_SWEEP_CLOUD_SPACING_SECONDS", "0.4"),
            ("GATE_LOCAL_SWEEP_CLOUD_SPACING_SECONDS", "6"),
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

    def test_a_completed_read_travels_with_the_digest_of_the_frame_it_came_from(self):
        recognition = LocalRecognition(plate="131D2696", score=0.877, status="recognized")
        region = PlateRegion(0.25, 0.0, 0.75, 0.6)
        reader = LocalSweepReader(FakeRecognizer(recognition, decides=True), plate_region=region)
        frame = jpeg((200, 100))
        read = reader.read(frame)
        self.assertTrue(read.carried)
        self.assertIs(read.recognition, recognition, "the recogniser's own answer, unaltered")
        self.assertEqual(read.frame_digest, sha256(frame).hexdigest(),
                         "the whole frame's identity, which is what the pipeline keys on")
        self.assertEqual(read.image, crop_to_region(frame, region), "the bytes the model saw")
        geometry = read.geometry
        self.assertEqual(
            (geometry.frame_width, geometry.frame_height, geometry.crop_left, geometry.crop_top,
             geometry.crop_width, geometry.crop_height),
            (200, 100, 50, 0, 150, 60),
        )

    def test_a_read_that_never_completed_carries_nothing(self):
        for status in ("unavailable", "error"):
            with self.subTest(status=status):
                reader = LocalSweepReader(FakeRecognizer(LocalRecognition(status=status)))
                read = reader.read(jpeg())
                self.assertFalse(read.carried)
                self.assertIsNone(read.recognition)

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
                 inject=None, max_fps=5.0, cloud=0, cloud_spacing=1.0, waiting=0.0,
                 waiting_fps=1.0, empty_scene=0.0):
        # cloud=0 by default so each test says for itself whether the paid
        # reader takes part; the shipped default is 5. Likewise waiting=0: the
        # tests written for the window keep testing the window, and the ones
        # about the waiting phase that follows it ask for one (shipped: 30 s).
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            sweep_enabled=True, sweep_seconds=seconds, sweep_max_fps=max_fps,
            sweep_fallback_frames=fallback, presence_max_frames=presence_frames,
            empty_scene_threshold=empty_scene, max_flat_fraction=0.0,
            sweep_cloud_frames=cloud, sweep_cloud_spacing_seconds=cloud_spacing,
            sweep_waiting_seconds=waiting, sweep_waiting_fps=waiting_fps,
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
        self.assertIn("authorised=1 injected=1 cloud_handovers=0 blind_handovers=0 fallback=0", output)
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
        self.assertIn("injected=0 cloud_handovers=0 blind_handovers=0 fallback=1 best_plate=11WH257 best_score=0.700", ended[0])
        self.assertIn("source=sweep_fallback", "\n".join(logs.output))
        self.assertGreaterEqual(self.clock.now, 103.0)

    def test_the_fallback_never_hands_over_a_frame_the_cloud_already_has(self):
        """19:15:53.99 on 2026-09-20. The best read of the window (0.162) was
        the first frame, which had gone to the cloud at 19:15:44.65. The
        fallback injected the same bytes again; the pipeline keys a frame by
        its content, answered `duplicate_event`, and the session ended."""
        first, second, third = jpeg(seed=1), jpeg(seed=2), jpeg(seed=3)
        source = FrameSource(self.clock, [(100.0, first), (100.4, second), (100.8, third)])
        sweep = ScriptedSweep({
            first: SweepRead(status="recognized", plate="12C68171", score=0.162, read_ms=170.0),
        })
        capture = self._capture(source, sweep, seconds=3.0, fallback=1, cloud=1)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))
        handed = [paths[0].read_bytes() for paths, _trigger in self.injected]
        self.assertEqual(handed[0], first, "the cloud handover took the frame with a plate in it")
        self.assertEqual(len(handed), len(set(handed)), "the same bytes were injected twice")
        self.assertEqual(handed[1], third, "the fallback moved on to the newest frame instead")
        self.assertIn("cloud_handovers=1 blind_handovers=0 fallback=1", "\n".join(logs.output))

    def test_the_sweeps_read_goes_with_the_frame_to_an_injector_that_takes_it(self):
        winner = jpeg(seed=2)
        source = FrameSource(self.clock, [(100.0, winner)])
        read = SweepRead(
            status="recognized", plate="131D2696", score=0.877, authorised=True,
            recognition=LocalRecognition(plate="131D2696", score=0.877, status="recognized"),
            frame_digest=sha256(winner).hexdigest(),
        )
        carried = []
        capture = None

        def inject(paths, received_at, trigger, sweep_read=None):
            carried.append(sweep_read)
            capture.note_result(paths, ProcessingResult(True, "exact_match"))

        capture = self._capture(source, ScriptedSweep({winner: read}), seconds=2.0, inject=inject)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual(carried, [read])

    def test_waiting_reads_slowly_and_stops_at_its_cap(self):
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(80)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({})
        capture = self._capture(source, sweep, seconds=2.0, fallback=1, waiting=5.0, waiting_fps=1.0)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))
        output = "\n".join(logs.output)
        self.assertIn("gate_local_sweep stage=waiting", output)
        self.assertIn("reason=wait_cap", output)
        self.assertLessEqual(self.clock.now, 107.6)
        waiting_reads = capture.status()["sweep"]["waiting_reads"]
        self.assertGreaterEqual(waiting_reads, 4)
        self.assertLessEqual(waiting_reads, 6, "one a second for five seconds")
        # The passage was put on record when the window closed, not 5 s later.
        self.assertLess(output.index("source=sweep_fallback"), output.index("stage=waiting"))

    def test_waiting_never_outlives_the_decoder_session(self):
        frames = [(100.0 + index * 0.5, jpeg(seed=index)) for index in range(200)]
        source = FrameSource(self.clock, frames)
        capture = self._capture(source, ScriptedSweep({}), seconds=2.0, fallback=0, waiting=60.0)
        object.__setattr__(capture.config, "session_seconds", 8.0)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertLessEqual(self.clock.now, 108.6)

    def test_an_empty_drive_at_the_end_of_the_window_is_not_waited_on(self):
        class EmptyDrive(FrameSource):
            def scene_difference(self, frame):
                return 0.001

        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(60)]
        source = EmptyDrive(self.clock, frames)
        sweep = ScriptedSweep({})
        capture = self._capture(source, sweep, seconds=2.0, fallback=0, waiting=30.0,
                                empty_scene=0.03)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertIn("reason=departed", "\n".join(logs.output))
        self.assertLess(self.clock.now, 103.0)
        self.assertEqual(sweep.reads, [], "an empty drive is never worth a read")

    def test_no_more_than_one_authorised_frame_is_ever_outstanding(self):
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(12)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({
            data: SweepRead(status="recognized", plate="131D2696", score=0.95, authorised=True)
            for _at, data in frames
        })
        # Verdicts never arrive: every authorised frame after the first is held back.
        capture = self._capture(source, sweep, seconds=2.0, fallback=0)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual(len(self.injected), 1)
        status = capture.status()["sweep"]
        self.assertGreater(status["authorised"], 1)
        self.assertEqual(status["injected"], 1)

    def test_a_refused_authorised_frame_lets_the_next_one_in_up_to_the_ceiling(self):
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(40)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({
            data: SweepRead(status="recognized", plate="131D2696", score=0.95, authorised=True)
            for _at, data in frames
        })
        capture = None

        def inject(paths, received_at, trigger):
            self.injected.append(paths)
            capture.note_result(paths, ProcessingResult(False, "decision_timeout"))

        capture = self._capture(source, sweep, seconds=6.0, fallback=0, inject=inject)
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual(len(self.injected), 3, "MAX_SWEEP_AUTHORISED_INJECTIONS")

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

    def test_once_a_plate_has_been_seen_the_paid_lookup_goes_to_that_frame(self):
        """Not simply the newest frame, which is what this used to send.

        A frame the on-device detector found no plate in is a picture problem,
        and the cloud answering "no plate found" still costs a lookup and,
        at one request a second with answers taking five or six, still delays
        every later answer -- including the one that opens the gate.

        The *first* handover still goes out blind and at once: with nothing
        read yet there is nothing better to send, and making the cloud wait
        for the on-device reader is the serial design this replaced.
        """
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(20)]
        source = FrameSource(self.clock, frames)
        seen = frames[2][1]
        # Only the third frame has a plate in it; every other one is blank.
        sweep = ScriptedSweep({
            seen: SweepRead(status="recognized", plate="131D2696", score=0.42, authorised=False),
        })
        handed = []

        capture = self._capture(
            source, sweep, seconds=3.0, fallback=0,
            inject=lambda paths, received_at, trigger: handed.append(paths[0].read_bytes()),
            cloud=2, cloud_spacing=1.0, max_fps=5.0,
        )
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))

        output = "\n".join(logs.output)
        self.assertEqual(len(handed), 2)
        self.assertNotEqual(handed[0], seen, "the first handover goes out before anything is read")
        self.assertEqual(handed[1], seen, "the second lookup ignored the frame with a plate in it")
        self.assertIn("stage=cloud_handover frame=1 of=2 plate_seen=False", output)
        self.assertIn("stage=cloud_handover frame=2 of=2 plate_seen=True", output)
        self.assertIn("blind_handovers=1", output)

    def test_with_no_plate_anywhere_the_freshest_frame_is_still_sent(self):
        """The detector missing what the cloud would find is the case being paid for."""
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(10)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({})
        handed = []

        capture = self._capture(
            source, sweep, seconds=3.0, fallback=0,
            inject=lambda paths, received_at, trigger: handed.append(paths[0].read_bytes()),
            cloud=1, cloud_spacing=1.0, max_fps=5.0,
        )
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))

        self.assertEqual(len(handed), 1)
        self.assertIn("plate_seen=False", "\n".join(logs.output))

    def test_a_repeated_frame_never_costs_a_second_read(self):
        """The session decoder repeats frames when the camera delivers under its rate."""
        same = jpeg(seed=7)
        frames = [(100.0, same), (100.2, same), (100.4, jpeg(seed=8)), (100.6, same)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({})

        capture = self._capture(source, sweep, seconds=2.0, fallback=0, cloud=0, max_fps=5.0)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))

        read_bytes = [frame for frame, _ in sweep.reads]
        self.assertEqual(read_bytes, [same, jpeg(seed=8), same],
                         "the consecutive repeat should not have been read again")
        self.assertIn("duplicates=1", "\n".join(logs.output))

    def test_the_cloud_reads_in_parallel_without_the_sweep_waiting_for_it(self):
        # Frames keep arriving for the whole window, as a live stream does.
        frames = [(100.0 + index * 0.2, jpeg(seed=index)) for index in range(20)]
        source = FrameSource(self.clock, frames)
        sweep = ScriptedSweep({})  # nothing reads locally: the blaze case
        handed = []

        def inject(paths, received_at, trigger):
            handed.append(paths[0].read_bytes())
            # Deliberately no verdict: a cloud handover must not block the sweep.

        capture = self._capture(
            source, sweep, seconds=3.0, fallback=0, inject=inject,
            cloud=2, cloud_spacing=1.0, max_fps=5.0,
        )
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))

        output = "\n".join(logs.output)
        self.assertEqual(len(handed), 2, "capped at sweep_cloud_frames")
        self.assertIn("stage=cloud_handover frame=1 of=2", output)
        self.assertIn("stage=cloud_handover frame=2 of=2", output)
        self.assertIn("source=sweep_cloud", output)
        self.assertIn("cloud_handovers=2", output)
        self.assertGreater(len(sweep.reads), 2, "the local reader kept going meanwhile")
        status = capture.status()["sweep"]
        self.assertEqual((status["cloud_frames"], status["cloud_handovers"]), (2, 2))

    def test_a_local_match_still_stops_the_sweep_and_is_waited_for(self):
        plain, winner = jpeg(seed=1), jpeg(seed=2)
        source = FrameSource(self.clock, [(100.0, plain), (100.4, winner)])
        sweep = ScriptedSweep({
            winner: SweepRead(status="recognized", plate="131D2696", score=1.0, authorised=True),
        })
        capture = None

        def inject(paths, received_at, trigger):
            self.injected.append(paths)
            # Only the locally authorised frame opens the gate; a cloud
            # handover of an unreadable frame gets no verdict at all.
            if paths[0].read_bytes() == winner:
                capture.note_result(paths, ProcessingResult(True, "exact_match"))

        capture = self._capture(source, sweep, seconds=5.0, fallback=0, inject=inject, cloud=5)
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.local_sweep(event(), 100.0, Stop(self.clock))
        output = "\n".join(logs.output)
        self.assertIn("reason=opened", output)
        self.assertIn("injected=1", output)
        self.assertLess(self.clock.now, 105.0, "the open ends the window early")

    def test_zero_cloud_frames_keeps_the_paid_reader_out_of_the_window(self):
        frames = [(100.0 + i * 0.2, jpeg(seed=i)) for i in range(4)]
        source = FrameSource(self.clock, frames)
        capture = self._capture(source, ScriptedSweep({}), seconds=2.0, fallback=0, cloud=0)
        self.clock.now = 100.8
        capture.local_sweep(event(), 100.0, Stop(self.clock))
        self.assertEqual(self.injected, [])
        self.assertEqual(capture.status()["sweep"]["cloud_handovers"], 0)

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
