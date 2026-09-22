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
        # The source lights the ground around it (+8): until the first shadow
        # night this fixture had a lamp on a black patch, which is exactly the
        # point-source false trigger of 2026-09-22 04:29 and no longer triggers.
        base = dark()
        lit = [value + 8 for value in base]
        scene = Scene(DetectorConfig(fps=FPS, night_persistence=8)).settle(base)
        scene.run(6, lambda _i: paint(lit, 4, 6, 8, 10, 240))
        self.assertEqual(scene.triggers, [], "six samples is not eight")
        scene.run(4, lambda _i: paint(lit, 4, 6, 8, 10, 240))
        self.assertEqual(len(scene.triggers), 1)

    def test_a_bright_point_that_lights_nothing_around_it_does_not_trigger(self):
        """Eyeshine, a droplet on the dome, a distant lamp: a point, and black all round it."""
        base = dark()
        scene = Scene().settle(base)
        scene.run(int(30 * FPS), lambda _i: paint(base, 5, 5, 7, 6, 181))   # two cells, as recorded
        self.assertEqual(scene.triggers, [])
        scene.run(int(30 * FPS), lambda _i: paint(base, 5, 5, 8, 7, 235))   # six cells, still no spill
        self.assertEqual(scene.triggers, [])
        self.assertIn("unlit", scene.verdicts)
        # The same six cells with the ground lit around them are headlamps.
        scene = Scene().settle(base)
        scene.run(int(5 * FPS), lambda _i: paint([value + 8 for value in base], 5, 5, 8, 7, 235))
        self.assertEqual(len(scene.triggers), 1)

    def test_the_spill_may_arrive_a_sample_after_the_lamps_and_still_count(self):
        base = dark()
        scene = Scene().settle(base)
        start = scene.index

        def arriving(i):
            lift = 8 if i - start >= 4 else 0
            return paint([value + lift for value in base], 4, 6, 8, 10, 240)

        scene.run(8, arriving)
        self.assertEqual(len(scene.triggers), 1, scene.verdicts[-8:])
        self.assertIn("unlit", scene.verdicts[-8:], "and the run was kept, not re-based")


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


