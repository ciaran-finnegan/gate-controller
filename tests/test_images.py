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

    def test_rank_images_skips_a_candidate_removed_before_digest_tiebreak(self):
        with tempfile.TemporaryDirectory() as directory:
            removed = Path(directory) / "removed.jpg"
            stable = Path(directory) / "stable.jpg"
            Image.new("L", (16, 16), color=100).save(removed)
            Image.new("L", (16, 16), color=100).save(stable)
            real_digest = image_tools._content_digest

            def digest(path):
                if Path(path) == removed:
                    raise FileNotFoundError(path)
                return real_digest(path)

            with patch("gate_controller.images._content_digest", side_effect=digest):
                self.assertEqual([stable], rank_images((removed, stable)))

class FlatFractionTests(unittest.TestCase):
    """A picture the decoder could not finish is uniform, not blank.

    The regions it never wrote keep whatever the buffer held - on the
    hardware path zeroes, which render as RGB(0,135,0) - while the blocks it
    did decode carry real content. That reads as a busy scene to the
    empty-scene check and a normal exposure to the clipping check.
    """

    @staticmethod
    def _jpeg(image):
        import io
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    @classmethod
    def _scene(cls, size, base, spread, seed=1, shading=70):
        """Low-frequency texture, the way a real scene varies."""
        import random
        rng = random.Random(seed)
        width, height = size
        image = Image.new("RGB", size)
        image.putdata([
            tuple(
                min(255, max(0, base[channel] + rng.randint(-spread, spread)
                             + (x * shading) // (2 * width)
                             + (y * shading) // (2 * height)))
                for channel in range(3)
            )
            for y in range(height) for x in range(width)
        ])
        return image

    def test_a_partially_decoded_picture_is_mostly_one_flat_colour(self):
        frame = Image.new("RGB", (480, 180), (0, 135, 0))
        frame.paste(self._scene((150, 90), (110, 110, 110), 40), (0, 0))
        flat = image_tools.measure_flat_fraction(self._jpeg(frame))
        self.assertGreater(flat, 0.6)

    def test_a_normal_scene_is_not_flat(self):
        flat = image_tools.measure_flat_fraction(
            self._jpeg(self._scene((480, 180), (110, 115, 110), 45)),
        )
        self.assertLess(flat, 0.6)

    def test_a_dark_night_frame_with_a_headlit_plate_is_never_called_flat(self):
        # With the IR illuminator off the drive is near-black by nature. It
        # is flat, and it can still carry a readable plate, so it is not
        # scored at all rather than scored and rejected.
        night = self._scene((480, 180), (5, 5, 6), 4, seed=3, shading=12)
        night.paste(self._scene((60, 20), (215, 215, 205), 20, seed=4), (200, 120))
        self.assertEqual(image_tools.measure_flat_fraction(self._jpeg(night)), 0.0)
        self.assertEqual(
            image_tools.measure_flat_fraction(
                self._jpeg(Image.new("RGB", (480, 180), (2, 2, 2))),
            ),
            0.0,
        )

    def test_only_the_plate_band_is_measured_when_one_is_configured(self):
        from gate_controller.plate_region import PlateRegion
        # Broken bottom half, real content on top: the band is what matters.
        frame = Image.new("RGB", (480, 360), (0, 135, 0))
        frame.paste(self._scene((480, 180), (110, 110, 110), 45), (0, 0))
        data = self._jpeg(frame)
        self.assertLess(image_tools.measure_flat_fraction(data), 0.6)
        band = PlateRegion(0.0, 0.5, 1.0, 0.5)
        self.assertGreater(image_tools.measure_flat_fraction(data, band), 0.6)

    def test_an_unreadable_frame_is_not_measurable_rather_than_flat(self):
        self.assertIsNone(image_tools.measure_flat_fraction(b"not a jpeg at all"))


class UploadCompletenessTests(unittest.TestCase):
    """A JPEG still being written must never count as readable."""

    def test_a_jpeg_without_its_end_marker_is_not_readable_until_the_tail_lands(self):
        import io
        import tempfile
        from PIL import Image
        from gate_controller.images import wait_until_readable

        output = io.BytesIO()
        Image.new("RGB", (64, 32), color="blue").save(output, format="JPEG")
        complete = output.getvalue()
        self.assertTrue(complete.endswith(b"\xff\xd9"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Front Gate_00_20260906131244.jpg"
            # The camera's FTP client writes sequentially; most of the file is
            # there long before the final two bytes.
            path.write_bytes(complete[:-2])
            self.assertFalse(wait_until_readable(path, timeout=0, poll_interval=0))
            path.write_bytes(complete[:-1])
            self.assertFalse(wait_until_readable(path, timeout=0, poll_interval=0))
            path.write_bytes(complete)
            self.assertTrue(wait_until_readable(path, timeout=0, poll_interval=0))
            path.write_bytes(b"\xff\xd8\xff\xd9")
            self.assertFalse(wait_until_readable(path, timeout=0, poll_interval=0), "a marker alone is not an image")



if __name__ == "__main__":
    unittest.main()
