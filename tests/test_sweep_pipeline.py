"""2026-09-20 19:15:43: an authorised driver waited 41 s at a gate that had read her plate.

Two things went wrong in that passage, and both survived because the tests
that covered the sweep called ``local_sweep()`` with a scripted reader and a
fake injector, so nothing ever ran what production runs. Everything here goes
the way the alarm goes:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

inside the real ``run_worker``, with the real sweep reader, on-device
recogniser, OCR client, processor, store and coordinator. The fakes sit only at
the true boundaries: the frame source (the decoder), the recogniser's engine
(the ONNX sessions), the HTTP session (the cloud), and the relay (the GPIO).

**A, the read that was thrown away.** The sweep read ``10CE1990`` at 0.877 and
injected the frame. The pipeline read the same frame again -- the same band,
re-encoded at JPEG quality 85 instead of 90 -- got 0.723, and refused it under
the 0.75 bar. The engine fake answers by the exact bytes it is handed, so the
two encodings of one frame can be given the two scores that were measured.

**B, nobody looking.** The first window closed with the plate unread, the
fallback re-injected a frame the cloud had already been handed, the pipeline
answered ``duplicate_event``, the session took that for a final answer, and
for 15.5 s nothing read a frame of a car sitting at the gate.
"""
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Lock, Thread
from time import monotonic, sleep

from PIL import Image

from gate_controller.local_recognizer import (
    EngineRead, EngineResult, LocalRecognition, LocalRecognizer, LocalRecognizerConfig,
)
from gate_controller.local_sweep import LocalSweepReader, SweepRead, crop_to_region
from gate_controller.match_policy import DEFAULT_POLICY, _single_band_policy
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.plate_region import PlateRegion
from gate_controller.processor import GateProcessor
from gate_controller.reolink_events import SanitizedCameraEvent
from gate_controller.store import LocalStore
from gate_controller.trigger_capture import TriggerCaptureConfig, TriggerFrameCapture
from gate_controller.worker import run_worker
from tests.test_local_recognizer import FakeResponse, FakeSession
from tests.test_processor import RecordingRelay

AUTHORISED = {"10CE1990"}
REGION = PlateRegion(0.25, 0.0, 0.75, 0.6)
FRAME_SIZE = (480, 270)
STRICT_POLICY = _single_band_policy("strict")


def wait_for(predicate, timeout=8.0, interval=0.01):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(interval)
    return bool(predicate())


def frame(seed: int) -> bytes:
    """A distinct, decodable camera frame. What is "in" it is the engine's script."""
    image = Image.new("RGB", FRAME_SIZE, color=(seed * 7 % 256, seed * 13 % 256, 90))
    for x in range(0, FRAME_SIZE[0], 16):
        image.putpixel((x, (seed * 3 + x) % FRAME_SIZE[1]), (255, 255, 255))
    output = BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue()


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


class ScriptedEngine:
    """The ONNX boundary: answers by the exact bytes it is handed.

    That is what lets one camera frame carry two scores -- one for the band as
    the sweep encodes it, one for the band as the pipeline encodes it -- which
    is precisely what was measured on the Pi.
    """

    def __init__(self, answers, *, read_seconds=0.02):
        self.answers = answers
        self.read_seconds = read_seconds
        self._lock = Lock()
        self.calls = []  # (monotonic, digest)

    def load(self):
        return None

    def warmup(self):
        return None

    def read(self, image):
        key = digest(image)
        with self._lock:
            self.calls.append((monotonic(), key))
        sleep(self.read_seconds)
        plate, score = self.answers.get(key, (None, 0.0))
        if plate is None:
            return EngineResult(width=360, height=162, decode_ms=2, detect_ms=15)
        return EngineResult(
            reads=(EngineRead(plate=plate, confidence=score, detection_confidence=0.9,
                              box=(100, 60, 220, 100), mean_confidence=min(1.0, score + 0.05)),),
            width=360, height=162, decode_ms=2, detect_ms=15, ocr_ms=3,
        )

    def read_count(self):
        with self._lock:
            return len(self.calls)

    def was_read(self, data: bytes) -> bool:
        key = digest(data)
        with self._lock:
            return any(seen == key for _at, seen in self.calls)


