import unittest

from gate_controller.plate_region import PlateRegion, parse_plate_region


class PlateRegionTests(unittest.TestCase):
    def test_parses_fractions_and_treats_empty_or_full_frame_as_none(self):
        region = parse_plate_region(" 0.05, 0.4, 0.9, 0.6 ")
        self.assertEqual(region, PlateRegion(0.05, 0.4, 0.9, 0.6))
        self.assertIsNone(parse_plate_region(None))
        self.assertIsNone(parse_plate_region(""))
        self.assertIsNone(parse_plate_region("0,0,1,1"))

    def test_rejects_malformed_or_out_of_frame_regions(self):
        for value in ("0.1,0.2,0.3", "a,b,c,d", "0.5,0,0.6,1", "0,0.5,1,0.6", "1,0,0.5,0.5",
                      "0,0,0.05,1", "0,0,1,0.05", "nan,0,1,1", "inf,0,1,1", "-0.1,0,1,1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_plate_region(value)

    def test_pixel_box_is_even_aligned_and_inside_the_frame(self):
        region = PlateRegion(0.05, 0.4, 0.9, 0.6)
        self.assertEqual(region.pixel_box(3840, 2160), (192, 864, 3648, 2160))
        left, top, right, bottom = region.pixel_box(2559, 1439)
        for edge in (left, top, right):
            self.assertEqual(edge % 2, 0)
        self.assertLessEqual(right, 2559)
        self.assertLessEqual(bottom, 1439)

    def test_ffmpeg_crop_uses_input_size_expressions(self):
        self.assertEqual(
            PlateRegion(0.05, 0.4, 0.9, 0.6).ffmpeg_crop_filter(),
            "crop=trunc(iw*0.9000/2)*2:trunc(ih*0.6000/2)*2:trunc(iw*0.0500/2)*2:trunc(ih*0.4000/2)*2",
        )

    def test_maps_a_box_inside_the_region_back_to_frame_fractions(self):
        region = PlateRegion(0.1, 0.4, 0.8, 0.6)
        x, y, w, h = region.to_frame((0.5, 0.5, 0.1, 0.1))
        self.assertAlmostEqual(x, 0.5)
        self.assertAlmostEqual(y, 0.7)
        self.assertAlmostEqual(w, 0.08)
        self.assertAlmostEqual(h, 0.06)
        self.assertEqual(region.as_env(), "0.1,0.4,0.8,0.6")
