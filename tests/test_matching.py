import unittest
from datetime import datetime, timezone

from gate_controller.match_policy import (
    LEVEL_STANDARD, LEVEL_STRICT, MINUTES_PER_DAY,
    RECOMMENDED_POLICY,
    Band, MatchPolicy, safe_policy,
)
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


class LevelledMatchingTests(unittest.TestCase):
    """Fuzziness is chosen by the band in force, and defaults to today's rule."""

    DAYTIME = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)   # 14:00 Dublin
    NIGHT = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)     # 02:00 Dublin

    def test_no_policy_reproduces_the_shipped_fuzzy_behaviour(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "two_frame_ocr_confusion")
        self.assertEqual(decision.policy_level, LEVEL_STANDARD)
        self.assertEqual(decision.policy_band, "00:00-24:00")

    def test_the_daytime_band_keeps_the_confusion_match(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
            RECOMMENDED_POLICY,
            now=self.DAYTIME,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.policy_band, "08:00-22:00")
        self.assertEqual(decision.policy_level, LEVEL_STANDARD)
        self.assertEqual(decision.match_rule, "ocr_confusion")
        self.assertEqual(decision.edit_distance, 1)
        self.assertEqual(decision.policy_local_time, "14:00")

    def test_the_overnight_band_denies_the_same_read(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
            RECOMMENDED_POLICY,
            now=self.NIGHT,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")
        self.assertEqual(decision.policy_level, LEVEL_STRICT)
        self.assertEqual(decision.policy_band, "22:00-08:00")

    def test_an_exact_plate_still_opens_overnight(self):
        decision = decide_access(
            [PlateObservation("1203456", 0.96)], {"1203456"},
            RECOMMENDED_POLICY, now=self.NIGHT,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")
        self.assertEqual(decision.match_rule, "exact")
        self.assertEqual(decision.edit_distance, 0)

    def test_a_denial_reports_the_nearest_authorised_plate(self):
        decision = decide_access(
            [
                PlateObservation("12O3456", 0.96),
                PlateObservation("12O3456", 0.97),
            ],
            {"1203456"},
            RECOMMENDED_POLICY,
            now=self.NIGHT,
        )

        self.assertEqual(decision.near_miss_plate, "1203456")
        self.assertEqual(decision.near_miss_distance, 1)

    def test_a_denial_with_nothing_close_reports_no_near_miss(self):
        decision = decide_access(
            [PlateObservation("99X9999", 0.96)], {"1203456"},
            RECOMMENDED_POLICY, now=self.NIGHT,
        )

        self.assertIsNone(decision.near_miss_plate)
        self.assertIsNone(decision.near_miss_distance)


class WithdrawnRelaxedLevelTests(unittest.TestCase):
    """`relaxed` was withdrawn before release; nothing may reach it.

    Measured against this matching code with `131-D-2696` as the only
    authorised plate, the two-edit rule returned *allowed* for 101 other
    syntactically valid Irish registrations one edit away and 3,360 two edits
    away — 3,461 in all, including `131-D-2695`, `132-D-2696`, `141-D-2696`
    and `131-C-2696`. An Irish registration puts the year and county in its
    first three or four characters, so a neighbouring plate is one edit away
    by construction. The two-frame rule guards against OCR noise, not against
    a correctly read stranger.
    """

    NEIGHBOURS = ("131D2695", "132D2696", "141D2696", "131C2696", "13D2696")

    def _decide(self, plate, authorised, policy, confidence=0.96):
        return decide_access(
            [PlateObservation(plate, confidence),
             PlateObservation(plate, confidence)],
            authorised, policy,
        )

    def test_a_band_naming_relaxed_matches_like_strict(self):
        policy = safe_policy({
            "bands": [{"start": "00:00", "end": "24:00", "level": "relaxed"}],
        })

        for plate in self.NEIGHBOURS:
            with self.subTest(plate=plate):
                decision = self._decide(plate, {"131D2696"}, policy)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.policy_level, LEVEL_STRICT)

    def test_a_band_constructed_around_the_validator_still_matches_like_strict(self):
        policy = MatchPolicy(bands=(Band(0, MINUTES_PER_DAY, "relaxed"),))

        for plate in self.NEIGHBOURS:
            with self.subTest(plate=plate):
                self.assertFalse(self._decide(plate, {"131D2696"}, policy).allowed)

    def test_the_exact_plate_still_opens_under_a_relaxed_band(self):
        policy = safe_policy({
            "bands": [{"start": "00:00", "end": "24:00", "level": "relaxed"}],
        })

        decision = decide_access(
            [PlateObservation("131-D-2696", 0.96)], {"131D2696"}, policy
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")