class LiveFrames:
    """The decoder boundary: session frames appear as real time passes."""

    def __init__(self):
        self._lock = Lock()
        self._script = []  # (offset_seconds, frame)
        self._started_at = None
        self.sessions = 0
        self.stopped = []
        self.empty = set()  # digests of frames that show the idle scene

    def script(self, frames):
        with self._lock:
            self._script = sorted(frames, key=lambda item: item[0])

    def start_session(self):
        with self._lock:
            self._started_at = monotonic()
            self.sessions += 1
        return True

    def stop_session(self, reason):
        with self._lock:
            self.stopped.append(reason)
            self._started_at = None

    def _available(self, after):
        with self._lock:
            started, script = self._started_at, list(self._script)
        if started is None:
            return []
        now = monotonic()
        return [
            (data, started + offset) for offset, data in script
            if started + offset <= now and (after is None or started + offset > after)
        ]

    def frames_since(self, after=None):
        return self._available(after)[-3:]

    def latest(self, *, after=None):
        available = self._available(after)
        return available[-1] if available else None

    def scene_difference(self, data):
        return 0.0 if digest(data) in self.empty else 0.5


class StopHook:
    """Lets the test end `run_worker`, which owns its own stop event."""

    def __init__(self):
        self.ready = ThreadEvent()
        self.stop_event = None

    def run_forever(self, stop_event):
        self.stop_event = stop_event
        self.ready.set()
        stop_event.wait()


class Gate:
    """The controller as `__main__` wires it, boundaries faked, nothing else."""

    def __init__(self, test, *, answers, cloud=None, sweep_seconds=1.0, waiting=0.0,
                 waiting_fps=2.0, cloud_frames=0, fallback=0, policy=None,
                 processor_policy=None, empty_scene=0.0, authorised=AUTHORISED):
        self.test = test
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.uploads = root / "uploads"
        self.uploads.mkdir()
        self.engine = ScriptedEngine(answers)
        self.frames = LiveFrames()
        self.relay_calls = []
        self.results = []
        self._lock = Lock()
        sweep_policy = policy or (lambda: DEFAULT_POLICY)
        pipeline_policy = processor_policy or sweep_policy
        self.local = LocalRecognizer(
            LocalRecognizerConfig(
                mode="active", cloud="fallback", min_confidence=0.5,
                model_dir=Path("/var/lib/gate-controller/models"),
            ),
            engine_factory=lambda config: self.engine, plate_region=REGION,
        )
        self.local.start()
        assert self.local.wait_ready(5)
        self.session = cloud or FakeSession([])
        self.client = PlateRecognizerClient(
            "token", session=self.session, local_recognizer=self.local,
            authorised=lambda: authorised, max_upload_width=1920,
            plate_region=REGION, match_policy=pipeline_policy,
        )
        self.store = LocalStore(root / "gate.db")
        self.processor = GateProcessor(
            recognizer=self.client, store=self.store, relay=RecordingRelay(self.relay_calls),
            authorised=lambda: authorised, cooldown=timedelta(seconds=20),
            decision_timeout=7.0, min_cloud_request_seconds=1.0,
            match_policy=pipeline_policy,
        )
        self.capture = TriggerFrameCapture(
            TriggerCaptureConfig(
                enabled=True, output_directory=root / "trigger-capture",
                plate_region=REGION, sweep_enabled=True, sweep_seconds=sweep_seconds,
                sweep_max_fps=10.0, sweep_fallback_frames=fallback,
                sweep_cloud_frames=cloud_frames, sweep_cloud_spacing_seconds=0.5,
                sweep_waiting_seconds=waiting, sweep_waiting_fps=waiting_fps,
                presence_max_frames=0, empty_scene_threshold=empty_scene,
                max_flat_fraction=0.0, min_interval_seconds=0.5,
            ),
            popen=lambda *a, **k: None, frame_source=self.frames,
            sweep=LocalSweepReader(
                self.local, authorised=lambda: authorised, match_policy=sweep_policy,
                plate_region=REGION,
            ),
        )
        self.hook = StopHook()
        self._thread = Thread(target=self._run, daemon=True, name="gate-under-test")
        self._thread.start()
        assert self.hook.ready.wait(10), "run_worker never started its background workers"

    def _run(self):
        run_worker(
            self.uploads, self.processor.process, quiet_window=0.1, poll_interval=0.01,
            background_workers=(self.capture, self.hook), trigger_capture=self.capture,
            prepare=self.processor.prepare, on_result=self._on_result,
        )

    def _on_result(self, paths, result):
        with self._lock:
            self.results.append((monotonic(), tuple(paths), result))

    def alarm(self):
        """The camera's vehicle alarm, exactly as the webhook delivers it."""
        now = datetime.now(timezone.utc)
        outcome = self.capture.on_camera_event(SanitizedCameraEvent(
            event_id=f"event-{monotonic()}", event_type="vehicle", rule_id="front_gate",
            received_at=now, event_at=now,
        ))
        self.test.assertEqual(outcome, "scheduled")

    def ftp_still(self, data: bytes, name="still.jpg"):
        """The camera's own FTP upload landing in the watched directory."""
        (self.uploads / name).write_bytes(data)

    def pipeline_bytes(self, data: bytes) -> bytes:
        """The band exactly as the pipeline's own local pass would encode it."""
        with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
            handle.write(data)
            handle.flush()
            upload, _geometry = self.client._open_upload(Path(handle.name))
            try:
                return upload.read()
            finally:
                upload.close()

    def outcomes(self):
        with self._lock:
            return [(result.opened, result.reason) for _at, _paths, result in self.results]

    def opened(self):
        return [outcome for outcome in self.outcomes() if outcome[0]]

    def stored(self, result):
        return self.store.event_payload(result.event_id)

    def opened_result(self):
        with self._lock:
            return next(result for _at, _paths, result in self.results if result.opened)

    def sweep_status(self):
        return self.capture.status()["sweep"]

    def close(self):
        if self.hook.stop_event is not None:
            self.hook.stop_event.set()
        self._thread.join(15)
        self.local.close()
        self.directory.cleanup()


