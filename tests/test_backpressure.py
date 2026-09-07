import unittest

from gate_controller.backpressure import (
    BLOCKED_EVENT_DELIVERY, BLOCKED_GATE_ACTIVITY, BLOCKED_QUIET_WINDOW,
    BLOCKED_UNKNOWN_QUEUE, NULL_GATE, ActivityGate, bounded_quiet_seconds,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ActivityGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def gate(self, **kwargs):
        kwargs.setdefault("quiet_seconds", 60.0)
        return ActivityGate(clock=self.clock, **kwargs)

    def test_the_priority_ladder_is_checked_in_order(self):
        """Gate decisions, then event delivery, then the corpus."""
        pending = [3]
        gate = self.gate(pending_events=lambda: pending[0])
        self.clock.advance(600)

        with gate.activity("burst"):
            # A gate decision outranks everything, including a full outbox.
            self.assertEqual(gate.blocked_by(), BLOCKED_GATE_ACTIVITY)
            self.assertEqual(gate.busy_reason(), "burst")

        # The quiet window starts when the decision ends, not before.
        self.assertEqual(gate.blocked_by(), BLOCKED_QUIET_WINDOW)
        self.clock.advance(60)
        # Real events still outrank the corpus once the link is quiet.
        self.assertEqual(gate.blocked_by(), BLOCKED_EVENT_DELIVERY)
        pending[0] = 0
        self.assertIsNone(gate.blocked_by())

    def test_a_queue_that_cannot_be_read_is_treated_as_work_pending(self):
        def broken():
            raise RuntimeError("database is locked")

        gate = self.gate(pending_events=broken)
        self.clock.advance(600)
        self.assertEqual(gate.blocked_by(), BLOCKED_UNKNOWN_QUEUE)
        gate = self.gate(pending_events=lambda: "seven")
        self.clock.advance(600)
        self.assertEqual(gate.blocked_by(), BLOCKED_UNKNOWN_QUEUE)

    def test_a_gate_event_that_starts_and_ends_still_disturbs_a_transfer(self):
        """The epoch is what makes an abort reliable, not the busy flag.

        A camera event can begin and finish between two chunks of a corpus
        upload. A transfer watching only "is something running" would see
        nothing and keep going; the epoch has moved and it stands down.
        """
        gate = self.gate()
        epoch = gate.epoch()
        self.assertFalse(gate.disturbed_since(epoch))
        with gate.activity("camera_event"):
            pass
        self.assertIsNone(gate.busy_reason())
        self.assertTrue(gate.disturbed_since(epoch))

    def test_nested_activity_keeps_the_link_held_until_the_outer_span_ends(self):
        gate = self.gate()
        self.clock.advance(600)
        with gate.activity("burst"):
            with gate.activity("ocr"):
                self.assertEqual(gate.blocked_by(), BLOCKED_GATE_ACTIVITY)
            self.assertEqual(gate.blocked_by(), BLOCKED_GATE_ACTIVITY,
                             "the burst still owns the link after the OCR call")
        self.assertEqual(gate.quiet_seconds(), 0.0)

    def test_an_unbalanced_end_never_leaves_the_depth_negative(self):
        gate = self.gate()
        gate.end("never_begun")
        gate.end("never_begun")
        gate.begin("burst")
        self.assertEqual(gate.blocked_by(), BLOCKED_GATE_ACTIVITY)
        gate.end("burst")
        self.clock.advance(60)
        self.assertIsNone(gate.blocked_by())

    def test_odd_reasons_are_reduced_to_a_greppable_token(self):
        gate = self.gate()
        gate.begin("camera event\nplate=12D 3456")
        self.assertEqual(gate.busy_reason(), "camera_event_plate12D_3456")
        gate.end()
        gate.begin(object())
        self.assertTrue(gate.busy_reason())

    def test_the_null_gate_never_asks_anyone_to_wait(self):
        self.assertIsNone(NULL_GATE.blocked_by())
        with NULL_GATE.activity("burst"):
            self.assertIsNone(NULL_GATE.blocked_by())

    def test_the_quiet_window_is_bounded(self):
        for value in ("0", "4", "100000", "nan", "not a number"):
            with self.assertRaises(ValueError):
                bounded_quiet_seconds(value)
        self.assertEqual(bounded_quiet_seconds("60"), 60.0)

    def test_status_reports_what_the_heartbeat_needs(self):
        gate = self.gate()
        self.clock.advance(30)
        status = gate.status()
        self.assertEqual(status["quiet_window_seconds"], 60.0)
        self.assertEqual(status["quiet_for_seconds"], 30.0)
        self.assertIsNone(status["busy"])
        with gate.activity("ocr"):
            self.assertEqual(gate.status()["busy"], "ocr")
        self.assertEqual(gate.status()["activities"], 1)


if __name__ == "__main__":
    unittest.main()
