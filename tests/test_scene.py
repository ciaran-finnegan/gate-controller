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


class WaitingCarTests(unittest.TestCase):
    """2026-10-10 10:05:38 and 10:07:34: the D-Max parked at the stop, half the
    picture, became the idle baseline 60-90 s after the last alarm, and the
    next alarm's sweep skipped its frames unread as "empty". The on-device
    detector now stands between a keyframe and the baseline.
    """

    def setUp(self):
        self.clock = [1000.0]
        self.asked = []
        self.empty = jpeg((120, 120, 120))
        self.car = jpeg_with_car()

    def scene(self, check):
        def vehicle_check(frame):
            self.asked.append(frame)
            return check(frame)

        return SceneBaseline(
            idle_seconds=60, refresh_seconds=30, clock=lambda: self.clock[0],
            vehicle_check=vehicle_check,
        )

    def test_a_keyframe_the_detector_boxes_a_plate_in_is_refused_and_the_old_baseline_kept(self):
        scene = self.scene(lambda frame: frame == self.car)
        self.assertTrue(scene.observe(self.empty), "the empty drive is adopted")
        self.clock[0] += 90.0  # the car has stood there since the last alarm
        with self.assertLogs("gate_controller.scene", level="INFO") as logs:
            self.assertFalse(scene.observe(self.car), "a boxed plate never becomes the idle drive")
        self.assertEqual(self.asked, [self.empty, self.car])
        # The baseline is still the empty drive, so the car's frames differ from it: read.
        self.assertGreater(scene.difference(self.car), 0.08)
        self.assertLess(scene.difference(self.empty), 0.03)
        status = scene.status()
        self.assertEqual((status["refreshes"], status["refused_vehicle"]), (1, 1))
        self.assertEqual(status["age_seconds"], 90.0)
        self.assertIn("gate_scene_baseline outcome=refresh_refused reason=plate_box refused=1",
                      logs.output[0])

    def test_a_detector_that_cannot_answer_keeps_the_old_baseline(self):
        # Not ready, busy with a sweep frame, timed out, or raising: none of
        # those is "no car here". A stale picture of the empty drive makes a
        # car's frames differ -- the safe side -- so the refresh waits.
        answers = iter([False, None, RuntimeError("engine")])

        def check(_frame):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        scene = self.scene(check)
        self.assertTrue(scene.observe(self.empty))
        self.clock[0] += 60.0
        self.assertFalse(scene.observe(self.car), "no answer is not an empty drive")
        self.clock[0] += 60.0
        self.assertFalse(scene.observe(self.car), "nor is a failure")
        self.assertGreater(scene.difference(self.car), 0.08)
        status = scene.status()
        self.assertEqual(status["refreshes"], 1)
        self.assertEqual((status["refused_vehicle"], status["refused_unanswered"]), (0, 2))

    def test_an_empty_frame_is_still_adopted_when_the_detector_finds_nothing(self):
        scene = self.scene(lambda frame: False)
        self.assertTrue(scene.observe(self.empty))
        self.clock[0] += 30.0
        later = jpeg((110, 110, 110))
        self.assertTrue(scene.observe(later), "the light changed; the empty drive is refreshed")
        self.assertLess(scene.difference(jpeg((111, 111, 111))), 0.03)
        self.assertEqual(scene.status()["refreshes"], 2)
        self.assertEqual(scene.status()["refused_vehicle"], 0)

    def test_without_a_detector_every_idle_frame_is_adopted_as_before(self):
        scene = SceneBaseline(idle_seconds=60, refresh_seconds=30, clock=lambda: self.clock[0])
        self.assertTrue(scene.observe(self.empty))
        self.clock[0] += 30.0
        self.assertTrue(scene.observe(self.car))
        self.assertEqual(scene.status()["refused_vehicle"], 0)

    def test_the_detector_is_asked_no_more_often_than_a_refresh_could_happen(self):
        # The decoded keyframe ring offers a frame every second; a car that
        # waits must not cost a detector read a second.
        scene = self.scene(lambda frame: frame == self.car)
        self.assertTrue(scene.observe(self.empty))
        self.clock[0] += 60.0
        for _second in range(45):
            scene.observe(self.car)
            self.clock[0] += 1.0
        self.assertEqual(len(self.asked), 3, "once for the empty frame, then once per 30 s")
        self.assertEqual(scene.status()["refused_vehicle"], 2)

    def test_refusals_are_journalled_once_per_rate_limit_with_the_count_between(self):
        from gate_controller.scene import REFUSAL_LOG_SECONDS
        scene = self.scene(lambda frame: frame == self.car)
        self.assertTrue(scene.observe(self.empty))
        self.clock[0] += 60.0
        with self.assertLogs("gate_controller.scene", level="INFO") as logs:
            # A car waiting ten minutes: twenty refusals at the 30 s cadence,
            # the first at once and the next when the rate limit has passed.
            for _refresh in range(20):
                scene.observe(self.car)
                self.clock[0] += 30.0
        self.assertEqual(scene.status()["refused_vehicle"], 20)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("reason=plate_box refused=1 ", logs.output[0])
        self.assertIn(f"refused={int(REFUSAL_LOG_SECONDS // 30)} ", logs.output[1])

    def test_a_dark_frame_with_a_boxed_plate_does_not_become_the_dark_baseline_either(self):
        # A car whose plate lamp is the only light in the picture: the dark
        # frame is never skipped unread anyway, and it is not the unlit drive.
        scene = self.scene(lambda frame: frame == night_jpeg(lamp=True))
        self.assertTrue(scene.observe(night_jpeg()))
        self.clock[0] += 60.0
        self.assertFalse(scene.observe(night_jpeg(lamp=True)))
        self.assertEqual(scene.status()["dark_age_seconds"], 60.0)
        self.assertTrue(scene.status()["dark_available"])

    def test_an_undecodable_frame_never_reaches_the_detector(self):
        scene = self.scene(lambda frame: False)
        self.assertFalse(scene.observe(b"not a jpeg"))
        self.assertEqual(self.asked, [])