class CapturedLogs:
    """Every gate_controller journal line of a test, thread-safe."""

    def __init__(self):
        self.lines = []
        lines = self.lines

        class Handler(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        self._handler = Handler(level=logging.INFO)
        self._logger = logging.getLogger("gate_controller")
        self._previous = self._logger.level

    def __enter__(self):
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous)

    def text(self):
        return "\n".join(list(self.lines))


class SweepPipelineTests(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _gate(self, **options):
        self.gate = Gate(self, **options)
        return self.gate

    # -- A: the authorised read that was thrown away ------------------------

    def test_the_frame_the_sweep_read_at_0877_opens_the_gate_once(self):
        """19:16:13.9-14.6, replayed: 0.526, 0.523, 0.678, then 0.877 -- and open."""
        frames = [frame(seed) for seed in range(1, 5)]
        answers = {
            digest(crop_to_region(frames[0], REGION)): ("CE9900", 0.526),
            digest(crop_to_region(frames[1], REGION)): ("10CE1990", 0.523),
            digest(crop_to_region(frames[2], REGION)): ("10CE1990", 0.678),
            digest(crop_to_region(frames[3], REGION)): ("10CE1990", 0.877),
        }
        gate = self._gate(answers=answers, sweep_seconds=2.0)
        # The pipeline's own encoding of the very same frame reads 0.723: under
        # the bar. This is the score that refused the driver.
        reread = gate.pipeline_bytes(frames[3])
        self.assertNotEqual(reread, crop_to_region(frames[3], REGION),
                            "the two paths encode the band differently; that is the bug's cause")
        answers[digest(reread)] = ("10CE1990", 0.723)
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))

        self.assertEqual(gate.relay_calls, ["relay"], "exactly one pulse")
        self.assertFalse(gate.engine.was_read(reread),
                         "the pipeline read the injected frame a second time")
        result = gate.opened_result()
        self.assertEqual(result.reason, "exact_match")
        stored = gate.stored(result)
        # The record says what happened: an on-device read, at the score the
        # sweep measured, of the plate it measured.
        self.assertEqual(stored["source"], "local")
        self.assertEqual(stored["observed_plate"], "10CE1990")
        self.assertAlmostEqual(stored["ocr_confidence"], 0.877, places=3)
        self.assertEqual(result.telemetry.local_ocr.to_wire()["decision_source"], "local")
        self.assertAlmostEqual(result.telemetry.local_ocr.to_wire()["score"], 0.877, places=3)
        output = logs.text()
        self.assertIn("stage=sweep_read_adopted", output)
        self.assertIn("plate=10CE1990 score=0.877 decided=true", output)
        self.assertIn("decided=true lane=fast read=sweep", output)
        self.assertIn("gate_local_sweep outcome=ended reason=opened", output)
        self.assertEqual(len(gate.session.calls), 0, "no cloud lookup was needed")

    def test_the_pipelines_own_0723_read_of_that_plate_is_still_refused(self):
        """The bar did not move: the same plate at 0.723 by the ordinary path stays shut."""
        still = frame(40)
        gate = self._gate(answers={})
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = ("10CE1990", 0.723)
        gate.ftp_still(still)
        self.assertTrue(wait_for(lambda: gate.outcomes()), "the still was never decided")
        self.assertEqual(gate.opened(), [])
        self.assertEqual(gate.relay_calls, [])

    def test_a_read_at_0_74_never_opens_the_gate_by_day(self):
        frames = [frame(seed) for seed in range(50, 56)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.74) for data in frames}
        gate = self._gate(answers=answers, sweep_seconds=1.0, waiting=1.0, cloud_frames=2,
                          fallback=1)
        gate.frames.script([(0.05 + index * 0.2, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))
            wait_for(lambda: len(gate.outcomes()) >= 3, 4.0)
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.opened(), [])
        self.assertEqual(gate.sweep_status()["authorised"], 0)
        self.assertIn("decided=false", logs.text())

    def test_a_plate_that_is_not_listed_never_opens_at_0_99(self):
        frames = [frame(seed) for seed in range(60, 66)]
        answers = {digest(crop_to_region(data, REGION)): ("191D12345", 0.99) for data in frames}
        gate = self._gate(answers=answers, sweep_seconds=1.0, waiting=1.0, cloud_frames=2,
                          fallback=1)
        gate.frames.script([(0.05 + index * 0.2, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))
            wait_for(lambda: len(gate.outcomes()) >= 2, 4.0)
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.opened(), [])
        self.assertEqual(gate.sweep_status()["authorised"], 0)

    def test_a_carried_read_is_judged_under_the_band_in_force_not_the_sweeps(self):
        """0.877 clears the daytime bar and not the overnight one. The carried
        read is judged by the pipeline's band at the moment it is judged."""
        data = frame(70)
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.877)}
        gate = self._gate(answers=answers, sweep_seconds=1.0,
                          processor_policy=lambda: STRICT_POLICY)
        gate.frames.script([(0.05, data)])
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(gate.outcomes), logs.text())
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.opened(), [])
        self.assertIn("stage=sweep_read_adopted", logs.text())
        self.assertIn("decided=false", logs.text())

    # -- the sweep does not go blind after an injection ---------------------

    def test_the_sweep_keeps_reading_after_a_refused_injection_and_a_later_frame_opens(self):
        """First injection refused (the band tightened between read and verdict);
        the sweep is still reading, and the next authorised frame opens. Once."""
        frames = [frame(seed) for seed in range(80, 100)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.88) for data in frames}
        strict = ThreadEvent()
        strict.set()

        class SlowCloud(FakeSession):
            def post(self, *args, **kwargs):
                self.calls.append(kwargs)
                sleep(0.6)  # the refused frame's verdict takes a while to come back
                return FakeResponse({"results": []})

        gate = self._gate(
            answers=answers, sweep_seconds=4.0, cloud=SlowCloud([]),
            processor_policy=lambda: STRICT_POLICY if strict.is_set() else DEFAULT_POLICY,
        )
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: gate.session.calls), "the first frame never reached the cloud")
            injected_at = monotonic()
            reads_at_injection = gate.engine.read_count()
            self.assertTrue(wait_for(gate.outcomes), "the first verdict never arrived")
            first_verdict_at = monotonic()
            reads_at_verdict = gate.engine.read_count()
            self.assertEqual(gate.outcomes()[0], (False, "no_match"))
            strict.clear()  # daytime again
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))

        self.assertGreater(first_verdict_at - injected_at, 0.3)
        self.assertGreaterEqual(
            reads_at_verdict - reads_at_injection, 2,
            "the sweep stopped reading while it waited for the injected frame's verdict",
        )
        self.assertEqual(gate.relay_calls, ["relay"], "one passage, one pulse")
        status = gate.sweep_status()
        self.assertEqual(status["injected"], 2, "one refused, one opened")
        self.assertGreater(status["authorised"], 2, "authorised reads seen while one was outstanding")
        self.assertIn("gate_local_sweep outcome=ended reason=opened", logs.text())

    def test_one_passage_never_pulses_twice_even_when_the_ftp_still_reads_too(self):
        """The sweep opens; every later authorised read, and the camera's own
        still of the same car, must find the gate already dealt with."""
        frames = [frame(seed) for seed in range(100, 112)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.95) for data in frames}
        gate = self._gate(answers=answers, sweep_seconds=2.0, waiting=1.0)
        still = frame(199)
        answers[digest(gate.pipeline_bytes(still))] = ("10CE1990", 0.954)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        gate.alarm()
        self.assertTrue(wait_for(gate.opened))
        gate.ftp_still(still)
        self.assertTrue(wait_for(lambda: len(gate.outcomes()) >= 2), "the still was never decided")
        sleep(0.5)
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertEqual(gate.sweep_status()["injected"], 1)
        self.assertIn((False, "cooldown"), gate.outcomes())

    # -- B: nobody looking at a waiting vehicle ------------------------------

    def test_a_waiting_vehicle_is_read_again_without_a_second_alarm(self):
        """The window closes unread (one weak read, already handed to the cloud,
        then nothing). One alarm, no other: the waiting phase reads the plate."""
        blurred = frame(120)
        nothing = [frame(seed) for seed in range(121, 131)]
        sharp = [frame(seed) for seed in range(131, 141)]
        answers = {digest(crop_to_region(blurred, REGION)): ("12C68171", 0.162)}
        answers.update(
            {digest(crop_to_region(data, REGION)): ("10CE1990", 0.95) for data in sharp}
        )
        gate = self._gate(answers=answers, sweep_seconds=1.0, waiting=4.0, waiting_fps=2.0,
                          cloud_frames=5, fallback=1)
        script = [(0.05, blurred)]
        script += [(0.15 + index * 0.1, data) for index, data in enumerate(nothing)]
        # The plate only becomes readable a second after the window has closed.
        script += [(2.0 + index * 0.2, data) for index, data in enumerate(sharp)]
        gate.frames.script(script)

        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))
            self.assertTrue(wait_for(lambda: gate.frames.stopped), "the session was left running")

        output = logs.text()
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertEqual(gate.frames.sessions, 1, "one alarm, one session")
        self.assertIn("gate_local_sweep stage=waiting", output)
        self.assertGreaterEqual(gate.sweep_status()["waiting_reads"], 1)
        # The 19:15:54 failure: the fallback re-injected the frame the cloud
        # already had, and `duplicate_event` ended the session.
        self.assertNotIn("duplicate_event", output)
        self.assertNotIn((False, "duplicate_event"), gate.outcomes())
        self.assertIn("reason=opened", output)

    def test_a_duplicate_event_does_not_end_the_session(self):
        """Belt and braces for the same failure, at the session's own door."""
        from gate_controller.models import ProcessingResult

        gate = self._gate(answers={})
        capture = gate.capture
        capture._reset_session()
        path = Path(gate.directory.name) / "frame.jpg"
        with capture._session_lock:
            capture._session_paths.add(path)
            capture._session_pending_paths.add(path)
            capture._session_pending = 1
        self.assertTrue(capture.note_result((path,), ProcessingResult(False, "duplicate_event")))
        with capture._session_lock:
            self.assertIsNone(capture._session_settled)

    def test_reading_stops_once_the_relay_has_fired(self):
        frames = [frame(seed) for seed in range(140, 180)]
        answers = {digest(crop_to_region(frames[2], REGION)): ("10CE1990", 0.95)}
        gate = self._gate(answers=answers, sweep_seconds=2.0, waiting=3.0)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(gate.opened))
            self.assertTrue(wait_for(lambda: gate.frames.stopped), "the session was left running")
            reads = gate.engine.read_count()
            sleep(1.0)
        self.assertEqual(gate.engine.read_count(), reads, "still reading after the gate opened")
        self.assertIn("reason=opened", logs.text())
        self.assertEqual(gate.relay_calls, ["relay"])

    def test_an_open_by_the_cameras_own_still_ends_the_waiting(self):
        """19:16:25: the FTP still opened the gate and the session never knew."""
        frames = [frame(seed) for seed in range(180, 240)]
        gate = self._gate(answers={}, sweep_seconds=1.0, waiting=5.0, waiting_fps=2.0)
        still = frame(241)
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = ("10CE1990", 0.954)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: "gate_local_sweep stage=waiting" in logs.text()))
            gate.ftp_still(still)
            self.assertTrue(wait_for(gate.opened))
            self.assertTrue(
                wait_for(lambda: "gate_local_sweep outcome=ended reason=opened" in logs.text(), 3.0),
                logs.text(),
            )
            reads = gate.engine.read_count()
            sleep(0.8)
        self.assertEqual(gate.engine.read_count(), reads)
        self.assertEqual(gate.relay_calls, ["relay"])

    def test_the_waiting_ends_when_the_vehicle_has_gone(self):
        present = [frame(seed) for seed in range(250, 262)]
        gone = [frame(seed) for seed in range(262, 300)]
        gate = self._gate(answers={}, sweep_seconds=1.0, waiting=8.0, waiting_fps=2.0,
                          empty_scene=0.03)
        gate.frames.empty = {digest(data) for data in gone}
        script = [(0.05 + index * 0.1, data) for index, data in enumerate(present)]
        script += [(1.6 + index * 0.1, data) for index, data in enumerate(gone)]
        gate.frames.script(script)
        with CapturedLogs() as logs:
            started = monotonic()
            gate.alarm()
            self.assertTrue(
                wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 8.0),
                logs.text(),
            )
            elapsed = monotonic() - started
        self.assertIn("gate_local_sweep outcome=ended reason=departed", logs.text())
        self.assertLess(elapsed, 5.0, "three empty frames at two a second, not the whole cap")
        self.assertEqual(gate.relay_calls, [])

    def test_the_waiting_is_capped_in_time_and_in_reads(self):
        frames = [frame(seed) for seed in range(300, 360)]
        gate = self._gate(answers={}, sweep_seconds=1.0, waiting=2.0, waiting_fps=2.0)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            started = monotonic()
            gate.alarm()
            self.assertTrue(
                wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 8.0),
                logs.text(),
            )
            elapsed = monotonic() - started
        self.assertIn("reason=wait_cap", logs.text())
        self.assertLess(elapsed, 4.5)
        # Two a second for two seconds, give or take the one in flight.
        self.assertLessEqual(gate.sweep_status()["waiting_reads"], 5)
        self.assertGreaterEqual(gate.sweep_status()["waiting_reads"], 2)

    def test_a_new_alarm_is_not_kept_waiting_by_the_waiting_phase(self):
        frames = [frame(seed) for seed in range(360, 460)]
        gate = self._gate(answers={}, sweep_seconds=1.0, waiting=6.0, waiting_fps=1.0)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(lambda: "gate_local_sweep stage=waiting" in logs.text()))
            sleep(0.6)  # past the capture's own min interval between alarms
            second = monotonic()
            gate.alarm()
            self.assertTrue(
                wait_for(lambda: "outcome=ended reason=new_event" in logs.text(), 3.0),
                logs.text(),
            )
            self.assertLess(monotonic() - second, 1.0, "a new alarm waited on a slow cadence")
            self.assertTrue(wait_for(lambda: gate.frames.sessions == 2, 3.0))


