"""2026-09-10 21:52:26: a perfect on-device read waited 4.15 s behind cloud calls.

The local pass had been moved off the serial cloud *slot* on 2026-09-08, but it
still ran on the one burst thread, and that thread blocked inside every cloud
request. Frame 2768 (131D2696 read locally at 1.000, the car stopped at the
gate) sat in the queue from 21:52:26.16 to 21:52:30.31 while lookups for two
frames that could not be read went out and came back, and the gate opened
8.9 s after the alarm instead of about 4.7 s.

The first class runs the real client, the real on-device recogniser (scripted
engine, ~170 ms a read), the real processor, the real burst thread and the real
cloud lane. The others pin the pieces down with fakes.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Lock, Thread, current_thread
from threading import Event as ThreadEvent
from time import monotonic, sleep

from PIL import Image

from gate_controller.local_recognizer import LocalRecognizer, LocalRecognizerConfig
from gate_controller.models import PlateObservation, ProcessingResult
from gate_controller.ocr import LocalPass, PlateRecognizerClient
from gate_controller.processor import (
    CLOUD_SKIP_MOVING_NO_PLATE, DEFAULT_CLOUD_SKIP_STILLNESS, GateProcessor,
    PreparedBurst, _bounded_cloud_skip_stillness,
)
from gate_controller.store import LocalStore
from gate_controller.telemetry import TriggerTelemetry
from gate_controller.worker import (
    BoundedBurstQueue, BurstIdentity, CloudLane, _process_bursts, run_worker,
)
from tests.test_local_pass_reserve import SlowEngine, StallingSession
from tests.test_local_recognizer import FakeSession
from tests.test_processor import RecordingRelay, TwoPhaseRecognizer

AUTHORISED = {"131D2696"}


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(interval)
    return bool(predicate())


class Lanes:
    """The burst thread and the cloud lane, wired the way `run_worker` wires them."""

    def __init__(self, processor, *, superseded=None):
        self.results = []
        self.coalesced = []
        self._lock = Lock()
        self.bursts = BoundedBurstQueue(2)

        def on_result(paths, result):
            with self._lock:
                self.results.append((monotonic(), tuple(paths), result))

        def coalesce(item, reason="queue_coalesced"):
            with self._lock:
                self.coalesced.append((tuple(item[0]), reason))

        self.lane = CloudLane(
            processor.process, on_result=on_result, superseded=superseded,
            coalesce=coalesce,
        )
        self._lane_thread = Thread(target=self.lane.run, daemon=True, name="GateCloudLane")
        self._burst_thread = Thread(
            target=_process_bursts, args=(self.bursts, processor.process),
            kwargs=dict(
                on_result=on_result, superseded=superseded, coalesce=coalesce,
                prepare=processor.prepare, cloud_lane=self.lane,
            ),
            daemon=True, name="GateBurstProcessor",
        )
        self._lane_thread.start()
        self._burst_thread.start()

    def inject(self, path, *, stillness=None, key=None):
        now = datetime.now(timezone.utc)
        identity = BurstIdentity(key or path.name, None, stillness)
        self.bursts.put(((path,), now, monotonic(), now, identity))
        return monotonic()

    def result_for(self, path):
        with self._lock:
            for at, paths, result in self.results:
                if paths == (path,):
                    return at, result
        return None

    def close(self):
        self.bursts.stop()
        self._burst_thread.join(10)
        self.lane.stop()
        self._lane_thread.join(10)


class FastLaneTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.local = None
        self.engine = None
        self.lanes = None

    def tearDown(self):
        if self.lanes is not None:
            self.lanes.close()
        if self.local is not None:
            self.local.close()
        self.directory.cleanup()

    def _jpeg(self, name, shade=120, size=(1920, 1080)):
        path = self.root / name
        Image.new("RGB", size, color=(shade, shade, shade)).save(path, format="JPEG")
        return path

    def _client(self, reads, session=None):
        config = LocalRecognizerConfig(
            mode="active", cloud="fallback", min_confidence=0.5,
            model_dir=Path("/var/lib/gate-controller/models"),
        )

        def factory(configuration):
            self.engine = SlowEngine(configuration, reads)
            return self.engine

        self.local = LocalRecognizer(config, engine_factory=factory)
        self.local.start()
        assert self.local.wait_ready(5)
        return PlateRecognizerClient(
            "token", session=session or FakeSession([]), local_recognizer=self.local,
            authorised=lambda: AUTHORISED, max_upload_width=1920,
        )

    def _processor(self, client, **options):
        settings = dict(
            cooldown=timedelta(seconds=0), clock=lambda: datetime.now(timezone.utc),
            decision_timeout=7.0, min_cloud_request_seconds=1.0,
        )
        settings.update(options)
        return GateProcessor(
            recognizer=client, store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay([]), authorised=AUTHORISED, **settings,
        )

    def _stored(self, processor, result):
        return processor._store.event_payload(result.event_id)

    def test_a_frame_the_device_can_read_is_decided_while_an_older_cloud_call_waits(self):
        # 21:52:24.57, frame 2767: nothing on the device, cloud 27.35-30.16.
        # 21:52:26.16, frame 2768: 131D2696 at 1.000 on the device -- and not
        # decided until 30.53, because the burst thread was inside that call.
        older = self._jpeg("2767.jpg", 110)
        newer = self._jpeg("2768.jpg", 120)
        session = StallingSession(stall_seconds=3.0)
        client = self._client([(None, 0.0), ("131D2696", 0.999)], session)
        processor = self._processor(client)
        self.lanes = Lanes(processor)

        self.lanes.inject(older, stillness=0.001)
        self.assertTrue(
            wait_for(lambda: session.calls, 3.0),
            "the older frame never reached its cloud request",
        )

        injected = self.lanes.inject(newer, stillness=0.002)
        self.assertTrue(
            wait_for(lambda: self.lanes.result_for(newer) is not None, 2.0),
            "the newer frame was not decided while the older cloud call was in flight",
        )
        decided_at, result = self.lanes.result_for(newer)

        self.assertTrue(result.opened, f"denied: {result.reason}")
        self.assertEqual(result.reason, "exact_match")
        self.assertEqual(self._stored(processor, result)["source"], "local")
        self.assertLess(
            decided_at - injected, 1.5,
            f"the on-device read waited {decided_at - injected:.2f}s for the cloud lane",
        )
        self.assertEqual(len(session.calls), 1, "the locally decided frame must not post")
        self.assertIsNone(
            self.lanes.result_for(older),
            "the older frame's cloud call should still be in flight",
        )
        # The older frame is still answered once its lookup returns.
        self.assertTrue(wait_for(lambda: self.lanes.result_for(older) is not None, 6.0))
        _, older_result = self.lanes.result_for(older)
        self.assertFalse(older_result.opened)
        self.assertEqual(older_result.reason, "no_match")

    def test_a_moving_frame_the_device_finds_no_plate_in_is_not_sent_to_the_cloud(self):
        # 21:52:22.47, frame 2766: the car still turning in, plate 88 px wide
        # and oblique, nothing on the device, then 2.3 s of cloud for "no
        # plate". The stillness the capture measured for creeping and moving
        # frames that night was 0.009-0.034; the frames that decided sat at
        # 0.001-0.002.
        frame = self._jpeg("2766.jpg", 100)
        session = FakeSession([])
        client = self._client([(None, 0.0)], session)
        processor = self._processor(client)
        self.lanes = Lanes(processor)

        with self.assertLogs("gate_controller.processor", level="INFO") as journal:
            self.lanes.inject(frame, stillness=0.02)
            self.assertTrue(wait_for(lambda: self.lanes.result_for(frame) is not None, 3.0))
        _, result = self.lanes.result_for(frame)

        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "no_match")
        self.assertEqual(session.calls, [], "a moving frame with no plate must not be billed")
        self.assertTrue(any(
            f"cloud_skipped reason={CLOUD_SKIP_MOVING_NO_PLATE}" in line
            for line in journal.output
        ), journal.output)
        stored = self._stored(processor, result)
        self.assertIsNone(stored["observed_plate"])

    def test_a_still_frame_with_no_local_plate_still_goes_to_the_cloud(self):
        # 21:52:24.57, frame 2767 (stillness 0.009 would have been sent too):
        # the device found nothing but the car had stopped; the cloud is the
        # second opinion that is worth paying for.
        frame = self._jpeg("still.jpg", 100)
        session = FakeSession([])
        client = self._client([(None, 0.0)], session)
        processor = self._processor(client)
        self.lanes = Lanes(processor)

        self.lanes.inject(frame, stillness=0.001)
        self.assertTrue(wait_for(lambda: self.lanes.result_for(frame) is not None, 3.0))

        self.assertEqual(len(session.calls), 1)

    def test_a_frame_of_unknown_stillness_is_never_skipped(self):
        # The camera's own FTP still and a hot keyframe carry no stillness.
        frame = self._jpeg("ftp-still.jpg", 100, (3840, 2160))
        session = FakeSession([])
        client = self._client([(None, 0.0)], session)
        processor = self._processor(client)
        self.lanes = Lanes(processor)

        self.lanes.inject(frame)
        self.assertTrue(wait_for(lambda: self.lanes.result_for(frame) is not None, 3.0))

        self.assertEqual(len(session.calls), 1)

    def test_a_moving_frame_the_device_read_something_in_is_still_sent(self):
        # Found and refused is not "nothing there": 21:52:21.76, frame 2765,
        # read 333399 at 0.212 on the device. The cloud may do better.
        frame = self._jpeg("2765.jpg", 100)
        session = FakeSession([])
        client = self._client([("333399", 0.212)], session)
        processor = self._processor(client)
        self.lanes = Lanes(processor)

        self.lanes.inject(frame, stillness=0.02)
        self.assertTrue(wait_for(lambda: self.lanes.result_for(frame) is not None, 3.0))

        self.assertEqual(len(session.calls), 1)

    def test_the_moving_frame_rule_can_be_switched_off(self):
        frame = self._jpeg("moving.jpg", 100)
        session = FakeSession([])
        client = self._client([(None, 0.0)], session)
        processor = self._processor(client, cloud_skip_stillness=0)
        self.lanes = Lanes(processor)

        self.lanes.inject(frame, stillness=0.02)
        self.assertTrue(wait_for(lambda: self.lanes.result_for(frame) is not None, 3.0))

        self.assertEqual(len(session.calls), 1)

    def test_a_burst_whose_passage_opened_while_it_waited_is_given_up_in_the_lane(self):
        # The burst thread asks first; the lane asks again, because the wait
        # in between is exactly when the passage's other frame opens the gate.
        frame = self._jpeg("late.jpg", 100)
        session = FakeSession([])
        client = self._client([(None, 0.0)], session)
        processor = self._processor(client)
        asked = []

        def superseded(paths):
            asked.append(current_thread().name)
            return len(asked) > 1

        self.lanes = Lanes(processor, superseded=superseded)

        self.lanes.inject(frame, stillness=0.001)
        self.assertTrue(wait_for(lambda: self.lanes.coalesced, 3.0))

        self.assertEqual(asked, ["GateBurstProcessor", "GateCloudLane"])
        self.assertEqual(self.lanes.coalesced, [((frame,), "queue_coalesced")])
        self.assertEqual(session.calls, [], "a superseded frame must not be billed")
        self.assertIsNone(self.lanes.result_for(frame))

    def test_process_without_the_fast_lane_still_reads_on_the_device_itself(self):
        # A caller that never prepared the burst gets the pre-fast-lane path:
        # the local pass inside `process`, and no cloud request when it decides.
        frame = self._jpeg("legacy.jpg", 120)
        session = FakeSession([])
        client = self._client([("131D2696", 0.999)], session)
        processor = self._processor(client)

        result = processor.process((frame,))

        self.assertTrue(result.opened, f"denied: {result.reason}")
        self.assertEqual(session.calls, [])
        self.assertEqual(self._stored(processor, result)["source"], "local")


class ForgetfulRecognizer(TwoPhaseRecognizer):
    """A two-phase recogniser that also holds per-trace state to be released."""

    def __init__(self, **options):
        super().__init__(**options)
        self.bound = []
        self.forgotten_local = []
        self.forgotten_direction = []

    def bind_direction(self, trace_id, passage):
        self.bound.append(trace_id)

    def forget_local_ocr(self, trace_id):
        self.forgotten_local.append(trace_id)

    def forget_direction(self, trace_id):
        self.forgotten_direction.append(trace_id)


class PrepareTests(unittest.TestCase):
    """`prepare` and `process(prepared=...)` share one on-device read."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _jpeg(self, name, colour=128):
        path = self.root / name
        Image.new("L", (16, 8), color=colour).save(path, format="JPEG")
        return path

    def _processor(self, recognizer, **options):
        settings = dict(
            cooldown=timedelta(seconds=0), clock=lambda: datetime.now(timezone.utc),
            decision_timeout=7.0,
        )
        settings.update(options)
        return GateProcessor(
            recognizer=recognizer, store=LocalStore(self.root / "gate.db"),
            relay=RecordingRelay([]), authorised={"12D3456"}, **settings,
        )

    def test_the_read_taken_in_prepare_is_the_one_process_decides_on(self):
        frame = self._jpeg("frame.jpg")
        recognizer = TwoPhaseRecognizer(local_plate="12D3456", local_confidence=0.99)
        processor = self._processor(recognizer)

        prepared = processor.prepare((frame,), decision_started_at=monotonic())
        self.assertTrue(prepared.decided)
        self.assertFalse(prepared.needs_cloud)
        result = processor.process((frame,), prepared=prepared)

        self.assertTrue(result.opened)
        self.assertEqual(len(recognizer.local_calls), 1, "the frame must be read once")
        self.assertEqual(recognizer.cloud_calls, [])
        self.assertIsNotNone(result.telemetry)

    def test_an_undecided_read_is_carried_into_the_cloud_request(self):
        frame = self._jpeg("frame.jpg")
        recognizer = TwoPhaseRecognizer(
            cloud_observation=PlateObservation("12D3456", 0.99),
        )
        processor = self._processor(recognizer)

        prepared = processor.prepare((frame,), decision_started_at=monotonic())
        self.assertFalse(prepared.decided)
        self.assertTrue(prepared.needs_cloud)
        result = processor.process((frame,), prepared=prepared)

        self.assertTrue(result.opened)
        self.assertEqual(len(recognizer.local_calls), 1)
        self.assertEqual(len(recognizer.cloud_calls), 1)

    def test_a_burst_the_store_already_holds_is_answered_without_a_read(self):
        frame = self._jpeg("frame.jpg")
        recognizer = TwoPhaseRecognizer(local_plate="12D3456", local_confidence=0.99)
        processor = self._processor(recognizer)
        first = processor.prepare((frame,), decision_started_at=monotonic())
        processor.process((frame,), prepared=first)

        again = processor.prepare((frame,), decision_started_at=monotonic())

        self.assertIsNotNone(again.duplicate)
        self.assertFalse(again.needs_cloud)
        self.assertEqual(len(recognizer.local_calls), 1)
        self.assertIs(processor.process((frame,), prepared=again), again.duplicate)

    def test_a_prepared_burst_that_outlived_its_deadline_times_out_unbilled(self):
        frame = self._jpeg("frame.jpg")
        recognizer = TwoPhaseRecognizer(
            cloud_observation=PlateObservation("12D3456", 0.99),
        )
        processor = self._processor(recognizer)
        prepared = processor.prepare((frame,), decision_started_at=monotonic() - 1.0)

        result = processor.process(
            (frame,), prepared=PreparedBurst(**{
                **prepared.__dict__, "started": monotonic() - 8.0,
            }),
        )

        self.assertEqual(result.reason, "decision_timeout")
        self.assertEqual(recognizer.cloud_calls, [])

    def test_prepare_skips_the_read_when_no_budget_is_left(self):
        frame = self._jpeg("frame.jpg")
        recognizer = TwoPhaseRecognizer(local_plate="12D3456", local_confidence=0.99)
        processor = self._processor(recognizer)

        prepared = processor.prepare((frame,), decision_started_at=monotonic() - 8.0)

        self.assertFalse(prepared.local_pass_ran)
        self.assertEqual(recognizer.local_calls, [])
        self.assertEqual(processor.process((frame,), prepared=prepared).reason, "decision_timeout")

    def test_discarding_a_prepared_burst_releases_what_it_bound(self):
        frame = self._jpeg("frame.jpg")
        recognizer = ForgetfulRecognizer()
        processor = self._processor(recognizer)
        trigger = TriggerTelemetry(
            source="reolink_webhook", event_type="vehicle", rule_id="front_gate",
            correlation="matched", event_at="2026-09-10T20:52:21+00:00", delta_ms=10,
        )

        prepared = processor.prepare((frame,), decision_started_at=monotonic(), trigger=trigger)
        trace_id = prepared.trace.trace_id
        self.assertEqual(recognizer.bound, [trace_id])

        prepared.discard("event_already_opened")

        self.assertEqual(recognizer.forgotten_local, [trace_id])
        self.assertEqual(recognizer.forgotten_direction, [trace_id])

    def test_the_stillness_threshold_is_validated(self):
        self.assertEqual(_bounded_cloud_skip_stillness(None), DEFAULT_CLOUD_SKIP_STILLNESS)
        self.assertEqual(_bounded_cloud_skip_stillness(0), 0.0)
        self.assertEqual(_bounded_cloud_skip_stillness("0.02"), 0.02)
        for bad in ("nan", "inf", -0.1, 1.5, "plenty"):
            with self.assertLogs("gate_controller.processor", level="WARNING"):
                self.assertEqual(
                    _bounded_cloud_skip_stillness(bad), DEFAULT_CLOUD_SKIP_STILLNESS, bad,
                )


