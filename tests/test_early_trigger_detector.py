"""The early trigger's two detectors, on synthetic patches, one nuisance at a time.

Pure Python, no numpy and no model (Pillow only to open the real-frame
fixtures). Each scene is a 24x16 grid of cell means fed at 4 samples a second,
which is exactly what the worker hands the detector on the Pi.
"""
import random
import unittest
from pathlib import Path

from gate_controller.early_trigger import (
    GRID_HEIGHT, GRID_WIDTH, LIGHT_DAY, LIGHT_NIGHT, DetectorConfig, PatchDetector,
)

W, H = GRID_WIDTH, GRID_HEIGHT
FPS = 4.0
STEP = 1.0 / FPS


def textured(seed=7, low=70, high=170):
    """A daytime drive: gravel, verge and hedge are not one flat grey."""
    rng = random.Random(seed)
    return [rng.randint(low, high) for _ in range(W * H)]


def dark(seed=3):
    rng = random.Random(seed)
    return [rng.randint(0, 3) for _ in range(W * H)]


def paint(cells, x0, y0, x1, y1, value):
    """A rectangle [x0, x1) x [y0, y1) of one brightness, clipped to the grid."""
    cells = list(cells)
    for y in range(max(0, y0), min(H, y1)):
        for x in range(max(0, x0), min(W, x1)):
            cells[y * W + x] = value
    return cells


def clamp(value):
    return max(0, min(255, int(round(value))))


class Scene:
    """Run a detector over a function of the sample index; collect the verdicts."""

    def __init__(self, config=None):
        self.detector = PatchDetector(config or DetectorConfig(fps=FPS))
        self.now = 1000.0
        self.index = 0
        self.verdicts = []
        self.triggers = []

    def run(self, frames, picture):
        for _ in range(frames):
            observation = self.detector.observe(picture(self.index), self.now)
            self.verdicts.append(observation.verdict)
            if observation.triggered:
                self.triggers.append((self.index, observation))
            self.index += 1
            self.now += STEP
        return self

    def settle(self, base, seconds=12):
        return self.run(int(seconds * FPS), lambda _i: base)


