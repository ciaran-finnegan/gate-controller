import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gate_controller.images import rank_images, wait_until_readable


class ImageTests(unittest.TestCase):
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