class BurstThreadFastLaneTests(unittest.TestCase):
    """The burst thread hands undecided bursts over and never waits for them."""

    def _item(self, path):
        now = datetime.now(timezone.utc)
        return ((path,), now, monotonic(), now, BurstIdentity("key", None, 0.001))

    def _prepared(self, paths, *, decided=False, discard_hook=None):
        prepared = PreparedBurst(
            paths=tuple(paths), digests=("d",), idempotency_key="key",
            received_at=datetime.now(timezone.utc), started=monotonic(),
            trace=None, trigger=None, discard_hook=discard_hook,
        )
        if decided:
            prepared.local_pass_ran = True
            prepared.local_attempt = LocalPass(
                observation=PlateObservation("12D3456", 0.99, source="local"),
            )
        return prepared

    def test_a_decided_burst_is_emitted_on_the_burst_thread_with_its_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")
            bursts = Queue()
            bursts.put(self._item(frame))
            bursts.put(None)
            prepared = self._prepared((frame,), decided=True)
            emitted = []
            lane = CloudLane(lambda *a, **k: None)

            def prepare(paths, received_at, *timing, **options):
                self.assertEqual(options.get("stillness"), 0.001)
                return prepared

            def emit(paths, received_at, *timing, **options):
                emitted.append((current_thread().name, options.get("prepared")))
                return ProcessingResult(True, "exact_match")

            _process_bursts(bursts, emit, prepare=prepare, cloud_lane=lane)

            self.assertEqual(emitted, [(current_thread().name, prepared)])
            self.assertEqual(lane.pending, 0)
            self.assertFalse(frame.exists(), "the upload is removed once decided")

    def test_an_undecided_burst_goes_to_the_lane_and_keeps_its_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")
            bursts = Queue()
            bursts.put(self._item(frame))
            bursts.put(None)
            prepared = self._prepared((frame,))
            emitted = []
            lane = CloudLane(
                lambda paths, received_at, *timing, **options: emitted.append(
                    (current_thread().name, options.get("prepared"))
                ) or ProcessingResult(False, "no_match"),
            )

            _process_bursts(
                bursts, lambda *a, **k: self.fail("the burst thread must not decide it"),
                prepare=lambda *a, **k: prepared, cloud_lane=lane,
            )

            self.assertEqual(emitted, [], "nothing is decided until the lane runs")
            self.assertTrue(frame.exists(), "the lane still needs the upload")
            self.assertEqual(lane.pending, 1)
            lane.stop()
            lane_thread = Thread(target=lane.run)
            lane_thread.start()
            lane_thread.join(5)
            # Stopped before it ran: the entry was handed back, not decided.
            self.assertEqual(emitted, [])

    def test_the_lane_decides_what_it_is_given_and_reports_it(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")
            item = self._item(frame)
            prepared = self._prepared((frame,))
            reported = []
            verdict = ProcessingResult(False, "no_match")
            lane = CloudLane(
                lambda paths, received_at, *timing, **options: verdict,
                on_result=lambda paths, result: reported.append((paths, result)),
            )
            lane_thread = Thread(target=lane.run, daemon=True)
            lane_thread.start()

            self.assertTrue(lane.submit(item, prepared, {}, None, [item[2], item[3]]))
            self.assertTrue(wait_for(lambda: reported, 5.0))
            lane.stop()
            lane_thread.join(5)

            self.assertEqual(reported, [((frame,), verdict)])
            self.assertFalse(frame.exists())

    def test_a_failed_prepare_is_reported_and_the_frame_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")
            bursts = Queue()
            bursts.put(self._item(frame))
            bursts.put(None)
            errors, dropped = [], []

            def prepare(*args, **options):
                raise RuntimeError("engine gone")

            _process_bursts(
                bursts, lambda *a, **k: self.fail("never emitted"),
                on_error=lambda paths, error, received_at, **kw: errors.append(str(error)),
                on_dropped=lambda paths, reason: dropped.append(reason),
                prepare=prepare, cloud_lane=CloudLane(lambda *a, **k: None),
            )

            self.assertEqual(errors, ["engine gone"])
            self.assertEqual(dropped, ["processing_error"])
            self.assertFalse(frame.exists())

    def test_a_closed_lane_gives_the_burst_up_as_service_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff\xd9")
            bursts = Queue()
            bursts.put(self._item(frame))
            bursts.put(None)
            discarded, coalesced = [], []
            prepared = self._prepared(
                (frame,), discard_hook=lambda p, reason: discarded.append(reason),
            )
            lane = CloudLane(lambda *a, **k: None)
            lane.stop()

            _process_bursts(
                bursts, lambda *a, **k: self.fail("never emitted"),
                coalesce=lambda item, reason="queue_coalesced": coalesced.append(reason),
                prepare=lambda *a, **k: prepared, cloud_lane=lane,
            )

            self.assertEqual(discarded, ["service_stopping"])
            self.assertEqual(coalesced, ["service_stopping"])

    def test_stopping_the_lane_hands_back_what_was_waiting(self):
        lane = CloudLane(lambda *a, **k: None)
        item = self._item(Path("/nonexistent/frame.jpg"))
        prepared = self._prepared(item[0])
        self.assertTrue(lane.submit(item, prepared, {}, None, []))

        pending = lane.stop()

        self.assertEqual([entry[1] for entry in pending], [prepared])
        self.assertFalse(lane.submit(item, prepared, {}, None, []))

    def test_run_worker_wires_the_lane_when_given_prepare(self):
        received_at = datetime(2026, 9, 10, 20, 52, 26, tzinfo=timezone.utc)
        trigger = TriggerTelemetry(
            source="reolink_webhook", event_type="vehicle",
            rule_id="front_gate", correlation="matched", delta_ms=10,
        )
        done = ThreadEvent()
        decided_on = []

        class Capture:
            output_directory = None

            def attach(self, inject):
                self.inject = inject

            def note_result(self, paths, result):
                decided_on.append((paths[0].name, result.reason))
                if len(decided_on) == 2:
                    done.set()

        capture = Capture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture.output_directory = root / ".trigger-capture"
            capture.output_directory.mkdir(mode=0o700)
            device = capture.output_directory / "device.jpg"
            cloud = capture.output_directory / "cloud.jpg"
            for frame in (device, cloud):
                frame.write_bytes(b"\xff\xd8\xff\xd9")
            threads = {}

            def prepare(paths, received_at, *timing, **options):
                return self._prepared(paths, decided=Path(paths[0]) == device)

            def emit(paths, received_at, *timing, prepared=None, **options):
                threads[paths[0].name] = current_thread().name
                return ProcessingResult(
                    prepared.decided, "exact_match" if prepared.decided else "no_match",
                )

            class OneShotWorker:
                def run_forever(self, stop_event):
                    capture.inject((device,), received_at, trigger, stillness=0.002)
                    capture.inject((cloud,), received_at, trigger, stillness=0.001)
                    done.wait(timeout=5)
                    stop_event.set()

            run_worker(
                root, emit, quiet_window=0.1, poll_interval=0.01,
                background_workers=(OneShotWorker(),),
                trigger_capture=capture, prepare=prepare,
            )

        self.assertEqual(sorted(decided_on), [("cloud.jpg", "no_match"), ("device.jpg", "exact_match")])
        self.assertEqual(threads["device.jpg"], "GateBurstProcessor")
        self.assertEqual(threads["cloud.jpg"], "GateCloudLane")


if __name__ == "__main__":
    unittest.main()
