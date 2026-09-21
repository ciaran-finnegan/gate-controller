"""Direction from every signal, not one slope."""
import unittest

from gate_controller.direction import DirectionEstimate
from gate_controller.direction_signals import (
    STRONG, VERDICT_ENTERING, VERDICT_EXITING, VERDICT_UNKNOWN,
    DirectionVerdict, Movement, Opinion, PassageEvidence,
    combine, from_box_width, from_gate, from_same_arrival, from_vision, judge, unseen_departures,
)

SEEN = 1000.0


def opened(start, seconds=20.0, clang=False):
    return Movement(SEEN + start, SEEN + start + seconds, clang=clang)


def cycle(start):
    """An opening, and the clang of it shutting again half a minute later."""
    return (opened(start), opened(start + 45.0, clang=True))


class FromTheGate(unittest.TestCase):
    def test_a_relay_we_fired_is_not_evidence_of_an_arrival(self):
        """gate-controller#171. This test used to assert the opposite --
        ``from_gate(SEEN, relay_at=SEEN + 2)`` was ``entering`` above 0.8, on
        the reasoning that "we only fire for a plate read on the approach". A
        departing car shows its rear plate to the same camera and the relay
        fires for that too: five of the 24 passages it fired in over the week
        to 2026-09-21 were departures, by their photos. The relay alone, with
        no gate movement to time, now says nothing."""
        opinion = from_gate(SEEN, commands_at=(SEEN + 2.0,))

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_departing_car_the_relay_fired_for_is_still_a_departure(self):
        """2026-09-19 18:35: the gate began opening 28 s before the camera saw
        anything, the rear plate matched, and the relay fired at +5.4 s."""
        opinion = from_gate(SEEN, movements=cycle(-28.0), commands_at=(SEEN + 5.4,))

        self.assertEqual(opinion.verdict, VERDICT_EXITING)
        self.assertEqual(opinion.method, "gate_opened_from_inside")
        self.assertGreaterEqual(opinion.confidence, STRONG)

    def test_a_gate_opened_from_inside_before_anything_was_seen_is_a_departure(self):
        """The camera cannot see inside the property, so a departing car cannot
        be seen until the gate has already opened for it."""
        opinion = from_gate(SEEN, movements=cycle(-26.0))

        self.assertEqual(opinion.verdict, VERDICT_EXITING)
        self.assertGreaterEqual(opinion.confidence, STRONG)

    def test_a_motor_run_no_clang_vouches_for_can_nudge_but_never_decide(self):
        """The detector reports about 122 movements a day."""
        opinion = from_gate(SEEN, movements=(opened(-30.0),))

        self.assertEqual(opinion.verdict, VERDICT_EXITING)
        self.assertLess(opinion.confidence, 0.2)
        self.assertEqual(combine(opinion).verdict, VERDICT_UNKNOWN)

    def test_a_gate_we_opened_earlier_is_not_an_exit(self):
        # Our own opening, seen late. Charging that to a fob would inflate the
        # one number this exists to produce.
        opinion = from_gate(SEEN, movements=cycle(-30.0), commands_at=(SEEN - 32.0,))

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_gate_opened_for_the_car_in_front_says_nothing_about_this_one(self):
        opinion = from_gate(SEEN, movements=cycle(-30.0), others_seen=((SEEN - 31.0, SEEN - 27.0),))

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_car_behind_does_not_claim_the_gate_that_opened_for_this_one(self):
        opinion = from_gate(SEEN, movements=cycle(-1.0), commands_at=(SEEN + 2.0,),
                            others_seen=((SEEN + 28.0, SEEN + 32.0),))

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)

    def test_seen_at_a_still_gate_that_then_opened_is_an_arrival(self):
        """On our relay or on a fob: either way the car was seen first, and a
        departing car could not have been."""
        for commands in ((SEEN + 2.0,), ()):
            opinion = from_gate(SEEN, movements=cycle(-3.0), commands_at=commands)
            self.assertEqual(opinion.verdict, VERDICT_ENTERING)
            self.assertEqual(opinion.method, "gate_opened_after_seen")
            self.assertGreaterEqual(opinion.confidence, STRONG)

    def test_a_still_gate_is_only_still_if_somebody_was_listening(self):
        opinion = from_gate(SEEN, movements=cycle(-3.0), gate_heard=False)

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_run_that_began_nineteen_seconds_out_could_be_either(self):
        """2026-09-18 16:56, an arrival, and 2026-09-18 10:30, a departure, both
        had a motor run begin 19 s before the first frame."""
        opinion = from_gate(SEEN, movements=cycle(-19.0), commands_at=(SEEN + 0.4,))

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_farm_machine_silences_the_gate(self):
        """2026-09-19 14:55: a telehandler *arriving*, heard as the gate motor
        for 45 s before its first frame, with a clang."""
        opinion = from_gate(SEEN, movements=cycle(-45.0), machine=True)

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_gate_that_finished_moving_long_ago_was_not_opened_for_this_car(self):
        opinion = from_gate(SEEN, movements=(opened(-116.0, 12.0, clang=True),))

        self.assertEqual(opinion.verdict, VERDICT_UNKNOWN)

    def test_a_movement_from_another_passage_says_nothing(self):
        self.assertEqual(from_gate(SEEN, movements=cycle(-600.0)).verdict, VERDICT_UNKNOWN)

    def test_no_movement_at_all_says_nothing_rather_than_guessing(self):
        self.assertEqual(from_gate(SEEN).verdict, VERDICT_UNKNOWN)

    def test_a_passage_nobody_was_seen_in_cannot_be_timed(self):
        self.assertEqual(from_gate(None, movements=cycle(-26.0)).verdict, VERDICT_UNKNOWN)


