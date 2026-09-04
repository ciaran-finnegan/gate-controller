import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from gate_controller.hot_stream import (
    HotFrameRing, HotStreamBuffer, JpegStreamParser, load_hot_stream_config,
)


def jpeg(colour, size=(64, 32)):
    output = BytesIO()
    Image.new("RGB", size, color=colour).save(output, format="JPEG")
    return output.getvalue()


class JpegStreamParserTests(unittest.TestCase):
    def test_extracts_complete_jpegs_across_arbitrary_chunks(self):
        first = jpeg("red")
        second = jpeg("blue")
        parser = JpegStreamParser(max_frame_bytes=4096)

        frames = []
        stream = b"discarded" + first + second
        for offset in range(0, len(stream), 37):
            frames.extend(parser.feed(stream[offset:offset + 37]))

        self.assertEqual([first, second], frames)

    def test_drops_an_oversized_partial_frame_and_recovers(self):
        valid = jpeg("green")
        parser = JpegStreamParser(max_frame_bytes=1024)

        self.assertEqual([], parser.feed(b"\xff\xd8\xff" + b"x" * 2048))
        self.assertEqual([valid], parser.feed(valid))


class HotFrameRingTests(unittest.TestCase):
    def test_retains_only_bounded_distinct_frames_and_selects_newest_fresh(self):
        ring = HotFrameRing(max_frames=2, max_frame_bytes=4096, max_total_bytes=8192)
        old = jpeg("red")
        middle = jpeg("green")
        newest = jpeg("blue")

        self.assertTrue(ring.add(old, captured_at=1.0))
        self.assertTrue(ring.add(middle, captured_at=2.0))
        self.assertFalse(ring.add(middle, captured_at=2.5))
        self.assertTrue(ring.add(newest, captured_at=3.0))

        self.assertEqual([newest, middle], ring.select(2, now=3.1, max_age=2.0))
        self.assertEqual([], ring.select(2, now=10.0, max_age=2.0))

    def test_materialises_owner_only_files_without_retaining_paths_in_the_ring(self):
        ring = HotFrameRing(max_frames=3, max_frame_bytes=4096, max_total_bytes=12288)
        frame = jpeg("purple")
        ring.add(frame, captured_at=5.0)

        with tempfile.TemporaryDirectory() as directory:
            paths = ring.materialise(Path(directory), 1, now=5.1, max_age=1.0)

            self.assertEqual(1, len(paths))
            self.assertEqual(frame, paths[0].read_bytes())
            self.assertEqual(0o600, paths[0].stat().st_mode & 0o777)

    def test_identical_live_samples_refresh_freshness_without_duplicate_selection(self):
        ring = HotFrameRing(max_frames=3, max_frame_bytes=4096, max_total_bytes=12288)
        frame = jpeg("black")

        self.assertTrue(ring.add(frame, captured_at=1.0))
        self.assertFalse(ring.add(frame, captured_at=5.0))

        self.assertEqual([frame], ring.select(3, now=5.5, max_age=1.0))
        self.assertTrue(ring.status(now=5.5, max_age=1.0)["ready"])


class HotStreamConfigurationTests(unittest.TestCase):
    def test_is_disabled_by_default_and_uses_only_the_fixed_loopback_fluent_path(self):
        disabled = load_hot_stream_config({}, Path("/var/lib/gate-controller/uploads"))
        enabled = load_hot_stream_config(
            {"GATE_HOT_STREAM_ENABLED": "true"},
            Path("/var/lib/gate-controller/uploads"),
        )

        self.assertFalse(disabled.enabled)
        self.assertTrue(enabled.enabled)
        self.assertEqual("rtsp://127.0.0.1:8554/camera", enabled.source_url)
        self.assertEqual(5.0, enabled.sample_fps)
        self.assertEqual(2, enabled.selection_count)

    def test_ffmpeg_command_and_child_environment_are_secret_free_and_bounded(self):
        config = load_hot_stream_config(
            {"GATE_HOT_STREAM_ENABLED": "true", "UNRELATED_SECRET": "do-not-inherit"},
            Path("/var/lib/gate-controller/uploads"),
        )
        buffer = HotStreamBuffer(config)

        self.assertEqual("/usr/bin/ffmpeg", buffer.command[0])
        self.assertIn("rtsp://127.0.0.1:8554/camera", buffer.command)
        self.assertIn("fps=5", buffer.command)
        self.assertEqual({"LANG": "C", "LC_ALL": "C"}, buffer.child_environment)

    def test_accepts_the_previous_three_frame_preset_but_caps_fallbacks_at_two(self):
        config = load_hot_stream_config(
            {
                "GATE_HOT_STREAM_ENABLED": "true",
                "GATE_HOT_STREAM_SELECTION_COUNT": "3",
            },
            Path("/var/lib/gate-controller/uploads"),
        )

        self.assertEqual(2, config.selection_count)

    def test_capture_loop_keeps_a_local_process_reference_during_close(self):
        class Stdout:
            def read(self, _size):
                return b""

        class Process:
            stdout = Stdout()

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        config = load_hot_stream_config(
            {"GATE_HOT_STREAM_ENABLED": "true"}, Path("/tmp"),
        )
        buffer = HotStreamBuffer(config, popen=lambda *_args, **_kwargs: Process())

        class Stop:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                if self.calls == 2:
                    buffer.close()
                    return False
                return self.calls >= 3

            def wait(self, _seconds=None):
                return True

        buffer.run_forever(Stop())


if __name__ == "__main__":
    unittest.main()


class PrivateFrameWriteTests(unittest.TestCase):
    def test_failed_frame_write_leaves_no_partial_file(self):
        import os
        import tempfile
        from unittest.mock import patch
        from pathlib import Path
        from gate_controller.hot_stream import write_private_frame

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            real_write = os.write
            calls = []

            def flaky_write(descriptor, view):
                calls.append(len(view))
                if len(calls) == 1:
                    real_write(descriptor, view[:2])
                    return 2
                raise OSError("disk gone")

            with patch("gate_controller.hot_stream.os.write", flaky_write):
                with self.assertRaises(OSError):
                    write_private_frame(root, b"\xff\xd8\xff\xd9")

            self.assertEqual(sorted(root.glob("*")), [])

    def test_failed_descriptor_close_leaves_no_frame(self):
        import os
        import tempfile
        from unittest.mock import patch
        from pathlib import Path
        from gate_controller.hot_stream import write_private_frame

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            real_close = os.close

            def failing_close(descriptor):
                real_close(descriptor)
                raise OSError("close failed")

            with patch("gate_controller.hot_stream.os.close", failing_close):
                with self.assertRaises(OSError):
                    write_private_frame(root, b"\xff\xd8\xff\xd9")

            self.assertEqual(sorted(root.glob("*")), [])
