"""When the controller nudges the camera's zoom, and what stops it.

The trigger logic runs with a fake wall clock, the controller's real
`ActivityGate` on a fake monotonic clock, and either a fake camera-control
client or -- in `EndToEndTests` -- the real camera-control service on a socket
in front of a fake RLC-811A. Stills are real JPEGs scored by the same
function event frames are scored with.
"""

import io
import json
import logging
import threading
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from PIL import Image, ImageFilter

from gate_camera_control.__main__ import CameraControlServer, build_service
from gate_controller.backpressure import ActivityGate
from gate_controller.camera_focus import (
    DEFAULT_MAX_PER_DAY, DEFAULT_MIN_BRIGHTNESS, DEFAULT_SHARPNESS_THRESHOLD,
    DEFER_SECONDS, CameraControlClient, CameraControlError, CameraRefocusWorker,
    FocusWindow, RefocusConfig, build_refocus_worker, load_refocus_config,
)
from gate_controller.images import measure_frame_quality, measure_jpeg_quality
from gate_controller.telemetry import FrameTelemetry
from tests.test_camera_control import FakeCamera, ManualClock


DUBLIN = ZoneInfo("Europe/Dublin")
LOGGER_NAME = "gate_controller.camera_focus"


def dublin(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=DUBLIN).timestamp()


