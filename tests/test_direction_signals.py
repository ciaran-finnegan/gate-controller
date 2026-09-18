"""Direction from every signal, not one slope."""
import unittest

from gate_controller.direction import DirectionEstimate
from gate_controller.direction_signals import (
    VERDICT_ENTERING, VERDICT_EXITING, VERDICT_UNKNOWN,
    combine, from_box_width, from_gate,
)

SEEN = 1000.0


class FromTheGate(unittest.TestCase):
    def test_a_relay_we_fired_means_the_car_was_coming_in(self):
        """We only fire for a plate read on the approach."""
        opinion = from_gate(SEEN, relay_at=SEEN + 2.0)

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)
        self.assertGreater(opinion.confidence, 0.8)

    def test_a_gate_already_moving_uncommanded_means_the_car_was_going_out(self):
        """The camera picks a departing car up late and from behind: it comes
        from inside, where the lens cannot see it, and somebody indoors opened
        the gate before it ever reached the frame."""
        opinion = from_gate(SEEN, movement_started_at=SEEN - 20.0, movement_uncommanded=True)

        self.assertEqual(opinion.verdict, VERDICT_EXITING)
        self.assertGreater(opinion.confidence, 0.8)

    def test_a_gate_that_moved_first_but_we_commanded_it_is_not_an_exit(self):
        # Our own opening, seen slightly late. Charging that to a fob would
        # inflate the one number this exists to produce.
        opinion = from_gate(SEEN, movement_started_at=SEEN - 20.0, movement_uncommanded=False)

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_gate_that_moved_after_the_car_leans_entering_but_only_leans(self):
        opinion = from_gate(SEEN, movement_started_at=SEEN + 3.0)

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)
        self.assertLess(opinion.confidence, 0.8, "an exit seen early looks the same")

    def test_a_movement_from_another_passage_says_nothing(self):
        opinion = from_gate(SEEN, movement_started_at=SEEN - 600.0, movement_uncommanded=True)

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_no_movement_at_all_says_nothing_rather_than_guessing(self):
        self.assertEqual(from_gate(SEEN).verdict, VERDICT_UNKNOWN)


class FromTheSlope(unittest.TestCase):
    def test_the_slope_fit_is_halved_because_it_was_fitted_on_another_camera(self):
        opinion = from_box_width(DirectionEstimate(
            verdict=VERDICT_ENTERING, method="box_width", score=0.8,
        ))

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)
        self.assertAlmostEqual(opinion.confidence, 0.4, places=3)

    def test_an_unknown_fit_contributes_nothing(self):
        self.assertFalse(from_box_width(DirectionEstimate()).decisive)


class Combining(unittest.TestCase):
    def test_the_gate_outvotes_a_slope_that_disagrees_with_it(self):
        """The 09:02 passage: a departing Audi the slope called entering at
        0.26, while the gate had been open before the camera saw it."""
        verdict = combine(
            from_gate(SEEN, movement_started_at=SEEN - 20.0, movement_uncommanded=True),
            from_box_width(DirectionEstimate(
                verdict=VERDICT_ENTERING, method="box_width", score=0.26)),
        )

        self.assertEqual(verdict.verdict, VERDICT_EXITING)
        self.assertGreater(verdict.confidence, 0.5)

    def test_disagreement_leaves_a_weak_answer_not_a_loud_one(self):
        verdict = combine(
            from_gate(SEEN, movement_started_at=SEEN + 3.0),
            from_box_width(DirectionEstimate(
                verdict=VERDICT_EXITING, method="box_width", score=0.9)),
        )

        self.assertLess(verdict.confidence, 0.3)

    def test_every_signal_is_kept_so_they_can_be_scored_against_each_other(self):
        verdict = combine(
            from_gate(SEEN, relay_at=SEEN),
            from_box_width(DirectionEstimate(verdict=VERDICT_EXITING, score=0.5)),
        )

        methods = [signal["method"] for signal in verdict.as_dict()["signals"]]
        self.assertEqual(methods, ["gate_commanded", "box_width"])

    def test_nothing_decisive_is_unknown_at_zero(self):
        verdict = combine(from_gate(SEEN), from_box_width(DirectionEstimate()))

        self.assertEqual(verdict.verdict, VERDICT_UNKNOWN)
        self.assertEqual(verdict.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()


class KeepingTheGeometry(unittest.TestCase):
    """The x position was handed in and thrown away on every frame."""

    def test_the_tracker_keeps_where_the_car_crossed_the_frame(self):
        from gate_controller.direction import DirectionTracker, DirectionConfig

        tracker = DirectionTracker(DirectionConfig())
        # The 09:02 departing Audi: right to left across the frame.
        for at, box in ((0.0, (0.915, 0.005, 0.040, 0.024)),
                        (1.8, (0.553, 0.089, 0.081, 0.042)),
                        (4.2, (0.478, 0.092, 0.075, 0.034))):
            tracker.observe("t", box=box, at=at)

        track = tracker.track("t")

        self.assertEqual([round(x, 3) for _at, x in track], [0.915, 0.553, 0.478])

    def test_a_frame_with_only_a_width_still_works_and_adds_no_track(self):
        from gate_controller.direction import DirectionTracker, DirectionConfig

        tracker = DirectionTracker(DirectionConfig())
        tracker.observe("t", width=0.05, at=0.0)

        self.assertEqual(tracker.track("t"), [])

    def test_an_unknown_trace_has_no_track_rather_than_raising(self):
        from gate_controller.direction import DirectionTracker, DirectionConfig

        self.assertEqual(DirectionTracker(DirectionConfig()).track("nope"), [])
