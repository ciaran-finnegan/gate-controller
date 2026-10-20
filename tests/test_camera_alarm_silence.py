"""Camera-alarm silence, through the path production takes.

From 2026-10-18 12:52 to 2026-10-20 11:26 UTC no camera alarm reached the Pi
while the early trigger kept firing, and the app showed two quiet days. Every test here
starts from what the early trigger writes -- the real `EarlyTriggerWorker`
recording its would-triggers and the camera's alarms into the real
`early-trigger.db` -- and ends with the payload the real `HeartbeatWorker`
posts to the Worker, built by `build_background_workers` exactly as `main`
builds it. None of them calls `evaluate()` on a hand-built row.
"""
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gate_controller import early_trigger
from gate_controller.__main__ import build_background_workers
from gate_controller.actuation import ActuationCoordinator
from gate_controller.camera_silence import (
    DEFAULT_HOURS, DEFAULT_WOULD_TRIGGERS, build_monitor,
)
from gate_controller.control_plane import HeartbeatWorker
from gate_controller.early_trigger import EarlyTriggerStore, EarlyTriggerWorker, load_config
from gate_controller.relay import RelayController
from gate_controller.store import LocalStore

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARE = {
    "GATE_CLOUDFLARE_API_URL": "https://gate.example.com",
    "GATE_CLOUDFLARE_ACCESS_CLIENT_ID": "client-id",
    "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET": "client-secret",
    "GATE_CONTROLLER_ID": "primary",
}
#: The camera's last alarm before it fell silent: the Audi, 2026-10-18.
LAST_ALARM = datetime(2026, 10, 18, 12, 52, 0, tzinfo=UTC)
#: Its first alarm after it came back.
FIRST_BACK = datetime(2026, 10, 20, 11, 26, 0, tzinfo=UTC)


class Event:
    event_type = "webhook"


class Wire:
    """The Worker's end of the heartbeat: keeps every body posted to it."""

    def __init__(self):
        self.posted = []

    def post_json(self, path, body):
        assert path == "/api/controller/status", path
        self.posted.append(body)


class RelaySpy:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"ready": True, "last_outcome": "initialized_safe", "last_outcome_at": None}

    def __getattr__(self, name):
        def record_call(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"the camera-silence check reached the relay: {name}")
        return record_call


def vision(light="day"):
    return early_trigger.Observation("trigger", True, light, {
        "blob_fraction": 0.1, "changed_fraction": 0.1, "scatter": 0.0, "mean_luma": 100.0,
        "luma_jump": 0.0, "peak": 200, "persistence": 2, "cx": 0.1, "cy": 0.4,
        "blob_contrast": -0.5, "blob_spread": 0.5, "scene_difference": 0.05})


