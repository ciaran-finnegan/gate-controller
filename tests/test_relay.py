import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from unittest.mock import patch

from gate_controller.relay import PiRelayAdapter, RelayController
from gate_controller.relay_safe import force_relay_off


class FailingOffRelay:
    def __init__(self):
        self.on_calls = 0
        self.off_calls = 0
        self.initialized = False

    def on(self):
        self.on_calls += 1

    def off(self):
        self.off_calls += 1
        if not self.initialized:
            self.initialized = True
            return
        raise RuntimeError("GPIO stuck high")


class FailingOnRelay:
    def __init__(self):
        self.off_calls = 0

    def on(self):
        raise RuntimeError("GPIO unavailable")

    def off(self):
        self.off_calls += 1


class RecordingBackend:
    def __init__(self):
        self.calls = []

    def on(self):
        self.calls.append("on")

    def off(self):
        self.calls.append("off")


class RelayControllerTests(unittest.TestCase):
    def test_pi_gpio_high_cannot_follow_shutdown_latch_establishment(self):
        shutdown_progress = Event()
        high_edge_reached = Event()
        release_high_edge = Event()

        class ObservedBoundary:
            def __init__(self):
                self._lock = RLock()

            def acquire(self):
                if current_thread().name == "relay-shutdown":
                    shutdown_progress.set()
                return self._lock.acquire()

            def release(self):
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, _type, _value, _traceback):
                self.release()

        class ObservedShutdownEvent:
            def __init__(self):
                self._event = Event()

            def set(self):
                self._event.set()
                shutdown_progress.set()

            def is_set(self):
                return self._event.is_set()

            def wait(self, timeout=None):
                return self._event.wait(timeout)

        gpio_calls = []
        shutdown_event = ObservedShutdownEvent()
        gpio = types.ModuleType("RPi.GPIO")
        gpio.BOARD, gpio.OUT, gpio.LOW, gpio.HIGH = 1, 2, 0, 1
        gpio.setmode = lambda mode: None
        gpio.setwarnings = lambda enabled: None
        gpio.setup = lambda pin, mode: None

        def output(_pin, value):
            if value == gpio.HIGH:
                high_edge_reached.set()
                release_high_edge.wait(1)
            gpio_calls.append((value, shutdown_event.is_set()))

        gpio.output = output
        rpi = types.ModuleType("RPi")
        rpi.GPIO = gpio
        spec = importlib.util.spec_from_file_location(
            "PiRelay_test_atomic_boundary", Path("PiRelay.py")
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"RPi": rpi, "RPi.GPIO": gpio}):
            spec.loader.exec_module(module)

        boundary = ObservedBoundary()
        pi_relay = module.Relay("RELAY1")
        pi_relay.activation_boundary = boundary
        adapter = object.__new__(PiRelayAdapter)
        adapter._relay = pi_relay
        controller = RelayController(adapter, pulse_seconds=0, sleeper=lambda _: None)
        controller._shutdown_requested = shutdown_event
        gpio_calls.clear()

        trigger = Thread(
            target=lambda: controller.trigger("remote_command", "command:atomic-boundary")
        )
        shutdown = Thread(target=controller.begin_shutdown, name="relay-shutdown")
        trigger.start()
        self.assertTrue(high_edge_reached.wait(1))
        shutdown.start()
        try:
            self.assertTrue(shutdown_progress.wait(1))
            self.assertFalse(
                shutdown_event.is_set(),
                "shutdown latch was established while the GPIO HIGH edge was pending",
            )
        finally:
            release_high_edge.set()
            trigger.join(1)
            shutdown.join(1)

        self.assertFalse(trigger.is_alive())
        self.assertFalse(shutdown.is_alive())
        high_states = [latched for value, latched in gpio_calls if value == gpio.HIGH]
        self.assertEqual([False], high_states)
        self.assertEqual(gpio.LOW, gpio_calls[-1][0])

    def test_shutdown_request_at_gpio_boundary_prevents_activation(self):
        boundary_reached = Event()
        release_boundary = Event()

        class BoundaryBackend:
            def __init__(self):
                self.calls = []

            def on(self, *, pre_activation_inhibit=None):
                boundary_reached.set()
                release_boundary.wait(1)
                inhibition = pre_activation_inhibit()
                if inhibition is not None:
                    return inhibition
                self.calls.append("on")
                return None

            def off(self):
                self.calls.append("off")

        backend = BoundaryBackend()
        controller = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)
        results = []
        worker = Thread(target=lambda: results.append(controller.trigger(
            "remote_command", "command:shutdown-race"
        )))
        worker.start()
        self.assertTrue(boundary_reached.wait(1))

        controller.begin_shutdown()
        release_boundary.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(backend.calls, ["off", "off"])
        self.assertEqual(results[0].reason, "relay_latched")
        self.assertTrue(results[0].latched)

    def test_expiry_inhibition_remains_authoritative_after_shutdown_is_requested(self):
        backend = RecordingBackend()
        controller = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)
        controller.begin_shutdown()
        checks = []

        result = controller.trigger(
            "remote_command",
            "command:expired-during-shutdown",
            pre_activation_inhibit=lambda: checks.append("expiry") or (
                "expired", "expired_before_activation"
            ),
        )

        self.assertEqual(checks, ["expiry"])
        self.assertFalse(result.activated)
        self.assertEqual(result.reason, "expired_before_activation")
        self.assertEqual(backend.calls, ["off", "off"])

    def test_pi_library_checks_inhibition_at_the_gpio_boundary(self):
        calls = []
        gpio = types.ModuleType("RPi.GPIO")
        gpio.BOARD, gpio.OUT, gpio.LOW, gpio.HIGH = 1, 2, 0, 1
        gpio.setmode = lambda mode: None
        gpio.setwarnings = lambda enabled: None
        gpio.setup = lambda pin, mode: None
        gpio.output = lambda pin, value: calls.append(("gpio", value))
        rpi = types.ModuleType("RPi")
        rpi.GPIO = gpio
        spec = importlib.util.spec_from_file_location(
            "PiRelay_test_boundary", Path("PiRelay.py")
        )
        module = importlib.util.module_from_spec(spec)

        with patch.dict(sys.modules, {"RPi": rpi, "RPi.GPIO": gpio}):
            spec.loader.exec_module(module)
        relay = module.Relay("RELAY1")
        calls.clear()
        inhibition = ("failed", "decision_timeout")

        inhibited = relay.on(pre_activation_inhibit=lambda: inhibition)
        with patch("builtins.print") as output:
            activated = relay.on(pre_activation_inhibit=lambda: None)

        self.assertEqual(inhibited, inhibition)
        self.assertIsNone(activated)
        self.assertEqual(calls, [("gpio", gpio.HIGH)])
        output.assert_not_called()

    def test_pi_adapter_forwards_the_last_moment_gpio_inhibition(self):
        calls = []

        class Backend:
            def on(self, *, pre_activation_inhibit=None):
                calls.append("backend")
                return pre_activation_inhibit()

        adapter = object.__new__(PiRelayAdapter)
        adapter._relay = Backend()
        inhibition = ("failed", "decision_timeout")

        result = adapter.on(pre_activation_inhibit=lambda: inhibition)

        self.assertEqual(result, inhibition)
        self.assertEqual(calls, ["backend"])

    def test_prestart_safety_helper_retries_until_relay_is_off(self):
        class Backend:
            def __init__(self):
                self.calls = 0

            def off(self):
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("temporary GPIO error")

        backend = Backend()

        self.assertTrue(force_relay_off(lambda: backend, max_attempts=3))
        self.assertEqual(backend.calls, 3)

    def test_prestart_safety_helper_fails_when_output_cannot_be_forced_off(self):
        class Backend:
            def off(self):
                raise RuntimeError("GPIO stuck")

        self.assertFalse(force_relay_off(lambda: Backend(), max_attempts=3))

    def test_pre_activation_inhibit_is_checked_under_lock_before_gpio(self):
        now = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        expires_at = now[0].replace(second=1)
        backend = RecordingBackend()
        controller = RelayController(
            backend, pulse_seconds=0, sleeper=lambda _: None, clock=lambda: now[0]
        )
        lock_states = []

        def expiry_inhibition():
            lock_states.append(controller._lock.locked())
            now[0] = now[0].replace(second=2)
            if expires_at <= now[0]:
                return "expired", "expired_before_activation"
            return None

        result = controller.trigger(
            "remote_command", "command:command-1",
            pre_activation_inhibit=expiry_inhibition,
        )

        self.assertFalse(result.activated)
        self.assertEqual(result.reason, "expired_before_activation")
        self.assertEqual(lock_states, [True])
        self.assertEqual(backend.calls, ["off"])

    def test_status_reports_measured_relay_readiness_and_last_outcome(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        backend = RecordingBackend()
        controller = RelayController(
            backend, pulse_seconds=0, sleeper=lambda _: None, clock=lambda: now
        )

        initial = controller.status()
        controller.trigger("ocr", "upload-1")
        activated = controller.status()

        self.assertEqual(initial, {
            "ready": True,
            "last_outcome": "initialized_safe",
            "last_outcome_at": now.isoformat(),
        })
        self.assertEqual(activated, {
            "ready": True,
            "last_outcome": "activated",
            "last_outcome_at": now.isoformat(),
        })

    def test_activation_hook_runs_at_gpio_on_before_the_pulse(self):
        backend = RecordingBackend()
        calls = backend.calls
        controller = RelayController(
            backend,
            pulse_seconds=2,
            sleeper=lambda seconds: calls.append(("sleep", seconds)),
        )

        result = controller.trigger(
            "ocr", "upload-1", on_activation=lambda: calls.append("activation")
        )

        self.assertTrue(result.activated)
        self.assertEqual(calls, ["off", "on", "activation", ("sleep", 2), "off"])

    def test_activation_hook_failure_does_not_change_the_relay_pulse(self):
        backend = RecordingBackend()
        calls = backend.calls
        controller = RelayController(
            backend,
            pulse_seconds=2,
            sleeper=lambda seconds: calls.append(("sleep", seconds)),
        )

        result = controller.trigger(
            "ocr",
            "upload-1",
            on_activation=lambda: (_ for _ in ()).throw(RuntimeError("telemetry failed")),
        )

        self.assertTrue(result.activated)
        self.assertEqual(calls, ["off", "on", ("sleep", 2), "off"])

    def test_initialization_and_shutdown_force_the_output_off(self):
        backend = RecordingBackend()
        controller = RelayController(backend, pulse_seconds=0, sleeper=lambda _: None)

        controller.shutdown()
        inhibited = controller.trigger("ocr", "late-work")

        self.assertEqual(backend.calls, ["off", "off"])
        self.assertEqual(inhibited.reason, "relay_latched")
        self.assertTrue(inhibited.latched)

    def test_base_exception_during_pulse_still_deenergizes(self):
        backend = RecordingBackend()
        controller = RelayController(
            backend, pulse_seconds=2, sleeper=lambda _: (_ for _ in ()).throw(SystemExit())
        )

        with self.assertRaises(SystemExit):
            controller.trigger("ocr", "upload-1")

        self.assertEqual(backend.calls, ["off", "on", "off"])

    def test_failed_deenergize_latches_the_relay_after_bounded_retries(self):
        backend = FailingOffRelay()
        controller = RelayController(
            backend, pulse_seconds=0, max_off_attempts=3, sleeper=lambda _: None
        )

        failed = controller.trigger("ocr", "upload-1")
        inhibited = controller.trigger("ocr", "upload-2")

        self.assertFalse(failed.activated)
        self.assertTrue(failed.latched)
        self.assertEqual(failed.reason, "relay_deenergize_error")
        self.assertEqual(backend.off_calls, 4)
        self.assertEqual(inhibited.reason, "relay_latched")
        self.assertEqual(backend.on_calls, 1)

    def test_on_failure_reports_no_activation_and_attempts_to_deenergize(self):
        backend = FailingOnRelay()
        result = RelayController(backend, sleeper=lambda _: None).trigger("ocr", "upload-1")

        self.assertFalse(result.activated)
        self.assertEqual(result.reason, "relay_error")
        self.assertIsNone(result.activated_at)
        self.assertEqual(backend.off_calls, 2)
