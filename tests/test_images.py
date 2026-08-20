import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import gate_controller.images as image_tools
from gate_controller.images import rank_images, wait_until_readable


class ImageTests(unittest.TestCase):
    def _measure_frame_quality(self, path: Path):
        measure = getattr(image_tools, "measure_frame_quality", None)
        self.assertIsNotNone(measure, "measure_frame_quality is not implemented")
        return measure(path)

    def test_validates_decodable_jpeg_bytes_without_a_filesystem_path(self):
        validator = getattr(image_tools, "is_decodable_jpeg", None)
        self.assertIsNotNone(validator, "byte-based JPEG validation is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            Image.new("RGB", (16, 8), color="blue").save(frame, format="JPEG")
            encoded = frame.read_bytes()

        self.assertTrue(validator(encoded))
        self.assertFalse(validator(encoded[:4]))
        self.assertFalse(validator(b"not a jpeg"))

    def test_measure_frame_quality_reports_dimensions_and_bounded_luma_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "split.jpg"
            image = Image.new("L", (16, 8))
            image.putdata([0 if x < 8 else 255 for _y in range(8) for x in range(16)])
            image.save(frame, format="JPEG", quality=100, subsampling=0)

            quality = self._measure_frame_quality(frame)

            self.assertEqual(quality.sequence, 0)
            self.assertEqual(quality.digest, hashlib.sha256(frame.read_bytes()).hexdigest())
            self.assertEqual((quality.width, quality.height), (16, 8))
            self.assertAlmostEqual(quality.brightness, 0.5, delta=0.01)
            self.assertAlmostEqual(quality.darkness, 0.5, delta=0.01)
            self.assertAlmostEqual(quality.highlight_clipping, 0.5, delta=0.01)
            self.assertGreater(quality.sharpness, 0.0)
            for metric in (
                quality.sharpness,
                quality.brightness,
                quality.darkness,
                quality.highlight_clipping,
            ):
                self.assertGreaterEqual(metric, 0.0)
                self.assertLessEqual(metric, 1.0)

    def test_measure_frame_quality_reuses_a_precomputed_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            Image.new("L", (16, 8), color=128).save(frame, format="JPEG")
            digest = hashlib.sha256(frame.read_bytes()).hexdigest()

            with patch("gate_controller.images._content_digest") as content_digest:
                quality = image_tools.measure_frame_quality(frame, digest=digest)

            self.assertEqual(quality.digest, digest)
            content_digest.assert_not_called()

    def test_measure_frame_quality_downsamples_before_filtering_for_sharpness(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "large.jpg"
            Image.new("L", (640, 360), color=128).save(frame, format="JPEG")
            filtered_sizes = []
            original_filter = Image.Image.filter

            def record_filter(image, image_filter):
                filtered_sizes.append(image.size)
                return original_filter(image, image_filter)

            with patch.object(Image.Image, "filter", autospec=True, side_effect=record_filter):
                quality = self._measure_frame_quality(frame)

            self.assertEqual((quality.width, quality.height), (640, 360))
            self.assertEqual(filtered_sizes, [(320, 180)])

    def test_measure_frame_quality_returns_a_redacted_status_for_invalid_images(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private-camera-frame.jpg"
            frame.write_bytes(b"not a jpeg: exceptionally sensitive details")

            quality = self._measure_frame_quality(frame)

            self.assertRegex(quality.status, r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
            self.assertEqual(quality.status, "quality_unavailable")
            self.assertNotIn(str(frame), quality.status)
            self.assertNotIn("sensitive", quality.status)

    def test_rejects_non_jpeg_magic_without_invoking_pillow(self):
        with tempfile.TemporaryDirectory() as directory:
            disguised = Path(directory) / "disguised.jpg"
            disguised.write_bytes(b"8BPS\x00\x01untrusted image payload")

            with patch("gate_controller.images.Image.open") as open_image:
                self.assertFalse(wait_until_readable(disguised, timeout=0, poll_interval=0))
                self.assertEqual(rank_images((disguised,)), [])

            open_image.assert_not_called()

    def test_rejects_images_that_trigger_a_decompression_bomb_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.jpg"
            Image.new("RGB", (16, 16), color="red").save(oversized, format="JPEG")

            with patch.object(Image, "MAX_IMAGE_PIXELS", 200):
                self.assertFalse(wait_until_readable(oversized, timeout=0, poll_interval=0))
                self.assertEqual(rank_images((oversized,)), [])

    def test_rejects_images_that_trigger_a_decompression_bomb_error(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.jpg"
            oversized.write_bytes(b"\xff\xd8\xff")
            with patch(
                "gate_controller.images.Image.open",
                side_effect=Image.DecompressionBombError("too many pixels"),
            ):
                self.assertFalse(wait_until_readable(oversized, timeout=0, poll_interval=0))
                self.assertEqual(rank_images((oversized,)), [])

    def test_runtime_requires_a_non_vulnerable_pillow_release(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")

        self.assertIn("Pillow==12.3.0", requirements)

    def test_rejects_a_partial_jpeg_file(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "partial.jpg"
            partial.write_bytes(b"\xff\xd8\xff\xe0")

            self.assertFalse(wait_until_readable(partial, timeout=0, poll_interval=0))

    def test_rank_images_excludes_non_image_files(self):
        with tempfile.TemporaryDirectory() as directory:
            non_image = Path(directory) / "notes.jpg"
            non_image.write_text("not an image")

            self.assertEqual(rank_images((non_image,)), [])

    def test_rank_images_excludes_files_over_the_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.jpg"
            Image.new("L", (64, 64), color=128).save(oversized)

            self.assertEqual(
                rank_images((oversized,), max_bytes=oversized.stat().st_size - 1),
                [],
            )

    def test_ranks_the_sharper_image_first(self):
        with tempfile.TemporaryDirectory() as directory:
            blurry = Path(directory) / "blurry.jpg"
            sharp = Path(directory) / "sharp.jpg"
            Image.new("L", (64, 64), color=128).save(blurry)
            pixels = Image.new("L", (64, 64))
            pixels.putdata([(0 if (x + y) % 2 else 255) for y in range(64) for x in range(64)])
            pixels.save(sharp)

            self.assertEqual(rank_images((blurry, sharp)), [sharp, blurry])

    def test_breaks_equal_sharpness_ties_by_content_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.jpg"
            second = Path(directory) / "second.jpg"
            Image.new("L", (16, 16), color=100).save(first)
            second.write_bytes(first.read_bytes() + b"camera metadata")

            forward = rank_images((first, second))
            reverse = rank_images((second, first))

            self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
