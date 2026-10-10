"""One car, one automatic pulse (docs/invariants.md 12).

2026-10-10, 10:04-10:13 IST: an authorised pickup (`172L66`) waited nine
minutes at the gate. The camera raised seven vehicle alarms; the Pi pulsed the
relay four times for the one car, every read between refused only by the 90 s
cooldown and the first read after each expiry let through. The relay is on the
operator's step-by-step input, so a pulse into a gate whose state is unknown
stops or reverses it: the leaves crossed and the gate jammed
(docs/reviews/2026-10-10-gate-jam.md).

Everything here goes through the path production uses from the processor on:
the real ``GateProcessor`` and the real ``DirectCommandExecutor`` sharing one
real ``ActuationCoordinator`` -- built from the environment by the same
functions ``main`` calls -- over a real ``RelayController`` on a fake GPIO
adapter and a real ``LocalStore`` on a temporary SQLite file. Only the clocks
and the plate reader are fakes. The alarm-to-sweep half of the path is
replayed in ``tests/test_gate_jam_2026_10_10.py``.

The rule under test can only withhold a pulse. Nothing here, and nothing it
guards, adds or retries one.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from gate_controller.__main__ import (
    _automatic_open_config, actuation_cooldowns, repulse_unseen,
)
from gate_controller.actuation import (
    AUTOMATIC_PAUSED, REPULSE_HOLD, ActuationCoordinator,
)
from gate_controller.automatic_open import AutomaticOpenConfig
from gate_controller.automatic_open import load_config as load_automatic_open
from gate_controller.command_server import DirectCommandExecutor
from gate_controller.models import GateEvent, PlateObservation
from gate_controller.processor import GateProcessor
from gate_controller.relay import RelayController
from gate_controller.settings import MatchPolicyCache
from gate_controller.store import LocalStore, _telemetry_payload
from tests.test_event_contract import (
    assert_ingest_accepts_event, assert_ingest_accepts_telemetry,
)

# 10:04:11 IST, the first alarm. A fixed instant: nothing here reads the
# calendar (invariant 10).
START = datetime(2026, 10, 10, 9, 4, 11, tzinfo=timezone.utc)
DMAX = "172L66"
AUDI = "131D2696"
MINUTE = 60.0


class FakeGpio:
    def __init__(self):
        self.pulses = 0

    def on(self):
        self.pulses += 1

    def off(self):
        pass


class PlateReader:
    def __init__(self):
        self.plate = DMAX
        self.confidence = 0.97

    def recognise(self, path):
        return PlateObservation(self.plate, self.confidence)


class OnePulsePerCarHarness(unittest.TestCase):
    AUTHORISED = {DMAX, AUDI}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.elapsed = 0.0
        self.frames = 0
        self.commands = 0
        self.gpio = FakeGpio()
        self.reader = PlateReader()
        self.database = self.directory / "gate.db"
        self.store = LocalStore(self.database)
        self.settings = MatchPolicyCache(self.directory / "match-policy.json")
        self.build({})

    def build(self, environment, *, boot_id="boot-1", uptime_at_start=1000.0,
              authorised=None):
        """Wire the controller the way ``main`` does, from ``environment``."""
        self.uptime_at_start = uptime_at_start
        automatic, command = actuation_cooldowns(environment)
        defaults = load_automatic_open(environment)
        relay = RelayController(
            self.gpio, sleeper=lambda _seconds: None, clock=self.wall_clock,
        )
        self.coordinator = ActuationCoordinator(
            self.store, relay, automatic, clock=self.wall_clock,
            monotonic_clock=self.monotonic_clock, boot_id=boot_id,
            command_cooldown=command,
            repulse_unseen=repulse_unseen(environment),
            automatic_open=lambda: _automatic_open_config(self.settings, defaults),
        )
        self.processor = GateProcessor(
            recognizer=self.reader, store=self.store, relay=relay,
            authorised=authorised or self.AUTHORISED, cooldown=automatic,
            coordinator=self.coordinator, clock=self.wall_clock,
        )
        self.addCleanup(self.processor.close)
        self.executor = DirectCommandExecutor(
            "primary", self.coordinator, self.store, clock=self.wall_clock,
        )

    def wall_clock(self):
        return START + timedelta(seconds=self.elapsed)

    def monotonic_clock(self):
        return self.uptime_at_start + self.elapsed

    def plate_read(self, at, plate=DMAX, confidence=0.97):
        """A fresh frame of ``plate`` arrives ``at`` seconds after the first alarm."""
        self.elapsed = at
        self.reader.plate = plate
        self.reader.confidence = confidence
        self.frames += 1
        frame = self.directory / f"frame-{self.frames}.jpg"
        # The event key is the frame's content digest: every frame differs.
        Image.new("L", (16, 8), color=self.frames % 256).save(frame, format="JPEG")
        return self.processor.process((frame,), received_at=self.wall_clock())

    def human_command(self, at):
        self.elapsed = at
        self.commands += 1
        return self.executor.execute({
            "controller_id": "primary", "command": "open_gate",
            "idempotency_key": f"request-{self.commands}",
            "expires_at": (self.wall_clock() + timedelta(seconds=5)).isoformat(),
        })

    def event(self, event_id):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()

    def assert_pulsed(self, result, plate=DMAX):
        self.assertTrue(result.opened, result.reason)
        row = self.event(result.event_id)
        self.assertEqual(row["opened"], 1)
        self.assertIsNone(row["actuation_outcome"])
        self.assertIsNotNone(row["relay_activated_at"])
        self.assertEqual(row["authorised_plate"], plate)

    def assert_withheld(self, result, outcome, plate=DMAX):
        """No pulse -- and the decision is still recorded as the grant it was."""
        self.assertFalse(result.opened)
        self.assertEqual(result.reason, outcome)
        row = self.event(result.event_id)
        self.assertEqual(row["opened"], 1, "a withheld match is a grant, not a denial")
        self.assertEqual(row["actuation_outcome"], outcome)
        self.assertEqual(row["reason"], "exact_match")
        self.assertEqual(row["authorised_plate"], plate)
        self.assertIsNone(row["relay_activated_at"])


class OnePulsePerCarTests(OnePulsePerCarHarness):
    def test_a_car_that_stays_in_view_gets_exactly_one_automatic_pulse(self):
        """The morning's reads, at the seconds they happened, through one coordinator.

        Pulse at +2.9 s (sweep frame). Then the sweep frame and the camera's
        still of each later alarm: refused by the cooldown while the gate's
        own cycle is running, and by the hold once it is not. On 10 Oct the
        reads at +154.8, +247.0 and +506.0 pulsed; now they do not.
        """
        self.assert_pulsed(self.plate_read(2.9))
        self.assertEqual(self.gpio.pulses, 1)

        timeline = (
            (87.0, "cooldown"), (89.4, "cooldown"),       # 10:05:38 alarm
            (154.8, REPULSE_HOLD), (156.3, REPULSE_HOLD),  # 10:06:44 -- pulsed on the day
            (203.0, REPULSE_HOLD), (205.1, REPULSE_HOLD),  # 10:07:34
            (243.6, REPULSE_HOLD), (247.0, REPULSE_HOLD),  # 10:08:14 -- the still pulsed
            (505.0, REPULSE_HOLD), (506.0, REPULSE_HOLD),  # 10:12:36 -- pulsed on the day
            (530.0, REPULSE_HOLD), (532.0, REPULSE_HOLD),  # 10:13:01
        )
        for at, outcome in timeline:
            with self.subTest(seconds_after_first_alarm=at):
                self.assert_withheld(self.plate_read(at), outcome)
                self.assertEqual(self.gpio.pulses, 1)

    def test_the_hold_names_only_the_pulses_the_cooldown_would_have_let_through(self):
        """Inside the gate's cycle the record still says cooldown, the truth it
        always was; repulse_hold is exactly the set of new refusals."""
        self.plate_read(0)
        self.assert_withheld(self.plate_read(60), "cooldown")
        self.assert_withheld(self.plate_read(89.9), "cooldown")
        self.assert_withheld(self.plate_read(90.1), REPULSE_HOLD)

    def test_a_withheld_grant_is_recorded_and_sent_as_a_grant_with_no_pulse(self):
        self.plate_read(0)
        self.store.bind_pending_outbox_controller("primary")

        held = self.plate_read(200)

        self.assert_withheld(held, REPULSE_HOLD)
        telemetry = _telemetry_payload(held.telemetry)
        self.assertEqual(telemetry["decision"], {"outcome": "allowed", "reason": "exact_match"})
        self.assertEqual(telemetry["actuation"], {
            "claim": REPULSE_HOLD, "attempted": False, "relay_outcome": "not_attempted",
        })
        assert_ingest_accepts_telemetry(telemetry)
        payload = self.store.event_payload(held.event_id)
        payload.setdefault("schema_version", 3)
        payload.setdefault("controller_id", "primary")
        payload.setdefault("event_id", held.event_id)
        assert_ingest_accepts_event(payload)
        self.assertNotIn("actuation_outcome", payload)
        self.assertNotIn("near_miss_plate", payload)
        self.assertTrue(payload["opened"])
        self.assertIsNone(payload["relay_activated_at"])

    def test_refused_reads_keep_the_hold_alive_for_as_long_as_the_car_stays(self):
        """Thirty minutes at the gate, an alarm every four minutes: one pulse."""
        self.plate_read(0)
        for minute in range(4, 32, 4):
            with self.subTest(minute=minute):
                self.assert_withheld(self.plate_read(minute * MINUTE), REPULSE_HOLD)
        self.assertEqual(self.gpio.pulses, 1)

    def test_the_hold_lasts_to_the_unseen_window_and_lapses_after_it(self):
        self.plate_read(0)
        self.assert_withheld(self.plate_read(100), REPULSE_HOLD)

        # Exactly the window after the last sighting: still held.
        self.assert_withheld(self.plate_read(100 + 10 * MINUTE), REPULSE_HOLD)
        # And a second past it: the car has been away; it is let in again.
        self.assert_pulsed(self.plate_read(100 + 20 * MINUTE + 1))
        self.assertEqual(self.gpio.pulses, 2)

    def test_the_same_plate_returning_after_the_window_is_let_in(self):
        self.plate_read(0)
        self.assert_pulsed(self.plate_read(10 * MINUTE + 1))
        self.assertEqual(self.gpio.pulses, 2)

    def test_a_sighting_after_the_hold_lapsed_starts_no_new_hold(self):
        """Only a pulse anchors a hold. A read refused for its own reasons after
        the window does not stand in for one."""
        self.plate_read(0)
        # 12 minutes on, a doubtful read of the same plate: refused under the bar.
        doubtful = self.plate_read(12 * MINUTE, confidence=0.6)
        self.assertFalse(doubtful.opened)
        self.assertEqual(doubtful.reason, "no_match")

        self.assert_pulsed(self.plate_read(14 * MINUTE))
        self.assertEqual(self.gpio.pulses, 2)

    def test_a_different_authorised_plate_arriving_during_the_hold_gets_its_own_pulse(self):
        """Different plates are independent; each gets one pulse, then its own hold."""
        self.plate_read(0)
        self.assert_pulsed(self.plate_read(120, AUDI), AUDI)
        self.assertEqual(self.gpio.pulses, 2)

        self.assert_withheld(self.plate_read(130), "cooldown")
        self.assert_withheld(self.plate_read(215, AUDI), REPULSE_HOLD, AUDI)
        self.assert_withheld(self.plate_read(220), REPULSE_HOLD)
        self.assertEqual(self.gpio.pulses, 2)

    def test_a_near_miss_read_of_the_plate_counts_as_the_car_still_being_there(self):
        """`172L61` x5 on the day: the D-Max's trailing characters misread. A
        misread attributed to the plate keeps its hold alive."""
        self.plate_read(0)
        misread = self.plate_read(5 * MINUTE, plate="172L61")
        self.assertFalse(misread.opened)
        self.assertEqual(misread.reason, "no_match")
        self.assertEqual(self.event(misread.event_id)["near_miss_plate"], DMAX)

        # 13 minutes after the pulse; 8 after the misread.
        self.assert_withheld(self.plate_read(13 * MINUTE), REPULSE_HOLD)
        self.assertEqual(self.gpio.pulses, 1)

    def test_without_the_near_miss_the_same_read_would_have_been_let_in(self):
        self.plate_read(0)
        self.assert_pulsed(self.plate_read(13 * MINUTE))

    def test_a_controller_restart_mid_hold_keeps_the_hold(self):
        """The hold is in the store, not in memory: a new process, and a new
        boot with a short uptime, both still know the car was let in."""
        self.plate_read(0)
        self.assert_withheld(self.plate_read(200), REPULSE_HOLD)

        self.build({})  # same boot, new process
        self.assert_withheld(self.plate_read(300), REPULSE_HOLD)

        self.build({}, boot_id="boot-2", uptime_at_start=200.0)  # rebooted
        self.assert_withheld(self.plate_read(400), REPULSE_HOLD)
        self.assertEqual(self.gpio.pulses, 1)

    def test_an_app_command_during_the_hold_still_pulses(self):
        """A person watching the camera is not held; they keep their own 20 s window."""
        self.plate_read(0)
        self.assert_withheld(self.plate_read(200), REPULSE_HOLD)

        self.assertEqual(self.human_command(201), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 2)
        # The person's pulse starts the automatic cooldown like any other, and
        # the car still in view is held after it.
        self.assert_withheld(self.plate_read(250), "cooldown")
        self.assert_withheld(self.plate_read(300), REPULSE_HOLD)
        self.assertEqual(self.gpio.pulses, 2)

    def test_the_plate_is_matched_in_its_normalised_form(self):
        """The authorised list may carry the plate with dashes; the reader never
        does, and the record carries the matcher's normalised form."""
        self.build({}, authorised={"172-L-66"})
        self.assert_pulsed(self.plate_read(0))
        self.assert_withheld(self.plate_read(200), REPULSE_HOLD)
        # And a pulse recorded in a different written form is still this car.
        self.store.record_event(GateEvent(
            source="ocr", reason="exact_match", opened=True, idempotency_key="legacy:1",
            received_at=self.wall_clock() + timedelta(seconds=1),
            decision_at=self.wall_clock() + timedelta(seconds=1),
            relay_activated_at=self.wall_clock() + timedelta(seconds=1),
            authorised_plate="172-l-66",
        ))
        self.assert_withheld(self.plate_read(300), REPULSE_HOLD)

    def test_the_hold_is_switched_off_only_by_an_explicit_zero(self):
        self.build({"GATE_REPULSE_UNSEEN_MINUTES": "0"})
        self.plate_read(0)
        self.assert_withheld(self.plate_read(60), "cooldown")
        self.assert_pulsed(self.plate_read(200))
        self.assertEqual(self.gpio.pulses, 2)

    def test_an_unreadable_window_keeps_the_shipped_ten_minutes(self):
        for value in ("ten", "0.5", "-10", "1441", "nan", "inf"):
            with self.subTest(value=value):
                with self.assertLogs("gate_controller.__main__", level="ERROR"):
                    self.build({"GATE_REPULSE_UNSEEN_MINUTES": value})
                self.assertEqual(self.coordinator.repulse_unseen, timedelta(minutes=10))

    def test_a_configured_window_reaches_the_relay_decision(self):
        self.build({"GATE_REPULSE_UNSEEN_MINUTES": "2"})
        self.plate_read(0)
        self.assert_withheld(self.plate_read(100), REPULSE_HOLD)
        self.assert_withheld(self.plate_read(100 + 2 * MINUTE), REPULSE_HOLD)
        self.assert_pulsed(self.plate_read(100 + 4 * MINUTE + 1))

    def test_the_hold_is_journalled_with_the_pulse_it_is_holding_for(self):
        self.plate_read(0)
        with self.assertLogs("gate_controller.actuation", level="WARNING") as logs:
            self.plate_read(200)
        line = "\n".join(logs.output)
        self.assertIn("gate_actuation outcome=repulse_hold plate=172L66", line)
        self.assertIn("pulsed_at=2026-10-10T09:04:11", line)
        self.assertIn("unseen_minutes=10", line)

    def test_a_hold_whose_grounds_lapsed_is_recorded_as_the_denial_it_is(self):
        """The same bar a pulse would clear: a plate withdrawn between the match
        and the actuation is a revoked authorisation, not a held grant."""
        self.plate_read(0)
        authorised = {DMAX}
        self.build({}, authorised=lambda: authorised)
        self.assert_withheld(self.plate_read(200), REPULSE_HOLD)

        # Withdrawn while the frame is being decided: the processor's
        # pre-activation check re-reads the list under the actuation lock.
        class Withdrawing:
            def __init__(self, plates):
                self.plates = plates
                self.reads = 0

            def __call__(self):
                self.reads += 1
                return () if self.reads > 1 else self.plates

        self.processor._authorised = Withdrawing((DMAX,))
        revoked = self.plate_read(300)

        self.assertFalse(revoked.opened)
        self.assertEqual(revoked.reason, "authorisation_revoked")
        self.assertEqual(self.event(revoked.event_id)["opened"], 0)
        self.assertEqual(self.gpio.pulses, 1)


