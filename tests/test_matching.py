import unittest

from gate_controller.matching import decide_access, normalise_plate
from gate_controller.models import PlateObservation


class MatchingTests(unittest.TestCase):
    def test_normalise_plate_removes_spacing_and_punctuation(self):
        self.assertEqual(normalise_plate("  12-d  3456 "), "12D3456")

    def test_rejects_a_low_confidence_exact_authorised_plate(self):
        decision = decide_access(
            [PlateObservation("12-D 3456", 0.42)], {"12D3456"}
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")

    def test_allows_a_minimum_confidence_exact_authorised_plate(self):
        decision = decide_access(
            [PlateObservation("12-D 3456", 0.90)], {"12D3456"}
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")
        self.assertEqual(decision.authorised_plate, "12D3456")

    def test_rejects_a_plate_that_only_contains_an_authorised_plate(self):
        decision = decide_access(
            [PlateObservation("12D34567", 0.99)], {"12D3456"}
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")

    def test_rejects_a_fuzzy_match_with_multiple_authorised_candidates(self):
        decision = decide_access(
            [
                PlateObservation("12I3456", 0.96),
                PlateObservation("12I3456", 0.97),
            ],
            {"1213456", "12L3456"},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ambiguous_fuzzy_match")

    def test_allows_two_high_confidence_frames_with_one_known_ocr_confusion(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "two_frame_ocr_confusion")
        self.assertEqual(decision.authorised_plate, "1203456")


if __name__ == "__main__":
    unittest.main()


class DeniedObservationTests(unittest.TestCase):
    def test_no_match_reports_the_best_read_plate_for_review(self):
        decision = decide_access(
            [PlateObservation("99-X 9999", 0.61), PlateObservation("99-X 9998", 0.88)],
            {"12D3456"},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")
        self.assertEqual(decision.observed_plate, "99X9998")
        self.assertEqual(decision.confidence, 0.88)
        self.assertIsNone(decision.authorised_plate)

    def test_no_match_without_any_plate_reports_nothing(self):
        decision = decide_access([PlateObservation(None, 0.0)], {"12D3456"})

        self.assertEqual(decision.reason, "no_match")
        self.assertIsNone(decision.observed_plate)
        self.assertEqual(decision.confidence, 0.0)

    def test_no_match_skips_a_read_that_normalises_to_nothing(self):
        decision = decide_access(
            [PlateObservation("---", 0.99), PlateObservation("99-X 9998", 0.70)],
            {"12D3456"},
        )

        self.assertEqual(decision.reason, "no_match")
        self.assertEqual(decision.observed_plate, "99X9998")
        self.assertEqual(decision.confidence, 0.70)
