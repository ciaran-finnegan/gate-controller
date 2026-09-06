import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Event

from PIL import Image

from gate_controller.reolink_events import SanitizedCameraEvent
from gate_controller.models import MatchDecision, ProcessingResult
from gate_controller.trigger_capture import (
    TriggerCaptureConfig,
    TriggerFrameCapture,
    load_trigger_capture_config,
)


def jpeg(size=(64, 32)):
    output = BytesIO()
    Image.new("RGB", size, color="blue").save(output, format="JPEG")
    return output.getvalue()


class FakeProcess:
    """Stand-in child whose stdout is a real pipe, so bounded reads and
    timeouts exercise the same select/os.read path as ffmpeg."""

    def __init__(self, output=b"", returncode=0, hang=False):
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.returncode = None
        self._exit_code = returncode
        self.killed = False
        if not hang:
            os.write(self._write_fd, output)
            self._close_writer()
            self.returncode = returncode

    def _close_writer(self):
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._close_writer()

    terminate = kill


class FakePopen:
    def __init__(self, processes):
        self._processes = list(processes)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        return self._processes.pop(0)


def event(event_type="vehicle", rule_id="front_gate"):
    return SanitizedCameraEvent(
        event_id="event-1", event_type=event_type, rule_id=rule_id,
        received_at=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
        event_at=datetime(2026, 9, 4, 9, 59, 59, tzinfo=timezone.utc),
    )


