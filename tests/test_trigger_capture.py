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
        self.assertEqual(buffer.status()["decode"], {"hwaccel": "software", "frame_width": 3840})

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
        self.assertEqual(ring[ring.index("-vf") + 1], "hwdownload,format=nv12,fps=1,scale=1920:-2")
        self.assertEqual(ClearKeyframeBuffer(config).status()["decode"],
                         {"hwaccel": "drm", "frame_width": 1920})

        grab = TriggerFrameCapture(config).command
        self.assertIn("-hwaccel", grab)
        self.assertLess(grab.index("-hwaccel"), grab.index("-i"))
        self.assertEqual(grab[grab.index("-vf") + 1], "hwdownload,format=nv12,scale=1920:-2")
        self.assertNotIn("fps=1", grab)

        software_scaled = TriggerFrameCapture(TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture", frame_width=1920,
        )).command
        self.assertNotIn("-hwaccel", software_scaled)
        self.assertEqual(software_scaled[software_scaled.index("-vf") + 1], "scale=1920:-2")
        default_grab = TriggerFrameCapture(TriggerCaptureConfig(
            enabled=True, output_directory=self.root / ".trigger-capture",
        )).command
        self.assertNotIn("-vf", default_grab)

    def test_hot_keyframes_can_be_disabled_by_environment(self):
        self.assertTrue(load_trigger_capture_config({}, Path("/u"), webhook_enabled=True).hot_keyframes)
        self.assertFalse(load_trigger_capture_config(
            {"GATE_TRIGGER_CAPTURE_HOT_KEYFRAMES": "false"}, Path("/u"), webhook_enabled=True,
        ).hot_keyframes)
