"""The second sighting of a car that has just driven in through our gate.

2026-09-22 10:00, an Audi arriving. It was seen twice, as arriving cars are:
pulled up at the shut gate, where the photo read its front at 0.93 and the
gate then opened on our relay -- passage ``e3182``, ``entering`` at 0.95 -- and
again twenty-one seconds later driving through, side-on and filling the frame.

The second passage, ``e3184``, came out ``unknown``. The model read the flank
as a rear at 0.42, which is 0.36 of vision, against the 0.45 the neighbour was
allowed to lend; 0.09 is under the bar, so a passage nothing was wrong with
produced no verdict at all.

Every number below is from ``passage_directions`` and the rows behind it on the
day, and everything goes through ``judge_passages`` rather than a helper.
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller import direction_passages, direction_signals
from gate_controller.store import LocalStore

SINCE = "2026-09-22T09:59:00+00:00"
DAY = "2026-09-22T"

#: ``e3182``: the car waiting at the shut gate. Event 3179 is the one the
#: relay fired for, and the only one with a plate.
ARRIVAL_EVENTS = (
    (3182, "10:00:02.556983", None, "unknown", 0.0, "empty"),
    (3179, "10:00:03.424723", "10:00:05.407805", "entering", 0.214, "front"),
    (3183, "10:00:03.828777", None, "unknown", 0.0, "empty"),
    (3177, "10:00:05.473541", None, "unknown", 0.0, None),
    (3178, "10:00:05.475507", None, "unknown", 0.0, None),
    (3180, "10:00:06.485079", None, "entering", 0.687, "front"),
    (3181, "10:00:07.132519", None, "entering", 0.929, "front"),
)
#: ``e3184``: the same car going by, twenty-one seconds later.
FLANK_EVENTS = (
    (3184, "10:00:28.196381", None, "unknown", 0.0, None),
    (3186, "10:00:29.332461", None, "entering", 0.063, "front"),
    (3185, "10:00:29.416560", None, "exiting", 0.201, "rear"),
    (3187, "10:00:30.775475", None, "exiting", 0.425, "rear"),
    (3188, "10:00:31.808214", None, "unknown", 0.0, None),
    (3189, "10:00:32.904148", None, "unknown", 0.0, None),
    (3190, "10:00:37.549903", None, "unknown", 0.0, None),
)
#: The gate-sound scan's three runs around the pair: the leaves opening as the
#: first car arrived, a second run while it drove through, and the cycle
#: closing itself a minute later with the clang that confirms all of it.
MOVEMENTS = (
    ("10:00:01.440000", "10:00:13.440000", None),
    ("10:00:15.360000", "10:00:39.840000", None),
    ("10:01:00", "10:01:17.280000", "10:01:17.536000"),
)


class TheCarThatJustDroveIn(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "gate.db"
        LocalStore(self.path)
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        direction_passages.ensure_schema(self.connection)
        for started, ended, clang in MOVEMENTS:
            self.movement(started, ended, clang)
        self.gate_was_heard()

    # -- the day, as it is in the database ---------------------------------
    def event(self, event_id, at, relay_at, verdict, score, top):
        with self.connection:
            self.connection.execute(
                "INSERT INTO events (id, received_at, relay_activated_at, source, reason, opened,"
                " idempotency_key) VALUES (?, ?, ?, 'ocr', 'no_match', ?, ?)",
                (event_id, DAY + at + "+00:00", None if relay_at is None else DAY + relay_at + "+00:00",
                 0 if relay_at is None else 1, f"k{event_id}"))
            self.connection.execute(
                "INSERT INTO event_directions (event_id, status, verdict, score, top, method,"
                " classified_at) VALUES (?, 'read', ?, ?, ?, 'vision-clip-v1', ?)",
                (event_id, verdict, score, top, DAY + at + "+00:00"))

    def movement(self, started, ended, clang):
        with self.connection:
            self.connection.execute(
                "INSERT INTO gate_movements (started_at, ended_at, seconds, outcome, uncommanded,"
                " clang_at, detector, created_at) VALUES (?, ?, 12.0, ?, 0, ?, 'yamnet-linear-v2', ?)",
                (DAY + started + "+00:00", DAY + ended + "+00:00", "shut" if clang else "open",
                 None if clang is None else DAY + clang + "+00:00", DAY + "10:16:24+00:00"))

    def gate_was_heard(self):
        base = datetime(2026, 9, 22, 9, 50, tzinfo=timezone.utc)
        with self.connection:
            for step in range(6):
                moment = base + timedelta(seconds=300 * step)
                self.connection.execute(
                    "INSERT OR REPLACE INTO gate_sound_scans (segment, scanned_at, frames, movements)"
                    " VALUES (?, ?, 620, 0)",
                    (moment.strftime("gate-%Y%m%dT%H%M%SZ.aac"), moment.isoformat()))

    def arrival(self):
        for row in ARRIVAL_EVENTS:
            self.event(*row)

    @staticmethod
    def reading(rear):
        """``e3184``'s frames with the strongest rear moved to ``rear``."""
        return tuple(row[:3] + ("exiting", rear, "rear") if row[0] == 3187 else row
                     for row in FLANK_EVENTS)

    def flank(self, events=FLANK_EVENTS):
        for row in events:
            self.event(*row)

    def judged(self):
        return {
            passage.key: passage.verdict
            for passage in direction_passages.judge_passages(self.connection, SINCE)
        }

    @staticmethod
    def signal(verdict, method):
        for opinion in verdict.opinions:
            if opinion.method == method:
                return opinion
        raise AssertionError(f"no {method} opinion among {[o.method for o in verdict.opinions]}")

    # -- what the day should have said ------------------------------------
    def test_the_first_sighting_is_the_arrival_it_always_was(self):
        self.arrival()
        self.flank()

        verdict = self.judged()["e3182"]

        self.assertEqual(verdict.verdict, "entering")
        self.assertAlmostEqual(verdict.confidence, 0.95, places=2)
        self.assertEqual(self.signal(verdict, "gate_opened_after_seen").verdict, "entering")
        self.assertFalse(self.signal(verdict, "same_arrival").decisive,
                         "the passage beside it is not an arrival on its own evidence")

    def test_the_flank_filling_the_frame_is_the_same_car_arriving(self):
        """e3184: vision exiting 0.36 against same_arrival 0.45 used to leave
        0.09, under the 0.2 bar. The rear is the arriving car's flank, so it is
        halved the way a wheel arch is halved inside a single passage."""
        self.arrival()
        self.flank()

        verdict = self.judged()["e3184"]

        self.assertEqual(verdict.verdict, "entering")
        self.assertFalse(verdict.conflict)
        self.assertAlmostEqual(verdict.confidence, 0.269, places=3)
        vision = self.signal(verdict, "vision")
        self.assertEqual(vision.verdict, "exiting")
        self.assertAlmostEqual(vision.confidence, 0.181, places=3)
        self.assertIn("passing the lens", vision.detail)
        self.assertEqual(self.signal(verdict, "same_arrival").verdict, "entering")
        self.assertAlmostEqual(self.signal(verdict, "same_arrival").confidence, 0.45, places=2)

    def test_the_lent_verdict_is_still_only_lent(self):
        """It may rescue a flank; it may never be worth more than one."""
        self.arrival()
        self.flank()

        verdict = self.judged()["e3184"]

        self.assertLess(self.signal(verdict, "same_arrival").confidence, direction_signals.STRONG)
        self.assertLess(verdict.confidence, self.judged()["e3182"].confidence)

    def test_a_confident_rear_is_still_a_departure_through_that_gate(self):
        """A car leaving through a gate that opened for the car arriving is a
        real thing, and the photo is allowed to say so. Only the reading below
        0.5 -- the one that is a flank -- is discounted."""
        self.arrival()
        self.flank(self.reading(0.95))

        verdict = self.judged()["e3184"]

        self.assertEqual(verdict.verdict, "exiting")
        vision = self.signal(verdict, "vision")
        self.assertAlmostEqual(vision.confidence, 0.887, places=3)
        self.assertNotIn("passing the lens", vision.detail)

    def test_a_rear_the_model_means_is_never_halved(self):
        """0.65 was the bar this rule was asked to keep, and it keeps it: the
        discount only ever touches a reading the model is unsure of."""
        self.arrival()
        self.flank(self.reading(0.75))

        verdict = self.judged()["e3184"]

        self.assertEqual(verdict.verdict, "exiting")
        vision = self.signal(verdict, "vision")
        self.assertGreater(vision.confidence, 0.65)
        self.assertNotIn("passing the lens", vision.detail)

    def test_only_a_reading_under_a_half_is_treated_as_a_flank(self):
        """At the boundary the photo keeps its full weight, and a rear at 0.5
        against a lent 0.45 cancels out rather than being bent into an
        arrival: two signals that close say nothing, which is the honest
        answer and the one the combiner already gave."""
        self.arrival()
        self.flank(self.reading(0.565))

        verdict = self.judged()["e3184"]

        vision = self.signal(verdict, "vision")
        self.assertGreaterEqual(vision.confidence, direction_signals.FLANK_REAR_SCORE)
        self.assertNotIn("passing the lens", vision.detail)
        self.assertEqual(verdict.verdict, "unknown")

    def test_a_weak_rear_beside_an_arrival_we_did_not_let_in_is_untouched(self):
        """The discount turns on the gate having actually opened for the car
        next door. Take the relay away and the neighbour is an arrival nobody
        admitted, whose flank explains nothing here."""
        self.arrival()
        self.flank()
        with self.connection:
            self.connection.execute("UPDATE events SET relay_activated_at = NULL, opened = 0")

        judged = self.judged()

        self.assertEqual(judged["e3182"].verdict, "entering", "still an arrival, from its photo")
        self.assertEqual(judged["e3184"].verdict, "unknown")
        self.assertAlmostEqual(self.signal(judged["e3184"], "vision").confidence, 0.362, places=3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
