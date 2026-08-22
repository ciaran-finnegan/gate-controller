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


class HotStreamConfigurationTests(unittest.TestCase):
    def test_is_disabled_by_default_and_uses_only_the_fixed_loopback_clear_path(self):
        disabled = load_hot_stream_config({}, Path("/var/lib/gate-controller/uploads"))
        enabled = load_hot_stream_config(
            {"GATE_HOT_STREAM_ENABLED": "true"},
            Path("/var/lib/gate-controller/uploads"),
        )

        self.assertFalse(disabled.enabled)
        self.assertTrue(enabled.enabled)
        self.assertEqual("rtsp://127.0.0.1:8554/clear", enabled.source_url)
        self.assertEqual(5.0, enabled.sample_fps)
        self.assertEqual(3, enabled.selection_count)

    def test_ffmpeg_command_and_child_environment_are_secret_free_and_bounded(self):
        config = load_hot_stream_config(
            {"GATE_HOT_STREAM_ENABLED": "true", "UNRELATED_SECRET": "do-not-inherit"},
            Path("/var/lib/gate-controller/uploads"),
        )
        buffer = HotStreamBuffer(config)

        self.assertEqual("/usr/bin/ffmpeg", buffer.command[0])
        self.assertIn("rtsp://127.0.0.1:8554/clear", buffer.command)
        self.assertIn("fps=5", buffer.command)
        self.assertEqual({"LANG": "C", "LC_ALL": "C"}, buffer.child_environment)


if __name__ == "__main__":
    unittest.main()