class ShadowDayTest(unittest.TestCase):
    """The first shadow day's own pictures, 2026-09-22, through the rules as shipped.

    Each fixture is the worker's before/after pair (320x90 grey JPEG, the
    patch two seconds apart) saved beside the row it made. Seven are the
    morning's false day triggers: an empty gravel lane under cloud shade
    coming and going on a gusty morning, every one a would-trigger of the
    vision rule and every one cleared by both second looks. Two are the day's
    one real arrival: the car's front, tiny at the far fence gap, at the
    instant of the camera's first alarm (a candidate, persistence 1), and its
    flank filling the patch 26 s later (a would-trigger, 0.1 s after the
    second alarm). One is the night's point source at 04:29 UTC.

    A pair replayed cold -- the before frame as the whole background, then
    the after frame -- is not the live run: the background then had been
    adapting through the shade's advance for two seconds. Three of the seven
    shade pairs re-make their blob this way and are refused as light; the
    other four make too small a blob to be a candidate at all, so all seven
    are also run with the area floor taken away, to be sure the *illumination*
    rule, not the area, is what stands between them and a trigger.
    """

    FIXTURES = Path(__file__).parent / "fixtures" / "early_trigger"
    SHADE = tuple(f"day-shade-{index}" for index in range(1, 8))

    def pair(self, stem):
        from PIL import Image

        from gate_controller.early_trigger import grid_from_frame

        path = next(self.FIXTURES.glob(f"{stem}*.jpg"))
        with Image.open(path) as image:
            image = image.convert("L")
            width, height = image.size
            before = image.crop((0, 0, width // 2, height))
            after = image.crop((width // 2, 0, width, height))
            return (grid_from_frame(before.tobytes(), before.size),
                    grid_from_frame(after.tobytes(), after.size))

    def replay(self, stem, config=None, after_samples=8):
        before, after = self.pair(stem)
        scene = Scene(config).settle(before)
        scene.run(after_samples, lambda _i: after)
        return scene

    def test_none_of_the_seven_shade_pairs_triggers(self):
        for stem in self.SHADE:
            scene = self.replay(stem)
            self.assertEqual(scene.triggers, [], stem)
            self.assertEqual(scene.detector.light, LIGHT_DAY, stem)

    def refusal(self, stem):
        """The sample at which a pair, replayed with no area floor, is refused as light."""
        before, after = self.pair(stem)
        detector = PatchDetector(DetectorConfig(fps=FPS, day_min_area=0.002))
        now = 0.0
        for _ in range(int(12 * FPS)):
            detector.observe(before, now)
            now += STEP
        for _ in range(8):
            observation = detector.observe(after, now)
            now += STEP
            self.assertNotEqual(observation.verdict, "trigger", stem)
            if observation.verdict == "illumination":
                return observation
        self.fail(f"{stem}: never refused as light: {observation.verdict}")

    def test_every_shade_pair_is_refused_as_light_not_merely_too_small(self):
        # Replayed cold, four of the seven re-make less of their blob than the
        # area floor asks for (one of them a single cell), so the floor is
        # taken away here: whatever changed, the rule has to call it light.
        for stem in self.SHADE:
            self.assertEqual(self.refusal(stem).verdict, "illumination", stem)

    def test_the_shade_is_uniform_and_one_signed_inside_its_blob(self):
        """The numbers the rule rests on: spread 0.00-0.17, contrast 0.22-0.40, nothing mixed."""
        for stem in self.SHADE:
            features = self.refusal(stem).features
            self.assertLess(features["blob_spread"], 0.20, stem)
            self.assertLess(abs(features["blob_contrast"]), 0.45, stem)
            self.assertGreater(abs(features["blob_contrast"]), 0.18, stem)
            self.assertEqual(features["blob_mixed"], 0.0, stem)

    def test_the_cars_flank_filling_the_patch_triggers(self):
        scene = self.replay("day-vehicle-flank")
        self.assertEqual(len(scene.triggers), 1, scene.verdicts[-8:])
        features = scene.triggers[0][1].features
        self.assertGreater(features["blob_spread"], 0.45, "a vehicle brings its own texture")
        self.assertGreater(features["blob_mixed"], 0.3, "light and dark bodywork, both")
        self.assertEqual(features["peak"], 255, "white bodywork clips: a clipped frame is not a nuisance")

    def test_the_cars_front_at_the_far_fence_gap_is_an_object_not_light(self):
        scene = self.replay("day-vehicle-front")
        self.assertEqual(len(scene.triggers), 1, scene.verdicts[-8:])
        features = scene.triggers[0][1].features
        self.assertLess(features["blob_contrast"], -0.6, "a dark car against sunlit gravel")
        self.assertGreater(features["blob_spread"], 0.45)
        self.assertNotIn("illumination", scene.verdicts)

    def test_the_night_point_source_does_not_trigger(self):
        before, after = self.pair("night-point-source")
        self.assertLess(sum(before) / len(before), 28, "the patch was black")
        scene = Scene().settle(before).run(int(30 * FPS), lambda _i: after)
        self.assertEqual(scene.triggers, [])
        self.assertEqual(scene.detector.light, LIGHT_NIGHT)
        # With the floors of the first shadow night (two cells, no spill
        # asked for) the same pictures do not trigger cold either: the
        # recorded row had persistence 4 over a run the live background had
        # not absorbed. The rule is asserted on the row's own numbers instead.
        features = {"blob_cells": 2, "shift": 0.0, "peak": 181}
        config = DetectorConfig()
        self.assertLess(features["blob_cells"], config.night_min_cells)
        self.assertLess(features["shift"], config.night_min_spill)


class DetectorRecordTest(unittest.TestCase):
    def test_every_sample_carries_the_evidence_numbers(self):
        scene = Scene().settle(textured(), seconds=1)
        features = scene.detector.last.features
        for name in ("light", "mean_luma", "bg_luma", "luma_jump", "peak", "shift",
                     "changed_fraction", "blob_fraction", "scatter", "cx", "cy",
                     "blob_contrast", "blob_spread", "blob_mixed",
                     "scene_difference", "stillness", "persistence", "verdict"):
            self.assertIn(name, features)

    def test_the_synthetic_vehicles_are_objects_by_the_illumination_rule(self):
        """The fixtures paint a flat body: uniform, so it is the contrast that makes it an object."""
        base = textured()
        scene = Scene().settle(base)
        start = scene.index
        scene.run(8, lambda i: paint(base, 0, 5, 2 * (i - start + 1), 14, 35))
        features = scene.triggers[0][1].features
        self.assertGreater(abs(features["blob_contrast"]), DetectorConfig().day_light_max_contrast)
        self.assertNotIn("illumination", scene.verdicts)

    def test_shade_of_the_measured_depth_over_a_textured_drive_is_refused_as_light(self):
        """Synthetic, from the measured numbers: the gravel 30% darker, texture kept."""
        base = textured()
        scene = Scene().settle(base)
        start = scene.index

        def shade_arriving(i):
            columns = 2 * (i - start + 1)
            return [clamp(value * 0.7) if (cell % W) < columns and cell >= 5 * W else value
                    for cell, value in enumerate(base)]

        scene.run(8, shade_arriving)
        self.assertEqual(scene.triggers, [], scene.verdicts[-8:])
        self.assertIn("illumination", scene.verdicts)

    def test_the_illumination_floors_are_settable(self):
        from gate_controller.early_trigger import load_config

        config = load_config({"GATE_EARLY_TRIGGER_DAY_LIGHT_MAX_SPREAD": "0.2",
                              "GATE_EARLY_TRIGGER_DAY_LIGHT_MAX_CONTRAST": "0.7",
                              "GATE_EARLY_TRIGGER_NIGHT_MIN_SPILL": "5",
                              "GATE_EARLY_TRIGGER_NIGHT_MIN_CELLS": "6"}).detector
        self.assertEqual((config.day_light_max_spread, config.day_light_max_contrast), (0.2, 0.7))
        self.assertEqual((config.night_min_spill, config.night_min_cells), (5.0, 6))

    def test_a_wrongly_sized_sample_is_refused(self):
        with self.assertRaises(ValueError):
            PatchDetector().observe([0] * 10, 0.0)


if __name__ == "__main__":
    unittest.main()
