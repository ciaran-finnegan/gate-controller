"""The direction scan, driven through its real entry point.

``direction_signals`` was once tested thoroughly and called by nothing: it never
ran on a real passage. So everything here goes through ``scan_direction.main``
-- the function the systemd timer runs -- against a real database, and asserts
on what is sent to the dashboard and what is kept. Only the true boundaries are
faked: the HTTP calls, the image model, and the environment.
"""
import ast
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from gate_controller import direction_signals
from gate_controller.direction_vision import FrameReading
from gate_controller.store import LocalStore

ROOT = Path(__file__).resolve().parents[1]
scan_direction = None


def setUpModule():
    """Import the script when the tests run, not when they are collected.

    It puts the repository root at the front of ``sys.path`` as it loads, which
    is right for a script and wrong during discovery: the root holds a legacy
    ``test_relay.py`` that would then shadow ``tests/test_relay.py``.
    """
    global scan_direction
    sys.path.insert(0, str(ROOT / "scripts"))
    import scan_direction as module
    scan_direction = module

ENVIRONMENT = {
    "GATE_CLOUDFLARE_API_URL": "https://gate.example",
    "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "id",
    "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "secret",
}
READINGS = {
    b"front": FrameReading(front=0.95, rear=0.03, top="front"),
    b"rear": FrameReading(front=0.03, rear=0.95, top="rear"),
    b"dim rear": FrameReading(front=0.10, rear=0.60, top="rear"),
    b"flank": FrameReading(front=0.05, rear=0.06, top="side"),
    b"empty": FrameReading(front=0.0, rear=0.0, top="empty"),
    b"machine": FrameReading(front=0.0, rear=0.01, top="machine"),
}


class FakeModel:
    """Stands in for CLIP: the 'photo' is a word, and the word is the reading."""

    available = True
    unavailable_reason = None

    def read(self, jpeg):
        return READINGS.get(jpeg)


class MissingModel:
    available = False
    unavailable_reason = "not installed"

    def read(self, jpeg):  # pragma: no cover - must never be reached
        raise AssertionError("an unavailable model was asked to read")


class FakeDashboard:
    """The two routes the scan uses, as ``urlopen``. Records every POST."""

    def __init__(self, photos, understands_retract=False):
        self.photos = photos
        self.understands_retract = understands_retract
        self.posts = []

    def __call__(self, req, timeout=None):
        if req.get_method() == "POST":
            body = json.loads(req.data)
            self.posts.append(body)
            answer = {"updated": len(body["directions"]), "skipped": 0}
            if self.understands_retract:
                answer["retracted"] = sum(1 for d in body["directions"] if d.get("retract"))
            return io.BytesIO(json.dumps(answer).encode())
        event_id = int(req.full_url.split("event_id=")[1].split("&")[0])
        if event_id not in self.photos:
            raise scan_direction.error.HTTPError(req.full_url, 404, "Not found", {}, None)
        return io.BytesIO(self.photos[event_id])

    @property
    def sent(self):
        return {d["event_id"]: d for body in self.posts for d in body["directions"]}


