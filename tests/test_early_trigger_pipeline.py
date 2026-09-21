"""An early-origin sweep, the way production runs it, and the one rule it lives under.

**No picture of a passage reaches the cloud plate reader unless the camera has
raised a vehicle event for that passage.** Everything here goes the way the
early trigger goes:

    on_early_trigger -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

inside the real ``run_worker``, on the harness ``tests/test_sweep_pipeline.py``
built for the camera's own alarm. The fakes are at the same true boundaries --
the frame source, the ONNX engine, the HTTP session, the relay -- and the HTTP
session is the spy: it is the single method a request to the cloud reader
leaves by, and it writes down when each one left.
"""
import ast
import sqlite3
import unittest
from contextlib import closing
from functools import partial
from pathlib import Path
from time import monotonic
from unittest import mock

from gate_controller.local_sweep import crop_to_region
from gate_controller.trigger_capture import (
    EarlyEvent, SweepPassage, TriggerCaptureConfig, TriggerFrameCapture,
)
from gate_controller.worker import BurstIdentity
from tests import test_sweep_pipeline as harness
from tests.test_local_recognizer import FakeResponse, FakeSession, cloud_payload
from tests.test_sweep_pipeline import REGION, CapturedLogs, Gate, digest, frame, wait_for

PACKAGE = Path(__file__).resolve().parents[1] / "gate_controller"


class SpySession(FakeSession):
    """The cloud boundary. Every request that leaves is timed."""

    def __init__(self, responses=()):
        super().__init__(responses)
        self.posted_at = []

    def post(self, *args, **kwargs):
        self.posted_at.append(monotonic())
        return super().post(*args, **kwargs)


def early_gate(test, *, early_max_seconds=1.2, abort_frames=0, **options):
    """The harness's gate, with the early trigger switched `on` in the capture."""
    with mock.patch.object(
        harness, "TriggerFrameCapture", partial(TriggerFrameCapture, early_trigger=True),
    ), mock.patch.object(
        harness, "TriggerCaptureConfig", partial(
            TriggerCaptureConfig, early_max_seconds=early_max_seconds,
            early_abort_frames=abort_frames,
        ),
    ):
        return Gate(test, **options)


def count_rows(store_path, table) -> int:
    with closing(sqlite3.connect(str(store_path))) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def camera_event():
    now = harness.datetime.now(harness.timezone.utc)
    return harness.SanitizedCameraEvent(
        event_id="e", event_type="vehicle", rule_id="front_gate", received_at=now, event_at=now,
    )


