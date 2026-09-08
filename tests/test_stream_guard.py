"""Proof that the live-stream command guard is installed and fires.

A guard nobody exercises is a guard that quietly stops working: #113 is the
precedent, where the live-state guard became a no-op on Python 3.10 because
``pathlib`` had bound the ``os`` functions at import time and the patch never
reached them. So every claim this guard makes is asserted here, on whichever
interpreter is running, including the analogous binding trap for
``subprocess``.

This module also *installs* the guard. ``unittest discover -s tests`` -- what
the on-device updater runs -- makes ``tests`` the top-level directory and never
imports ``tests/__init__.py``, so installing only from the package would leave
the Pi unguarded. Discovery imports every test module before running any test,
so installing here covers the whole suite under every invocation.
"""
import os
import subprocess
import sys
import unittest

try:  # discovery rooted at the repository ...
    from tests.stream_guard import (
        FORBIDDEN_FRAGMENTS, LiveStreamAccess, acknowledge_blocked,
        allow_live_stream, install_command_guard, is_installed,
    )
except ImportError:  # ... or rooted at `tests` itself (`discover -s tests`)
    from stream_guard import (  # type: ignore[no-redef]
        FORBIDDEN_FRAGMENTS, LiveStreamAccess, acknowledge_blocked,
        allow_live_stream, install_command_guard, is_installed,
    )

install_command_guard()

# Captured the way `gate_controller` captures it: by value, at import time, as
# a default argument. This is the reference the guard has to reach.
_POPEN_CAPTURED_AT_IMPORT = subprocess.Popen

LIVE = "rtsp://127.0.0.1:8554/clear"
FLUENT = "rtsp://127.0.0.1:8554/camera"
# A command that names the live stream but could not decode it even if it ran:
# the self-test must never depend on ffmpeg, and must never spawn anything that
# reads a stream, even one that is not there.
INERT = [sys.executable, "-c", "raise SystemExit(0)", LIVE]


