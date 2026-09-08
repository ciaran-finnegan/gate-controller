import unittest
from datetime import datetime, timezone

from gate_controller.match_policy import (
    LEGACY_MIN_CONFIDENCE, LEVEL_STANDARD, LEVEL_STRICT, LEVELS,
    MINUTES_PER_DAY, RECOMMENDED_POLICY,
    Band, MatchPolicy, apply_confidence, safe_policy,
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


def _standard_policy():
    return MatchPolicy(bands=(Band(0, MINUTES_PER_DAY, LEVEL_STANDARD),))


def _strict_policy():
    return MatchPolicy(bands=(Band(0, MINUTES_PER_DAY, LEVEL_STRICT),))


def _cloud(plate, confidence):
    return PlateObservation(plate, confidence, source="cloud")


def _local(plate, confidence):
    return PlateObservation(plate, confidence, source="local")


class LevelledConfidenceTests(unittest.TestCase):
    """Requirement (e): the confidence bars belong to the level, not the module."""

    def setUp(self):
        self.addCleanup(apply_confidence, {})
        apply_confidence({})

    def test_the_daytime_bar_admits_the_cloud_read_that_was_turned_away(self):
        # 2026-09-08 11:13:48, source=cloud, 10CE1990 at 0.806 against the
        # hardcoded 0.90. `standard` now asks 0.75 of an exact match.
        decision = decide_access(
            [_cloud("10CE1990", 0.806)], {"10CE1990"}, _standard_policy(),
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")

    def test_the_overnight_bar_is_unchanged_by_this_release(self):
        strict = _strict_policy()
        self.assertFalse(
            decide_access([_cloud("10CE1990", 0.806)], {"10CE1990"}, strict).allowed
        )
        self.assertTrue(
            decide_access([_cloud("10CE1990", 0.90)], {"10CE1990"}, strict).allowed
        )

    def test_a_fuzzy_match_keeps_a_higher_bar_than_an_exact_one(self):
        # 11WH2S71 is one known S/5 confusion from 11WH2571: not the
        # authorised plate, so it is held to more than an exact read is.
        policy = _standard_policy()
        near = [_cloud("11WH2S71", 0.80), _cloud("11WH2S71", 0.80)]
        self.assertFalse(decide_access(near, {"11WH2571"}, policy).allowed)
        confident = [_cloud("11WH2S71", 0.86), _cloud("11WH2S71", 0.86)]
        self.assertTrue(decide_access(confident, {"11WH2571"}, policy).allowed)

    def test_each_level_takes_its_bar_from_its_own_environment_key(self):
        apply_confidence({
            "GATE_MATCH_MIN_CONFIDENCE_STANDARD": "0.40",
            "GATE_MATCH_MIN_CONFIDENCE_STRICT": "0.99",
        })

        self.assertTrue(
            decide_access([_cloud("12D3456", 0.41)], {"12D3456"},
                          _standard_policy()).allowed
        )
        self.assertFalse(
            decide_access([_cloud("12D3456", 0.98)], {"12D3456"},
                          _strict_policy()).allowed
        )

    def test_a_bar_no_read_can_fail_is_refused_like_any_other_bad_value(self):
        # `0` opened the gate on a 0.0-confidence exact read, and `1e-9` is
        # the same thing with a decimal point in it.
        for value in ("0", "0.0", "1e-9", "0.09"):
            with self.subTest(value=value):
                with self.assertLogs(
                    "gate_controller.match_policy", level="WARNING",
                ) as logs:
                    apply_confidence({"GATE_MATCH_MIN_CONFIDENCE_STANDARD": value})
                self.assertEqual(
                    LEVELS[LEVEL_STANDARD].min_exact_confidence,
                    LEGACY_MIN_CONFIDENCE,
                    f"{value!r} is a disabled bar, not a posture",
                )
                self.assertIn("status=rejected", "\n".join(logs.output))
                self.assertFalse(
                    decide_access([_cloud("12D3456", 0.0)], {"12D3456"},
                                  _standard_policy()).allowed,
                    "a 0.0-confidence exact read must never open the gate",
                )

    def test_the_floor_itself_is_still_a_usable_bar(self):
        apply_confidence({"GATE_MATCH_MIN_CONFIDENCE_STANDARD": "0.10"})

        self.assertEqual(LEVELS[LEVEL_STANDARD].min_exact_confidence, 0.10)

    def test_an_unusable_value_falls_closed_to_the_bar_that_shipped_before(self):
        for value in ("nan", "inf", "-0.5", "1.5", "banana", "0x3"):
            with self.assertLogs("gate_controller.match_policy", level="WARNING") as logs:
                apply_confidence({"GATE_MATCH_MIN_CONFIDENCE_STANDARD": value})
            self.assertEqual(
                LEVELS[LEVEL_STANDARD].min_exact_confidence,
                LEGACY_MIN_CONFIDENCE,
                f"{value!r} must not widen the gate",
            )
            self.assertIn("status=rejected", "\n".join(logs.output))

    def test_a_rejected_value_is_logged_once_per_load(self):
        with self.assertLogs("gate_controller.match_policy", level="WARNING") as logs:
            apply_confidence({"GATE_MATCH_MIN_CONFIDENCE_STANDARD": "nan"})

        rejections = [line for line in logs.output if "status=rejected" in line]
        self.assertEqual(len(rejections), 1)

    def test_an_environment_that_raises_is_read_as_unset(self):
        class Hostile:
            def get(self, key, default=None):
                raise RuntimeError("no environment here")

        apply_confidence(Hostile())

        self.assertEqual(LEVELS[LEVEL_STANDARD].min_exact_confidence, 0.75)


class AgreementRuleTests(unittest.TestCase):
    """Requirement (b): two readers, the same string, a lower bar on each."""

    def setUp(self):
        self.addCleanup(apply_confidence, {})
        apply_confidence({})

    def test_both_readers_below_their_own_bars_still_open_when_they_agree(self):
        decision = decide_access(
            [_cloud("10CE1990", 0.71)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("10CE1990", 0.566)],
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")
        self.assertEqual(decision.match_rule, "exact")
        self.assertEqual(decision.observed_plate, "10CE1990")
        self.assertEqual(decision.authorised_plate, "10CE1990")

    def test_the_measured_september_reads_are_admitted(self):
        # The exact pair from the 11:13:48 journal line.
        decision = decide_access(
            [_cloud("10CE1990", 0.806)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("10CE1990", 0.566)],
        )
        self.assertTrue(decision.allowed)

    def test_it_is_journalled_distinctly(self):
        with self.assertLogs("gate_controller.matching", level="INFO") as logs:
            decide_access(
                [_cloud("10CE1990", 0.71)], {"10CE1990"}, _standard_policy(),
                corroborations=[_local("10CE1990", 0.566)],
            )

        combined = "\n".join(logs.output)
        self.assertIn("gate_match stage=agreement_grant", combined)
        self.assertIn("plate=10CE1990", combined)
        self.assertIn("local_score=0.566", combined)
        self.assertIn("cloud_score=0.710", combined)

    def test_one_reader_agreeing_with_itself_is_not_agreement(self):
        decision = decide_access(
            [_cloud("10CE1990", 0.71), _cloud("10CE1990", 0.72)],
            {"10CE1990"}, _standard_policy(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")

    def test_two_readers_of_a_plate_that_is_not_authorised_are_denied(self):
        decision = decide_access(
            [_cloud("99XX9999", 0.99)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("99XX9999", 0.99)],
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")

    def test_a_reader_under_its_own_agreement_bar_does_not_corroborate(self):
        self.assertFalse(decide_access(
            [_cloud("10CE1990", 0.69)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("10CE1990", 0.99)],
        ).allowed)
        self.assertFalse(decide_access(
            [_cloud("10CE1990", 0.74)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("10CE1990", 0.49)],
        ).allowed)

    def test_readers_that_disagree_never_corroborate_each_other(self):
        decision = decide_access(
            [_cloud("10CE1991", 0.71)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("10CE1990", 0.99)],
        )

        self.assertFalse(decision.allowed)

    def test_a_non_finite_confidence_is_not_a_confidence(self):
        for confidence in (float("nan"), float("inf")):
            self.assertFalse(decide_access(
                [_cloud("10CE1990", 0.71)], {"10CE1990"}, _standard_policy(),
                corroborations=[_local("10CE1990", confidence)],
            ).allowed, f"{confidence!r} corroborated a read")

    def test_the_exact_rule_refuses_a_non_finite_confidence_too(self):
        """`inf >= bar` is true for every bar, including strict's 0.90."""
        for policy in (_standard_policy(), _strict_policy()):
            for confidence in (float("inf"), float("nan")):
                with self.subTest(level=policy.bands[0].level, value=confidence):
                    self.assertFalse(decide_access(
                        [_cloud("10CE1990", confidence)], {"10CE1990"}, policy,
                    ).allowed)

    def test_agreement_buys_no_extra_edit(self):
        # Two readers agreeing on a plate two edits away is still two edits.
        decision = decide_access(
            [_cloud("11WH2S7I", 0.99)], {"11WH2571"}, _standard_policy(),
            corroborations=[_local("11WH2S7I", 0.99)],
        )

        self.assertFalse(decision.allowed)

    def test_agreement_keeps_the_bands_frame_requirement_for_a_fuzzy_match(self):
        policy = _standard_policy()
        one_frame = decide_access(
            [_cloud("11WH2S71", 0.71)], {"11WH2571"}, policy,
            corroborations=[_local("11WH2S71", 0.99)],
        )
        self.assertFalse(
            one_frame.allowed,
            "a non-exact match still needs the two frames the band asks for",
        )

        two_frames = decide_access(
            [_cloud("11WH2S71", 0.71), _cloud("11WH2S71", 0.72)],
            {"11WH2571"}, policy,
            corroborations=[_local("11WH2S71", 0.99), _local("11WH2S71", 0.98)],
        )
        self.assertTrue(two_frames.allowed)
        self.assertEqual(two_frames.reason, "two_frame_ocr_confusion")
        self.assertEqual(two_frames.match_rule, "ocr_confusion")

    def test_the_frame_count_is_the_weaker_readers_own_count(self):
        """The band's two frames are asked of each reader, not of the pair.

        Counting the frames as the maximum over the two readers let a reader
        that saw the plate twice cover for one that saw it once, which is a
        one-reader fuzzy match taken at the agreement bars instead of the much
        higher fuzzy bar. Authorised ``10CE1990``; the reader sees the
        one-confusion string ``1OCE1990``.
        """
        policy = _standard_policy()

        # Frame 0: local only. Frame 1: both. The cloud saw it once, below
        # every bar the fuzzy rule asks for.
        cloud_saw_it_once = decide_access(
            [_cloud("1OCE1990", 0.71)], {"10CE1990"}, policy,
            corroborations=[_local("1OCE1990", 0.55), _local("1OCE1990", 0.55)],
        )
        self.assertFalse(
            cloud_saw_it_once.allowed,
            "one cloud frame is not the two frames standard asks for",
        )

        local_saw_it_once = decide_access(
            [_cloud("1OCE1990", 0.71), _cloud("1OCE1990", 0.72)],
            {"10CE1990"}, policy,
            corroborations=[_local("1OCE1990", 0.55)],
        )
        self.assertFalse(
            local_saw_it_once.allowed,
            "one local frame is not the two frames standard asks for",
        )

        both_saw_it_twice = decide_access(
            [_cloud("1OCE1990", 0.71), _cloud("1OCE1990", 0.72)],
            {"10CE1990"}, policy,
            corroborations=[_local("1OCE1990", 0.55), _local("1OCE1990", 0.56)],
        )
        self.assertTrue(
            both_saw_it_twice.allowed,
            "a genuine two-and-two agreement is what the rule is for",
        )
        self.assertEqual(both_saw_it_twice.reason, "two_frame_ocr_confusion")

    def test_the_must_open_cases_are_untouched_by_the_frame_count(self):
        """Every case the release was written to open still opens."""
        # 2026-09-08 11:13:48: exact agreement, one frame each.
        self.assertTrue(decide_access(
            [_cloud("10CE1990", 0.806)], {"10CE1990"}, _standard_policy(),
            corroborations=[_local("10CE1990", 0.566)],
        ).allowed)
        # The cloud alone, at the daytime exact bar.
        self.assertTrue(decide_access(
            [_cloud("10CE1990", 0.806)], {"10CE1990"}, _standard_policy(),
        ).allowed)
        # The on-device reader alone, deciding its own frame.
        self.assertTrue(decide_access(
            [_local("10CE1990", 1.0)], {"10CE1990"}, _standard_policy(),
        ).allowed)

    def test_overnight_agreement_defaults_change_nothing(self):
        decision = decide_access(
            [_cloud("10CE1990", 0.806)], {"10CE1990"}, _strict_policy(),
            corroborations=[_local("10CE1990", 0.89)],
        )

        self.assertFalse(
            decision.allowed,
            "strict ships with its agreement bars at the historical 0.90",
        )

    def test_the_agreement_bars_are_tunable_per_level(self):
        apply_confidence({
            "GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE_STRICT": "0.80",
            "GATE_MATCH_AGREEMENT_MIN_CLOUD_CONFIDENCE_STRICT": "0.80",
        })

        decision = decide_access(
            [_cloud("10CE1990", 0.806)], {"10CE1990"}, _strict_policy(),
            corroborations=[_local("10CE1990", 0.89)],
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")

    def test_an_ambiguous_agreed_plate_is_never_resolved_by_favour(self):
        decision = decide_access(
            [_cloud("11WH2S71", 0.71), _cloud("11WH2S71", 0.72)],
            {"11WH2571", "11WH2S7I"}, _standard_policy(),
            corroborations=[_local("11WH2S71", 0.99)],
        )

        self.assertFalse(decision.allowed)

    def test_no_corroborations_leaves_every_decision_exactly_as_it_was(self):
        self.assertFalse(decide_access(
            [_cloud("10CE1990", 0.71)], {"10CE1990"}, _standard_policy(),
        ).allowed)
        self.assertFalse(decide_access(
            [_cloud("10CE1990", 0.71)], {"10CE1990"}, _standard_policy(),
            corroborations=(),
        ).allowed)


class LongerMisreadTests(unittest.TestCase):
    """The 11:07:10 report: a 9-character read beside an 8-character plate.

    The journal line that prompted it pairs both readers on one line --
    `local_plate=131D26956 local_score=0.389 ... cloud_plate=131D2696
    cloud_score=0.961`. The grant was the *cloud* read of the authorised
    plate; the nine-character string was the on-device reader's misread, at a
    confidence far below its own gate, and it never took part in anything.
    These tests pin that down so the pairing cannot start to matter later.
    """

    def setUp(self):
        self.addCleanup(apply_confidence, {})
        apply_confidence({})

    def test_a_longer_read_never_exact_matches_a_shorter_authorised_plate(self):
        decision = decide_access(
            [_cloud("131D26956", 0.99)], {"131D2696"}, _standard_policy(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_match")
        self.assertEqual(decision.observed_plate, "131D26956")
        # One deletion away, so it is reportable as a near miss -- and a near
        # miss never widens a match.
        self.assertEqual(decision.near_miss_plate, "131D2696")
        self.assertEqual(decision.near_miss_distance, 1)

    def test_a_deletion_is_not_a_known_confusion_at_any_level(self):
        for policy in (_standard_policy(), _strict_policy()):
            self.assertFalse(decide_access(
                [_cloud("131D26956", 0.99), _cloud("131D26956", 0.99)],
                {"131D2696"}, policy,
            ).allowed)

    def test_a_longer_agreed_read_is_denied_even_when_both_readers_agree(self):
        decision = decide_access(
            [_cloud("131D26956", 0.99)], {"131D2696"}, _standard_policy(),
            corroborations=[_local("131D26956", 0.99)],
        )

        self.assertFalse(decision.allowed)

    def test_the_cloud_read_of_the_authorised_plate_is_what_granted(self):
        # Both readings of the same frame, as they were journalled.
        decision = decide_access(
            [_cloud("131D2696", 0.961)], {"131D2696"}, _standard_policy(),
            corroborations=[_local("131D26956", 0.389)],
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "exact_match")
        self.assertEqual(decision.observed_plate, "131D2696")
