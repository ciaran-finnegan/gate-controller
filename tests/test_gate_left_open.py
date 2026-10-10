"""The "gate may have been left open" check, through the path production takes.

Every test here starts from what the sound scanner writes (`record()` into the
real store's tables), reads it back through the heartbeat's own status
assembly (`_controller_status`), and asserts on the `gate.left_open` block the
Worker receives. None of them calls `evaluate()` on a hand-built row, because
a helper tested alone is how things ship that nothing calls.
"""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import gate_controller.__main__ as gate_main
from gate_controller.actuation import ActuationCoordinator
from gate_controller.gate_audio_detect import Clang, MotorRun, movements_from
from gate_controller.gate_left_open import (
    DEFAULT_THRESHOLD_MINUTES, LIKELY, POSSIBLE, LeftOpenConfig, config_from_settings,
    load_config,
)
from gate_controller.gate_sound_scan import Listening, record
from gate_controller.relay import RelayController
from gate_controller.settings import MatchPolicyCache
from gate_controller.store import LocalStore

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]

#: 2026-10-09, as the scanner wrote it (UTC; 21:41-21:42 IST). The operator's
#: exit loop opened the gate for a departing car (17 s), the auto-close
#: started (9 s), the car broke the beam and it reversed open (11 s). Then
#: nothing until the owner drove in through the open gate at 22:21 IST.
NINTH_OF_OCTOBER = [
    ("2026-10-09T20:41:38.400+00:00", "2026-10-09T20:41:55.680+00:00"),
    ("2026-10-09T20:42:08.640+00:00", "2026-10-09T20:42:17.760+00:00"),
    ("2026-10-09T20:42:23.840+00:00", "2026-10-09T20:42:34.880+00:00"),
]
LAST_HEARD = datetime(2026, 10, 9, 20, 42, 34, 880000, tzinfo=UTC)


class Prompt:
    available = False


class RelaySpy:
    """A relay that reports itself ready and records anything else asked of it."""

    def __init__(self):
        self.calls = []

    def status(self):
        return {"ready": True, "last_outcome": "initialized_safe", "last_outcome_at": None}

    def __getattr__(self, name):
        def record_call(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"the left-open check reached the relay: {name}")
        return record_call


