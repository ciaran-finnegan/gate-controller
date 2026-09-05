import unittest
from io import BytesIO

from PIL import Image

from gate_controller.scene import SceneBaseline, frame_thumbnail, thumbnail_difference


def jpeg(color, size=(192, 108)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def jpeg_with_car(size=(192, 108)):
    image = Image.new("RGB", size, color=(120, 120, 120))
    for x in range(40, 150):
        for y in range(50, 100):
            image.putpixel((x, y), (230, 230, 230))
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


class SceneBaselineTests(unittest.TestCase):
    def test_baseline_is_taken_only_while_idle_and_refreshed_no_faster_than_configured(self):
        clock = [1000.0]
        scene = SceneBaseline(idle_seconds=60, refresh_seconds=30, clock=lambda: clock[0])
        self.assertIsNone(scene.difference(jpeg((120, 120, 120))))

        self.assertTrue(scene.observe(jpeg((120, 120, 120))), "first idle frame becomes the baseline")
        clock[0] += 10
        self.assertFalse(scene.observe(jpeg((120, 120, 120))), "too soon to refresh")
        clock[0] += 25
        self.assertTrue(scene.observe(jpeg((120, 120, 120))))

        scene.note_activity()
        clock[0] += 40
        self.assertFalse(scene.observe(jpeg((0, 0, 0))), "busy scene must not become the baseline")
        clock[0] += 30
        self.assertTrue(scene.observe(jpeg((120, 120, 120))))
        self.assertEqual(scene.status()["refreshes"], 3)
        self.assertTrue(scene.status()["available"])

    def test_difference_is_small_for_the_same_scene_and_large_with_a_vehicle(self):
        scene = SceneBaseline(clock=lambda: 0.0)
        scene.observe(jpeg((120, 120, 120)))

        same = scene.difference(jpeg((122, 122, 122)))
        car = scene.difference(jpeg_with_car())

        self.assertLess(same, 0.03)
        self.assertGreater(car, 0.08)

    def test_undecodable_frames_never_become_or_score_against_the_baseline(self):
        scene = SceneBaseline(clock=lambda: 0.0)
        self.assertFalse(scene.observe(b"not a jpeg"))
        self.assertIsNone(frame_thumbnail(b"\xff\xd8\xff garbage"))
        scene.observe(jpeg((120, 120, 120)))
        self.assertIsNone(scene.difference(b"not a jpeg"))
        self.assertEqual(thumbnail_difference([], [1]), 1.0)

    def test_timings_are_validated(self):
        with self.assertRaises(ValueError):
            SceneBaseline(refresh_seconds=0)
        with self.assertRaises(ValueError):
            SceneBaseline(idle_seconds=-1)