def scene(*, blur=0, level=150, size=(1920, 1080)):
    """A textured, lit still; blurred, it scores the way the soft frames did."""
    width, height = size
    texture = Image.effect_noise((width // 6, height // 6), 60).point(
        lambda value: max(0, min(255, value - 128 + level)))
    image = texture.resize(size, Image.Resampling.NEAREST)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


SHARP = scene()
SOFT = scene(blur=8)
DARK = scene(level=15)


def frame(sharpness, *, width=3840, height=2160, brightness=0.42, status="ok"):
    return FrameTelemetry(
        sequence=0, digest="a" * 64, width=width, height=height, sharpness=sharpness,
        brightness=brightness, darkness=0.0, highlight_clipping=0.0, status=status,
    )


def event(trace, *sharpness, **frame_options):
    return SimpleNamespace(telemetry=SimpleNamespace(
        trace_id=trace, frames=tuple(frame(value, **frame_options) for value in sharpness),
    ))


class FakeCameraControl:
    """The camera-control service as the worker sees it."""

    def __init__(self, stills=(SOFT, SHARP)):
        self.stills = list(stills)
        self.snapshots = 0
        self.refocus_calls = []
        self.answer = {
            "status": "completed", "reason": "daily",
            "zoom": {"before": 2, "stepped_to": 3, "after": 2},
            "focus": {"before": 86, "after": 78},
        }
        self.during_refocus = None

    def snapshot(self):
        self.snapshots += 1
        if not self.stills:
            raise CameraControlError("camera_busy", status=503)
        return self.stills.pop(0) if len(self.stills) > 1 else self.stills[0]

    def refocus(self, reason):
        self.refocus_calls.append(reason)
        if self.during_refocus is not None:
            self.during_refocus()
        if isinstance(self.answer, Exception):
            raise self.answer
        return dict(self.answer, reason=reason)


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "camera-refocus.json"
        # The gate's own idle clock runs with the wall clock here, and the
        # gate has been quiet since ten minutes before the first look.
        self.wall = ManualClock(dublin(5, 8, 50))
        self.activity = ActivityGate(quiet_seconds=60, clock=self.wall)
        self.at(5, 9)
        self.session = False
        self.ir = "Off"
        self.client = FakeCameraControl()

    def worker(self, config=None, client=None):
        return CameraRefocusWorker(
            config or RefocusConfig(), client or self.client, state_path=self.state_path,
            activity=self.activity, session_active=lambda: self.session,
            ir_state=lambda: self.ir, clock=self.wall, sleep=lambda _seconds: None,
        )

    def at(self, day, hour, minute=0):
        self.wall.now = dublin(day, hour, minute)


class DailyNudgeTests(WorkerTestCase):
    def test_once_a_day_inside_the_midday_window_and_not_outside_it(self):
        worker = self.worker()

        self.assertIsNone(worker.run_once())  # 09:00, nothing soft
        self.assertEqual(0, self.client.snapshots)

        self.at(5, 12, 30)
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.assertEqual("completed", worker.run_once())
        self.assertEqual(["daily"], self.client.refocus_calls)
        [line] = [line for line in logs.output if "stage=attempt" in line]
        self.assertTrue(line.startswith("INFO:"))
        for field in ("trigger=daily", "outcome=completed", "zoom_before=2", "zoom_step=3",
                      "zoom_after=2", "focus_before=86", "focus_after=78", "disturbed=false"):
            self.assertIn(field, line)
        before = measure_jpeg_quality(SOFT).sharpness
        after = measure_jpeg_quality(SHARP).sharpness
        self.assertIn(f"sharpness_before={before:.3f}", line)
        self.assertIn(f"sharpness_after={after:.3f}", line)

        self.at(5, 14, 45)
        self.assertIsNone(worker.run_once())
        self.at(6, 11, 59)
        self.assertIsNone(worker.run_once())
        self.client.stills = [SOFT, SHARP]
        self.at(6, 12, 0)
        self.assertEqual("completed", worker.run_once())
        self.assertEqual(["daily", "daily"], self.client.refocus_calls)

    def test_the_window_is_local_time_across_the_clock_change(self):
        worker = self.worker()
        # 25 October 2026 is the first day of Irish winter time: 12:30 local
        # is 12:30 UTC, an hour later in UTC than the week before.
        self.wall.now = datetime(2026, 10, 25, 11, 45, tzinfo=ZoneInfo("UTC")).timestamp()
        self.assertIsNone(worker.run_once())
        self.wall.now = datetime(2026, 10, 25, 12, 30, tzinfo=ZoneInfo("UTC")).timestamp()
        self.assertEqual("completed", worker.run_once())


class DetectionTests(WorkerTestCase):
    def feed(self, worker, *events):
        for item in events:
            worker.observe_result(item)

    def test_the_4_october_frames_trigger_a_nudge_outside_the_daily_window(self):
        worker = self.worker()
        # Measured 4-5 October: every daylight frame at or under 0.158, on
        # both widths the controller sees.
        self.feed(worker, event("a", 0.142, 0.151), event("b", 0.158, width=1920, height=1080))

        self.assertTrue(worker.window.verdict()["soft"])
        self.assertEqual("completed", worker.run_once())
        self.assertEqual(["detection"], self.client.refocus_calls)
        # The frames described the old focus; they are gone.
        self.assertEqual(0, worker.window.verdict()["frames"])

    def test_healthy_september_frames_never_trigger(self):
        worker = self.worker()
        # 15-30 September daylight: frames 0.19-0.29, medians 0.21-0.26.
        self.feed(worker, event("a", 0.19, 0.21, 0.22), event("b", 0.26, 0.29, 0.19))

        verdict = worker.window.verdict()
        self.assertFalse(verdict["soft"])
        self.assertEqual(6, verdict["frames"])
        self.assertIsNone(worker.run_once())
        self.assertEqual(0, self.client.snapshots)

    def test_one_blurred_passage_alone_is_not_a_lens_fault(self):
        worker = self.worker()
        self.feed(worker, event("a", 0.10, 0.12, 0.11, 0.09))

        self.assertFalse(worker.window.verdict()["soft"])
        self.assertIsNone(worker.run_once())

    def test_two_soft_frames_are_not_enough(self):
        worker = self.worker()
        self.feed(worker, event("a", 0.12), event("b", 0.13))

        self.assertFalse(worker.window.verdict()["soft"])

    def test_only_lit_whole_frames_taken_with_ir_off_are_compared(self):
        worker = self.worker()
        self.feed(
            worker,
            # The plate band crop scores on a different footing.
            event("crop", 0.10, 0.10, width=1440, height=648),
            # Night: the autofocus is not judged by headlights.
            event("dark", 0.05, 0.05, brightness=0.05),
            event("small", 0.10, 0.10, width=640, height=360),
            event("broken", 0.0, 0.0, status="quality_unavailable"),
        )
        self.assertEqual(0, worker.window.verdict()["frames"])

        self.ir = "Auto"
        self.feed(worker, event("ir", 0.10, 0.10, 0.10), event("ir2", 0.10))
        self.ir = "unknown"
        self.feed(worker, event("unknown", 0.10, 0.10, 0.10), event("unknown2", 0.10))
        self.assertEqual(0, worker.window.verdict()["frames"])

    def test_old_frames_age_out_of_the_window(self):
        worker = self.worker()
        self.feed(worker, event("a", 0.12, 0.13), event("b", 0.14))
        self.wall.advance(37 * 3600)

        self.assertEqual(0, worker.window.verdict()["frames"])

    def test_a_burst_without_telemetry_or_with_junk_never_raises(self):
        worker = self.worker()
        worker.observe_result(None)
        worker.observe_result(SimpleNamespace(telemetry=None))
        worker.observe_result(SimpleNamespace(telemetry=SimpleNamespace(frames=[object()])))
        worker._ir_state = lambda: (_ for _ in ()).throw(OSError("gone"))
        worker.observe_result(event("a", 0.1))
        self.assertEqual(0, worker.window.verdict()["frames"])

    def test_detection_runs_are_spaced_and_the_day_is_capped(self):
        worker = self.worker(RefocusConfig(min_interval_seconds=3600.0, max_per_day=3))
        outcomes = []
        for hour in (10, 11, 12, 13):
            self.at(5, hour)
            self.client.stills = [SOFT, SHARP]
            self.feed(worker, event(f"a{hour}", 0.12, 0.13), event(f"b{hour}", 0.14))
            outcomes.append(worker.run_once())
        self.assertEqual(["completed", "completed", "completed", "skipped_daily_cap"], outcomes)
        self.assertEqual(3, len(self.client.refocus_calls))

    def test_a_nudge_that_does_not_help_is_said_once_and_not_repeated(self):
        worker = self.worker()
        self.client.stills = [SOFT, SOFT]
        self.feed(worker, event("a", 0.12, 0.13), event("b", 0.14))

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.assertEqual("not_recovered", worker.run_once())
        [line] = [line for line in logs.output if "stage=attempt" in line]
        self.assertTrue(line.startswith("WARNING:"))
        self.assertIn("outcome=not_recovered", line)

        # Still soft afterwards, frame after frame: no loop.
        for minutes in (30, 120, 300):
            self.wall.now = dublin(5, 9) + minutes * 60
            self.feed(worker, event(f"c{minutes}", 0.12, 0.13), event(f"d{minutes}", 0.14))
            self.assertIsNone(worker.run_once())
        self.assertEqual(1, len(self.client.refocus_calls))
        # The interval is what lets it try again, not the frames.
        self.wall.now = dublin(5, 15, 1)
        self.client.stills = [SOFT, SHARP]
        self.assertEqual("completed", worker.run_once())


class GuardTests(WorkerTestCase):
    def setUp(self):
        super().setUp()
        self.at(5, 12, 30)

    def assert_nothing_touched(self):
        self.assertEqual(0, self.client.snapshots)
        self.assertEqual([], self.client.refocus_calls)

    def test_never_while_a_vehicle_is_being_handled(self):
        worker = self.worker()
        with self.activity.activity("camera_event"):
            self.assertEqual("skipped_vehicle_activity", worker.run_once())
        self.assert_nothing_touched()

    def test_not_until_the_gate_has_been_quiet_for_the_window(self):
        worker = self.worker()
        with self.activity.activity("burst"):
            pass
        self.wall.advance(60)
        self.assertEqual("skipped_vehicle_activity", worker.run_once())
        self.assert_nothing_touched()
        self.wall.advance(61)
        self.assertEqual("completed", worker.run_once())

    def test_never_during_a_presence_session(self):
        worker = self.worker()
        self.session = True
        self.assertEqual("skipped_vehicle_activity", worker.run_once())
        self.assert_nothing_touched()

    def test_a_presence_reader_that_raises_means_no(self):
        worker = self.worker()
        worker._session_active = lambda: (_ for _ in ()).throw(RuntimeError("?"))
        self.assertEqual("skipped_vehicle_activity", worker.run_once())
        self.assert_nothing_touched()

    def test_only_with_ir_off(self):
        worker = self.worker()
        for state in ("Auto", "unknown"):
            with self.subTest(state=state):
                self.ir = state
                self.assertEqual("skipped_ir_not_off", worker.run_once())
        self.assert_nothing_touched()

    def test_daylight_only_and_a_dark_still_is_not_asked_again_every_poll(self):
        worker = self.worker()
        self.client.stills = [DARK]
        self.assertGreater(DEFAULT_MIN_BRIGHTNESS, measure_jpeg_quality(DARK).brightness)

        self.assertEqual("skipped_dark", worker.run_once())
        self.assertEqual(1, self.client.snapshots)
        self.assertEqual([], self.client.refocus_calls)
        self.wall.advance(30)
        self.assertIsNone(worker.run_once())
        self.assertEqual(1, self.client.snapshots)
        self.wall.advance(DEFER_SECONDS)
        self.client.stills = [SOFT, SHARP]
        self.assertEqual("completed", worker.run_once())

    def test_no_still_means_no_nudge(self):
        worker = self.worker()
        self.client.stills = []
        self.assertEqual("skipped_snapshot_camera_busy", worker.run_once())
        self.assertEqual([], self.client.refocus_calls)

    def test_a_car_arriving_while_the_still_is_taken_stops_it(self):
        worker = self.worker()
        original = self.client.snapshot

        def snapshot_then_car():
            image = original()
            self.session = True
            return image

        self.client.snapshot = snapshot_then_car
        self.assertEqual("skipped_vehicle_activity", worker.run_once())
        self.assertEqual([], self.client.refocus_calls)

    def test_the_nudge_holds_the_activity_gate_and_records_a_car_that_came_during_it(self):
        worker = self.worker()
        seen = []

        def car_arrives():
            seen.append(self.activity.busy_reason())
            with self.activity.activity("camera_event"):
                pass

        self.client.during_refocus = car_arrives
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            worker.run_once()

        # The early-trigger watcher pauses on a busy gate rather than reading
        # a zooming scene as an arrival.
        self.assertEqual(["camera_refocus"], seen)
        self.assertIsNone(self.activity.busy_reason())
        self.assertIn("disturbed=true", [l for l in logs.output if "stage=attempt" in l][0])

    def test_shadow_measures_and_journals_but_never_moves_the_lens(self):
        worker = self.worker(RefocusConfig(mode="shadow"))
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.assertEqual("shadow", worker.run_once())
        self.assertEqual([], self.client.refocus_calls)
        self.assertEqual(1, self.client.snapshots)
        self.assertIn("outcome=shadow", [l for l in logs.output if "stage=attempt" in l][0])
        self.assertIsNone(worker.run_once())  # that was the day's

    def test_off_does_nothing_at_all(self):
        worker = self.worker(RefocusConfig(mode="off"))
        worker.observe_result(event("a", 0.1, 0.1, 0.1))
        self.assertIsNone(worker.run_once())
        self.assert_nothing_touched()
        self.assertIsNone(build_refocus_worker(
            {"GATE_CAMERA_REFOCUS": "off"}, state_directory=self.directory.name,
        ))

    def test_a_zoom_that_was_not_put_back_is_an_error_line_with_the_positions(self):
        worker = self.worker()
        self.client.answer = CameraControlError(
            "zoom_return_failed", status=502,
            body={"error": "zoom_return_failed", "zoom": {"expected": 2, "observed": 3}},
        )
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.assertEqual("zoom_return_failed", worker.run_once())
        [line] = [l for l in logs.output if "stage=attempt" in l]
        self.assertTrue(line.startswith("ERROR:"))
        self.assertIn("zoom_before=2", line)
        self.assertIn("zoom_after=3", line)

    def test_a_service_that_refuses_is_recorded_and_spends_the_attempt(self):
        worker = self.worker()
        self.client.answer = CameraControlError("rate_limited", status=429)
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            self.assertEqual("rate_limited", worker.run_once())
        self.wall.advance(600)
        self.assertIsNone(worker.run_once())


class PersistenceTests(WorkerTestCase):
    def test_a_restart_remembers_the_day_and_the_attempts(self):
        self.at(5, 12, 30)
        self.assertEqual("completed", self.worker().run_once())
        stored = json.loads(self.state_path.read_text())
        self.assertEqual("2026-10-05", stored["daily_date"])

        self.at(5, 13, 45)
        restarted = self.worker()
        self.assertIsNone(restarted.run_once())
        self.assertEqual(1, restarted.status()["attempts_24h"])
        self.assertEqual("completed", restarted.status()["last_attempt"]["outcome"])

    def test_a_state_file_that_cannot_be_read_holds_the_lens_still(self):
        self.state_path.write_text("{broken")
        self.at(5, 12, 30)
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            worker = self.worker()
        self.assertIsNone(worker.run_once())
        self.assertEqual([], self.client.refocus_calls)

    def test_the_heartbeat_block_is_bounded_and_says_what_happened(self):
        self.at(5, 12, 30)
        worker = self.worker()
        worker.run_once()
        status = worker.status()

        self.assertEqual({"mode", "window", "attempts_24h", "max_per_day", "daily_done_for",
                          "last_attempt"}, set(status))
        self.assertEqual(DEFAULT_SHARPNESS_THRESHOLD, status["window"]["threshold"])
        last = status["last_attempt"]
        self.assertEqual((2, 2, 86, 78), (last["zoom_before"], last["zoom_after"],
                                          last["focus_before"], last["focus_after"]))
        self.assertLess(last["sharpness_before"], DEFAULT_SHARPNESS_THRESHOLD)
        self.assertGreater(last["sharpness_after"], DEFAULT_SHARPNESS_THRESHOLD)
        json.dumps(status)


class ConfigTests(unittest.TestCase):
    def test_defaults_ship_enabled_with_the_measured_thresholds(self):
        config = load_refocus_config({})

        self.assertEqual("on", config.mode)
        self.assertEqual("Europe/Dublin", config.timezone_name)
        self.assertEqual((12 * 60, 15 * 60), (config.daily_start_minute, config.daily_end_minute))
        self.assertEqual(0.18, config.sharpness_threshold)
        self.assertEqual(3, config.min_frames)
        self.assertEqual(0.25, config.min_brightness)
        self.assertEqual(6 * 3600, config.min_interval_seconds)
        self.assertEqual(DEFAULT_MAX_PER_DAY, config.max_per_day)

    def test_values_are_read_within_bounds(self):
        config = load_refocus_config({
            "GATE_CAMERA_REFOCUS": "shadow",
            "GATE_CAMERA_REFOCUS_DAILY_WINDOW": "11:30-13:00",
            "GATE_CAMERA_REFOCUS_SHARPNESS_THRESHOLD": "0.17",
            "GATE_CAMERA_REFOCUS_MIN_FRAMES": "4",
            "GATE_CAMERA_REFOCUS_MIN_INTERVAL_HOURS": "12",
            "GATE_CAMERA_REFOCUS_MAX_PER_DAY": "2",
        })
        self.assertEqual("shadow", config.mode)
        self.assertEqual((690, 780), (config.daily_start_minute, config.daily_end_minute))
        self.assertEqual((0.17, 4, 12 * 3600, 2), (
            config.sharpness_threshold, config.min_frames,
            config.min_interval_seconds, config.max_per_day))

    def test_a_mode_typo_turns_it_off_rather_than_on(self):
        with self.assertLogs(LOGGER_NAME, level="ERROR"):
            self.assertEqual("off", load_refocus_config({"GATE_CAMERA_REFOCUS": "yes"}).mode)

    def test_unusable_values_fall_back_to_the_defaults_and_say_so(self):
        for key, value in (
            ("GATE_CAMERA_REFOCUS_SHARPNESS_THRESHOLD", "0.9"),
            ("GATE_CAMERA_REFOCUS_MIN_FRAMES", "1"),
            ("GATE_CAMERA_REFOCUS_MIN_FRAMES", "3.5"),
            ("GATE_CAMERA_REFOCUS_MAX_PER_DAY", "50"),
            ("GATE_CAMERA_REFOCUS_MIN_INTERVAL_HOURS", "nan"),
            ("GATE_CAMERA_REFOCUS_QUIET_SECONDS", "1"),
            ("GATE_CAMERA_REFOCUS_DAILY_WINDOW", "15:00-12:00"),
            ("GATE_CAMERA_REFOCUS_DAILY_WINDOW", "noon"),
            ("GATE_CAMERA_REFOCUS_TIMEZONE", "Mars/Olympus"),
        ):
            with self.subTest(key=key, value=value), self.assertLogs(LOGGER_NAME, level="ERROR"):
                self.assertEqual(load_refocus_config({}), load_refocus_config({key: value}))

    def test_every_setting_is_in_the_example_environment(self):
        example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
        for key in ("GATE_CAMERA_REFOCUS", "GATE_CAMERA_REFOCUS_DAILY_WINDOW",
                    "GATE_CAMERA_REFOCUS_TIMEZONE", "GATE_CAMERA_REFOCUS_SHARPNESS_THRESHOLD",
                    "GATE_CAMERA_REFOCUS_MIN_FRAMES", "GATE_CAMERA_REFOCUS_MIN_BRIGHTNESS",
                    "GATE_CAMERA_REFOCUS_MIN_INTERVAL_HOURS", "GATE_CAMERA_REFOCUS_MAX_PER_DAY",
                    "GATE_CAMERA_REFOCUS_QUIET_SECONDS"):
            self.assertIn(f"\n{key}=", example)


class MeasurementTests(unittest.TestCase):
    def test_a_still_in_memory_scores_exactly_as_the_same_frame_on_disk(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            path.write_bytes(SOFT)
            on_disk = measure_frame_quality(path)
        in_memory = measure_jpeg_quality(SOFT)

        self.assertEqual(on_disk, in_memory)
        self.assertEqual("quality_unavailable", measure_jpeg_quality(b"not a jpeg").status)


class EndToEndTests(unittest.TestCase):
    """Worker -> loopback HTTP -> camera-control service -> fake RLC-811A."""

    def setUp(self):
        camera = FakeCamera()
        self.camera = camera
        self.addCleanup(camera.close)
        original_snapshot = camera.snapshot

        def snapshot(token):
            # What the lens looks like at its current focus position.
            camera.snapshot_body = SOFT if camera.focus_pos == 86 else SHARP
            return original_snapshot(token)

        camera.snapshot = snapshot
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        service_clock = ManualClock()
        service = build_service(
            {
                "GATE_CAMERA_HOST": camera.host, "GATE_CAMERA_USERNAME": "gate",
                "GATE_CAMERA_PASSWORD": "s3cret", "GATE_CAMERA_IR_DEFAULT": "Off",
                "GATE_CAMERA_IR_LEASE_DEFAULT_MINUTES": "10",
                "GATE_CAMERA_IR_LEASE_MAX_MINUTES": "60",
            },
            token_path=Path(self.directory.name) / "token.json",
            lease_path=Path(self.directory.name) / "lease.json",
            spotlight_lease_path=Path(self.directory.name) / "spotlight-lease.json",
            refocus_record_path=Path(self.directory.name) / "refocus-return.json",
            logger=logging.getLogger("test.camera_control"),
            connection_factory=camera.connection_factory, clock=service_clock,
            refocus_options={"sleep": service_clock.advance},
        )
        server = CameraControlServer(("127.0.0.1", 0), service,
                                     logger=logging.getLogger("test.camera_control"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.client = CameraControlClient(port=server.server_address[1])
        monotonic = ManualClock(1000.0)
        self.activity = ActivityGate(quiet_seconds=60, clock=monotonic)
        monotonic.advance(600)
        self.worker = CameraRefocusWorker(
            RefocusConfig(), self.client,
            state_path=Path(self.directory.name) / "camera-refocus.json",
            activity=self.activity, session_active=lambda: False, ir_state=lambda: "Off",
            clock=ManualClock(dublin(5, 9)), sleep=lambda _seconds: None,
        )

    def test_soft_frames_lead_to_a_nudge_that_brings_the_picture_back(self):
        self.worker.observe_result(event("a", 0.142, 0.151))
        self.worker.observe_result(event("b", 0.158))

        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.assertEqual("completed", self.worker.run_once())

        self.assertEqual(2, self.camera.zoom_pos)
        self.assertEqual(78, self.camera.focus_pos)
        self.assertEqual(["ZoomPos", "ZoomPos"], [op["op"] for op in self.camera.zoom_ops])
        [line] = [l for l in logs.output if "stage=attempt" in l]
        for field in ("trigger=detection", "zoom_before=2", "zoom_step=3", "zoom_after=2",
                      "focus_before=86", "focus_after=78"):
            self.assertIn(field, line)
        last = self.worker.status()["last_attempt"]
        self.assertLess(last["sharpness_before"], DEFAULT_SHARPNESS_THRESHOLD)
        self.assertGreaterEqual(last["sharpness_after"], DEFAULT_SHARPNESS_THRESHOLD)

    def test_a_nudge_after_which_the_lens_is_still_soft_is_a_warning(self):
        self.camera.focus_after_zoom = {2: 86, 3: 86}
        self.worker.observe_result(event("a", 0.142, 0.151))
        self.worker.observe_result(event("b", 0.158))

        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            self.assertEqual("not_recovered", self.worker.run_once())
        self.assertIn("outcome=not_recovered", logs.output[0])
        self.assertEqual(2, self.camera.zoom_pos)

    def test_the_client_reports_the_service_refusal_by_its_code(self):
        self.assertEqual("completed", self.client.refocus("manual")["status"])
        with self.assertRaises(CameraControlError) as raised:
            self.client.refocus("manual")
        self.assertEqual("rate_limited", raised.exception.code)
        self.assertEqual(429, raised.exception.status)

    def test_a_service_that_is_not_there_is_service_unreachable(self):
        client = CameraControlClient(port=1)
        with self.assertRaises(CameraControlError) as raised:
            client.snapshot()
        self.assertEqual("service_unreachable", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