class TheScan(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "gate.db"
        LocalStore(self.path)
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        # Two hours ago, on a whole five-minute boundary so audio segments line up.
        now = datetime.now(timezone.utc) - timedelta(hours=2)
        self.base = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        self.photos = {}

    # -- building a history ------------------------------------------------
    def at(self, seconds):
        return (self.base + timedelta(seconds=seconds)).isoformat()

    def event(self, event_id, seconds, photo=None, relay_after=None, source="ocr", box_width=None):
        with self.connection:
            self.connection.execute(
                "INSERT INTO events (id, received_at, relay_activated_at, source, reason, opened, idempotency_key)"
                " VALUES (?, ?, ?, ?, 'no_match', ?, ?)",
                (event_id, self.at(seconds), None if relay_after is None else self.at(seconds + relay_after),
                 source, 0 if relay_after is None else 1, f"k{event_id}"))
            if box_width is not None:
                self.connection.execute(
                    "INSERT INTO event_telemetry (event_id, trace_id, payload, created_at) VALUES (?, ?, ?, ?)",
                    (event_id, f"t{event_id}", json.dumps({"direction": box_width}), self.at(seconds)))
        if photo is not None:
            self.photos[event_id] = photo

    def movement(self, seconds, length=20.0, clang=False):
        with self.connection:
            self.connection.execute(
                "INSERT INTO gate_movements (started_at, ended_at, seconds, outcome, uncommanded, clang_at,"
                " detector, created_at) VALUES (?, ?, ?, ?, 0, ?, 'test', ?)",
                (self.at(seconds), self.at(seconds + length), length, "shut" if clang else "open",
                 self.at(seconds + length) if clang else None, self.at(seconds + length)))

    def cycle(self, seconds):
        self.movement(seconds)
        self.movement(seconds + 45.0, clang=True)

    def gate_was_heard(self):
        """The gate-sound scan has read the audio either side of ``base``."""
        with self.connection:
            for offset in range(-900, 1200, 300):
                name = (self.base + timedelta(seconds=offset)).strftime("gate-%Y%m%dT%H%M%SZ.aac")
                self.connection.execute(
                    "INSERT OR REPLACE INTO gate_sound_scans (segment, scanned_at, frames, movements)"
                    " VALUES (?, ?, 620, 0)", (name, self.at(offset + 600)))

    # -- running it ----------------------------------------------------------
    def run_scan(self, *arguments, model=FakeModel, dashboard=None, environment=ENVIRONMENT):
        dashboard = dashboard or FakeDashboard(self.photos)
        with mock.patch.object(scan_direction, "VisionDirection", model), \
                mock.patch.object(scan_direction.logging, "basicConfig"), \
                mock.patch.object(scan_direction.request, "urlopen", dashboard), \
                mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(scan_direction.main(["--database", str(self.path), *arguments]), 0)
        return dashboard

    def kept(self):
        return {
            row[0]: {"kind": row[1], "verdict": row[2], "confidence": row[3], "conflict": row[4],
                     "contributing": row[5].split(",") if row[5] else [], "signals": json.loads(row[6])}
            for row in self.connection.execute(
                "SELECT passage_key, kind, verdict, confidence, conflict, contributing, signals"
                " FROM passage_directions")
        }

    # -- the wiring ----------------------------------------------------------
    def test_the_scan_really_calls_the_combiner(self):
        """The failure this file exists to prevent: helpers nothing calls."""
        self.event(1, 0, b"rear")
        with mock.patch.object(direction_signals, "combine", wraps=direction_signals.combine) as combine:
            self.run_scan()

        self.assertTrue(combine.called, "scan_direction.main no longer reaches direction_signals.combine")
        methods = [opinion.method for opinion in combine.call_args_list[0].args]
        self.assertEqual(methods[:3], ["vision", "gate", "box_width"])

    def test_production_code_imports_the_combiner(self):
        """Static, so it fails even if the test above is deleted with the wiring."""
        def imported(path):
            names = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    names.add((node.module or "").split(".")[-1])
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[-1] for alias in node.names)
            return names

        self.assertIn("direction_passages", imported(ROOT / "scripts" / "scan_direction.py"))
        self.assertIn("direction_signals", imported(ROOT / "gate_controller" / "direction_passages.py"))
        unit = (ROOT / "deployment" / "systemd" / "gate-direction-scan.service").read_text(encoding="utf-8")
        self.assertIn("scripts/scan_direction.py", unit)

    # -- what is sent ----------------------------------------------------------
    def test_every_event_of_the_passage_carries_the_passage_verdict(self):
        """Most frames of a passage show nothing by themselves. They are still
        frames of a car that was leaving."""
        self.event(1, 0, b"flank")
        self.event(2, 2, b"rear")
        self.event(3, 5, b"empty")

        sent = self.run_scan().sent

        self.assertEqual(sorted(sent), [1, 2, 3])
        for entry in sent.values():
            self.assertEqual(entry["direction"], "exiting")
            self.assertEqual(entry["method"], "combined-v1")
            self.assertEqual(entry["signals"], ["vision"])
            self.assertTrue(0.0 <= entry["score"] <= 1.0)
        self.assertEqual(sent[1]["idempotency_key"], "k1")

    def test_only_the_keys_the_deployed_dashboard_reads_or_ignores_are_sent(self):
        self.event(1, 0, b"front")

        body = self.run_scan().posts[0]

        self.assertEqual(sorted(body), ["controller_id", "directions"])
        self.assertEqual(sorted(body["directions"][0]),
                         ["direction", "event_id", "idempotency_key", "method", "score", "signals"])

    def test_a_departure_the_relay_fired_for_is_sent_as_a_departure(self):
        """gate-controller#171: the rear plate matched, the relay fired, and the
        car was on its way out through a gate opened from inside."""
        self.gate_was_heard()
        self.cycle(-28)
        self.event(1, 0, b"rear", relay_after=5.4)

        sent = self.run_scan().sent

        self.assertEqual(sent[1]["direction"], "exiting")
        self.assertEqual(sent[1]["signals"], ["vision", "gate_opened_from_inside"])
        self.assertGreater(sent[1]["score"], 0.9)

    def test_an_arrival_the_photo_missed_is_found_from_the_gate(self):
        """2026-09-20 09:57: the car sat in the corner of the frame, outside the
        model's crop; the gate had been still and then opened on our relay."""
        self.gate_was_heard()
        self.cycle(-3)
        self.event(1, 0, b"empty", relay_after=1.8,
                   box_width={"verdict": "entering", "method": "box_width", "score": 0.4})

        sent = self.run_scan().sent

        self.assertEqual(sent[1]["direction"], "entering")
        self.assertEqual(sent[1]["signals"], ["gate_opened_after_seen", "box_width"])

    def test_the_car_seen_waiting_is_the_car_that_then_drives_through(self):
        self.event(1, 0, b"front")
        self.event(2, 28, b"flank")
        self.event(3, 30, b"empty")

        sent = self.run_scan().sent

        self.assertEqual(sent[2]["direction"], "entering")
        self.assertEqual(sent[3]["signals"], ["same_arrival"])
        self.assertLess(sent[2]["score"], sent[1]["score"])

    def test_a_passage_nothing_can_be_said_about_is_not_sent(self):
        self.event(1, 0, b"empty")
        self.event(2, 300, b"machine")
        self.event(3, 600, source="remote_command", relay_after=0.0)

        dashboard = self.run_scan()

        self.assertEqual(dashboard.posts, [])
        self.assertEqual({row["verdict"] for row in self.kept().values()}, {"unknown"})

    def test_a_farm_machine_is_not_judged_from_the_sound_of_its_own_engine(self):
        self.gate_was_heard()
        self.cycle(-45)
        self.event(1, 0, b"machine")

        self.assertEqual(self.run_scan().posts, [])

    # -- evidence that arrives late -------------------------------------------
    def test_nothing_is_sent_twice_and_a_late_movement_is_sent_once(self):
        self.event(1, 0, b"dim rear")
        first = self.run_scan()
        self.assertEqual(first.sent[1]["signals"], ["vision"])

        self.assertEqual(self.run_scan().posts, [], "an unchanged verdict was sent again")

        # Twenty minutes later the gate-sound scan writes what the gate did.
        self.gate_was_heard()
        self.cycle(-28)
        third = self.run_scan()

        self.assertEqual(third.sent[1]["signals"], ["vision", "gate_opened_from_inside"])
        self.assertGreater(third.sent[1]["score"], first.sent[1]["score"])
        self.assertEqual(self.run_scan().posts, [])

    def test_strong_signals_that_disagree_retract_what_was_said(self):
        """A front in the photo and a gate opened from inside cannot both be
        right. The passage becomes unknown, and the dashboard -- which was told
        ``entering`` before the movement was known -- has to be told."""
        self.event(1, 0, b"front")
        self.assertEqual(self.run_scan().sent[1]["direction"], "entering")
        self.gate_was_heard()
        self.cycle(-28)

        # The dashboard deployed today skips an unknown and says nothing of it.
        old = self.run_scan()
        self.assertEqual(old.sent[1], {"event_id": 1, "idempotency_key": "k1", "method": "combined-v1",
                                       "direction": "unknown", "score": 0.0, "retract": True})
        kept = self.kept()["e1"]
        self.assertEqual((kept["verdict"], kept["conflict"]), ("unknown", 1))

        # ...so it is offered again, until one that understands it answers.
        new = self.run_scan(dashboard=FakeDashboard(self.photos, understands_retract=True))
        self.assertTrue(new.sent[1]["retract"])
        self.assertEqual(self.run_scan().posts, [])

    def test_a_retraction_is_never_mixed_into_a_batch_of_verdicts(self):
        self.event(1, 0, b"front")
        self.run_scan()
        self.gate_was_heard()
        self.cycle(-28)
        self.event(2, 900, b"rear")

        posts = self.run_scan().posts

        self.assertEqual([[d["direction"] for d in body["directions"]] for body in posts],
                         [["exiting"], ["unknown"]])

    def test_the_live_width_fit_already_on_the_dashboard_is_corrected(self):
        """2026-09-19 18:35: the event went up saying ``entering`` from the
        slope fit. The photo shows a rear."""
        self.event(1, 0, b"rear", box_width={"verdict": "entering", "method": "box_width", "score": 0.26})

        self.assertEqual(self.run_scan().sent[1]["direction"], "exiting")

    # -- what is kept ------------------------------------------------------------
    def test_every_signal_is_kept_beside_the_verdict(self):
        self.gate_was_heard()
        self.cycle(-28)
        self.event(1, 0, b"rear", box_width={"verdict": "entering", "method": "box_width", "score": 0.26})

        self.run_scan("--no-ship")
        kept = self.kept()["e1"]

        self.assertEqual(kept["verdict"], "exiting")
        self.assertEqual(kept["contributing"], ["vision", "gate_opened_from_inside"])
        self.assertEqual([(s["method"], s["verdict"]) for s in kept["signals"]],
                         [("vision", "exiting"), ("gate_opened_from_inside", "exiting"),
                          ("box_width", "entering")])

    def test_no_ship_judges_and_keeps_and_sends_nothing(self):
        self.event(1, 0, b"rear")

        dashboard = self.run_scan("--no-ship")

        self.assertEqual(dashboard.posts, [])
        self.assertEqual(self.kept()["e1"]["verdict"], "exiting")

    def test_a_gate_cycle_nobody_was_seen_at_is_kept_as_a_departure_and_not_sent(self):
        """There is no event to hang it on, and the dashboard has no row for it."""
        self.gate_was_heard()
        self.cycle(0)

        dashboard = self.run_scan()
        (key, kept), = self.kept().items()

        self.assertTrue(key.startswith("m"))
        self.assertEqual((kept["kind"], kept["verdict"], kept["contributing"]),
                         ("gate_only", "exiting", ["gate_moved_unseen"]))
        self.assertEqual(dashboard.posts, [])

    def test_without_the_model_the_gate_is_still_heard(self):
        self.gate_was_heard()
        self.cycle(-28)
        self.event(1, 0)

        sent = self.run_scan(model=MissingModel).sent

        self.assertEqual(sent[1]["direction"], "exiting")
        self.assertEqual(sent[1]["signals"], ["gate_opened_from_inside"])

    def test_without_credentials_it_still_judges_and_keeps(self):
        self.gate_was_heard()
        self.cycle(-28)
        self.event(1, 0)

        dashboard = self.run_scan(environment={})

        self.assertEqual(dashboard.posts, [])
        self.assertEqual(self.kept()["e1"]["verdict"], "exiting")

    def test_a_passage_still_arriving_waits_for_the_next_pass(self):
        """Its events may not be on the dashboard yet, and an update for a row
        that is not there changes nothing and is not reported."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO events (id, received_at, source, reason, opened, idempotency_key)"
                " VALUES (1, ?, 'ocr', 'no_match', 0, 'k1')",
                ((datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat(),))
        self.photos[1] = b"rear"

        dashboard = self.run_scan()

        self.assertEqual(dashboard.posts, [])
        self.assertEqual(self.kept()["e1"]["verdict"], "exiting")

    def test_a_failed_send_is_tried_again_next_pass(self):
        self.event(1, 0, b"rear")

        class Refusing(FakeDashboard):
            def __call__(self, req, timeout=None):
                if req.get_method() == "POST":
                    raise OSError("unreachable")
                return super().__call__(req, timeout)

        self.run_scan(dashboard=Refusing(self.photos))

        self.assertEqual(self.run_scan().sent[1]["direction"], "exiting")

    def test_the_relay_is_nowhere_near_any_of_this(self):
        """Direction is a label. Nothing on this path may reach the gate."""
        for name in ("scripts/scan_direction.py", "gate_controller/direction_passages.py",
                     "gate_controller/direction_signals.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            for forbidden in ("import relay", "from .relay", "actuation", "decide_access", "PiRelay"):
                self.assertNotIn(forbidden, source, f"{name} mentions {forbidden}")


if __name__ == "__main__":
    unittest.main()
