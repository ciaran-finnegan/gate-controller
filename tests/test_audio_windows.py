"""Cutting windows out of the recorded segments, and labelling them for free."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gate_controller.audio_segments import SegmentStore, iter_adts_frames
from gate_controller.audio_windows import (
    build_sidecar, extract_windows, plan_windows, read_events, Window,
)
from tests.test_audio_segments import stream

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def event(offset_seconds: float, *, identifier: int = 1, relay: float | None = None,
          opened: int = 0, **extra) -> dict:
    moment = NOW + timedelta(seconds=offset_seconds)
    row = {
        "id": identifier,
        "received_at": moment.isoformat(),
        "decision_at": None,
        "relay_activated_at": (
            (moment + timedelta(seconds=relay)).isoformat() if relay is not None else None
        ),
        "source": "reolink_webhook",
        "reason": "vehicle_detected_from_front_gate",
        "opened": opened,
        "observed_plate": None,
        "authorised_plate": None,
        "ocr_confidence": None,
        "actuation_outcome": None,
    }
    row.update(extra)
    return row


class WindowPlanningTests(unittest.TestCase):
    def test_a_window_reaches_back_before_the_event(self):
        # The pre-roll is the whole point: the vehicle and the motor start
        # before the relay fires, which the per-event recorder could never see.
        window = plan_windows([event(0)], before_seconds=60, after_seconds=180)[0]
        self.assertEqual(window.start, NOW - timedelta(seconds=60))
        self.assertEqual(window.end, NOW + timedelta(seconds=180))

    def test_events_far_apart_get_their_own_windows(self):
        windows = plan_windows([event(0), event(3600, identifier=2)])
        self.assertEqual(len(windows), 2)

    def test_overlapping_events_are_merged_into_one_cut(self):
        # Two clips of mostly the same audio would inflate whichever class
        # happens to arrive in bursts.
        windows = plan_windows([event(0), event(30, identifier=2)],
                               before_seconds=60, after_seconds=180)
        self.assertEqual(len(windows), 1)
        self.assertEqual(len(windows[0].events), 2)
        self.assertEqual(windows[0].end, NOW + timedelta(seconds=210))

    def test_a_burst_collapses_to_a_single_window(self):
        events = [event(index * 20, identifier=index) for index in range(8)]
        windows = plan_windows(events)
        self.assertEqual(len(windows), 1)
        self.assertEqual(len(windows[0].events), 8)

    def test_an_event_with_no_timestamp_is_skipped_not_guessed(self):
        broken = event(0)
        broken["received_at"] = None
        self.assertEqual(plan_windows([broken]), [])

    def test_a_window_outside_the_permitted_bounds_is_refused(self):
        with self.assertRaises(ValueError):
            plan_windows([event(0)], before_seconds=0.5)
        with self.assertRaises(ValueError):
            plan_windows([event(0)], after_seconds=99999)


class SidecarTests(unittest.TestCase):
    def test_an_actuation_inside_the_window_labels_it_and_places_the_relay(self):
        window = plan_windows([event(0, relay=2.0, opened=1)])[0]
        sidecar = build_sidecar(window, captured_at=window.start, audio_seconds=240.0,
                                requested_seconds=240.0, source_url="rtsp://127.0.0.1:8554/clear")
        self.assertEqual(sidecar["label"], "actuated")
        self.assertEqual(len(sidecar["actuations"]), 1)
        # The relay is 60 s of pre-roll plus its own 2 s delay into the clip.
        self.assertAlmostEqual(sidecar["actuations"][0]["offset_seconds"], 62.0, places=2)

    def test_a_passage_that_never_moved_the_gate_is_a_negative(self):
        window = plan_windows([event(0)])[0]
        sidecar = build_sidecar(window, captured_at=window.start, audio_seconds=240.0,
                                requested_seconds=240.0, source_url="")
        self.assertEqual(sidecar["label"], "no_actuation")
        self.assertIsNone(sidecar["actuations"])

    def test_the_sidecar_says_the_audio_was_copied_and_not_decoded(self):
        window = plan_windows([event(0)])[0]
        audio = build_sidecar(window, captured_at=window.start, audio_seconds=1.0,
                              requested_seconds=240.0, source_url="")["audio"]
        self.assertTrue(audio["raw_copy"])
        self.assertFalse(audio["decoded"])
        self.assertEqual(audio["sample_rate_hz"], 16000)

    def test_a_short_recovery_is_declared_rather_than_hidden(self):
        window = plan_windows([event(0)])[0]
        sidecar = build_sidecar(window, captured_at=window.start, audio_seconds=12.0,
                                requested_seconds=240.0, source_url="")
        self.assertFalse(sidecar["window"]["complete"])
        self.assertEqual(sidecar["window"]["recovered_seconds"], 12.0)

    def test_every_event_in_a_merged_window_is_recorded(self):
        window = plan_windows([event(0), event(30, identifier=2, relay=1.0)])[0]
        sidecar = build_sidecar(window, captured_at=window.start, audio_seconds=1.0,
                                requested_seconds=1.0, source_url="")
        self.assertEqual([row["id"] for row in sidecar["events"]], [1, 2])

    def test_the_artefact_block_routes_it_through_the_existing_uploader(self):
        window = plan_windows([event(0)])[0]
        sidecar = build_sidecar(window, captured_at=window.start, audio_seconds=1.0,
                                requested_seconds=1.0, source_url="")
        self.assertEqual(sidecar["artefact"], {"kind": "audio", "media_type": "audio/aac"})


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.segments = self.root / "segments"
        self.segments.mkdir()
        self.corpus = self.root / "corpus" / "audio"
        self.database = self.root / "gate.db"
        self._build_database()
        self.store = SegmentStore(self.segments, clock=lambda: NOW + timedelta(hours=1))

    def _build_database(self, rows=()):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, received_at TEXT, decision_at TEXT,"
            " relay_activated_at TEXT, source TEXT, reason TEXT, opened INTEGER,"
            " idempotency_key TEXT, authorised_plate TEXT, observed_plate TEXT,"
            " ocr_confidence REAL, actuation_outcome TEXT)"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO events (id, received_at, relay_activated_at, source, reason,"
                " opened, actuation_outcome) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["received_at"], row["relay_activated_at"], row["source"],
                 row["reason"], row["opened"], row["actuation_outcome"]),
            )
        connection.commit()
        connection.close()

    def _write_segment(self, started_at: datetime, frames: int) -> None:
        path = self.segments / f"gate-{started_at.strftime('%Y%m%dT%H%M%S')}Z.aac"
        path.write_bytes(stream(frames))

    def test_events_are_read_only_within_the_period(self):
        self.database.unlink()
        self._build_database([event(0), event(7200, identifier=2)])
        rows = read_events(self.database, NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        self.assertEqual([row["id"] for row in rows], [1])

    def test_a_window_is_written_as_a_matching_private_pair(self):
        self.database.unlink()
        self._build_database([event(0, relay=2.0, opened=1)])
        # 9000 frames is 576 s, comfortably covering the window.
        self._write_segment(NOW - timedelta(seconds=120), 9000)
        report = extract_windows(
            database=self.database, store=self.store, corpus_directory=self.corpus,
            start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=5),
            source_url="rtsp://127.0.0.1:8554/clear")
        self.assertEqual(report["written"], 1)
        payloads = sorted(self.corpus.glob("*.aac"))
        sidecars = sorted(self.corpus.glob("*.json"))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].stem, sidecars[0].stem)
        self.assertEqual(payloads[0].stat().st_mode & 0o777, 0o600)
        # What was written is whole frames and nothing else.
        data = payloads[0].read_bytes()
        self.assertEqual(sum(length for _, length, _ in iter_adts_frames(data)), len(data))
        sidecar = json.loads(sidecars[0].read_text())
        self.assertEqual(sidecar["label"], "actuated")

    def test_a_window_with_no_recorded_audio_is_counted_not_invented(self):
        self.database.unlink()
        self._build_database([event(0)])
        report = extract_windows(
            database=self.database, store=self.store, corpus_directory=self.corpus,
            start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=5))
        self.assertEqual(report["written"], 0)
        self.assertEqual(report["empty"], 1)
        self.assertFalse(self.corpus.exists() and any(self.corpus.iterdir()))

    def test_a_dry_run_reports_without_writing_anything(self):
        self.database.unlink()
        self._build_database([event(0)])
        self._write_segment(NOW - timedelta(seconds=120), 9000)
        report = extract_windows(
            database=self.database, store=self.store, corpus_directory=self.corpus,
            start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=5), dry_run=True)
        self.assertEqual(report["written"], 1)
        self.assertFalse(self.corpus.exists() and any(self.corpus.iterdir()))

    def test_the_database_is_opened_read_only(self):
        self.database.unlink()
        self._build_database([event(0)])
        # A writable connection here could take a lock a gate decision needs.
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO events (id, received_at) VALUES (99, 'x')")
        connection.close()


if __name__ == "__main__":
    unittest.main()