class GateCyclesNobodyWasSeenAt(unittest.TestCase):
    def test_a_confirmed_cycle_with_nothing_in_frame_is_a_departure_nobody_counted(self):
        found = unseen_departures(cycle(0.0), (), ())

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1].verdict, VERDICT_EXITING)
        self.assertLess(found[0][1].confidence, STRONG)

    def test_a_motor_only_run_is_not_counted(self):
        self.assertEqual(unseen_departures((opened(0.0),), (), ()), [])

    def test_a_cycle_we_commanded_or_a_vehicle_explains_is_not_counted(self):
        self.assertEqual(unseen_departures(cycle(0.0), (SEEN - 1.0,), ()), [])
        self.assertEqual(unseen_departures(cycle(0.0), (), ((SEEN + 20.0, SEEN + 25.0),)), [])


class FromThePhoto(unittest.TestCase):
    def test_the_strongest_reading_less_the_strongest_the_other_way(self):
        opinion = from_vision([("exiting", 0.94), ("exiting", 0.91), ("entering", 0.14), ("unknown", 0.0)])

        self.assertEqual(opinion.verdict, VERDICT_EXITING)
        self.assertAlmostEqual(opinion.confidence, 0.80, places=2)

    def test_an_arriving_car_passing_the_lens_is_not_a_contradiction(self):
        """2026-09-17 11:08: front at 0.97, then a wheel arch a foot from the
        lens read as rear at 0.77. One Audi, driving in."""
        opinion = from_vision([("entering", 0.966), ("unknown", 0.0), ("exiting", 0.769)])

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)
        self.assertGreater(opinion.confidence, 0.7)

    def test_a_rear_seen_first_is_never_discounted(self):
        """A departing car never shows its front, so only a front that came
        *first* earns the discount."""
        opinion = from_vision([("exiting", 0.9), ("entering", 0.6)])

        self.assertEqual(opinion.verdict, VERDICT_EXITING)
        self.assertAlmostEqual(opinion.confidence, 0.3, places=2)

    def test_no_single_photo_is_proof(self):
        self.assertLess(from_vision([("entering", 1.0)]).confidence, 1.0)

    def test_frames_that_showed_nothing_say_nothing(self):
        self.assertFalse(from_vision([("unknown", 0.0)]).decisive)
        self.assertFalse(from_vision([]).decisive)