class CameraSilenceThroughTheHeartbeat(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.now = LAST_ALARM - timedelta(hours=1)
        # The early trigger as the controller runs it in shadow: its own
        # store beside the controller's database, the real worker writing.
        self.worker = EarlyTriggerWorker(
            load_config({"GATE_EARLY_TRIGGER": "shadow"}, self.state),
            store=EarlyTriggerStore(self.state / "early-trigger.db"),
            wall_clock=lambda: self.now.timestamp(),
        )
        self.relay = RelaySpy()
        self.store = LocalStore(self.state / "gate.db")
        self.wire = Wire()

    # -- what the early trigger writes ------------------------------------------

    def alarm(self, at: datetime):
        self.now = at
        self.worker.note_camera_alarm(Event())

    def would_trigger(self, at: datetime, light="day"):
        self.now = at
        self.worker._would_trigger(vision(light))

    def vehicles(self, start: datetime, count: int, every: timedelta):
        for index in range(count):
            self.would_trigger(start + index * every)

    # -- what the Worker receives -----------------------------------------------

    def heartbeat(self, at: datetime, environment=None):
        """One heartbeat at `at`, through the workers `main` builds."""
        self.now = at
        monitor = build_monitor(environment or {}, self.worker,
                                wall_clock=lambda: self.now.timestamp())
        workers, _, _ = build_background_workers(
            self.store, relay=self.relay, environment={**CLOUDFLARE, **(environment or {})},
            camera_silence=monitor,
        )
        heartbeat = next(w for w in workers if isinstance(w, HeartbeatWorker))
        heartbeat._control_plane.client = self.wire
        self.assertTrue(heartbeat.run_once())
        return self.wire.posted[-1]

    def heartbeats(self, monitor, *moments):
        """Several heartbeats from one running controller (one monitor)."""
        workers, _, _ = build_background_workers(
            self.store, relay=self.relay, environment=CLOUDFLARE, camera_silence=monitor,
        )
        heartbeat = next(w for w in workers if isinstance(w, HeartbeatWorker))
        heartbeat._control_plane.client = self.wire
        posted = []
        for moment in moments:
            self.now = moment
            self.assertTrue(heartbeat.run_once())
            posted.append(self.wire.posted[-1]["camera_alarms"])
        return posted

    # -- the case it exists for -------------------------------------------------

    def test_the_nineteenth_of_october_is_reported_silent(self):
        """0 camera alarms for 21 would-triggers, as on 19 Oct."""
        self.alarm(LAST_ALARM)
        self.vehicles(datetime(2026, 10, 19, 6, 0, tzinfo=UTC), 21, timedelta(minutes=30))

        payload = self.heartbeat(datetime(2026, 10, 19, 16, 0, tzinfo=UTC))

        block = payload["camera_alarms"]
        self.assertEqual(block["state"], "silent")
        self.assertEqual(datetime.fromisoformat(block["silent_since"]), LAST_ALARM)
        self.assertEqual(datetime.fromisoformat(block["last_alarm_at"]), LAST_ALARM)
        self.assertEqual(block["would_triggers_without_alarm"], 21)
        self.assertEqual(block["hours_without_alarm"], 27.1)
        self.assertEqual(block["threshold_hours"], DEFAULT_HOURS)
        self.assertEqual(block["threshold_would_triggers"], DEFAULT_WOULD_TRIGGERS)
        self.assertEqual(payload["controller_id"], "primary")

    def test_it_is_journalled_once_when_it_starts_and_once_when_it_clears(self):
        self.alarm(LAST_ALARM)
        self.vehicles(datetime(2026, 10, 19, 6, 0, tzinfo=UTC), 21, timedelta(minutes=30))
        monitor = build_monitor({}, self.worker, clock=lambda: self.now.timestamp(),
                                wall_clock=lambda: self.now.timestamp())
        afternoon = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)

        with self.assertLogs("gate_controller.camera_silence", level="WARNING") as logs:
            during = self.heartbeats(
                monitor, afternoon, afternoon + timedelta(minutes=5),
                afternoon + timedelta(hours=12))
            self.alarm(FIRST_BACK)
            after = self.heartbeats(monitor, FIRST_BACK + timedelta(minutes=2),
                                    FIRST_BACK + timedelta(minutes=10))

        self.assertEqual([b["state"] for b in during], ["silent"] * 3)
        self.assertEqual(len({b["silent_since"] for b in during}), 1)
        self.assertEqual([b["state"] for b in after], ["clear", "clear"])
        self.assertIsNone(after[0]["silent_since"])
        self.assertEqual(len(logs.output), 2, logs.output)
        self.assertIn("stage=silent silent_since=2026-10-18T12:52:00+00:00", logs.output[0])
        self.assertIn("would_triggers=21", logs.output[0])
        self.assertIn("stage=cleared silent_since=2026-10-18T12:52:00+00:00", logs.output[1])
        self.assertIn("last_alarm_at=2026-10-20T11:26:00+00:00", logs.output[1])

    def test_it_never_reaches_the_relay_or_the_actuation_coordinator(self):
        """Report only (CLAUDE.md rule 3): no pulse, no recovery, whatever it says."""
        self.alarm(LAST_ALARM)
        self.vehicles(datetime(2026, 10, 19, 6, 0, tzinfo=UTC), 21, timedelta(minutes=30))
        tripped = []

        def tripwire(*args, **kwargs):
            tripped.append(args)
            raise AssertionError("the camera-silence check tried to actuate the gate")

        with patch.object(RelayController, "trigger", tripwire), \
                patch.object(ActuationCoordinator, "actuate", tripwire):
            payload = self.heartbeat(datetime(2026, 10, 19, 16, 0, tzinfo=UTC))

        self.assertEqual(payload["camera_alarms"]["state"], "silent")
        self.assertEqual(self.relay.calls, [])
        self.assertEqual(tripped, [])

    def test_the_check_imports_nothing_that_can_move_the_gate(self):
        """A fresh interpreter: loading the check must not load the relay."""
        loaded = subprocess.run(
            [sys.executable, "-c",
             "import sys, gate_controller.camera_silence;"
             "print(' '.join(sorted(m for m in sys.modules if m.startswith('gate_controller'))))"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertIn("gate_controller.camera_silence", loaded)
        for forbidden in ("relay", "relay_safe", "actuation", "command_server", "processor",
                          "worker", "control_plane", "trigger_capture", "camera_control_state"):
            self.assertNotIn(f"gate_controller.{forbidden}", loaded)

    # -- what it must not claim -------------------------------------------------

    def test_a_normal_day_is_clear(self):
        """The camera alarming about half as often as the vision trigger fires."""
        day = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)
        for index in range(32):
            at = day + index * timedelta(minutes=25)
            self.would_trigger(at)
            if index % 3 != 2:  # 22 alarms for 32 would-triggers, as on 16 Oct
                self.alarm(at + timedelta(seconds=4))

        block = self.heartbeat(day + timedelta(hours=18))["camera_alarms"]

        self.assertEqual(block["state"], "clear")
        self.assertIsNone(block["silent_since"])

    def test_the_busiest_ordinary_night_is_clear(self):
        """10-11 Oct: 15 would-triggers in 17.7 h without an alarm, then the morning's first car."""
        evening = datetime(2026, 10, 10, 17, 2, tzinfo=UTC)
        self.alarm(evening)
        self.vehicles(evening + timedelta(hours=1), 15, timedelta(minutes=45))

        block = self.heartbeat(evening + timedelta(hours=17, minutes=40))["camera_alarms"]

        self.assertEqual(block["state"], "clear")
        self.assertEqual(block["would_triggers_without_alarm"], 15)

    def test_the_longest_ordinary_silence_is_clear(self):
        """27-28 Sep: a Sunday with no alarm for 23.1 h and 7 would-triggers."""
        evening = datetime(2026, 9, 27, 18, 52, tzinfo=UTC)
        self.alarm(evening)
        self.vehicles(evening + timedelta(hours=2), 7, timedelta(hours=2))

        block = self.heartbeat(evening + timedelta(hours=23))["camera_alarms"]

        self.assertEqual(block["state"], "clear")
        self.assertEqual(block["hours_without_alarm"], 23.0)

    def test_a_long_quiet_spell_with_nothing_seen_is_not_a_silence(self):
        """No vehicles in front of the vision trigger: the camera had nothing to say."""
        self.alarm(LAST_ALARM)
        self.vehicles(LAST_ALARM + timedelta(hours=1), 3, timedelta(hours=1))

        block = self.heartbeat(LAST_ALARM + timedelta(days=3))["camera_alarms"]

        self.assertEqual(block["state"], "clear")

    def test_a_camera_that_never_alarmed_is_silent_from_the_first_observation(self):
        first = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
        self.vehicles(first, 20, timedelta(hours=1))

        block = self.heartbeat(first + timedelta(hours=21))["camera_alarms"]

        self.assertEqual(block["state"], "silent")
        self.assertEqual(datetime.fromisoformat(block["silent_since"]), first)
        self.assertIsNone(block["last_alarm_at"])

    def test_the_thresholds_are_settable_and_a_bad_value_keeps_the_default(self):
        self.alarm(LAST_ALARM)
        self.vehicles(LAST_ALARM + timedelta(hours=1), 5, timedelta(minutes=30))
        at = LAST_ALARM + timedelta(hours=7)

        tuned = self.heartbeat(at, {"GATE_CAMERA_ALARM_SILENCE_HOURS": "6",
                                    "GATE_CAMERA_ALARM_SILENCE_WOULD_TRIGGERS": "5"})
        with self.assertLogs("gate_controller.camera_silence", level="WARNING"):
            rejected = self.heartbeat(at, {"GATE_CAMERA_ALARM_SILENCE_HOURS": "0.5"})

        self.assertEqual(tuned["camera_alarms"]["state"], "silent")
        self.assertEqual(rejected["camera_alarms"]["threshold_hours"], DEFAULT_HOURS)
        self.assertEqual(rejected["camera_alarms"]["state"], "clear")

    def test_with_the_early_trigger_off_there_is_no_block(self):
        """`off` builds no worker and no record, so nothing can tell silence from quiet."""
        self.assertIsNone(early_trigger.build_worker({}, state_directory=self.state))
        self.assertIsNone(build_monitor({}, None))
        workers, _, _ = build_background_workers(
            self.store, relay=self.relay, environment=CLOUDFLARE,
            camera_silence=build_monitor({}, None),
        )
        heartbeat = next(w for w in workers if isinstance(w, HeartbeatWorker))
        heartbeat._control_plane.client = self.wire
        heartbeat.run_once()
        self.assertNotIn("camera_alarms", self.wire.posted[-1])

    def test_a_record_not_yet_written_is_unknown(self):
        block = self.heartbeat(LAST_ALARM)["camera_alarms"]
        self.assertEqual((block["state"], block["reason"]), ("unknown", "no_record"))


if __name__ == "__main__":
    unittest.main()
