"""Direction from the photo: the rules, and the pass that applies them."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate_controller.direction_vision import (
    ENTERING, EXITING, UNKNOWN, FrameReading, VisionDirection,
)
from gate_controller.store import LocalStore


class Readings(unittest.TestCase):
    def test_the_front_of_a_vehicle_is_arriving(self):
        # 10CE1990 on 2026-09-19 at 10:30: headlights and plate toward the lens.
        reading = FrameReading(front=1.00, rear=0.00, top="front")
        self.assertEqual(reading.direction, ENTERING)
        self.assertAlmostEqual(reading.score, 1.0)

    def test_the_rear_of_a_vehicle_is_leaving(self):
        # The Audi on 2026-09-17 at 08:02, tail lights receding.
        reading = FrameReading(front=0.04, rear=0.95, top="rear")
        self.assertEqual(reading.direction, EXITING)
        self.assertAlmostEqual(reading.score, 0.91)

    def test_a_frame_with_neither_end_in_it_says_nothing(self):
        """An empty driveway, a telehandler, a door panel: no opinion."""
        for reading in (FrameReading(0.00, 0.00, "empty"), FrameReading(0.01, 0.03, "machine")):
            self.assertEqual(reading.direction, UNKNOWN)
            self.assertEqual(reading.score, 0.0)


class Availability(unittest.TestCase):
    def test_a_missing_model_is_reported_not_raised(self):
        model = VisionDirection(model_dir=Path("/nonexistent"))
        self.assertFalse(model.available)
        self.assertIn("not installed", model.unavailable_reason or "")
        self.assertIsNone(model.read(b"\xff\xd8\xff"))


class ThePass(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import scan_direction
        self.scan = scan_direction
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "gate.db"
        LocalStore(self.path)
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        with self.connection:
            for event_id, key in ((1, "k1"), (2, None), (3, "k3")):
                self.connection.execute(
                    "INSERT INTO events (id, received_at, source, reason, opened, idempotency_key)"
                    " VALUES (?, '2026-09-20T08:00:00+00:00', 'ocr', 'no_match', 0, ?)", (event_id, key))

    def test_only_decisive_verdicts_are_sent(self):
        """An unknown is kept locally but never sent: it could only erase."""
        now = "2026-09-20T09:00:00+00:00"
        self.scan.record(self.connection, 1, status="read", reading=FrameReading(0.04, 0.95, "rear"), now=now)
        self.scan.record(self.connection, 2, status="read", reading=FrameReading(0.0, 0.0, "empty"), now=now)
        self.scan.record(self.connection, 3, status="no_image", now=now)

        self.assertEqual([(row[0], row[2]) for row in self.scan.unshipped(self.connection)], [(1, EXITING)])

    def test_a_judged_event_is_not_judged_again(self):
        self.scan.record(self.connection, 1, status="read", reading=FrameReading(1.0, 0.0, "front"),
                         now="2026-09-20T09:00:00+00:00")
        ids = [row[0] for row in self.scan.pending(self.connection, "2026-09-19T00:00:00+00:00", 10)]
        self.assertEqual(ids, [2, 3])

    def test_the_dashboard_token_travels_as_headers_with_an_agent(self):
        """Cloudflare refuses Python's default agent with 403, error 1010."""
        dashboard = self.scan.Dashboard({
            "GATE_CLOUDFLARE_API_URL": "https://gate.example",
            "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "id", "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "secret",
        })
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"], captured["headers"] = req.full_url, req.headers
            import io
            return io.BytesIO(b"\xff\xd8\xff")

        with mock.patch.object(self.scan.request, "urlopen", fake_urlopen):
            dashboard.image(2968, None)

        self.assertNotIn("secret", captured["url"])
        self.assertIn("event_id=2968", captured["url"])
        self.assertEqual(captured["headers"]["User-agent"], self.scan.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