class GuardTestCase(unittest.TestCase):
    """Base for tests that provoke the guard on purpose.

    The guard records every command it blocks and fails the test that made the
    attempt, so that a broad ``except Exception`` elsewhere cannot turn a
    blocked live-stream command into a passing test. These tests block commands
    deliberately, so they take their own attempts off the record.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(acknowledge_blocked)


class StreamGuardInstallationTests(GuardTestCase):
    def test_the_guard_is_installed_for_the_whole_suite(self):
        self.assertTrue(is_installed())
        self.assertTrue(
            getattr(subprocess.Popen.__init__, "__gate_stream_guard__", False),
            "subprocess.Popen.__init__ is not the guarded one",
        )
        self.assertTrue(getattr(os.execve, "__gate_stream_guard__", False))
        self.assertTrue(getattr(unittest.TestCase.run, "__gate_stream_guard__", False))

    def test_installing_twice_is_a_no_op(self):
        guarded = subprocess.Popen.__init__

        self.assertFalse(install_command_guard())

        self.assertIs(guarded, subprocess.Popen.__init__)

    def test_the_class_object_is_unchanged_so_captured_references_are_guarded(self):
        # The trap: rebinding `subprocess.Popen` would leave every
        # `popen=subprocess.Popen` default in gate_controller pointing at the
        # original class, and the guard would be a silent no-op exactly where
        # it matters. Patching the method keeps one class object.
        self.assertIs(_POPEN_CAPTURED_AT_IMPORT, subprocess.Popen)

        with self.assertRaises(LiveStreamAccess):
            _POPEN_CAPTURED_AT_IMPORT(INERT)


class StreamGuardFiresTests(GuardTestCase):
    def assertBlocked(self, call, *args, **kwargs):
        with self.assertRaises(LiveStreamAccess) as blocked:
            call(*args, **kwargs)
        return str(blocked.exception)

    def test_popen_against_the_live_stream_is_blocked_before_it_spawns(self):
        message = self.assertBlocked(subprocess.Popen, INERT)

        self.assertIn("rtsp://", message)
        self.assertIn("popen=", message)

    def test_every_subprocess_entry_point_funnels_through_the_guard(self):
        for call in (
            subprocess.run, subprocess.call, subprocess.check_call,
            subprocess.check_output,
        ):
            with self.subTest(call=call.__name__):
                self.assertBlocked(call, INERT)

    def test_a_shell_string_naming_the_stream_is_blocked(self):
        self.assertBlocked(
            subprocess.run, f"ffmpeg -i {LIVE} out.jpg", shell=True,
        )

    def test_the_process_replacing_calls_are_blocked(self):
        # gate_media_transcoder execve()s into ffmpeg with two RTSP URLs; a
        # test that stopped mocking execve would replace the test runner with
        # an unbounded transcode of the camera.
        self.assertBlocked(
            os.execve, "/usr/bin/ffmpeg", ["/usr/bin/ffmpeg", "-i", FLUENT], {},
        )
        self.assertBlocked(os.system, f"ffmpeg -i {LIVE}")
        self.assertBlocked(
            os.posix_spawn, "/usr/bin/ffmpeg", ["/usr/bin/ffmpeg", "-i", LIVE], {},
        )

    def test_every_forbidden_fragment_is_actually_rejected(self):
        for fragment in FORBIDDEN_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertBlocked(
                    subprocess.Popen,
                    [sys.executable, "-c", "pass", f"x{fragment}y"],
                )

    def test_the_default_source_of_every_stream_path_is_covered(self):
        from gate_controller.audio_capture import DEFAULT_SOURCE
        from gate_controller.hot_stream import LOOPBACK_FLUENT_STREAM
        from gate_controller.trigger_capture import LOOPBACK_CLEAR_STREAM

        for url in (DEFAULT_SOURCE, LOOPBACK_FLUENT_STREAM, LOOPBACK_CLEAR_STREAM):
            with self.subTest(url=url):
                self.assertBlocked(
                    subprocess.Popen, [sys.executable, "-c", "pass", "-i", url],
                )

    def test_the_real_capture_objects_cannot_reach_the_stream_by_default(self):
        # The shape of the hazard the guard exists for: a test that builds the
        # real object and forgets the fake process factory.
        from gate_controller.trigger_capture import (
            TriggerCaptureConfig, TriggerFrameCapture,
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            config = TriggerCaptureConfig(
                enabled=True, output_directory=Path(directory) / ".capture",
            )
            capture = TriggerFrameCapture(config)

            with self.assertRaises(LiveStreamAccess):
                capture.capture_once(
                    type("Event", (), {"event_type": "vehicle", "to_wire": dict})(),
                    100.0,
                )


class StreamGuardAllowsOtherCommandsTests(GuardTestCase):
    def test_an_ordinary_command_still_runs(self):
        completed = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            stdout=subprocess.PIPE, text=True, check=True,
        )

        self.assertEqual("ok", completed.stdout.strip())

    def test_a_command_mentioning_another_scheme_is_not_blocked(self):
        completed = subprocess.run(
            [sys.executable, "-c", "print('ok')", "https://127.0.0.1:8555/clear"],
            stdout=subprocess.PIPE, text=True, check=True,
        )

        self.assertEqual("ok", completed.stdout.strip())


class StreamGuardOptInTests(GuardTestCase):
    def test_the_context_manager_opens_and_closes_the_exemption(self):
        with allow_live_stream("an inert command, for the guard's own test"):
            completed = subprocess.run(
                INERT, stdout=subprocess.PIPE, check=False,
            )
        self.assertEqual(0, completed.returncode)

        with self.assertRaises(LiveStreamAccess):
            subprocess.Popen(INERT)

    @allow_live_stream("an inert command, for the guard's own test")
    def test_the_decorator_opens_the_exemption_for_one_test(self):
        self.assertEqual(0, subprocess.run(INERT, check=False).returncode)

    def test_the_exemption_closes_even_when_the_test_body_raises(self):
        with self.assertRaises(ZeroDivisionError):
            with allow_live_stream("deliberate failure inside the exemption"):
                raise ZeroDivisionError

        with self.assertRaises(LiveStreamAccess):
            subprocess.Popen(INERT)

    def test_an_exemption_without_a_written_reason_is_refused(self):
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    allow_live_stream(reason)


if __name__ == "__main__":
    unittest.main()
