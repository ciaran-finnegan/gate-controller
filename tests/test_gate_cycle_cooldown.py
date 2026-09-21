"""The relay is not pulsed twice inside one gate cycle (gate-controller#171).

The relay contact drives the operator's step-by-step input, so a second pulse
does not hold the gate open: on a fully open gate it starts it closing, and on
a moving gate it stops it dead. Measured on the site gate, the leaves are shut
again +69.0 .. +71.8 s after the opening pulse; the old 20 s window re-pulsed a
car that was still waiting at +29.3 s, +51.8 s and +65.3 s.

Everything here goes through the path production uses: the real
``GateProcessor`` and the real ``DirectCommandExecutor`` sharing one real
``ActuationCoordinator`` -- built from the environment by the same function
``main`` calls -- over a real ``RelayController`` on a fake GPIO adapter and a
real ``LocalStore`` on a temporary SQLite file. Only the clocks and the plate
reader are fakes.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from gate_controller.__main__ import actuation_cooldowns
from gate_controller.actuation import ActuationCoordinator
from gate_controller.command_server import DirectCommandExecutor
from gate_controller.models import PlateObservation
from gate_controller.processor import GateProcessor
from gate_controller.relay import RelayController
from gate_controller.store import LocalStore


START = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
RESIDENT = "12D3456"
NEIGHBOUR = "131D2696"


class FakeGpio:
    """The adapter under ``RelayController``: counts energisations."""

    def __init__(self):
        self.pulses = 0

    def on(self):
        self.pulses += 1

    def off(self):
        pass


class PlateReader:
    def __init__(self):
        self.plate = RESIDENT

    def recognise(self, path):
        return PlateObservation(self.plate, 0.97)


class GateCycleCooldownTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.elapsed = 0.0
        self.wall_skew = 0.0
        self.frames = 0
        self.commands = 0
        self.gpio = FakeGpio()
        self.reader = PlateReader()
        self.database = self.directory / "gate.db"
        self.store = LocalStore(self.database)
        self.build({})

    def build(self, environment, *, boot_id="boot-1", uptime_at_start=1000.0):
        """Wire the controller the way ``main`` does, from ``environment``."""
        self.uptime_at_start = uptime_at_start
        automatic, command = actuation_cooldowns(environment)
        relay = RelayController(
            self.gpio, sleeper=lambda _seconds: None, clock=self.wall_clock,
        )
        self.coordinator = ActuationCoordinator(
            self.store, relay, automatic, clock=self.wall_clock,
            monotonic_clock=self.monotonic_clock, boot_id=boot_id,
            command_cooldown=command,
        )
        self.processor = GateProcessor(
            recognizer=self.reader, store=self.store, relay=relay,
            authorised={RESIDENT, NEIGHBOUR}, cooldown=automatic,
            coordinator=self.coordinator, clock=self.wall_clock,
        )
        self.addCleanup(self.processor.close)
        self.executor = DirectCommandExecutor(
            "primary", self.coordinator, self.store, clock=self.wall_clock,
        )

    def wall_clock(self):
        return START + timedelta(seconds=self.elapsed + self.wall_skew)

    def monotonic_clock(self):
        return self.uptime_at_start + self.elapsed

    def plate_read(self, at, plate=RESIDENT):
        """A fresh frame of ``plate`` arrives ``at`` seconds after the start."""
        self.elapsed = at
        self.reader.plate = plate
        self.frames += 1
        frame = self.directory / f"frame-{self.frames}.jpg"
        # The event key is the frame's content digest: every frame differs.
        Image.new("L", (16, 8), color=self.frames).save(frame, format="JPEG")
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

    def assert_cooldown_grant(self, result, plate=RESIDENT):
        """No pulse -- and the decision is still recorded as the grant it was."""
        self.assertFalse(result.opened)
        self.assertEqual(result.reason, "cooldown")
        row = self.event(result.event_id)
        self.assertEqual(row["opened"], 1, "a cooled-down match is a grant, not a denial")
        self.assertEqual(row["actuation_outcome"], "cooldown")
        self.assertEqual(row["reason"], "exact_match")
        self.assertEqual(row["authorised_plate"], plate)
        self.assertIsNone(row["relay_activated_at"])

    def test_a_waiting_car_is_not_pulsed_again_at_the_times_it_was_on_site(self):
        first = self.plate_read(0)
        self.assertTrue(first.opened)
        self.assertEqual(self.gpio.pulses, 1)

        for at in (29.3, 51.8, 65.3):
            with self.subTest(seconds_after_first_pulse=at):
                self.assert_cooldown_grant(self.plate_read(at))
                self.assertEqual(self.gpio.pulses, 1)

    def test_whole_second_reads_inside_the_cycle_do_not_pulse(self):
        self.plate_read(0)
        for at in (29, 52, 65, 89):
            with self.subTest(seconds_after_first_pulse=at):
                self.assert_cooldown_grant(self.plate_read(at))
        self.assertEqual(self.gpio.pulses, 1)

    def test_the_same_plate_pulses_again_once_the_cycle_is_certainly_over(self):
        self.plate_read(0)
        self.assert_cooldown_grant(self.plate_read(65))

        again = self.plate_read(91)

        self.assertTrue(again.opened)
        self.assertEqual(self.gpio.pulses, 2)
        row = self.event(again.event_id)
        self.assertEqual(row["opened"], 1)
        self.assertIsNone(row["actuation_outcome"])
        self.assertIsNotNone(row["relay_activated_at"])

    def test_a_different_authorised_plate_inside_the_cycle_does_not_pulse(self):
        """The gate is one physical object: the window is global, not per plate."""
        self.assertTrue(self.plate_read(0).opened)

        self.assert_cooldown_grant(self.plate_read(40, NEIGHBOUR), NEIGHBOUR)

        self.assertEqual(self.gpio.pulses, 1)

    def test_a_person_can_pulse_again_25_seconds_after_a_plate_pulse(self):
        """Someone watching the camera can recover a gate stopped mid-travel."""
        self.assertTrue(self.plate_read(0).opened)

        response = self.human_command(25)

        self.assertEqual(response, {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 2)

    def test_a_person_is_still_refused_inside_the_command_window(self):
        self.assertTrue(self.plate_read(0).opened)

        response = self.human_command(19)

        self.assertEqual(response, {"status": "failed", "detail": "cooldown"})
        self.assertEqual(self.gpio.pulses, 1)

    def test_a_plate_read_30_seconds_after_a_human_command_does_not_pulse(self):
        self.assertEqual(self.human_command(0), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 1)

        self.assert_cooldown_grant(self.plate_read(30))

        self.assertEqual(self.gpio.pulses, 1)

    def test_a_human_pulse_restarts_the_automatic_window(self):
        """Plate at 0, person at +25 s: a plate at +95 s is 70 s after a pulse."""
        self.plate_read(0)
        self.human_command(25)
        self.assertEqual(self.gpio.pulses, 2)

        self.assert_cooldown_grant(self.plate_read(95))
        self.assertEqual(self.gpio.pulses, 2)

        self.assertTrue(self.plate_read(116).opened)
        self.assertEqual(self.gpio.pulses, 3)

    def test_the_window_survives_a_restart_and_a_wall_clock_jump(self):
        """A restarted process has no memory; the store's monotonic record does.

        The wall clock leaps a day forward (NTP arriving late), so only the
        monotonic cutoff can still see the pulse 40 s ago -- and it has to be
        the 90 s one for a plate read, the 20 s one for a person.
        """
        self.plate_read(0)
        self.build({})  # same boot, new process: nothing remembered in memory
        self.wall_skew = 86400.0

        self.assert_cooldown_grant(self.plate_read(40))
        self.assertEqual(self.gpio.pulses, 1)
        self.assertEqual(self.human_command(41), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 2)

    def test_the_wall_clock_window_alone_still_separates_the_two_sources(self):
        """A different boot with a long uptime: only the wall clock can answer.

        No in-process memory and no monotonic record from this boot, so the
        store falls back to the event's relay timestamp -- and that cutoff has
        to be the asker's own window too.
        """
        self.plate_read(0)
        self.build({}, boot_id="boot-2", uptime_at_start=5000.0)

        self.assert_cooldown_grant(self.plate_read(40))
        self.assertEqual(self.gpio.pulses, 1)
        self.assertEqual(self.human_command(41), {"status": "completed"})
        self.assertEqual(self.gpio.pulses, 2)

    def test_the_environment_variable_reaches_the_relay_decision(self):
        self.build({
            "GATE_ACTUATION_COOLDOWN_SECONDS": "120",
            "GATE_COMMAND_COOLDOWN_SECONDS": "10",
        })
        self.plate_read(0)

        self.assert_cooldown_grant(self.plate_read(91))
        self.assertEqual(self.human_command(100)["status"], "completed")
        self.assert_cooldown_grant(self.plate_read(215))
        self.assertTrue(self.plate_read(221).opened)
        self.assertEqual(self.gpio.pulses, 3)


if __name__ == "__main__":
    unittest.main()
