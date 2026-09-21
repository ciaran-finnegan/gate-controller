"""The early trigger's worker: what it records, what it may start, and what it may not.

The detectors have their own file (``test_early_trigger_detector.py``) and the
sweep it can start has another (``test_early_trigger_pipeline.py``). This one
is the worker between them: configuration, the shadow record, the caps, the
confirmation layers, the correlator, and the wiring in ``main()``.
"""
import inspect
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image

import gate_controller.__main__ as gate_main
from gate_controller import early_trigger
from gate_controller.backpressure import ActivityGate
from gate_controller.early_trigger import (
    ConfirmationLayers, EarlyTriggerStore, EarlyTriggerWorker, ThumbnailStore,
    build_worker, ensure_schema, grid_from_frame, lane_square, load_config,
)
from gate_controller.trigger_capture import TriggerCaptureConfig, TriggerFrameCapture


def picture(size, nose=0, base=110, body=30):
    """A raw grey patch: a plain drive, and a vehicle ``nose`` pixels in from the left."""
    width, height = size
    image = Image.new("L", size, base)
    for x in range(0, width, 8):  # texture, so the background is not one flat grey
        for y in range(0, height, 8):
            image.putpixel((x, y), base + 25)
    if nose > 0:
        image.paste(body, (0, height // 3, min(width, nose), height - height // 6))
    return image.tobytes()


class EndingStream(BytesIO):
    """A child's stdout that ends the run when it runs dry, as a shutdown would."""

    def __init__(self, data: bytes, stop=None):
        super().__init__(data)
        self._stop = stop

    def read(self, size=-1):
        data = super().read(size)
        if not data and self._stop is not None:
            self._stop.set()
        return data


class FakeChild:
    def __init__(self, data: bytes, stop=None):
        self.stdout = EndingStream(data, stop)
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class SteppingClock:
    """Every look at the clock is a quarter of a second later: one per sample."""

    def __init__(self, step=0.25):
        self.now = 100.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class SpyCapture:
    def __init__(self, outcome="scheduled"):
        self.calls = []
        self.outcome = outcome
        self.observer = None

    def on_early_trigger(self, features=None):
        self.calls.append(features)
        return self.outcome

    def set_early_observer(self, observer):
        self.observer = observer

    def session_active(self):
        return False


def arrival(config, frames_before=80, frames_after=12):
    size = config.frame_size
    data = b"".join(picture(size) for _ in range(frames_before))
    data += b"".join(picture(size, nose=14 * (index + 1)) for index in range(frames_after))
    return data


class WorkerHarness:
    def __init__(self, test, mode, *, capture=None, environment=None, activity=None, data=None):
        self.directory = tempfile.TemporaryDirectory()
        test.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        values = {"GATE_EARLY_TRIGGER": mode}
        values.update(environment or {})
        self.config = load_config(values, self.state)
        self.store = EarlyTriggerStore(self.state / "early-trigger.db")
        self.stop = Event()
        self.spawned = []
        stream = arrival(self.config) if data is None else data

        def popen(command, **kwargs):
            self.spawned.append((command, kwargs))
            return FakeChild(stream, self.stop)

        self.clock = SteppingClock()
        self.worker = EarlyTriggerWorker(
            self.config, capture=capture, activity=activity, store=self.store,
            thumbnails=ThumbnailStore(self.state / "thumbs", max_files=5, max_days=1),
            popen=popen, clock=self.clock, wall_clock=lambda: 1_790_000_000.0 + self.clock.now,
        )

    def run(self):
        with patch.object(Event, "wait", lambda event, timeout=None: event.is_set()):
            self.worker.run_forever(self.stop)
        return self

    def rows(self):
        with closing(sqlite3.connect(str(self.state / "early-trigger.db"))) as connection:
            connection.row_factory = sqlite3.Row
            ensure_schema(connection)
            return [dict(row) for row in connection.execute(
                "SELECT * FROM early_trigger_observations ORDER BY id")]


class ConfigurationTests(unittest.TestCase):
    def test_the_codes_default_is_off_and_a_typo_is_off_too(self):
        self.assertEqual(load_config({}).mode, "off")
        self.assertFalse(load_config({}).enabled)
        with self.assertLogs("gate_controller.early_trigger", level="WARNING"):
            config = load_config({"GATE_EARLY_TRIGGER": "shadwo"})
        self.assertEqual((config.mode, config.requested_mode), ("off", "shadwo"))
        self.assertEqual(load_config({"GATE_EARLY_TRIGGER": " Shadow "}).mode, "shadow")
        self.assertEqual(load_config({"GATE_EARLY_TRIGGER": "on"}).mode, "on")

    def test_the_patch_is_frame_fractions_like_the_plate_region(self):
        config = load_config({"GATE_EARLY_TRIGGER_PATCH": "0.05,0.2,0.3,0.4"})
        self.assertEqual(config.patch.as_env(), "0.05,0.2,0.3,0.4")
        with self.assertLogs("gate_controller.early_trigger", level="WARNING"):
            bad = load_config({"GATE_EARLY_TRIGGER_PATCH": "0.9,0.2,0.5,0.4"})
        self.assertEqual(bad.patch.as_env(), early_trigger.DEFAULT_PATCH)

    def test_the_default_patch_holds_the_measured_first_appearance_and_clears_the_osd(self):
        patch_ = load_config({}).patch
        # First seen at x 0.05-0.15, y 0.30-0.45; first readable plate at (0.19, 0.42).
        self.assertLessEqual(patch_.x, 0.05)
        self.assertGreaterEqual(patch_.x + patch_.width, 0.19 + 0.10)
        self.assertLessEqual(patch_.y, 0.30)
        self.assertGreaterEqual(patch_.y + patch_.height, 0.45 + 0.10)
        self.assertGreaterEqual(patch_.y, 0.10, "the camera's clock and watermark change by themselves")

    def test_hours_use_the_farm_machinery_format_and_default_to_all_day(self):
        self.assertEqual(load_config({}).hours_text, "00:00-24:00")
        self.assertEqual(load_config({"GATE_EARLY_TRIGGER_HOURS": "07:00-19:30"}).hours_text,
                         "07:00-19:30")
        with self.assertLogs("gate_controller.early_trigger", level="WARNING"):
            self.assertEqual(load_config({"GATE_EARLY_TRIGGER_HOURS": "dawn"}).hours_text,
                             "00:00-24:00")

    def test_day_and_night_thresholds_are_separately_settable(self):
        config = load_config({
            "GATE_EARLY_TRIGGER_DAY_PERSISTENCE": "3", "GATE_EARLY_TRIGGER_NIGHT_PERSISTENCE": "6",
            "GATE_EARLY_TRIGGER_DAY_MIN_AREA": "0.1", "GATE_EARLY_TRIGGER_NIGHT_DELTA": "90",
        }).detector
        self.assertEqual((config.day_persistence, config.night_persistence), (3, 6))
        self.assertEqual((config.day_min_area, config.night_delta), (0.1, 90.0))

    def test_the_child_reads_the_sub_stream_on_one_thread_into_a_small_grey_patch(self):
        worker = EarlyTriggerWorker(load_config({"GATE_EARLY_TRIGGER": "shadow"}))
        command = " ".join(worker.command)
        self.assertIn("rtsp://127.0.0.1:8554/camera", command)
        self.assertIn("-threads 1", command)
        self.assertIn("fps=4,crop=", command)
        self.assertIn("scale=160:90:flags=area,format=gray", command)
        self.assertIn("-f rawvideo pipe:1", command)
        self.assertNotIn("hwaccel", command, "the hardware decoder belongs to the sweep")


class ShadowTests(unittest.TestCase):
    def test_shadow_records_a_would_trigger_and_starts_nothing(self):
        capture = SpyCapture()
        with self.assertLogs("gate_controller.early_trigger", level="INFO") as logs:
            harness = WorkerHarness(self, "shadow", capture=capture).run()
        self.assertEqual(capture.calls, [], "shadow reached the capture")
        rows = harness.rows()
        self.assertEqual([row["kind"] for row in rows], ["would_trigger"])
        row = rows[0]
        self.assertEqual((row["source"], row["mode"], row["action"]), ("vision", "shadow", "none"))
        self.assertEqual(row["light"], "day")
        features = json.loads(row["features"])
        self.assertGreaterEqual(features["blob_fraction"], 0.06)
        self.assertGreaterEqual(features["persistence"], 2)
        self.assertIn("luma_jump", features)
        line = next(line for line in logs.output if "stage=would_trigger" in line)
        self.assertIn("source=vision mode=shadow light=day action=none", line)
        self.assertIn("blob=", line)
        self.assertIn("persistence=", line)

    def test_shadow_cannot_act_even_through_the_real_capture(self):
        """Spied at the real entry points: nothing is queued, no session starts, nothing is injected."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sessions = []

        class Source:
            def start_session(self):
                sessions.append(1)
                return True

        class Reader:
            def available(self):
                return True

        capture = TriggerFrameCapture(
            TriggerCaptureConfig(enabled=True, output_directory=Path(directory.name),
                                 sweep_enabled=True),
            frame_source=Source(), sweep=Reader(),
        )
        injected = []
        capture.attach(lambda *args, **kwargs: injected.append(args))
        with patch.object(capture, "local_sweep") as sweep, \
                patch.object(capture, "capture_series") as series, \
                patch.object(capture, "on_early_trigger", wraps=capture.on_early_trigger) as entry:
            WorkerHarness(self, "shadow", capture=capture).run()
            self.assertEqual(entry.call_count, 0)
            # ...and had anything called it, the capture itself would have refused.
            self.assertEqual(capture.on_early_trigger(), "disabled")
        self.assertTrue(capture._queue.empty())
        sweep.assert_not_called()
        series.assert_not_called()
        self.assertEqual((sessions, injected), ([], []))

    def test_a_small_before_and_after_picture_is_kept_locally_and_bounded(self):
        harness = WorkerHarness(self, "shadow").run()
        row = harness.rows()[0]
        self.assertTrue(row["thumbnail"].endswith("-trigger.jpg"))
        with Image.open(harness.state / "thumbs" / row["thumbnail"]) as image:
            self.assertEqual((image.mode, image.size), ("L", (320, 90)))
        thumbs = ThumbnailStore(harness.state / "thumbs", max_files=5, max_days=1)
        frame = picture((160, 90))
        for index in range(12):
            thumbs.save(frame, frame, (160, 90), 1_790_000_000.0 + index, "trigger")
        self.assertLessEqual(len(list((harness.state / "thumbs").glob("*.jpg"))), 5)
        thumbs.prune(1_790_000_000.0 + 3 * 86400)
        self.assertEqual(list((harness.state / "thumbs").glob("*.jpg")), [])

    def test_nothing_is_looked_at_while_the_decoder_may_still_be_showing_rubbish(self):
        harness = WorkerHarness(self, "shadow", data=arrival(load_config({}), frames_before=4))
        harness.run()
        self.assertEqual(harness.rows(), [])

    def test_a_camera_alarm_is_recorded_with_what_the_detector_saw(self):
        harness = WorkerHarness(self, "shadow").run()

        class Alarm:
            event_type = "vehicle"

        harness.worker.note_camera_alarm(Alarm())
        rows = harness.rows()
        self.assertEqual([row["kind"] for row in rows], ["would_trigger", "camera_alarm"])
        features = json.loads(rows[1]["features"])
        self.assertEqual(features["event_type"], "vehicle")
        self.assertIn("blob_fraction", features, "a miss has to be analysable too")
        self.assertTrue(rows[1]["thumbnail"].endswith("-alarm.jpg"))

    def test_it_stands_down_while_a_sweep_or_a_burst_has_the_controller(self):
        activity = ActivityGate()
        with activity.activity("camera_event"):
            harness = WorkerHarness(self, "shadow", activity=activity)
            harness.stop.set()
            self.assertTrue(harness.worker._paused())
            harness.run()
            self.assertEqual(harness.spawned, [], "a child was started during a sweep")
        self.assertFalse(harness.worker._paused())

    def test_off_builds_nothing(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertIsNone(build_worker({}, state_directory=Path(directory.name)))
        self.assertIsNone(build_worker({"GATE_EARLY_TRIGGER": "off"},
                                       state_directory=Path(directory.name)))
        self.assertEqual(list(Path(directory.name).iterdir()), [])


class ActingTests(unittest.TestCase):
    def test_on_asks_the_capture_for_a_sweep_and_records_that_it_did(self):
        capture = SpyCapture()
        harness = WorkerHarness(self, "on", capture=capture).run()
        self.assertEqual(len(capture.calls), 1)
        self.assertIn("blob_fraction", capture.calls[0])
        row = harness.rows()[0]
        self.assertEqual((row["mode"], row["action"]), ("on", "scheduled"))
        capture.observer({"reason": "early_abort", "upgraded": False, "reads": 0})
        self.assertEqual(json.loads(harness.rows()[0]["sweep"])["reason"], "early_abort")

    def _observation(self):
        return early_trigger.Observation("trigger", True, "day", {"blob_fraction": 0.1})

    def test_the_minimum_interval_and_the_hourly_cap_with_backoff(self):
        capture = SpyCapture()
        harness = WorkerHarness(self, "on", capture=capture, environment={
            "GATE_EARLY_TRIGGER_MIN_INTERVAL_SECONDS": "20",
            "GATE_EARLY_TRIGGER_MAX_PER_HOUR": "3",
            "GATE_EARLY_TRIGGER_BACKOFF_SECONDS": "600",
        })
        worker, clock = harness.worker, harness.clock
        clock.step = 0.0
        self.assertEqual(worker._act(self._observation()), "scheduled")
        clock.now += 5
        self.assertEqual(worker._act(self._observation()), "skipped_interval")
        for _ in range(2):
            clock.now += 30
            self.assertEqual(worker._act(self._observation()), "scheduled")
        clock.now += 30
        with self.assertLogs("gate_controller.early_trigger", level="WARNING") as logs:
            self.assertEqual(worker._act(self._observation()), "skipped_backoff")
        self.assertIn("stage=backoff", logs.output[0])
        clock.now += 300
        self.assertEqual(worker._act(self._observation()), "skipped_backoff")
        self.assertEqual(len(capture.calls), 3, "nothing reaches the capture during a backoff")
        clock.now += 301
        self.assertEqual(worker._act(self._observation()), "scheduled")
        # The second backoff is twice the first.
        for _ in range(2):
            clock.now += 30
            worker._act(self._observation())
        clock.now += 30
        with self.assertLogs("gate_controller.early_trigger", level="WARNING") as logs:
            self.assertEqual(worker._act(self._observation()), "skipped_backoff")
        self.assertIn("seconds=1200", logs.output[0])
        clock.now += 900
        self.assertEqual(worker._act(self._observation()), "skipped_backoff")
        clock.now += 301
        self.assertEqual(worker._act(self._observation()), "scheduled")

    def test_a_camera_alarm_forgives_the_count_because_the_triggers_were_true(self):
        capture = SpyCapture()
        harness = WorkerHarness(self, "on", capture=capture, environment={
            "GATE_EARLY_TRIGGER_MAX_PER_HOUR": "2", "GATE_EARLY_TRIGGER_MIN_INTERVAL_SECONDS": "5",
        })
        worker, clock = harness.worker, harness.clock
        clock.step = 0.0
        for _ in range(6):
            clock.now += 30
            self.assertEqual(worker._act(self._observation()), "scheduled")
            worker.note_camera_alarm(None)

    def test_outside_its_hours_it_records_and_does_not_act(self):
        capture = SpyCapture()
        harness = WorkerHarness(self, "on", capture=capture, environment={
            "GATE_EARLY_TRIGGER_HOURS": "10:00-10:01", "GATE_EARLY_TRIGGER_TIMEZONE": "UTC",
        })
        harness.worker._wall = lambda: 1_790_000_000.0  # 2026-09-22 21:33 UTC
        harness.run()
        self.assertEqual(capture.calls, [])
        self.assertEqual(harness.rows()[0]["action"], "skipped_hours")

    def test_a_capture_that_is_busy_is_recorded_as_busy_and_not_counted_against_the_cap(self):
        capture = SpyCapture(outcome="skipped_busy")
        harness = WorkerHarness(self, "on", capture=capture).run()
        self.assertEqual(harness.rows()[0]["action"], "skipped_busy")
        self.assertEqual(len(harness.worker._unconfirmed), 0)


class LayerTests(unittest.TestCase):
    def _layers(self, **options):
        options.setdefault("sleep", lambda seconds: None)
        return ConfirmationLayers(**options)

    def test_the_clip_look_and_the_plate_look_are_timed_and_recorded(self):
        class Read:
            status, recognised, score, authorised = "recognized", True, 0.91, False

        layers = self._layers(
            clip_look=lambda jpeg: {"status": "ok", "top": "car", "empty": 0.02,
                                    "shares": {"car": 0.9, "empty": 0.02}},
            lane_frame=lambda: b"jpeg", plate_frame=lambda: b"jpeg", plate_read=lambda frame: Read(),
        )
        answer = layers.evaluate()
        self.assertEqual(answer["clip"]["top"], "car")
        self.assertIn("ms", answer["clip"])
        self.assertTrue(answer["plate_look"]["plate_box"])
        self.assertEqual(len(answer["plate_look"]["reads"]), 3)
        self.assertIn("ms", answer["plate_look"])

    def test_every_layer_is_skipped_and_says_so_while_a_vehicle_has_the_controller(self):
        done = []
        layers = self._layers(clip_look=lambda jpeg: self.fail("looked while busy"),
                              lane_frame=lambda: b"jpeg", busy=lambda: "camera_event")
        self.assertFalse(layers.begin(done.append))
        self.assertEqual(done, [{"clip": {"status": "skipped_busy"},
                                 "plate_look": {"status": "skipped_busy"}}])

    def test_the_plate_look_lets_go_of_the_reader_the_moment_a_sweep_starts(self):
        state = {"busy": None}

        def read(frame):
            state["busy"] = "camera_event"
            return type("Read", (), {"status": "no_plate", "recognised": False, "score": 0.0,
                                     "authorised": False})()

        layers = self._layers(plate_frame=lambda: b"jpeg", plate_read=read,
                              busy=lambda: state["busy"])
        answer = layers.evaluate()["plate_look"]
        self.assertEqual(answer["status"], "skipped_busy")
        self.assertEqual(len(answer["reads"]), 1)

    def test_the_looks_are_capped_a_minute(self):
        done = []
        ticks = iter(range(100, 200))
        layers = self._layers(per_minute=2, clock=lambda: next(ticks))
        with patch.object(early_trigger, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: layers._running.release()
            self.assertTrue(layers.begin(done.append))
            self.assertTrue(layers.begin(done.append))
            self.assertFalse(layers.begin(done.append))
        self.assertEqual(done[-1]["clip"]["status"], "skipped_rate")

    def test_without_a_model_or_a_reader_the_layers_say_unavailable(self):
        self.assertEqual(self._layers().evaluate(), {"clip": {"status": "unavailable"},
                                                     "plate_look": {"status": "unavailable"}})

    def test_the_image_tower_is_shown_a_square_around_the_lane_not_the_centre_of_the_frame(self):
        patch_ = load_config({}).patch
        x, y, width, height = lane_square(patch_)
        self.assertAlmostEqual(width * 16, height * 9, places=2, msg="square in pixels")
        self.assertLessEqual(x, patch_.x)
        self.assertGreaterEqual(x + width, patch_.x + patch_.width - 1e-6)
        self.assertLessEqual(y, patch_.y)
        self.assertGreaterEqual(y + height, patch_.y + patch_.height - 1e-6)
        self.assertLess(x + width, 0.5, "the far lane, not the gate in the middle of the picture")

    def test_the_farm_machinery_policy_answers_a_look_without_touching_its_own_rate_or_memory(self):
        from gate_controller.agricultural import FarmMachineryPolicy, PromptScorer

        scorer = PromptScorer()
        embedding = [0.0] * len(scorer._text[0])
        embedding[0] = 1.0
        policy = FarmMachineryPolicy("shadow", lambda jpeg: embedding, scorer)
        answer = policy.look(b"jpeg")
        self.assertEqual(answer["status"], "ok")
        self.assertAlmostEqual(sum(answer["shares"].values()), 1.0, places=2)
        self.assertIn("empty", answer["shares"])
        self.assertEqual(len(policy._begun), 0)
        self.assertEqual(len(policy._clear_frames), 0)
        policy._running.acquire()
        self.assertEqual(policy.look(b"jpeg")["status"], "skipped_busy")
        unavailable = FarmMachineryPolicy("shadow", None, scorer, unavailable_reason="no model")
        self.assertEqual(unavailable.look(b"jpeg")["status"], "unavailable")


class CorrelatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EarlyTriggerStore(Path(self.directory.name) / "early-trigger.db")

    def _rows(self):
        with closing(sqlite3.connect(str(self.store.path))) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM early_trigger_observations ORDER BY at_epoch")]

    def _record(self, kind, at):
        source = "vision" if kind == "would_trigger" else "camera"
        return self.store.record(kind=kind, source=source, at_epoch=at, mode="shadow", light="day")

    def test_true_false_led_and_missed(self):
        base = 1_790_000_000.0
        self._record("would_trigger", base)            # true, 1.8 s ahead
        self._record("camera_alarm", base + 1.8)       # led
        self._record("would_trigger", base + 500)      # false: no alarm within a minute
        self._record("camera_alarm", base + 1000)      # missed: nothing before it
        self._record("would_trigger", base + 2000)     # true, but 20 s ahead
        self._record("camera_alarm", base + 2020)      # late_lead: too early to have helped
        self.assertEqual(self.store.correlate(base + 2100), 6)
        rows = self._rows()
        self.assertEqual([row["verdict"] for row in rows],
                         ["true", "led", "false", "missed", "true", "late_lead"])
        self.assertAlmostEqual(rows[0]["lead_seconds"], 1.8)
        self.assertAlmostEqual(rows[1]["lead_seconds"], 1.8)
        self.assertIsNone(rows[2]["lead_seconds"])
        self.assertIsNone(rows[2]["camera_alarm_at"])
        self.assertEqual(rows[0]["matched_id"], rows[1]["id"])

    def test_a_row_is_left_pending_until_its_window_has_closed(self):
        base = 1_790_000_000.0
        self._record("would_trigger", base)
        self.assertEqual(self.store.correlate(base + 30), 0)
        self.assertEqual(self._rows()[0]["verdict"], "pending")

    def test_the_first_good_local_read_of_the_passage_is_read_from_the_controllers_database(self):
        base = 1_790_000_000.0
        events = Path(self.directory.name) / "gate.db"
        with closing(sqlite3.connect(str(events))) as connection, connection:
            connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, received_at TEXT)")
            connection.execute(
                "CREATE TABLE event_telemetry (event_id INTEGER PRIMARY KEY, payload TEXT)")
            for event_id, offset, score in ((1, 2.5, 0.41), (2, 3.4, 0.93), (3, 4.0, 0.99)):
                connection.execute("INSERT INTO events VALUES (?, ?)",
                                   (event_id, early_trigger._iso(base + offset)))
                connection.execute("INSERT INTO event_telemetry VALUES (?, ?)", (event_id, json.dumps({
                    "local_ocr": {"status": "recognized", "plate": "10CE1990", "score": score}})))
        store = EarlyTriggerStore(self.store.path, events)
        store.record(kind="would_trigger", source="vision", at_epoch=base, mode="shadow")
        store.record(kind="camera_alarm", source="camera", at_epoch=base + 2.0, mode="shadow")
        store.correlate(base + 200)
        row = self._rows()[0]
        self.assertEqual((row["first_read_event_id"], row["first_read_score"]), (2, 0.93))

    def test_the_record_is_its_own_file_beside_the_controllers_database(self):
        directory = Path(self.directory.name)
        worker = build_worker({"GATE_EARLY_TRIGGER": "shadow"}, state_directory=directory,
                              events_database=directory / "gate-controller.db")
        self.assertEqual(worker._store.path, directory / "early-trigger.db")
        self.assertNotEqual(worker._store.path, worker._store.events_database)


class GridTests(unittest.TestCase):
    def test_a_grey_picture_is_averaged_down_to_the_grid(self):
        cells = grid_from_frame(picture((160, 90), nose=80, base=200, body=10), (160, 90))
        self.assertEqual(len(cells), 24 * 16)
        self.assertLess(cells[8 * 24 + 2], 40)
        self.assertGreater(cells[8 * 24 + 20], 180)


class MainWiringTests(unittest.TestCase):
    """The real ``main()``, with the boundaries patched as the other wiring tests patch them."""

    def _run_main(self, setting):
        from tests.test_main import no_live_state_access

        state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(state_directory.cleanup)
        state = Path(state_directory.name)
        environment = {
            "PLATE_RECOGNIZER_API_TOKEN": "token",
            "GATE_DATABASE": str(state / "gate-controller.db"),
            "GATE_AUTHORISED_PLATES": str(state / "authorised_licence_plates.csv"),
            "GATE_WATCH_DIRECTORY": str(state / "uploads"),
            "GATE_MODEL_DIR": str(state / "no-models-here"),
            # The capture exists only where the camera's webhook does.
            "GATE_REOLINK_WEBHOOK_SECRET": "correct-horse-battery-staple",
        }
        if setting is not None:
            environment["GATE_EARLY_TRIGGER"] = setting
        with patch.dict(os.environ, environment, clear=True), patch("sys.argv", [
            "gate-controller",
        ]), patch.object(gate_main, "require_python_version"), patch.object(
            gate_main, "PiRelayAdapter", return_value=object(),
        ), patch.object(gate_main, "RelayController"), patch.object(
            gate_main, "LocalStore",
        ), patch.object(gate_main, "AuthorisedPlateCache"), patch.object(
            gate_main, "build_background_workers", return_value=((), object(), object()),
        ), patch.object(gate_main, "PlateRecognizerClient"), patch.object(
            gate_main, "GateProcessor",
        ), patch.object(
            gate_main, "build_reolink_trigger_pipeline", wraps=gate_main.build_reolink_trigger_pipeline,
        ) as pipeline, patch.object(gate_main, "run_worker") as run, patch.object(
            logging, "basicConfig",
        ), no_live_state_access():
            logging.disable(logging.CRITICAL)
            try:
                gate_main.main()
            finally:
                logging.disable(logging.NOTSET)
        workers = run.call_args.kwargs["background_workers"]
        capture = run.call_args.kwargs["trigger_capture"]
        return workers, capture, pipeline.call_args.kwargs["on_accepted"], run.call_args.kwargs

    def test_shadow_and_on_run_the_worker_and_off_and_a_typo_do_not(self):
        for setting, expected in (("shadow", "shadow"), ("on", "on"), (None, None),
                                  ("off", None), ("yes", None)):
            workers, _capture, _handler, _kwargs = self._run_main(setting)
            early = [worker for worker in workers if isinstance(worker, EarlyTriggerWorker)]
            self.assertEqual([worker.config.mode for worker in early],
                             [] if expected is None else [expected], setting)

    def test_only_on_lets_the_capture_accept_an_early_trigger(self):
        for setting, accepts in (("shadow", False), ("on", True), (None, False)):
            _workers, capture, _handler, _kwargs = self._run_main(setting)
            self.assertIsNotNone(capture)
            self.assertEqual(capture._early_enabled, accepts, setting)
            self.assertEqual(capture.on_early_trigger() == "disabled", not accepts, setting)

    def test_the_worker_is_handed_the_production_capture_and_hears_the_cameras_alarms(self):
        workers, capture, handler, _kwargs = self._run_main("shadow")
        early = next(worker for worker in workers if isinstance(worker, EarlyTriggerWorker))
        self.assertIsNotNone(capture)
        self.assertIs(early._capture, capture)
        event = type("Event", (), {"event_type": "vehicle", "rule_id": "r", "event_at": None})()
        self.assertEqual(handler(event), "scheduled")
        with closing(sqlite3.connect(str(early._store.path))) as connection:
            rows = connection.execute(
                "SELECT kind, source, mode FROM early_trigger_observations").fetchall()
        self.assertEqual(rows, [("camera_alarm", "camera", "shadow")])

    def test_the_early_trigger_can_never_change_what_the_capture_answers_the_webhook(self):
        event = type("Event", (), {"event_type": "manual_test", "rule_id": "r", "event_at": None})()
        with patch.object(EarlyTriggerWorker, "note_camera_alarm", side_effect=RuntimeError("boom")):
            _workers, _capture, handler, _kwargs = self._run_main("shadow")
            self.assertEqual(handler(event), "skipped_type")

    def test_the_prepare_main_hands_the_worker_carries_the_permit_to_the_processor(self):
        _workers, _capture, _handler, kwargs = self._run_main("on")
        self.assertIn("cloud_permit", inspect.signature(kwargs["prepare"]).parameters)


if __name__ == "__main__":
    unittest.main()