class TriggerCaptureConfigTests(unittest.TestCase):
    def test_defaults_on_when_the_webhook_is_enabled(self):
        config = load_trigger_capture_config({}, Path("/uploads"), webhook_enabled=True)

        self.assertTrue(config.enabled)
        self.assertEqual(config.source_url, "rtsp://127.0.0.1:8554/clear")
        self.assertEqual(config.output_directory, Path("/uploads/trigger-capture"))
        self.assertEqual(config.timeout_seconds, 2.5)
        self.assertEqual(config.min_interval_seconds, 5.0)
        self.assertEqual(config.delay_seconds, 1.5)
        self.assertEqual(config.capture_count, 3)
        self.assertEqual(config.spacing_seconds, 1.0)
        self.assertEqual(config.hwaccel, "")
        self.assertEqual(config.frame_width, 0)

    def test_hardware_decode_and_frame_width_are_opt_in_and_validated(self):
        config = load_trigger_capture_config(
            {"GATE_TRIGGER_CAPTURE_HWACCEL": " DRM ", "GATE_TRIGGER_CAPTURE_FRAME_WIDTH": "1920"},
            Path("/uploads"), webhook_enabled=True,
        )
        self.assertEqual(config.hwaccel, "drm")
        self.assertEqual(config.frame_width, 1920)

        for environment in (
            {"GATE_TRIGGER_CAPTURE_HWACCEL": "vaapi"},
            {"GATE_TRIGGER_CAPTURE_FRAME_WIDTH": "320"},
            {"GATE_TRIGGER_CAPTURE_FRAME_WIDTH": "7680"},
            {"GATE_TRIGGER_CAPTURE_FRAME_WIDTH": "wide"},
        ):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_trigger_capture_config(environment, Path("/uploads"), webhook_enabled=True)

    def test_presence_session_is_on_by_default_and_bounded(self):
        config = load_trigger_capture_config({}, Path("/uploads"), webhook_enabled=True)
        self.assertEqual(config.presence_window_seconds, 20.0)
        self.assertEqual(config.presence_spacing_seconds, 3.0)
        self.assertEqual(config.presence_max_frames, 4)

        tuned = load_trigger_capture_config(
            {"GATE_PRESENCE_WINDOW_SECONDS": "45", "GATE_PRESENCE_SPACING_SECONDS": "2",
             "GATE_PRESENCE_MAX_FRAMES": "0"},
            Path("/uploads"), webhook_enabled=True,
        )
        self.assertEqual((tuned.presence_window_seconds, tuned.presence_spacing_seconds,
                          tuned.presence_max_frames), (45.0, 2.0, 0))
        for environment in ({"GATE_PRESENCE_WINDOW_SECONDS": "500"},
                            {"GATE_PRESENCE_SPACING_SECONDS": "0.2"},
                            {"GATE_PRESENCE_MAX_FRAMES": "50"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_trigger_capture_config(environment, Path("/u"), webhook_enabled=True)

    def test_the_verdict_guard_follows_the_processor_decision_timeout(self):
        config = load_trigger_capture_config({}, Path("/uploads"), webhook_enabled=True)
        self.assertEqual(config.decision_timeout_seconds, 4.0)
        tuned = load_trigger_capture_config(
            {"GATE_DECISION_TIMEOUT_SECONDS": "6"}, Path("/uploads"), webhook_enabled=True,
        )
        self.assertEqual(tuned.decision_timeout_seconds, 6.0)
        # The processor owns this setting: anything it tolerates is clamped
        # here rather than stopping capture from starting at all.
        for value, expected in (("900", 30.0), ("0", 0.5), ("soon", 4.0)):
            with self.subTest(value=value):
                clamped = load_trigger_capture_config(
                    {"GATE_DECISION_TIMEOUT_SECONDS": value}, Path("/u"), webhook_enabled=True,
                )
                self.assertEqual(clamped.decision_timeout_seconds, expected)

    def test_frame_gates_default_to_empty_scene_only_and_are_bounded(self):
        config = load_trigger_capture_config({}, Path("/uploads"), webhook_enabled=True)
        self.assertEqual(config.empty_scene_threshold, 0.03)
        self.assertEqual(config.max_highlight_clipping, 0.0)
        tuned = load_trigger_capture_config(
            {"GATE_EMPTY_SCENE_THRESHOLD": "0", "GATE_MAX_HIGHLIGHT_CLIPPING": "0.35"},
            Path("/uploads"), webhook_enabled=True,
        )
        self.assertEqual((tuned.empty_scene_threshold, tuned.max_highlight_clipping), (0.0, 0.35))
        for environment in ({"GATE_EMPTY_SCENE_THRESHOLD": "0.9"}, {"GATE_MAX_HIGHLIGHT_CLIPPING": "1.5"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_trigger_capture_config(environment, Path("/u"), webhook_enabled=True)

    def test_clear_stream_defaults_to_compressed_and_validates_session_settings(self):
        config = load_trigger_capture_config({}, Path("/uploads"), webhook_enabled=True)
        self.assertEqual((config.clear_stream_mode, config.session_fps, config.session_seconds), ("compressed", 5.0, 45.0))
        decoded = load_trigger_capture_config(
            {"GATE_CLEAR_STREAM_MODE": "decoded", "GATE_SESSION_FPS": "2", "GATE_SESSION_SECONDS": "30"},
            Path("/uploads"), webhook_enabled=True,
        )
        self.assertEqual((decoded.clear_stream_mode, decoded.session_fps, decoded.session_seconds), ("decoded", 2.0, 30.0))
        for environment in ({"GATE_CLEAR_STREAM_MODE": "raw"}, {"GATE_SESSION_FPS": "30"}, {"GATE_SESSION_SECONDS": "1"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_trigger_capture_config(environment, Path("/u"), webhook_enabled=True)

    def test_output_directory_lives_in_the_state_root_not_the_upload_tree(self):
        config = load_trigger_capture_config(
            {}, Path("/var/lib/gate-controller"), webhook_enabled=True,
        )
        overridden = load_trigger_capture_config(
            {"GATE_TRIGGER_CAPTURE_DIRECTORY": "/var/lib/gate-controller/captures"},
            Path("/var/lib/gate-controller"), webhook_enabled=True,
        )

        self.assertEqual(config.output_directory, Path("/var/lib/gate-controller/trigger-capture"))
        self.assertEqual(overridden.output_directory, Path("/var/lib/gate-controller/captures"))
        with self.assertRaises(ValueError):
            load_trigger_capture_config(
                {"GATE_TRIGGER_CAPTURE_DIRECTORY": "relative/captures"},
                Path("/var/lib/gate-controller"), webhook_enabled=True,
            )

    def test_stays_off_without_the_webhook_or_when_disabled(self):
        self.assertFalse(load_trigger_capture_config({}, Path("/u"), webhook_enabled=False).enabled)
        self.assertFalse(load_trigger_capture_config(
            {"GATE_TRIGGER_CAPTURE_ENABLED": "false"}, Path("/u"), webhook_enabled=True,
        ).enabled)

    def test_rejects_non_loopback_sources_and_unsafe_bounds(self):
        for environment in (
            {"GATE_TRIGGER_CAPTURE_SOURCE": "rtsp://192.168.0.54/h264Preview_01_main"},
            {"GATE_TRIGGER_CAPTURE_SOURCE": "rtsp://user:secret@127.0.0.1:8554/clear"},
            {"GATE_TRIGGER_CAPTURE_SOURCE": "http://127.0.0.1:8554/clear"},
            {"GATE_TRIGGER_CAPTURE_TIMEOUT_SECONDS": "30"},
            {"GATE_TRIGGER_CAPTURE_MIN_INTERVAL_SECONDS": "0"},
            {"GATE_TRIGGER_CAPTURE_ENABLED": "yes"},
            {"GATE_TRIGGER_CAPTURE_DELAY_SECONDS": "6"},
            {"GATE_TRIGGER_CAPTURE_COUNT": "4"},
            {"GATE_TRIGGER_CAPTURE_SPACING_SECONDS": "0.1"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    load_trigger_capture_config(environment, Path("/u"), webhook_enabled=True)


class TriggerFrameCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
        )
        self.injected = []

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, processes, clock=None):
        popen = FakePopen(processes)
        capture = TriggerFrameCapture(
            self.config, popen=popen,
            clock=clock or (lambda: 100.0),
            wall_clock=lambda: datetime(2026, 9, 4, 10, 0, 1, tzinfo=timezone.utc),
        )
        capture.attach(lambda paths, received_at, trigger: self.injected.append(
            (paths, received_at, trigger)
        ))
        return capture, popen

    def test_captured_frame_is_injected_with_a_sanitized_matched_trigger(self):
        capture, popen = self.capture([FakeProcess(output=jpeg())])

        paths = capture.capture_once(event(), scheduled_at=99.75)

        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].parent, self.config.output_directory)
        self.assertEqual(paths[0].read_bytes(), jpeg())
        self.assertEqual(oct(paths[0].stat().st_mode & 0o777), oct(0o600))
        (injected_paths, received_at, trigger), = self.injected
        self.assertEqual(injected_paths, paths)
        self.assertEqual(received_at, datetime(2026, 9, 4, 10, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(trigger.to_wire(), {
            "source": "reolink_webhook",
            "event_type": "vehicle",
            "rule_id": "front_gate",
            "correlation": "matched",
            "event_at": "2026-09-04T09:59:59+00:00",
            "delta_ms": 250,
        })
        command, kwargs = popen.calls[0]
        self.assertIn("rtsp://127.0.0.1:8554/clear", command)
        self.assertIn("-frames:v", command)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("secret", " ".join(command))

    def test_timeout_kills_the_child_and_injects_nothing(self):
        process = FakeProcess(hang=True)
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            timeout_seconds=0.5,
        )
        capture = TriggerFrameCapture(config, popen=FakePopen([process]))
        capture.attach(lambda *args: self.injected.append(args))

        self.assertEqual(capture.capture_once(event()), ())
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(self.injected, [])
        self.assertEqual(capture.status()["failures"], 1)

    def test_close_before_publication_kills_the_child_immediately(self):
        process = FakeProcess(hang=True)
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            timeout_seconds=4.0,
        )
        capture = TriggerFrameCapture(config, popen=FakePopen([process]))
        capture.attach(lambda *args: self.injected.append(args))
        capture.close()

        started = time.monotonic()
        self.assertEqual(capture.capture_once(event()), ())

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(self.injected, [])

    def test_close_kills_and_reaps_a_child_still_running_at_shutdown(self):
        process = FakeProcess(hang=True)
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            timeout_seconds=4.0,
        )
        capture = TriggerFrameCapture(config, popen=FakePopen([process]))
        capture._process = process

        capture.close()

        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertIsNone(capture._process)
        capture.close()

    def test_invalid_output_or_failed_exit_injects_nothing(self):
        for process in (
            FakeProcess(output=b"not a jpeg"),
            FakeProcess(output=jpeg(), returncode=1),
            FakeProcess(output=b""),
            FakeProcess(output=b"\xff\xd8\xff" + b"x" * 10),
        ):
            with self.subTest(process=process):
                capture, _popen = self.capture([process])
                self.assertEqual(capture.capture_once(event()), ())
        self.assertEqual(self.injected, [])
        self.assertEqual(sorted(self.config.output_directory.glob("*")), [])

    def test_oversized_output_is_cut_off_at_the_cap_and_the_child_killed(self):
        from unittest.mock import patch
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            max_frame_bytes=64,
        )
        process = FakeProcess(output=jpeg() + b"x" * 4096)
        capture = TriggerFrameCapture(config, popen=FakePopen([process]))
        capture.attach(lambda *args: self.injected.append(args))
        requested = []
        real_read = os.read

        def counting_read(descriptor, size):
            requested.append(size)
            return real_read(descriptor, size)

        with patch("gate_controller.trigger_capture.os.read", counting_read):
            self.assertEqual(capture.capture_once(event()), ())

        self.assertEqual(self.injected, [])
        self.assertTrue(process.stdout.closed)
        self.assertLessEqual(max(requested), 65)

    def test_scheduling_is_serial_rate_limited_and_skips_manual_tests(self):
        now = [100.0]
        capture, _popen = self.capture([], clock=lambda: now[0])

        self.assertEqual(capture.on_camera_event(event("manual_test")), "skipped_type")
        self.assertEqual(capture.on_camera_event(event()), "scheduled")
        self.assertEqual(capture.on_camera_event(event()), "skipped_interval")
        now[0] += 5
        self.assertEqual(capture.on_camera_event(event("line_crossing")), "skipped_busy")

    def test_disabled_capture_ignores_events_and_idles(self):
        config = TriggerCaptureConfig(enabled=False, output_directory=self.root / ".x")
        capture = TriggerFrameCapture(config, popen=FakePopen([]))
        stop = Event()
        stop.set()

        self.assertEqual(capture.on_camera_event(event()), "disabled")
        capture.run_forever(stop)
        self.assertFalse((self.root / ".x").exists())

    def test_series_waits_for_the_stop_then_grabs_spaced_frames(self):
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            delay_seconds=1.5, capture_count=3, spacing_seconds=0.75,
        )
        popen = FakePopen([FakeProcess(output=jpeg()) for _ in range(3)])
        capture = TriggerFrameCapture(config, popen=popen, clock=lambda: 10.0)
        capture.attach(lambda paths, received_at, trigger: self.injected.append(paths))
        pauses = []

        class Stop:
            def is_set(self):
                return False

            def wait(self, seconds):
                pauses.append(seconds)
                return False

        injected = capture.capture_series(event(), 9.0, Stop())

        self.assertEqual(injected, 3)
        self.assertEqual(pauses, [1.5, 0.75, 0.75])
        self.assertEqual(len(popen.calls), 3)
        self.assertEqual(len(self.injected), 3)

    def test_series_stops_promptly_when_the_service_is_stopping(self):
        popen = FakePopen([FakeProcess(output=jpeg())])
        capture = TriggerFrameCapture(self.config, popen=popen)
        capture.attach(lambda *args: self.injected.append(args))
        stop = Event()
        stop.set()

        self.assertEqual(capture.capture_series(event(), 0.0, stop), 0)
        self.assertEqual(popen.calls, [])

    def test_series_continues_past_one_failed_grab(self):
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            delay_seconds=0, capture_count=2, spacing_seconds=0.5,
        )
        popen = FakePopen([FakeProcess(hang=True), FakeProcess(output=jpeg())])
        capture = TriggerFrameCapture(config, popen=popen)
        capture.attach(lambda paths, received_at, trigger: self.injected.append(paths))

        class Stop:
            def is_set(self):
                return False

            def wait(self, _seconds):
                return False

        self.assertEqual(capture.capture_series(event(), 0.0, Stop()), 1)
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(capture.status()["failures"], 1)

    def _presence_capture(self, frames, *, verdict, clock, max_frames=4, window=20.0):
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            presence_window_seconds=window, presence_spacing_seconds=0.5,
            presence_max_frames=max_frames,
        )
        popen = FakePopen([FakeProcess(output=jpeg()) for _ in range(frames)])
        capture = TriggerFrameCapture(config, popen=popen, clock=lambda: clock[0])
        results = []

        def inject(paths, received_at, trigger):
            self.injected.append(paths)
            result = verdict(len(self.injected))
            results.append(result)
            if result is not None:
                capture.note_result(paths, result)

        capture.attach(inject)
        return capture, popen

    class _Stop:
        def __init__(self, clock, step=0.0):
            self.clock, self.step, self.pauses = clock, step, []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.pauses.append(seconds)
            self.clock[0] += self.step or seconds
            return False

    def test_presence_session_ends_the_moment_the_gate_opens(self):
        clock = [100.0]
        capture, popen = self._presence_capture(
            5, clock=clock,
            verdict=lambda n: ProcessingResult(n >= 4, "exact_match" if n >= 4 else "ocr_error"),
        )
        capture.capture_series(event(), 100.0, self._Stop(clock))

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, self._Stop(clock))

        self.assertEqual(extra, 1)
        self.assertEqual(len(popen.calls), 4, "three series frames plus one presence frame")
        combined = "\n".join(logs.output)
        self.assertIn("outcome=presence_retry frame=1", combined)
        self.assertIn("outcome=presence_ended reason=opened extra_frames=1", combined)
        self.assertEqual(capture.status()["presence"]["retries"], 1)

    def test_presence_session_stops_when_a_plate_was_read_even_if_denied(self):
        clock = [100.0]
        read = ProcessingResult(False, "no_match", decision=MatchDecision(
            False, "no_match", observed_plate="99X9999", confidence=0.91,
        ))
        capture, popen = self._presence_capture(
            5, clock=clock, verdict=lambda n: ProcessingResult(False, "ocr_error") if n < 4 else read,
        )
        capture.capture_series(event(), 100.0, self._Stop(clock))

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, self._Stop(clock))

        self.assertEqual(extra, 1)
        self.assertIn("presence_ended reason=plate_read", "\n".join(logs.output))

    def test_presence_session_spends_at_most_the_frame_budget_then_the_window(self):
        clock = [100.0]
        capture, popen = self._presence_capture(
            9, clock=clock, max_frames=3, verdict=lambda n: ProcessingResult(False, "decision_timeout"),
        )
        capture.capture_series(event(), 100.0, self._Stop(clock))
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            self.assertEqual(capture.presence_session(event(), 100.0, self._Stop(clock)), 3)
        self.assertIn("presence_ended reason=budget extra_frames=3", "\n".join(logs.output))

        clock[0] = 200.0
        capture, popen = self._presence_capture(
            8, clock=clock, window=10.0, verdict=lambda n: ProcessingResult(False, "decision_timeout"),
        )
        capture.capture_series(event(), 200.0, self._Stop(clock))
        # The series pauses take the clock to 203.5; each presence pause then
        # advances it 3 s, so a 10 s window fits two frames before 210.
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            windowed = capture.presence_session(event(), 200.0, self._Stop(clock, step=3.0))
        self.assertEqual(windowed, 2)
        self.assertIn("presence_ended reason=window", "\n".join(logs.output))

    def test_presence_session_keeps_one_frame_outstanding_and_yields_to_a_new_event(self):
        clock = [100.0]
        # Verdicts never arrive: the session must not stack frames into the queue.
        capture, popen = self._presence_capture(6, clock=clock, verdict=lambda n: None)
        capture.capture_series(event(), 100.0, self._Stop(clock))

        class TickingStop(self._Stop):
            def is_set(self):
                clock[0] += 5.0  # each poll of the pending verdict ages the clock
                return False

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, TickingStop(clock))
        self.assertEqual(extra, 0, "the series frames were still pending, so nothing more was sent")
        self.assertIn("presence_ended reason=window", "\n".join(logs.output))

        clock[0] = 300.0
        capture.capture_series(event(), 300.0, self._Stop(clock))
        for paths in self.injected[-3:]:
            capture.note_result(paths, ProcessingResult(False, "ocr_error"))
        capture.on_camera_event(event())
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            self.assertEqual(capture.presence_session(event(), 300.0, self._Stop(clock)), 0)
        self.assertIn("presence_ended reason=new_event", "\n".join(logs.output))

    def test_a_session_frame_coalesced_out_of_the_queue_does_not_stall_the_window(self):
        # The frame was pushed out of the bounded burst queue by an FTP upload
        # landing at the same moment, so no verdict was ever produced for it.
        # Before note_dropped() the session waited out the whole window on a
        # pending count that could never come down, with the car at the gate.
        clock = [100.0]
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            capture_count=1, delay_seconds=0, presence_spacing_seconds=0.5,
            presence_max_frames=2,
        )
        popen = FakePopen([FakeProcess(output=jpeg()) for _ in range(3)])
        capture = TriggerFrameCapture(config, popen=popen, clock=lambda: clock[0])

        def inject(paths, received_at, trigger):
            self.injected.append(paths)
            if len(self.injected) == 2:
                capture.note_dropped(paths, "queue_coalesced")
            else:
                capture.note_result(paths, ProcessingResult(False, "ocr_error"))

        capture.attach(inject)
        capture.capture_series(event(), 100.0, self._Stop(clock))

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, self._Stop(clock))

        self.assertEqual(extra, 2, "the dropped frame must not end the session")
        combined = "\n".join(logs.output)
        self.assertIn("gate_presence stage=frame_dropped reason=queue_coalesced", combined)
        self.assertIn("outcome=presence_retry frame=2", combined)
        self.assertIn("outcome=presence_ended reason=budget", combined)
        self.assertNotIn("stage=verdict_overdue", combined, "no waiting on the lost frame")
        self.assertEqual(capture.status()["presence"]["dropped_frames"], 1)

    def test_a_verdict_that_never_arrives_is_written_off_once_and_warned_about(self):
        clock = [100.0]
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            capture_count=1, delay_seconds=0, presence_spacing_seconds=0.5,
            presence_max_frames=2, presence_window_seconds=60.0,
        )
        popen = FakePopen([FakeProcess(output=jpeg()) for _ in range(3)])
        capture = TriggerFrameCapture(config, popen=popen, clock=lambda: clock[0])
        # No frame is ever decided and nothing reports the loss either.
        capture.attach(lambda paths, received_at, trigger: self.injected.append(paths))

        class TickingStop(self._Stop):
            def is_set(self):
                clock[0] += 5.0  # each poll of the pending verdict ages the clock
                return False

        capture.capture_series(event(), 100.0, TickingStop(clock))
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, TickingStop(clock))

        # Three decision timeouts plus the relay pulse allowance is 17 s from
        # the injection; the loop gives up on that verdict and carries on.
        self.assertEqual(extra, 2)
        combined = "\n".join(logs.output)
        self.assertIn(
            "WARNING:gate_controller.trigger_capture:"
            "gate_presence stage=verdict_overdue pending=1", combined,
        )
        self.assertEqual(combined.count("stage=verdict_overdue"), 1, "warned once per session")
        self.assertGreaterEqual(capture.status()["presence"]["lost_verdicts"], 1)

    def test_a_verdict_that_arrives_after_the_write_off_still_settles_the_session(self):
        clock = [100.0]
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            capture_count=1, delay_seconds=0, presence_spacing_seconds=0.5,
            presence_max_frames=4, presence_window_seconds=120.0,
        )
        popen = FakePopen([FakeProcess(output=jpeg()) for _ in range(3)])
        capture = TriggerFrameCapture(config, popen=popen, clock=lambda: clock[0])

        def inject(paths, received_at, trigger):
            self.injected.append(paths)
            if len(self.injected) == 2:
                # The verdict for the written-off series frame lands at last,
                # and it opened the gate: that must still end the session.
                capture.note_result(self.injected[0], ProcessingResult(True, "exact_match"))

        capture.attach(inject)

        class TickingStop(self._Stop):
            def is_set(self):
                clock[0] += 5.0
                return False

        capture.capture_series(event(), 100.0, TickingStop(clock))
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, TickingStop(clock))

        self.assertEqual(extra, 1)
        combined = "\n".join(logs.output)
        self.assertIn("stage=verdict_overdue", combined)
        self.assertIn("outcome=presence_ended reason=opened", combined)
        self.assertNotIn("stage=unresolved", combined)

    def test_presence_session_can_be_disabled_and_ignores_foreign_results(self):
        clock = [100.0]
        capture, popen = self._presence_capture(3, clock=clock, max_frames=0, verdict=lambda n: None)
        capture.capture_series(event(), 100.0, self._Stop(clock))
        self.assertEqual(capture.presence_session(event(), 100.0, self._Stop(clock)), 0)
        self.assertEqual(len(popen.calls), 3)
        capture.note_result((self.root / "somewhere-else.jpg",), ProcessingResult(True, "exact_match"))
        self.assertEqual(capture.status()["presence"]["max_frames"], 0)

    class _SceneSource:
        """A frame source that also scores frames against an idle baseline."""

        def __init__(self, frames, differences):
            self.frames, self.differences = list(frames), list(differences)
            self.activity = 0
            self.served = 0

        def latest(self, *, after=None):
            if self.served >= len(self.frames):
                return None
            frame = self.frames[self.served]
            self.served += 1
            return frame, 100.0 + self.served

        def note_activity(self):
            self.activity += 1

        def scene_difference(self, frame):
            return self.differences[min(self.served - 1, len(self.differences) - 1)]

    def test_an_empty_scene_frame_is_not_captured_and_the_event_marks_activity(self):
        source = self._SceneSource([jpeg(), jpeg()], [0.012, 0.21])
        capture = TriggerFrameCapture(self.config, popen=FakePopen([]), frame_source=source)
        capture.attach(lambda paths, received_at, trigger: self.injected.append(paths))
        capture.on_camera_event(event())
        self.assertEqual(source.activity, 1)

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            self.assertEqual(capture.capture_once(event(), 100.0), ())
            paths = capture.capture_once(event(), 100.0, after=101.0)
        self.assertEqual(len(paths), 1)
        combined = "\n".join(logs.output)
        self.assertIn("outcome=skipped_empty_scene event_type=vehicle source=keyframe scene_difference=0.012", combined)
        self.assertIn("outcome=captured", combined)
        self.assertIn("scene_difference=0.210", combined)
        self.assertRegex(combined, r"clipping=\d\.\d\d")
        self.assertEqual(capture.status()["skipped"]["empty_scene"], 1)

    def test_clipped_frames_are_skipped_only_when_a_threshold_is_set(self):
        from io import BytesIO
        from PIL import Image
        output = BytesIO()
        Image.new("RGB", (64, 32), color="white").save(output, format="JPEG")
        blazed = output.getvalue()
        strict = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            max_highlight_clipping=0.5, empty_scene_threshold=0,
        )
        capture = TriggerFrameCapture(strict, popen=FakePopen([]), frame_source=self._SceneSource([blazed], [None]))
        capture.attach(lambda paths, received_at, trigger: self.injected.append(paths))
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            self.assertEqual(capture.capture_once(event(), 100.0), ())
        self.assertIn("outcome=skipped_clipped", "\n".join(logs.output))
        self.assertIn("clipping=1.00", "\n".join(logs.output))
        self.assertEqual(list(strict.output_directory.iterdir()), [], "the skipped frame is not left on disk")
        self.assertEqual(capture.status()["skipped"]["clipped"], 1)

        lenient = TriggerFrameCapture(self.config, popen=FakePopen([]), frame_source=self._SceneSource([blazed], [None]))
        lenient.attach(lambda paths, received_at, trigger: self.injected.append(paths))
        self.assertEqual(len(lenient.capture_once(event(), 100.0)), 1)

    def test_presence_session_ends_as_departed_and_warns_when_nothing_read_the_plate(self):
        clock = [100.0]
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            capture_count=1, presence_spacing_seconds=0.5, presence_max_frames=4,
        )
        # Series frame shows the car; the first presence frame shows an empty drive.
        source = self._SceneSource([jpeg(), jpeg()], [0.3, 0.005])
        capture = TriggerFrameCapture(config, popen=FakePopen([]), frame_source=source, clock=lambda: clock[0])
        capture.attach(lambda paths, received_at, trigger: (
            self.injected.append(paths), capture.note_result(paths, ProcessingResult(False, "ocr_error"))
        ))
        capture.capture_series(event(), 100.0, self._Stop(clock))

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            extra = capture.presence_session(event(), 100.0, self._Stop(clock))

        self.assertEqual(extra, 0)
        combined = "\n".join(logs.output)
        self.assertIn("outcome=presence_ended reason=departed", combined)
        self.assertIn("WARNING:gate_controller.trigger_capture:gate_presence stage=unresolved reason=departed event_type=vehicle extra_frames=0", combined)
        self.assertEqual(capture.status()["presence"]["unresolved"], 1)

    def test_a_session_that_opened_the_gate_is_not_unresolved(self):
        clock = [100.0]
        capture, popen = self._presence_capture(
            4, clock=clock, verdict=lambda n: ProcessingResult(n >= 4, "exact_match" if n >= 4 else "ocr_error"),
        )
        capture.capture_series(event(), 100.0, self._Stop(clock))
        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            capture.presence_session(event(), 100.0, self._Stop(clock))
        self.assertNotIn("stage=unresolved", "\n".join(logs.output))
        self.assertEqual(capture.status()["presence"]["unresolved"], 0)

    def test_a_live_session_is_started_for_the_series_and_its_stillest_frame_is_captured(self):
        clock = [100.0]

        class LiveSource:
            def __init__(self):
                self.started = 0
                self.stopped = []
                self.served = 0

            def start_session(self):
                self.started += 1
                return True

            def stop_session(self, reason):
                self.stopped.append(reason)

            def stillest(self, *, after=None, window_seconds=1.0):
                self.served += 1
                return jpeg(), 100.0 + self.served, 0.004

            def latest(self, *, after=None):
                raise AssertionError("stillest must be preferred while a session is live")

        source = LiveSource()
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            capture_count=2, delay_seconds=0, spacing_seconds=0.5, presence_max_frames=0,
        )
        capture = TriggerFrameCapture(config, popen=FakePopen([]), frame_source=source, clock=lambda: clock[0])
        capture.attach(lambda paths, received_at, trigger: (
            self.injected.append(paths), capture.note_result(paths, ProcessingResult(False, "ocr_error"))
        ))

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            self.assertEqual(capture.capture_series(event(), 100.0, self._Stop(clock)), 2)
            capture.presence_session(event(), 100.0, self._Stop(clock))

        self.assertEqual(source.started, 1)
        self.assertEqual(len(source.stopped), 1, "the live session stops when the presence session ends")
        combined = "\n".join(logs.output)
        self.assertIn("source=session", combined)
        self.assertIn("stillness=0.004", combined)

    def test_run_forever_drains_scheduled_events_and_unlinks_when_unattached(self):
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            delay_seconds=0, capture_count=1,
        )
        popen = FakePopen([FakeProcess(output=jpeg())])
        capture = TriggerFrameCapture(config, popen=popen, clock=lambda: 1.0)
        stop = Event()
        capture.on_camera_event(event())
        original_capture = capture.capture_once

        def capture_then_stop(*args, **kwargs):
            try:
                return original_capture(*args, **kwargs)
            finally:
                stop.set()

        capture.capture_once = capture_then_stop
        capture.run_forever(stop)

        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(sorted(config.output_directory.glob("*")), [])
        self.assertEqual(capture.status()["captures"], 0)