class EarlyOriginSweepTests(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _gate(self, **options):
        self.gate = early_gate(self, **options)
        return self.gate

    def _database(self):
        return Path(self.gate.directory.name) / "gate.db"

    # -- (a) ------------------------------------------------------------------
    def test_an_early_trigger_and_an_authorised_local_read_open_the_gate_once_with_no_cloud(self):
        frames = [frame(seed) for seed in range(201, 205)]
        answers = {
            digest(crop_to_region(frames[0], REGION)): (None, 0.0),
            digest(crop_to_region(frames[1], REGION)): ("10CE1990", 0.62),
            digest(crop_to_region(frames[2], REGION)): ("10CE1990", 0.93),
            digest(crop_to_region(frames[3], REGION)): ("10CE1990", 0.95),
        }
        session = SpySession()
        gate = self._gate(answers=answers, cloud=session, cloud_frames=5, fallback=1,
                          early_max_seconds=3.0)
        gate.frames.script([(0.05 + index * 0.15, data) for index, data in enumerate(frames)])
        reports = []
        gate.capture.set_early_observer(reports.append)

        with CapturedLogs() as logs:
            self.assertEqual(gate.capture.on_early_trigger({"blob_fraction": 0.1}), "scheduled")
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))
            self.assertTrue(wait_for(lambda: reports))

        self.assertEqual(gate.relay_calls, ["relay"], "exactly one pulse")
        self.assertEqual(session.posted_at, [], "an early-origin passage asked the cloud reader")
        result = gate.opened_result()
        self.assertEqual(result.reason, "exact_match")
        self.assertEqual(gate.stored(result)["source"], "local")
        # The same bar, the same carried-read path, the same record as a
        # camera-origin sweep -- and on the wire it does not pose as one.
        self.assertIn("stage=sweep_read_adopted", logs.text())
        self.assertEqual(result.telemetry.trigger.to_wire()["source"], "camera_ftp")
        self.assertEqual(result.telemetry.trigger.to_wire()["correlation"], "unverified")
        self.assertIn("gate_local_sweep outcome=ended reason=opened", logs.text())
        self.assertFalse(reports[0]["upgraded"])
        self.assertEqual(reports[0]["cloud_handovers"], 0)
        self.assertGreaterEqual(reports[0]["plate_reads"], 1)

    # -- (b) ------------------------------------------------------------------
    def test_an_unread_plate_and_no_camera_event_sends_nothing_and_records_nothing(self):
        frames = [frame(seed) for seed in range(211, 223)]
        # A plate is there and legible, and it is nobody's: exactly what the
        # cloud lane and the fallback exist to be handed.
        answers = {digest(crop_to_region(data, REGION)): ("99KK999", 0.91) for data in frames}
        session = SpySession()
        gate = self._gate(answers=answers, cloud=session, cloud_frames=5, fallback=1, waiting=2.0)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        reports = []
        gate.capture.set_early_observer(reports.append)

        with CapturedLogs() as logs:
            self.assertEqual(gate.capture.on_early_trigger(), "scheduled")
            self.assertTrue(wait_for(lambda: reports), logs.text())
            self.assertTrue(wait_for(lambda: gate.frames.stopped), "the session was left running")

        self.assertEqual(reports[0]["reason"], "early_unconfirmed")
        self.assertGreaterEqual(reports[0]["reads"], 3)
        self.assertEqual(reports[0]["injected"], 0)
        self.assertEqual(session.posted_at, [], "a frame went to the cloud with no camera event")
        self.assertEqual(gate.outcomes(), [], "a burst reached the pipeline")
        self.assertEqual(count_rows(self._database(), "events"), 0, "an access-log event was written")
        self.assertEqual(count_rows(self._database(), "outbox"), 0, "something was queued for upload")
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.frames.stopped, ["early_unconfirmed"])
        self.assertNotIn("stage=cloud_handover", logs.text())
        self.assertNotIn("source=sweep_fallback", logs.text())
        self.assertNotIn("stage=waiting", logs.text())
        self.assertNotIn("presence", logs.text())

    def test_an_empty_drive_ends_an_early_sweep_after_a_few_frames(self):
        frames = [frame(seed) for seed in range(231, 251)]
        session = SpySession()
        gate = self._gate(answers={}, cloud=session, cloud_frames=5, fallback=1,
                          empty_scene=0.03, abort_frames=5, early_max_seconds=5.0)
        gate.frames.empty = {digest(data) for data in frames}
        gate.frames.script([(0.05 + index * 0.05, data) for index, data in enumerate(frames)])
        reports = []
        gate.capture.set_early_observer(reports.append)
        started = monotonic()
        self.assertEqual(gate.capture.on_early_trigger(), "scheduled")
        self.assertTrue(wait_for(lambda: reports))
        self.assertEqual(reports[0]["reason"], "early_abort")
        self.assertEqual(reports[0]["reads"], 0, "an empty frame costs a thumbnail, not a read")
        self.assertLess(monotonic() - started, 2.0, "well inside the seconds it may run")
        self.assertEqual(session.posted_at, [])
        self.assertEqual(count_rows(self._database(), "events"), 0)

    def test_a_vehicle_in_the_picture_is_not_aborted_on_for_having_no_plate_yet(self):
        frames = [frame(seed) for seed in range(261, 275)]
        gate = self._gate(answers={}, cloud=SpySession(), empty_scene=0.03, abort_frames=3,
                          early_max_seconds=1.0)
        gate.frames.script([(0.05 + index * 0.06, data) for index, data in enumerate(frames)])
        reports = []
        gate.capture.set_early_observer(reports.append)
        gate.capture.on_early_trigger()
        self.assertTrue(wait_for(lambda: reports))
        self.assertEqual(reports[0]["reason"], "early_unconfirmed")
        self.assertGreaterEqual(reports[0]["reads"], 4)

    # -- (c) ------------------------------------------------------------------
    def test_a_camera_alarm_mid_sweep_upgrades_it_in_place(self):
        frames = [frame(seed) for seed in range(301, 341)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.41) for data in frames}
        session = SpySession([FakeResponse(cloud_payload("10CE1990", 0.96))])
        gate = self._gate(answers=answers, cloud=session, cloud_frames=3, fallback=1,
                          sweep_seconds=1.5, early_max_seconds=1.5)
        gate.frames.script([(0.05 + index * 0.08, data) for index, data in enumerate(frames)])
        reports = []
        gate.capture.set_early_observer(reports.append)

        with CapturedLogs() as logs:
            self.assertEqual(gate.capture.on_early_trigger(), "scheduled")
            self.assertTrue(wait_for(lambda: gate.engine.read_count() >= 3), "the sweep never read")
            self.assertEqual(session.posted_at, [], "asked the cloud before the camera spoke")
            self.assertNotIn("stage=cloud_handover", logs.text())
            alarm_at = monotonic()
            gate.alarm()
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(lambda: reports))

        output = logs.text()
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertGreaterEqual(len(session.posted_at), 1)
        self.assertTrue(all(at >= alarm_at for at in session.posted_at),
                        "a request left before the camera's alarm")
        self.assertEqual(gate.frames.sessions, 1, "one sweep, one decoder session -- not two")
        self.assertEqual(output.count("gate_local_sweep outcome=ended"), 1)
        self.assertIn("gate_local_sweep stage=upgraded origin=early event_type=vehicle", output)
        self.assertTrue(reports[0]["upgraded"])
        self.assertGreaterEqual(reports[0]["lead_ms"], 100)
        # After the alarm the passage is the camera's, and says so.
        self.assertEqual(gate.opened_result().telemetry.trigger.to_wire()["source"],
                         "reolink_webhook")

    def test_the_car_gets_its_whole_read_window_from_the_cameras_alarm(self):
        frames = [frame(seed) for seed in range(351, 420)]
        gate = self._gate(answers={}, cloud=SpySession(), cloud_frames=0, fallback=0,
                          sweep_seconds=1.2, early_max_seconds=1.2)
        gate.frames.script([(0.05 + index * 0.05, data) for index, data in enumerate(frames)])
        reports = []
        gate.capture.set_early_observer(reports.append)
        with CapturedLogs():
            started = monotonic()
            gate.capture.on_early_trigger()
            self.assertTrue(wait_for(lambda: gate.engine.read_count() >= 8))
            alarm_at = monotonic()
            gate.alarm()
            self.assertTrue(wait_for(lambda: reports))
            ended_at = monotonic()
        self.assertEqual(reports[0]["reason"], "window")
        self.assertGreaterEqual(ended_at - alarm_at, 1.2 - 0.05,
                                "the early sweep's head start was charged to the window")
        self.assertGreater(ended_at - started, 1.2 + (alarm_at - started) - 0.1)

    # -- (d) ------------------------------------------------------------------
    def _spied_prepare(self, trigger):
        frames = [frame(seed) for seed in range(431, 434)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.95) for data in frames}
        seen = []
        real = harness.GateProcessor.prepare

        def spying_prepare(processor, *args, **kwargs):
            seen.append(dict(kwargs))
            return real(processor, *args, **kwargs)

        with mock.patch.object(harness.GateProcessor, "prepare", spying_prepare):
            gate = self._gate(answers=answers, cloud=SpySession())
            gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
            trigger(gate)
            self.assertTrue(wait_for(gate.opened))
        self.assertTrue(seen)
        return seen

    def test_a_camera_origin_frame_carries_no_permit_and_no_origin_at_all(self):
        for kwargs in self._spied_prepare(lambda gate: gate.alarm()):
            self.assertNotIn("cloud_permit", kwargs, "today's path was handed something new")

    def test_an_early_origin_frame_carries_the_permit_all_the_way_to_prepare(self):
        seen = self._spied_prepare(lambda gate: gate.capture.on_early_trigger())
        self.assertTrue(all(callable(kwargs.get("cloud_permit")) for kwargs in seen))
        self.assertFalse(seen[0]["cloud_permit"](), "and with no camera event it answers no")

    # -- the pipeline's own half of the rule -----------------------------------
    def test_a_frame_the_pipeline_cannot_decide_is_not_sent_on_to_the_cloud(self):
        """The sweep admits the read under `standard`; the pipeline, under `strict`,
        does not. A camera-origin frame would now go to the cloud lane. This one may not."""
        frames = [frame(seed) for seed in range(451, 454)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.80) for data in frames}
        session = SpySession([FakeResponse(cloud_payload("10CE1990", 0.99))])
        gate = self._gate(answers=answers, cloud=session, cloud_frames=5, fallback=1,
                          policy=lambda: harness.DEFAULT_POLICY,
                          processor_policy=lambda: harness.STRICT_POLICY)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.capture.on_early_trigger()
            self.assertTrue(wait_for(gate.outcomes), logs.text())
            self.assertTrue(wait_for(lambda: gate.frames.stopped))
        self.assertEqual(session.posted_at, [], "the pipeline asked the cloud for an early frame")
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.opened(), [])
        self.assertIn("gate_ocr stage=cloud_refused reason=no_camera_event", logs.text())

    def test_in_always_mode_the_local_answer_still_opens_and_the_label_request_is_not_sent(self):
        """GATE_LOCAL_OCR_CLOUD=always posts every frame for a label. Not these."""
        frames = [frame(seed) for seed in range(461, 464)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.95) for data in frames}
        session = SpySession()
        real = harness.LocalRecognizerConfig
        with mock.patch.object(
            harness, "LocalRecognizerConfig", lambda **kw: real(**{**kw, "cloud": "always"}),
        ):
            gate = self._gate(answers=answers, cloud=session, cloud_frames=5, fallback=1)
        for data in frames:
            answers[digest(gate.pipeline_bytes(data))] = ("10CE1990", 0.95)
        gate.frames.script([(0.05 + index * 0.1, data) for index, data in enumerate(frames)])
        with CapturedLogs() as logs:
            gate.capture.on_early_trigger()
            self.assertTrue(wait_for(gate.opened), f"{gate.outcomes()}\n{logs.text()}")
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertEqual(session.posted_at, [])

    # -- never in the way of the camera -----------------------------------------
    def test_an_early_trigger_never_costs_the_camera_its_alarm(self):
        gate = self._gate(answers={}, cloud=SpySession())
        # Straight after an early trigger the camera's alarm is still taken: the
        # early trigger takes no part in the camera's own rate limit.
        self.assertEqual(gate.capture.on_early_trigger(), "scheduled")
        gate.alarm()

    def test_a_queued_early_trigger_gives_way_to_the_cameras_alarm(self):
        capture = TriggerFrameCapture(
            TriggerCaptureConfig(enabled=True, output_directory=Path("/nonexistent")),
            early_trigger=True,
        )
        capture._queue.put_nowait((EarlyEvent(), 1.0))
        self.assertEqual(capture.on_camera_event(camera_event()), "scheduled")
        queued, _at = capture._queue.get_nowait()
        self.assertEqual(queued.event_type, "vehicle")

    def test_a_second_camera_alarm_still_waits_its_turn_behind_the_first(self):
        capture = TriggerFrameCapture(
            TriggerCaptureConfig(enabled=True, output_directory=Path("/nonexistent"),
                                 min_interval_seconds=0.0),
            early_trigger=True,
        )
        self.assertEqual(capture.on_camera_event(camera_event()), "scheduled")
        self.assertEqual(capture.on_camera_event(camera_event()), "skipped_busy")

    def test_the_capture_refuses_an_early_trigger_unless_it_was_switched_on(self):
        self.gate = Gate(self, answers={}, cloud=SpySession())
        self.assertEqual(self.gate.capture.on_early_trigger(), "disabled")
        self.assertTrue(self.gate.capture._queue.empty())
        self.assertEqual(self.gate.frames.sessions, 0)


