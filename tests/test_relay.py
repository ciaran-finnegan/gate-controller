import unittest
from datetime import datetime, timezone

from gate_controller.relay import RelayController
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