if __name__ == "__main__":
    unittest.main()


class FakeKeyframeSource:
    """Stand-in for the ClearKeyframeBuffer ring."""

    max_age = 1.6

    def __init__(self, frames=(), clock=lambda: 10.0):
        # (captured_at, jpeg_bytes) pairs, newest last
        self.frames = list(frames)
        self.requests = []
        self._clock = clock

    def latest(self, *, after=None):
        self.requests.append(after)
        if not self.frames:
            return None
        captured_at, frame = self.frames[-1]
        if after is not None and captured_at <= after:
            return None
        if not 0 <= self._clock() - captured_at <= self.max_age:
            return None
        return frame, captured_at


class HotKeyframeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.injected = []

    def tearDown(self):
        self.temporary.cleanup()

    def _capture(self, source, popen=None, count=3, clock=None):
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            delay_seconds=1.5, capture_count=count, spacing_seconds=1.0,
        )
        capture = TriggerFrameCapture(
            config, popen=popen or FakePopen([]), clock=clock or (lambda: 10.0),
            frame_source=source,
        )
        capture.attach(lambda paths, received_at, trigger: self.injected.append(paths))
        return capture

    def test_the_buffered_keyframe_is_injected_before_any_wait(self):
        source = FakeKeyframeSource([(9.4, jpeg())])
        capture = self._capture(source, count=1)
        pauses = []

        class Stop:
            def is_set(self):
                return False

            def wait(self, seconds):
                pauses.append(seconds)
                return False

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            injected = capture.capture_series(event(), 9.5, Stop())

        self.assertEqual(injected, 1)
        self.assertEqual(pauses, [])
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][0].read_bytes(), jpeg())
        self.assertIn("source=keyframe frame_age_ms=600", "\n".join(logs.output))

    def test_later_slots_wait_for_the_stop_and_never_reuse_a_frame(self):
        clock = {"now": 10.0}
        source = FakeKeyframeSource([(9.4, jpeg())], clock=lambda: clock["now"])
        capture = self._capture(source, count=3, clock=lambda: clock["now"])

        class Stop:
            def is_set(self):
                return False

            def wait(self, seconds):
                clock["now"] += seconds
                # A new keyframe lands during each wait.
                source.frames.append((clock["now"] - 0.2, jpeg((80, 40))))
                return False

        injected = capture.capture_series(event(), 9.5, Stop())

        self.assertEqual(injected, 3)
        # The first request is unconstrained; each later one asks for a frame
        # newer than the one just injected.
        self.assertEqual(source.requests, [None, 9.4, 11.3])
        self.assertEqual(len(self.injected), 3)

    def test_a_stale_ring_falls_back_to_a_grab(self):
        source = FakeKeyframeSource([(5.0, jpeg())])  # far older than max age
        popen = FakePopen([FakeProcess(output=jpeg((32, 16)))])
        capture = self._capture(source, popen=popen, count=1)

        class Stop:
            def is_set(self):
                return False

            def wait(self, seconds):
                return False

        with self.assertLogs("gate_controller.trigger_capture", level="INFO") as logs:
            injected = capture.capture_series(event(), 9.5, Stop())

        self.assertEqual(injected, 1)
        self.assertEqual(len(popen.calls), 1)
        combined = "\n".join(logs.output)
        self.assertIn("keyframe=unavailable fallback=grab", combined)
        self.assertIn("source=grab", combined)

    def test_keyframe_buffer_decodes_only_keyframes_without_probing(self):
        from gate_controller.trigger_capture import ClearKeyframeBuffer
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
        )
        buffer = ClearKeyframeBuffer(config)

        command = buffer.command
        self.assertIn("-skip_frame", command)
        self.assertEqual(command[command.index("-skip_frame") + 1], "nokey")
        self.assertIn("-probesize", command)
        self.assertIn("rtsp://127.0.0.1:8554/clear", command)
        self.assertEqual(buffer.status()["stream"], "clear")
        self.assertTrue(buffer.status()["keyframes_only"])
        self.assertIsNone(buffer.latest())
        self.assertNotIn("-hwaccel", command)
        self.assertEqual(command[command.index("-vf") + 1], "fps=1")
        self.assertEqual(buffer.status()["decode"], {"hwaccel": "software", "frame_width": 3840, "plate_region": "full"})

    def test_hardware_decode_and_scaling_apply_to_the_ring_and_the_grab(self):
        from gate_controller.trigger_capture import ClearKeyframeBuffer, TriggerFrameCapture
        config = TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
            hwaccel="drm", frame_width=1920,
        )

        ring = ClearKeyframeBuffer(config).command
        hw = ring.index("-hwaccel")
        self.assertEqual(ring[hw:hw + 6], (
            "-hwaccel", "drm", "-hwaccel_device", "/dev/dri/renderD128",
            "-hwaccel_output_format", "drm_prime",
        ))
        self.assertLess(hw, ring.index("-i"), "decoder selection must precede the input")
        self.assertEqual(ring[ring.index("-vf") + 1], "fps=1,hwdownload,format=nv12,scale=w='min(iw,1920)':h=-2")
        self.assertEqual(ClearKeyframeBuffer(config).status()["decode"],
                         {"hwaccel": "drm", "frame_width": 1920, "plate_region": "full"})

        grab = TriggerFrameCapture(config).command
        self.assertIn("-hwaccel", grab)
        self.assertLess(grab.index("-hwaccel"), grab.index("-i"))
        self.assertEqual(grab[grab.index("-vf") + 1], "hwdownload,format=nv12,scale=w='min(iw,1920)':h=-2")
        self.assertNotIn("fps=1", grab)

        software_scaled = TriggerFrameCapture(TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture", frame_width=1920,
        )).command
        self.assertNotIn("-hwaccel", software_scaled)
        self.assertEqual(software_scaled[software_scaled.index("-vf") + 1], "scale=w='min(iw,1920)':h=-2")
        default_grab = TriggerFrameCapture(TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
        )).command
        self.assertNotIn("-vf", default_grab)

    def test_plate_region_crops_at_native_resolution_before_a_shrink_only_scale(self):
        from gate_controller.plate_region import PlateRegion
        from gate_controller.trigger_capture import ClearKeyframeBuffer, TriggerFrameCapture
        config = load_trigger_capture_config(
            {"GATE_PLATE_REGION": "0.05,0.4,0.9,0.6", "GATE_TRIGGER_CAPTURE_HWACCEL": "drm",
             "GATE_TRIGGER_CAPTURE_FRAME_WIDTH": "1920", "GATE_TRIGGER_CAPTURE_CROP": "true"},
            self.root, webhook_enabled=True,
        )
        self.assertEqual(config.plate_region, PlateRegion(0.05, 0.4, 0.9, 0.6))
        self.assertTrue(config.crop_capture)

        # By default the region is for OCR only: captured frames stay whole so
        # evidence keeps the camera's timestamp overlay and full context.
        whole = load_trigger_capture_config(
            {"GATE_PLATE_REGION": "0.05,0.4,0.9,0.6", "GATE_TRIGGER_CAPTURE_HWACCEL": "drm",
             "GATE_TRIGGER_CAPTURE_FRAME_WIDTH": "1920"},
            self.root, webhook_enabled=True,
        )
        self.assertFalse(whole.crop_capture)
        whole_ring = ClearKeyframeBuffer(whole).command
        self.assertNotIn("crop=", whole_ring[whole_ring.index("-vf") + 1])
        self.assertEqual(ClearKeyframeBuffer(whole).status()["decode"]["plate_region"], "full")

        ring = ClearKeyframeBuffer(config).command
        self.assertEqual(ring[ring.index("-vf") + 1], (
            "fps=1,hwdownload,format=nv12,"
            "crop=trunc(iw*0.9000/2)*2:trunc(ih*0.6000/2)*2:trunc(iw*0.0500/2)*2:trunc(ih*0.4000/2)*2,"
            "scale=w='min(iw,1920)':h=-2"
        ))
        self.assertEqual(ClearKeyframeBuffer(config).status()["decode"]["plate_region"], "0.05,0.4,0.9,0.6")
        grab = TriggerFrameCapture(config).command
        self.assertIn("crop=trunc(iw*0.9000/2)*2", grab[grab.index("-vf") + 1])

        with self.assertRaises(ValueError):
            load_trigger_capture_config({"GATE_PLATE_REGION": "0.5,0,0.9,1"}, self.root, webhook_enabled=True)

    def test_hot_keyframes_can_be_disabled_by_environment(self):
        self.assertTrue(load_trigger_capture_config({}, Path("/u"), webhook_enabled=True).hot_keyframes)
        self.assertFalse(load_trigger_capture_config(
            {"GATE_TRIGGER_CAPTURE_HOT_KEYFRAMES": "false"}, Path("/u"), webhook_enabled=True,
        ).hot_keyframes)