class DayDetectorTest(unittest.TestCase):
    def test_a_vehicle_entering_from_the_left_triggers_within_a_second(self):
        base = textured()
        scene = Scene().settle(base)
        entered_at = scene.index

        def arriving(i):
            # Two columns further in each sample: a car nosing out from
            # behind the fence at about walking-to-driving pace.
            return paint(base, 0, 5, 2 * (i - entered_at + 1), 14, 35)

        scene.run(8, arriving)
        self.assertEqual(len(scene.triggers), 1, scene.verdicts[-8:])
        index, observation = scene.triggers[0]
        self.assertLessEqual(index - entered_at, 3, "four samples is one second at 4 fps")
        self.assertEqual(observation.light, LIGHT_DAY)
        self.assertGreaterEqual(observation.features["blob_fraction"], 0.06)
        self.assertGreaterEqual(observation.features["persistence"], 2)
        self.assertGreater(observation.features["track_dx"], 0, "it travelled along the lane")

    def test_a_bright_vehicle_on_a_dark_drive_triggers_too(self):
        base = textured(low=40, high=90)
        scene = Scene().settle(base)
        start = scene.index
        scene.run(8, lambda i: paint(base, 0, 4, 2 * (i - start + 1), 13, 230))
        self.assertEqual(len(scene.triggers), 1)

    def test_swaying_foliage_and_its_shadows_never_trigger(self):
        base = textured()
        rng = random.Random(11)
        # The hedge along the top of the patch and its shadow across the
        # bottom corner: a fifth of the cells, each moving on its own.
        leaves = [y * W + x for y in range(0, 3) for x in range(W)]
        leaves += [y * W + x for y in range(12, 16) for x in range(16, 24)]

        def windy(_i):
            cells = list(base)
            for cell in leaves:
                cells[cell] = clamp(base[cell] * rng.uniform(0.65, 1.35))
            return cells

        self.assertEqual(Scene().run(int(300 * FPS), windy).triggers, [])

    def test_a_gust_that_moves_the_whole_hedge_a_little_never_triggers(self):
        base = textured()

        def gusty(i):
            gain = 0.87 if (i // 4) % 5 == 0 else 1.0   # a second in every five
            return [clamp(value * gain) if cell < 3 * W else value
                    for cell, value in enumerate(base)]

        self.assertEqual(Scene().run(int(300 * FPS), gusty).triggers, [])

    def test_a_vehicle_is_still_seen_through_the_foliage(self):
        base = textured()
        rng = random.Random(12)
        leaves = [y * W + x for y in range(0, 3) for x in range(W)]

        def windy(cells):
            cells = list(cells)
            for cell in leaves:
                cells[cell] = clamp(cells[cell] * rng.uniform(0.7, 1.3))
            return cells

        scene = Scene().run(int(60 * FPS), lambda _i: windy(base))
        start = scene.index
        scene.run(8, lambda i: windy(paint(base, 0, 5, 2 * (i - start + 1), 14, 35)))
        self.assertEqual(len(scene.triggers), 1)

    def test_a_gradual_change_of_light_never_triggers(self):
        base = textured()
        frames = int(240 * FPS)

        def dusk(i):
            gain = 1.0 - 0.6 * i / frames
            return [clamp(value * gain) for value in base]

        self.assertEqual(Scene().run(frames, dusk).triggers, [])

    def test_the_camera_stepping_its_exposure_never_triggers(self):
        base = textured()
        scene = Scene().settle(base)
        for gain in (1.45, 0.7, 1.3, 0.55):
            stepped = [clamp(value * gain) for value in base]
            scene.run(int(10 * FPS), lambda _i, stepped=stepped: stepped)
        self.assertEqual(scene.triggers, [])
        self.assertIn("global", scene.verdicts)

    def test_a_vehicle_right_after_an_exposure_step_is_still_seen(self):
        base = textured()
        scene = Scene().settle(base)
        stepped = [clamp(value * 1.4) for value in base]
        scene.run(int(4 * FPS), lambda _i: stepped)
        start = scene.index
        scene.run(8, lambda i: paint(stepped, 0, 5, 2 * (i - start + 1), 14, 30))
        self.assertEqual(len(scene.triggers), 1)

    def test_the_sun_coming_out_through_the_trees_never_triggers(self):
        base = textured()
        rng = random.Random(5)
        # Dappled light: most of the patch brightens at once, in patches.
        lit = {cell for cell in range(W * H) if rng.random() < 0.55}
        sunny = [clamp(value * (1.6 if cell in lit else 1.05)) for cell, value in enumerate(base)]
        scene = Scene().settle(base)
        scene.run(int(20 * FPS), lambda _i: sunny)
        scene.run(int(20 * FPS), lambda _i: base)
        self.assertEqual(scene.triggers, [])

    def test_a_block_of_shade_arriving_all_at_once_is_not_a_vehicle_entering(self):
        base = textured()
        shaded = paint(base, 0, 0, 12, 16, 40)  # half the patch, in one sample
        scene = Scene().settle(base).run(int(10 * FPS), lambda _i: shaded)
        self.assertEqual(scene.triggers, [])
        self.assertIn("sudden", scene.verdicts)

    def test_rain_never_triggers(self):
        base = textured()
        rng = random.Random(9)

        def raining(_i):
            cells = [clamp(value * rng.uniform(0.94, 1.06)) for value in base]
            for _ in range(4):  # streaks caught in a cell
                cells[rng.randrange(W * H)] = clamp(rng.uniform(150, 230))
            return cells

        self.assertEqual(Scene().run(int(300 * FPS), raining).triggers, [])

    def test_a_car_that_parks_in_the_patch_triggers_once_and_never_again(self):
        base = textured()
        scene = Scene().settle(base)
        start = scene.index

        def arrives_and_stays(i):
            return paint(base, 0, 5, min(12, 2 * (i - start + 1)), 14, 35)

        scene.run(int(15 * 60 * FPS), arrives_and_stays)
        self.assertEqual(len(scene.triggers), 1, "one appearance, one would-trigger")
        # It has been absorbed into the scene by now, so the detector is armed
        # again -- and says nothing, because nothing is new.
        self.assertEqual(scene.verdicts[-1], "clear")
        # When it finally drives off, the hole it leaves is one more event at
        # most, not a stream of them.
        scene.run(int(5 * 60 * FPS), lambda _i: base)
        self.assertLessEqual(len(scene.triggers), 2)

    def test_nothing_triggers_while_warming_up(self):
        base = textured()
        scene = Scene()
        scene.run(8, lambda i: paint(base, 0, 5, 2 * (i + 1), 14, 35))
        self.assertEqual(scene.triggers, [])
        self.assertEqual(set(scene.verdicts), {"warming"})

    def test_a_second_vehicle_inside_the_refractory_gap_does_not_trigger_again(self):
        base = textured()
        scene = Scene().settle(base)
        for _ in range(2):
            start = scene.index
            scene.run(8, lambda i, start=start: paint(base, 0, 5, 2 * (i - start + 1), 14, 35))
            scene.run(int(4 * FPS), lambda _i: base)
        self.assertEqual(len(scene.triggers), 1)


class NightDetectorTest(unittest.TestCase):
    def test_total_darkness_is_night_and_never_triggers(self):
        rng = random.Random(2)
        scene = Scene().run(
            int(600 * FPS), lambda _i: [rng.randint(0, 4) for _ in range(W * H)],
        )
        self.assertEqual(scene.triggers, [])
        self.assertEqual(scene.detector.light, LIGHT_NIGHT)

    def test_headlights_that_appear_persist_and_grow_trigger(self):
        base = dark()
        scene = Scene().settle(base)
        start = scene.index

        def arriving(i):
            step = i - start
            # The lamps come into view, bloom, and move a little along the
            # lane; the whole patch lifts a few levels with the spill.
            cells = [value + 8 for value in base]
            size = 2 + step // 2
            left = 2 + step // 2
            return paint(cells, left, 7, left + size, 7 + size, 235)

        scene.run(10, arriving)
        self.assertEqual(len(scene.triggers), 1, scene.verdicts[-10:])
        index, observation = scene.triggers[0]
        self.assertEqual(observation.light, LIGHT_NIGHT)
        self.assertLessEqual(index - start, 4)
        self.assertGreaterEqual(observation.features["persistence"], 4)
        self.assertGreaterEqual(observation.features["peak"], 200)
        self.assertGreaterEqual(observation.features["growth"], 1.0)

    def test_a_passing_cars_beam_sweeping_across_the_patch_does_not_trigger(self):
        base = dark()
        scene = Scene().settle(base)
        for _ in range(20):
            start = scene.index
            # A bright bar crossing the whole patch in under a second.
            scene.run(4, lambda i, start=start: paint(
                [value + 12 for value in base], 6 * (i - start), 0, 6 * (i - start) + 3, H, 225))
            scene.run(int(15 * FPS), lambda _i: base)
        self.assertEqual(scene.triggers, [])
        self.assertIn("sweeping", scene.verdicts)

    def test_without_the_speed_gate_that_beam_would_have_triggered(self):
        base = dark()
        scene = Scene(DetectorConfig(fps=FPS, night_max_speed=50.0)).settle(base)
        start = scene.index
        scene.run(4, lambda i: paint(
            [value + 12 for value in base], 6 * (i - start), 0, 6 * (i - start) + 3, H, 225))
        self.assertEqual(len(scene.triggers), 1, "so the test above is testing the gate")

    def test_a_passing_car_lighting_the_whole_scene_diffusely_does_not_trigger(self):
        base = dark()
        scene = Scene().settle(base)
        for lift in (25, 60, 110):
            scene.run(3, lambda _i, lift=lift: [value + lift for value in base])
            scene.run(int(10 * FPS), lambda _i: base)
        self.assertEqual(scene.triggers, [])

    def test_a_flash_shorter_than_the_persistence_does_not_trigger(self):
        base = dark()
        scene = Scene().settle(base)
        scene.run(2, lambda _i: paint(base, 4, 6, 8, 10, 240))
        scene.run(int(10 * FPS), lambda _i: base)
        self.assertEqual(scene.triggers, [])

    def test_a_source_that_is_fading_away_does_not_trigger(self):
        base = dark()
        scene = Scene().settle(base)
        start = scene.index
        scene.run(6, lambda i: paint(base, 4, 5, 4 + max(1, 6 - 2 * (i - start)), 11, 230))
        self.assertEqual(scene.triggers, [])

    def test_light_coming_on_with_no_bright_source_in_it_is_the_scene_changing(self):
        base = dark()
        lit = textured(low=60, high=140)
        scene = Scene().settle(base).run(int(60 * FPS), lambda _i: lit)
        self.assertEqual(scene.triggers, [])
        self.assertEqual(scene.detector.light, LIGHT_DAY, "and it is judged as day from here")

    def test_night_thresholds_are_separate_from_day_ones(self):
        base = dark()
        scene = Scene(DetectorConfig(fps=FPS, night_persistence=8)).settle(base)
        scene.run(6, lambda _i: paint(base, 4, 6, 8, 10, 240))
        self.assertEqual(scene.triggers, [], "six samples is not eight")
        scene.run(4, lambda _i: paint(base, 4, 6, 8, 10, 240))
        self.assertEqual(len(scene.triggers), 1)


class RealFrameTest(unittest.TestCase):
    """The default patch cut from real frames of this camera, as the worker sees it.

    Three frames of the empty drive by day, ten seconds apart, taken from the
    sub stream on 2026-09-21 at 06:27 UTC: real gravel, real fence, real trees
    moving, real sensor noise. There is no real frame of a vehicle *entering*
    the re-aimed view yet -- the camera's alarm has so far come after the car
    filled the patch -- so the vehicle here is painted onto the real drive.
    """

    FIXTURES = Path(__file__).parent / "fixtures" / "early_trigger"

    def grid(self, name):
        from PIL import Image

        from gate_controller.early_trigger import grid_from_frame

        with Image.open(self.FIXTURES / name) as image:
            image = image.convert("L")
            return grid_from_frame(image.tobytes(), image.size)

    def frames(self):
        return [self.grid(f"day-empty-{index}.png") for index in (1, 2, 3)]

    def test_the_real_empty_drive_by_day_never_triggers(self):
        frames = self.frames()
        self.assertGreater(sum(frames[0]) / len(frames[0]), 60, "daylight")
        scene = Scene().run(int(300 * FPS), lambda i: frames[(i // 8) % 3])
        self.assertEqual(scene.triggers, [])
        self.assertEqual(scene.detector.light, LIGHT_DAY)
        self.assertNotIn("candidate", scene.verdicts[40:], "not even a candidate")

    def test_a_vehicle_nosing_onto_the_real_drive_triggers_within_a_second(self):
        frames = self.frames()
        scene = Scene().run(int(30 * FPS), lambda i: frames[(i // 8) % 3])
        start = scene.index

        def arriving(i):
            base = frames[(i // 8) % 3]
            return paint(base, 1, 3, 1 + 3 * (i - start + 1), 13, 45)

        scene.run(6, arriving)
        self.assertEqual(len(scene.triggers), 1, scene.verdicts[-6:])
        self.assertLessEqual(scene.triggers[0][0] - start, 3)

    def test_a_pale_vehicle_against_the_pale_gravel_is_the_hard_case_and_is_still_seen(self):
        frames = self.frames()
        gravel = sorted(frames[0])[len(frames[0]) // 2]
        scene = Scene().run(int(30 * FPS), lambda i: frames[(i // 8) % 3])
        start = scene.index
        body = clamp(gravel * 1.35)   # a silver car, a third brighter than the drive
        scene.run(6, lambda i: paint(frames[(i // 8) % 3], 1, 3, 1 + 3 * (i - start + 1), 13, body))
        self.assertEqual(len(scene.triggers), 1)


class DetectorRecordTest(unittest.TestCase):
    def test_every_sample_carries_the_evidence_numbers(self):
        scene = Scene().settle(textured(), seconds=1)
        features = scene.detector.last.features
        for name in ("light", "mean_luma", "bg_luma", "luma_jump", "peak", "shift",
                     "changed_fraction", "blob_fraction", "scatter", "cx", "cy",
                     "scene_difference", "stillness", "persistence", "verdict"):
            self.assertIn(name, features)

    def test_a_wrongly_sized_sample_is_refused(self):
        with self.assertRaises(ValueError):
            PatchDetector().observe([0] * 10, 0.0)


if __name__ == "__main__":
    unittest.main()