class FromTheSlope(unittest.TestCase):
    def test_the_slope_fit_is_halved_because_it_was_fitted_on_another_camera(self):
        opinion = from_box_width(DirectionEstimate(
            verdict=VERDICT_ENTERING, method="box_width", score=0.8,
        ))

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)
        self.assertAlmostEqual(opinion.confidence, 0.4, places=3)

    def test_the_block_stored_with_the_event_is_read_the_same_way(self):
        opinion = from_box_width({"verdict": "entering", "method": "box_width", "score": 0.8})

        self.assertAlmostEqual(opinion.confidence, 0.4, places=3)

    def test_it_can_never_be_strong_enough_to_veto(self):
        opinion = from_box_width({"verdict": "exiting", "score": 1.0})

        self.assertLess(opinion.confidence, STRONG)

    def test_an_unknown_fit_contributes_nothing(self):
        self.assertFalse(from_box_width(DirectionEstimate()).decisive)
        self.assertFalse(from_box_width(None).decisive)


class FromThePassageNextDoor(unittest.TestCase):
    ARRIVAL = DirectionVerdict(VERDICT_ENTERING, 0.9)

    def test_the_car_seen_waiting_is_the_car_that_then_drives_through(self):
        opinion = from_same_arrival(self.ARRIVAL, 28.0)

        self.assertEqual(opinion.verdict, VERDICT_ENTERING)
        self.assertLess(opinion.confidence, STRONG, "lent, so it may never veto what was seen")

    def test_a_departure_next_door_lends_nothing(self):
        self.assertFalse(from_same_arrival(DirectionVerdict(VERDICT_EXITING, 0.9), 28.0).decisive)

    def test_too_far_apart_to_be_the_same_car(self):
        self.assertFalse(from_same_arrival(self.ARRIVAL, 90.0).decisive)


class Combining(unittest.TestCase):
    def test_signals_that_agree_are_worth_more_than_either(self):
        verdict = combine(Opinion(VERDICT_EXITING, 0.6, "a"), Opinion(VERDICT_EXITING, 0.6, "b"))

        self.assertEqual(verdict.verdict, VERDICT_EXITING)
        self.assertAlmostEqual(verdict.confidence, 0.84, places=2)
        self.assertEqual(verdict.contributing, ("a", "b"))

    def test_the_gate_and_the_photo_outvote_a_slope_that_disagrees(self):
        """2026-09-19 18:35: a departing Audi the slope called entering."""
        verdict = combine(
            from_vision([("exiting", 0.986)]),
            from_gate(SEEN, movements=cycle(-28.0), commands_at=(SEEN + 5.4,)),
            from_box_width({"verdict": "entering", "score": 0.26}),
        )

        self.assertEqual(verdict.verdict, VERDICT_EXITING)
        self.assertGreater(verdict.confidence, 0.7)
        self.assertNotIn("box_width", verdict.contributing)

    def test_strong_signals_that_disagree_are_unknown_not_a_coin_toss(self):
        verdict = combine(
            from_vision([("entering", 0.9)]),
            from_gate(SEEN, movements=cycle(-28.0)),
        )

        self.assertEqual(verdict.verdict, VERDICT_UNKNOWN)
        self.assertEqual(verdict.confidence, 0.0)
        self.assertTrue(verdict.conflict)
        self.assertEqual(verdict.contributing, ())

    def test_a_weak_lean_left_over_is_not_an_answer(self):
        verdict = combine(Opinion(VERDICT_ENTERING, 0.45, "a"), Opinion(VERDICT_EXITING, 0.35, "b"))

        self.assertEqual(verdict.verdict, VERDICT_UNKNOWN)
        self.assertFalse(verdict.conflict)

    def test_every_signal_is_kept_so_they_can_be_scored_against_each_other(self):
        verdict = judge(PassageEvidence(
            first_seen_at=SEEN, frames=(("exiting", 0.9),), movements=cycle(-28.0), gate_heard=True,
            box_width={"verdict": "entering", "score": 0.5},
        ))

        methods = [signal["method"] for signal in verdict.as_dict()["signals"]]
        self.assertEqual(methods, ["vision", "gate_opened_from_inside", "box_width"])
        self.assertEqual(verdict.as_dict()["contributing"], ["vision", "gate_opened_from_inside"])

    def test_nothing_decisive_is_unknown_at_zero(self):
        verdict = judge(PassageEvidence(first_seen_at=SEEN))

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