class CarriedReadSafetyTests(unittest.TestCase):
    """What a carried read may and may not do, at the processor's own door."""

    def setUp(self):
        self.gate = Gate(self, answers={})
        self.root = Path(self.gate.directory.name)

    def tearDown(self):
        self.gate.close()

    def _file(self, data: bytes, name="frame.jpg") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def _read(self, plate, score, data: bytes, *, authorised=True) -> SweepRead:
        return SweepRead(
            status="recognized", plate=plate, score=score, authorised=authorised,
            recognition=LocalRecognition(
                plate=plate, score=score, mean_score=score, status="recognized",
            ),
            frame_digest=digest(data),
        )

    def _decide(self, path, read):
        prepared = self.gate.processor.prepare((path,), sweep_read=read)
        return self.gate.processor.process((path,), prepared=prepared)

    def test_a_read_carried_for_other_pixels_is_refused_and_the_frame_is_read_afresh(self):
        data, other = frame(500), frame(501)
        self.gate.engine.answers[digest(self.gate.pipeline_bytes(data))] = ("191D12345", 0.99)
        with CapturedLogs() as logs:
            result = self._decide(self._file(data), self._read("10CE1990", 0.99, other))
        self.assertFalse(result.opened)
        self.assertEqual(self.gate.relay_calls, [])
        self.assertIn("stage=sweep_read_refused reason=digest_mismatch", logs.text())
        self.assertIn("read=pipeline", logs.text())
        self.assertTrue(self.gate.engine.was_read(self.gate.pipeline_bytes(data)))

    def test_the_sweeps_authorised_flag_is_never_what_opens_the_gate(self):
        """The flag travels, and nothing consults it: the plate list does."""
        data = frame(502)
        result = self._decide(
            self._file(data), self._read("191D12345", 0.99, data, authorised=True),
        )
        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "no_match")
        self.assertEqual(self.gate.relay_calls, [])

    def test_a_carried_read_under_the_bar_is_under_the_bar(self):
        data = frame(503)
        result = self._decide(
            self._file(data), self._read("10CE1990", 0.74, data, authorised=True),
        )
        self.assertFalse(result.opened)
        self.assertEqual(self.gate.relay_calls, [])

    def test_a_non_finite_carried_score_opens_nothing(self):
        for index, score in enumerate((float("nan"), float("inf"))):
            data = frame(510 + index)
            with self.subTest(score=score):
                result = self._decide(
                    self._file(data, f"frame-{index}.jpg"),
                    self._read("10CE1990", score, data),
                )
                self.assertFalse(result.opened)
        self.assertEqual(self.gate.relay_calls, [])

    def test_something_that_is_not_the_recognisers_own_read_is_ignored(self):
        data = frame(520)

        class Forged:
            carried = True
            frame_digest = digest(data)
            recognition = {"plate": "10CE1990", "score": 0.99}

        with CapturedLogs() as logs:
            result = self._decide(self._file(data), Forged())
        self.assertFalse(result.opened)
        self.assertEqual(self.gate.relay_calls, [])
        self.assertIn("read=pipeline", logs.text())

    def test_a_carried_read_that_does_open_opens_once_and_is_recorded_as_local(self):
        data = frame(530)
        result = self._decide(self._file(data), self._read("10CE1990", 0.877, data))
        self.assertTrue(result.opened)
        self.assertEqual(self.gate.relay_calls, ["relay"])
        stored = self.gate.stored(result)
        self.assertEqual((stored["source"], stored["observed_plate"]), ("local", "10CE1990"))
        # The very same frame again is the same event, not a second pulse.
        again = self._decide(self._file(data, "again.jpg"), self._read("10CE1990", 0.877, data))
        self.assertFalse(again.opened)
        self.assertEqual(self.gate.relay_calls, ["relay"])


if __name__ == "__main__":
    unittest.main()