class AutomaticOpenPauseTests(OnePulsePerCarHarness):
    def assert_paused(self, result, plate=DMAX):
        self.assertFalse(result.opened)
        self.assertEqual(result.reason, AUTOMATIC_PAUSED)
        row = self.event(result.event_id)
        self.assertEqual(row["opened"], 0, "the gate did not open for this car")
        self.assertEqual(row["reason"], AUTOMATIC_PAUSED)
        self.assertEqual(row["actuation_outcome"], AUTOMATIC_PAUSED)
        self.assertEqual(row["authorised_plate"], plate)
        self.assertIsNone(row["relay_activated_at"])

    def test_off_refuses_every_automatic_grant_and_not_a_persons_command(self):
        self.build({"GATE_AUTOMATIC_OPEN": "off"})

        self.assert_paused(self.plate_read(0))
        self.assert_paused(self.plate_read(5, AUDI), AUDI)
        self.assertEqual(self.gpio.pulses, 0)

        self.assertEqual(self.human_command(6), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 1)
        self.assert_paused(self.plate_read(200))
        self.assertEqual(self.gpio.pulses, 1)

    def test_a_paused_grant_is_told_to_the_app_as_the_denial_it_is(self):
        self.build({"GATE_AUTOMATIC_OPEN": "off"})
        self.store.bind_pending_outbox_controller("primary")

        paused = self.plate_read(0)

        telemetry = _telemetry_payload(paused.telemetry)
        self.assertEqual(telemetry["decision"], {"outcome": "denied", "reason": AUTOMATIC_PAUSED})
        self.assertEqual(telemetry["actuation"], {
            "claim": AUTOMATIC_PAUSED, "attempted": False, "relay_outcome": "not_attempted",
        })
        assert_ingest_accepts_telemetry(telemetry)
        payload = self.store.event_payload(paused.event_id)
        payload.setdefault("schema_version", 3)
        payload.setdefault("controller_id", "primary")
        payload.setdefault("event_id", paused.event_id)
        assert_ingest_accepts_event(payload)
        self.assertFalse(payload["opened"])
        self.assertEqual(payload["reason"], AUTOMATIC_PAUSED)
        self.assertEqual(payload["authorised_plate"], DMAX)
        self.assertAlmostEqual(payload["ocr_confidence"], 0.97)

    def test_on_and_an_unset_switch_change_nothing(self):
        for environment in ({}, {"GATE_AUTOMATIC_OPEN": "on"}, {"GATE_AUTOMATIC_OPEN": " ON "}):
            with self.subTest(environment=environment):
                self.build(environment)
                self.assertTrue(self.plate_read(self.elapsed + 1000).opened)

    def test_a_malformed_environment_value_is_rejected_and_the_gate_keeps_opening(self):
        with self.assertLogs("gate_controller.automatic_open", level="ERROR"):
            self.build({"GATE_AUTOMATIC_OPEN": "maybe"})
        self.assertTrue(self.plate_read(0).opened)
        self.assertEqual(self.coordinator.automatic_open_config(), AutomaticOpenConfig())

    def test_the_app_can_pause_through_the_settings_envelope(self):
        """The same envelope the schedule arrives in, read on every grant."""
        self.settings.replace({
            "controller_id": "primary", "settings_version": 1,
            "automatic_open": {"enabled": False},
        })
        self.assert_paused(self.plate_read(0))
        self.assertEqual(self.coordinator.automatic_open_config().source, "app")

        self.settings.replace({
            "controller_id": "primary", "settings_version": 1,
            "automatic_open": {"enabled": True},
        })
        self.assertTrue(self.plate_read(1000).opened)

    def test_a_malformed_app_setting_changes_nothing(self):
        """Paused by the app, then nonsense from the app: still paused. And the
        plate-matching schedule beside it is untouched."""
        self.settings.replace({
            "controller_id": "primary", "settings_version": 1,
            "automatic_open": {"enabled": False},
        })
        before = self.settings.get()
        for section in ({"enabled": "no"}, {"enabled": 0}, {}, "off", 7, None):
            with self.subTest(section=section):
                self.settings.replace({
                    "controller_id": "primary", "settings_version": 1,
                    "automatic_open": section,
                })
                self.assert_paused(self.plate_read(self.elapsed + 1))
                self.assertIs(self.settings.get(), before)
                self.assertIsNone(self.settings.status()["last_error"])

    def test_an_envelope_without_the_section_falls_back_to_the_environment(self):
        self.build({"GATE_AUTOMATIC_OPEN": "off"})
        self.settings.replace({"controller_id": "primary", "settings_version": 1})
        self.assert_paused(self.plate_read(0))
        self.assertEqual(self.coordinator.automatic_open_config().source, "environment")

    def test_the_apps_pause_survives_a_restart(self):
        self.settings.replace({
            "controller_id": "primary", "settings_version": 1,
            "automatic_open": {"enabled": False},
        })
        self.settings = MatchPolicyCache(self.directory / "match-policy.json")
        self.build({})
        self.assert_paused(self.plate_read(0))

    def test_every_automatic_source_is_refused_at_the_coordinator(self):
        """The choke point: whatever the source says, only a person's command passes."""
        self.build({"GATE_AUTOMATIC_OPEN": "off"})
        for source in ("local", "ocr", "cloud", "appearance", "sweep", "presence",
                       "early_trigger", "something_new"):
            with self.subTest(source=source):
                execution = self.coordinator.actuate(GateEvent(
                    source=source, reason="exact_match", opened=False,
                    idempotency_key=f"frame:{source}", received_at=self.wall_clock(),
                    decision_at=self.wall_clock(), authorised_plate=DMAX,
                ))
                self.assertFalse(execution.opened)
                self.assertEqual(execution.reason, AUTOMATIC_PAUSED)
        self.assertEqual(self.gpio.pulses, 0)
        self.assertEqual(self.human_command(1), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 1)

    def test_the_pause_is_journalled_and_reported(self):
        self.build({"GATE_AUTOMATIC_OPEN": "off"})
        with self.assertLogs("gate_controller.actuation", level="WARNING") as logs:
            self.plate_read(0)
        self.assertIn("gate_actuation outcome=automatic_paused", "\n".join(logs.output))
        self.assertIn("plate=172L66 setting_source=environment", "\n".join(logs.output))
        self.assertEqual(self.coordinator.status(), {
            "automatic_open": False, "automatic_open_source": "environment",
            "repulse_unseen_minutes": 10.0,
            "automatic_cooldown_seconds": 90.0, "command_cooldown_seconds": 20.0,
        })


if __name__ == "__main__":
    unittest.main()
