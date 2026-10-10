import unittest
from io import BytesIO

from PIL import Image

from gate_controller.scene import (
    DARK_THUMBNAIL_LUMA, SceneBaseline, frame_thumbnail, is_dark, thumbnail_difference,
)


def jpeg(color, size=(192, 108)):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def night_jpeg(noise=0, lamp=False, size=(192, 108)):
    """The drive with the spotlight off: near-black, the camera's white overlay
    text at the top, sensor noise, and optionally a plate lamp's worth of light.

    The real black stills of 2026-10-09 measure a thumbnail mean of 7.4.
    """
    image = Image.new("RGB", size, color=(6, 6, 6))
    for x in range(70, 110):
        image.putpixel((x, 2), (220, 220, 220))
    for index in range(noise):
        x, y = (index * 37) % size[0], 10 + (index * 53) % (size[1] - 10)
        image.putpixel((x, y), (14, 14, 14))
    if lamp:
        # A lit plate: roughly the fraction of a 4K frame a 232 px plate covers.
        for x in range(90, 102):
            for y in range(60, 63):
                image.putpixel((x, y), (200, 200, 200))
    output = BytesIO()
    image.save(output, format="JPEG")
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

    def test_a_lit_refresh_does_not_forget_the_dark_drive(self):
        # 2026-10-09 21:41-21:42: the drive had been dark for an hour, the
        # spotlight came on with motion a minute before the alarm, the
        # baseline was refreshed under it, and once the car took the light
        # away every black frame was "not the idle scene".
        clock = [1000.0]
        scene = SceneBaseline(idle_seconds=60, refresh_seconds=30, clock=lambda: clock[0])
        self.assertTrue(scene.observe(night_jpeg()))
        self.assertTrue(scene.status()["dark_available"])
        clock[0] += 30
        self.assertTrue(scene.observe(jpeg((110, 110, 110))), "the spotlit refresh")
        black = night_jpeg(noise=40)

        self.assertGreater(scene.difference(black), 0.3, "the lit baseline is no help")
        self.assertLess(scene.dark_difference(black), 0.03, "the dark one is")
        status = scene.status()
        self.assertEqual((status["age_seconds"], status["dark_age_seconds"]), (0.0, 30.0))

    def test_a_dark_frame_is_only_scored_against_a_dark_idle_frame(self):
        scene = SceneBaseline(clock=lambda: 0.0)
        self.assertIsNone(scene.dark_difference(night_jpeg()), "no baseline at all")
        scene.observe(jpeg((110, 110, 110)))
        self.assertIsNone(scene.dark_difference(night_jpeg()), "no dark baseline yet")
        scene.observe(night_jpeg(), now=100.0)
        self.assertIsNone(scene.dark_difference(jpeg((110, 110, 110))), "a lit frame is not dark")
        self.assertIsNone(scene.dark_difference(b"not a jpeg"))
        self.assertIsNotNone(scene.dark_difference(night_jpeg(noise=10)))

    def test_a_plate_lamp_is_below_what_a_thumbnail_can_see(self):
        # Which is why a dark match is never, on its own, "nothing to read":
        # the sweep asks the reader before it concludes anything from one.
        scene = SceneBaseline(clock=lambda: 0.0)
        scene.observe(night_jpeg())
        lamp = frame_thumbnail(night_jpeg(lamp=True))
        self.assertTrue(is_dark(lamp))
        self.assertLess(scene.dark_difference(night_jpeg(lamp=True)), 0.03)

    def test_the_dark_floor_sits_between_the_measured_scenes(self):
        # Black stills 7.4-7.5; the same drive under the spotlight 71-106.
        self.assertLess(sum(frame_thumbnail(night_jpeg())) / (96 * 54), DARK_THUMBNAIL_LUMA)
        self.assertTrue(is_dark(frame_thumbnail(night_jpeg(noise=200))))
        self.assertFalse(is_dark(frame_thumbnail(jpeg((40, 40, 40)))))
        self.assertFalse(is_dark(frame_thumbnail(jpeg((110, 110, 110)))))

    def test_timings_are_validated(self):
        with self.assertRaises(ValueError):
            SceneBaseline(refresh_seconds=0)
        with self.assertRaises(ValueError):
            SceneBaseline(idle_seconds=-1)