class LeftOpenThroughTheHeartbeat(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = LocalStore(Path(self.directory.name) / "gate.db")
        self.connection = sqlite3.connect(self.store.path)
        self.addCleanup(self.connection.close)

    # -- what the scanner writes ------------------------------------------------

    def scan(self, runs, *, latch_at=None, initial_state="shut", segment="gate-a.aac"):
        """Write runs exactly as `scripts/scan_gate_sound.py` does."""
        motor = [MotorRun(start=datetime.fromisoformat(a), end=datetime.fromisoformat(b))
                 for a, b in runs]
        clangs = [Clang(at=latch_at, high_share=0.7, peak_dbfs=-25.0)] if latch_at else []
        moves = movements_from(motor, clangs, initial_state=initial_state)
        record(self.connection, moves, [(segment, 100)])

    def listened(self, start: datetime, minutes: int, *, heard_fraction: float = 1.0):
        """Five-minute segments the scanner measured, from `start` for `minutes`."""
        coverage = []
        at = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % 5)
        while at < start + timedelta(minutes=minutes):
            coverage.append({
                "segment": f"gate-{at:%Y%m%dT%H%M%S}Z.aac", "started_at": at,
                "span_seconds": 300.0, "audio_seconds": 300.0 * heard_fraction,
                "missing_seconds": 300.0 * (1 - heard_fraction),
            })
            at += timedelta(minutes=5)
        record(self.connection, [], [], listening=Listening(tuple(coverage), ()))

    def heartbeat(self, now: datetime, *, relay=None, match_policy=None, defaults=None):
        return gate_main._controller_status(
            self.store, Prompt(), {}, relay=relay or RelaySpy(),
            match_policy=match_policy, left_open_defaults=defaults,
            clock=lambda: now,
        )

    # -- the case it exists for -------------------------------------------------

    def test_the_ninth_of_october_is_reported_as_likely_left_open(self):
        """The 39 minutes nobody knew about, replayed from the scanner's rows."""
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 15)

        status = self.heartbeat(LAST_HEARD + timedelta(minutes=12))

        left_open = status["gate"]["left_open"]
        self.assertEqual(left_open["state"], "open")
        self.assertEqual(left_open["confidence"], LIKELY)
        self.assertEqual(left_open["reason"], "closing_interrupted")
        self.assertEqual(left_open["runs"], 3)
        self.assertEqual(datetime.fromisoformat(left_open["since"]), LAST_HEARD)
        self.assertEqual(left_open["threshold_minutes"], DEFAULT_THRESHOLD_MINUTES)

    def test_it_never_reaches_the_relay_or_the_actuation_coordinator(self):
        """Notify only (CLAUDE.md rules 1-3). Detection must not be able to pulse.

        The heartbeat is assembled with a relay that fails the test if anything
        but its status is read, and with the real relay controller's trigger and
        the coordinator's actuate both replaced by tripwires, so no path through
        the check -- including one added later -- can send a pulse unnoticed.
        """
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 15)
        relay = RelaySpy()
        tripped = []

        def tripwire(*args, **kwargs):
            tripped.append(args)
            raise AssertionError("the left-open check tried to actuate the gate")

        with patch.object(RelayController, "trigger", tripwire), \
                patch.object(ActuationCoordinator, "actuate", tripwire):
            status = self.heartbeat(LAST_HEARD + timedelta(minutes=12), relay=relay)

        self.assertEqual(status["gate"]["left_open"]["state"], "open")
        self.assertEqual(relay.calls, [])
        self.assertEqual(tripped, [])

    def test_the_check_imports_nothing_that_can_move_the_gate(self):
        """A fresh interpreter: loading the check must not load the relay."""
        loaded = subprocess.run(
            [sys.executable, "-c",
             "import sys, gate_controller.gate_left_open;"
             "print(' '.join(sorted(m for m in sys.modules if m.startswith('gate_controller'))))"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        for forbidden in ("relay", "relay_safe", "actuation", "command_server", "processor",
                          "worker", "control_plane"):
            self.assertNotIn(f"gate_controller.{forbidden}", loaded)

    # -- what it must not claim -------------------------------------------------

    def test_an_ordinary_cycle_with_no_latch_heard_is_only_possible(self):
        """10 of 22 recorded commanded cycles had a latch heard; most do not.

        Open and close with no clang, then silence, is what nearly every
        missed closing looks like. It is reported, at the lower confidence the
        app does not push.
        """
        start = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
        self.scan([
            (start.isoformat(), (start + timedelta(seconds=20)).isoformat()),
            ((start + timedelta(seconds=40)).isoformat(), (start + timedelta(seconds=62)).isoformat()),
        ])
        self.listened(start, 20)

        left_open = self.heartbeat(start + timedelta(minutes=15))["gate"]["left_open"]

        self.assertEqual(left_open["state"], "open")
        self.assertEqual(left_open["confidence"], POSSIBLE)

    def test_a_latch_heard_at_the_end_clears_it(self):
        self.scan(NINTH_OF_OCTOBER, latch_at=LAST_HEARD - timedelta(seconds=1))
        self.listened(LAST_HEARD - timedelta(minutes=2), 15)

        left_open = self.heartbeat(LAST_HEARD + timedelta(minutes=12))["gate"]["left_open"]

        self.assertEqual(left_open["state"], "clear")
        self.assertEqual(left_open["reason"], "latched")

    def test_the_next_movement_ends_the_episode(self):
        """`since` is the episode key; a new movement moves it, which clears the alert."""
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 45)
        first = self.heartbeat(LAST_HEARD + timedelta(minutes=12))["gate"]["left_open"]
        arrival = datetime(2026, 10, 9, 21, 21, 47, tzinfo=UTC)
        self.scan([(arrival.isoformat(), (arrival + timedelta(seconds=24)).isoformat())],
                  initial_state="open", segment="gate-b.aac")

        after = self.heartbeat(arrival + timedelta(minutes=1))["gate"]["left_open"]

        self.assertEqual(first["state"], "open")
        self.assertNotEqual(after["since"], first["since"])
        self.assertNotEqual(after["state"], "open")

    def test_silence_from_a_recorder_that_was_not_listening_is_unknown(self):
        """The recorder held 70% of the wall clock over the measured weeks."""
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 15, heard_fraction=0.5)

        left_open = self.heartbeat(LAST_HEARD + timedelta(minutes=12))["gate"]["left_open"]

        self.assertEqual(left_open["state"], "unknown")
        self.assertEqual(left_open["reason"], "not_listening")

    def test_nothing_is_claimed_before_the_scan_has_reached_the_threshold(self):
        """The scanner runs quarter-hourly; a movement may sit in unread audio."""
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 5)

        left_open = self.heartbeat(LAST_HEARD + timedelta(minutes=30))["gate"]["left_open"]

        self.assertEqual(left_open["state"], "clear")
        self.assertEqual(left_open["reason"], "within_threshold")

    # -- configuration ----------------------------------------------------------

    def test_the_owner_can_switch_it_off_from_the_app(self):
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 15)
        cache = MatchPolicyCache(Path(self.directory.name) / "match-policy.json")
        cache.replace({"controller_id": "primary", "settings_version": 1,
                       "gate_left_open": {"enabled": False, "threshold_minutes": 10}})

        left_open = self.heartbeat(LAST_HEARD + timedelta(minutes=12),
                                   match_policy=cache)["gate"]["left_open"]

        self.assertEqual(left_open["state"], "off")
        self.assertEqual(left_open["config_source"], "app")

    def test_the_apps_threshold_is_used_and_survives_a_restart(self):
        self.scan(NINTH_OF_OCTOBER)
        self.listened(LAST_HEARD - timedelta(minutes=2), 15)
        path = Path(self.directory.name) / "match-policy.json"
        MatchPolicyCache(path).replace({
            "controller_id": "primary", "settings_version": 1,
            "gate_left_open": {"enabled": True, "threshold_minutes": 30},
        })

        left_open = self.heartbeat(LAST_HEARD + timedelta(minutes=12),
                                   match_policy=MatchPolicyCache(path))["gate"]["left_open"]

        self.assertEqual(left_open["threshold_minutes"], 30)
        self.assertEqual(left_open["state"], "clear", "12 minutes is inside a 30-minute threshold")

    def test_a_switch_sent_beside_a_refused_schedule_survives_a_restart(self):
        """The two halves of the envelope are accepted independently."""
        path = Path(self.directory.name) / "match-policy.json"
        MatchPolicyCache(path).replace({
            "controller_id": "primary", "settings_version": 1,
            "gate_left_open": {"enabled": True, "threshold_minutes": 10},
        })
        MatchPolicyCache(path).replace({
            "controller_id": "primary", "settings_version": 1,
            "plate_matching": {"schema_version": 99},
            "gate_left_open": {"enabled": False, "threshold_minutes": 10},
        })

        restarted = MatchPolicyCache(path)

        self.assertFalse(restarted.gate_left_open(LeftOpenConfig()).enabled)
        self.assertIsNotNone(restarted.status()["last_error"], "the schedule was refused")

    def test_an_hour_long_segment_that_began_before_the_movement_still_counts(self):
        """GATE_AUDIO_SEGMENTS_SECONDS may be up to 3600."""
        self.scan(NINTH_OF_OCTOBER)
        start = LAST_HEARD - timedelta(minutes=40)
        record(self.connection, [], [], listening=Listening(({
            "segment": "gate-long.aac", "started_at": start, "span_seconds": 3600.0,
            "audio_seconds": 3600.0, "missing_seconds": 0.0,
        },), ()))

        left_open = self.heartbeat(LAST_HEARD + timedelta(minutes=25))["gate"]["left_open"]

        self.assertEqual(left_open["state"], "open")
        self.assertEqual(left_open["confidence"], LIKELY)

    def test_a_section_too_large_to_keep_is_not_adopted(self):
        path = Path(self.directory.name) / "match-policy.json"
        cache = MatchPolicyCache(path)
        cache.replace({"controller_id": "primary", "settings_version": 1,
                       "gate_left_open": {"enabled": False, "threshold_minutes": 10}})
        cache.replace({"controller_id": "primary", "settings_version": 1,
                       "gate_left_open": {"enabled": True, "threshold_minutes": 10,
                                          "padding": "x" * 20000}})

        self.assertFalse(cache.gate_left_open(LeftOpenConfig()).enabled)
        self.assertFalse(MatchPolicyCache(path).gate_left_open(LeftOpenConfig()).enabled)

    def test_a_malformed_setting_changes_nothing_about_plate_matching(self):
        """The section shares the schedule's envelope and must never fail it closed."""
        cache = MatchPolicyCache(Path(self.directory.name) / "match-policy.json")
        before = cache.get()
        cache.replace({"controller_id": "primary", "settings_version": 1,
                       "gate_left_open": {"enabled": "yes", "threshold_minutes": 1}})

        self.assertIs(cache.get(), before)
        self.assertIsNone(cache.status()["last_error"])
        self.assertEqual(cache.gate_left_open(LeftOpenConfig()), LeftOpenConfig())

    def test_environment_defaults_are_bounded_and_never_raise(self):
        self.assertEqual(load_config({}), LeftOpenConfig())
        self.assertEqual(load_config({"GATE_LEFT_OPEN_MINUTES": "15"}).threshold_minutes, 15)
        self.assertEqual(load_config({"GATE_LEFT_OPEN_MINUTES": "1"}).threshold_minutes,
                         DEFAULT_THRESHOLD_MINUTES)
        self.assertEqual(load_config({"GATE_LEFT_OPEN_MINUTES": "soon"}).threshold_minutes,
                         DEFAULT_THRESHOLD_MINUTES)
        self.assertFalse(load_config({"GATE_LEFT_OPEN_ALERT_ENABLED": "0"}).enabled)
        self.assertEqual(config_from_settings({"enabled": True, "threshold_minutes": 999},
                                              LeftOpenConfig()), LeftOpenConfig())


if __name__ == "__main__":
    unittest.main()