class CloudPathGuardTests(unittest.TestCase):
    """The routes to the cloud reader, enumerated. A new one fails here first."""

    def _source(self, name):
        return (PACKAGE / name).read_text(encoding="utf-8")

    def test_one_method_posts_to_the_cloud_reader_and_it_asks_the_permit_first(self):
        posts = {}
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "post"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "session"
                ):
                    posts.setdefault(path.name, []).append(node.lineno)
        self.assertEqual(list(posts), ["ocr.py"], "a second module posts on an OCR session")
        self.assertEqual(len(posts["ocr.py"]), 1, "a second request site in the cloud client")
        source = self._source("ocr.py")
        body = source[source.index("def _recognise_once("):]
        self.assertLess(
            body.index('_cloud_permitted(state.get("cloud_permit"))'), body.index("session.post("),
            "the request leaves before the permit is asked",
        )

    def test_the_recognisers_network_entry_has_one_caller_and_it_passes_the_permit(self):
        callers = []
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "._recognise_call(" in source or "recognizer.recognise(" in source:
                callers.append(path.name)
        self.assertEqual(callers, ["processor.py"])
        source = self._source("processor.py")
        recognise = source[source.index("    def _recognise("):source.index("    def _bounded_cloud_call(")]
        self.assertEqual(recognise.count("self._recognise_call("), 2)
        self.assertIn('extra["cloud_permit"] = cloud_permit', recognise)
        self.assertLess(recognise.index("not _permits(cloud_permit)"),
                        recognise.index("self._recognise_call("))
        self.assertIn("cloud_permit=prepared.cloud_permit", source)
        self.assertIn("and not self.appearance_admits and self.cloud_allowed", source)

    def test_every_frame_a_sweep_hands_on_goes_through_the_guarded_hand_over(self):
        source = self._source("trigger_capture.py")
        sweep = source[source.index("    def local_sweep("):source.index("    def _take_camera_event(")]
        self.assertEqual(sweep.count("self._inject_bytes("), 1, "a sweep injects outside hand_over")
        sources = sorted(set(
            part.split('"')[1] for part in sweep.split("source=")[1:] if part.startswith('"')
        ))
        self.assertEqual(sources, ["sweep", "sweep_cloud", "sweep_fallback"],
                         "a new kind of sweep hand-over: decide whether the cloud may see it")
        guard = sweep[sweep.index("def hand_over("):sweep.index("def run_fallback(")]
        self.assertIn('if source != "sweep" and not passage.cloud_allowed():', guard)
        self.assertLess(guard.index("passage.cloud_allowed()"), guard.index("self._inject_bytes("))
        self.assertIn("and passage.cloud_allowed()\n", sweep)
        # The presence session and the spaced series hand frames on for the
        # cloud to read; an unconfirmed early passage reaches neither.
        run = source[source.index("    def run_forever("):source.index("    def _sweep_ready(")]
        self.assertIn('self._stop_live_session("early_unconfirmed")', run)
        self.assertIn("if early and not (self._early_enabled and self._sweep_ready()):", run)

    def test_an_early_frame_without_a_permit_is_never_asked_about(self):
        self.assertIsNone(BurstIdentity("key", origin="early").cloud_permit)
        source = self._source("worker.py")
        self.assertIn('if origin != "camera" and cloud_permit is None:', source)
        self.assertIn("cause=early_origin_needs_fast_lane", source)

    def test_a_passage_says_who_started_it_and_only_the_camera_confirms_it(self):
        self.assertTrue(SweepPassage("camera").cloud_allowed())
        early = SweepPassage("early")
        self.assertFalse(early.cloud_allowed())
        early.confirm(1.0)
        self.assertTrue(early.cloud_allowed())
        self.assertTrue(early.early, "and it never forgets who started it")
        self.assertFalse(SweepPassage("something else").cloud_allowed(), "unknown is not the camera")


if __name__ == "__main__":
    unittest.main()
