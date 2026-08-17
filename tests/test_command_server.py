import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller import command_server
from gate_controller.actuation import ActuationCoordinator
from gate_controller.command_server import DirectCommandExecutor, build_command_server
from gate_controller.models import RelayResult
from gate_controller.store import LocalStore


class FakeRelay:
    def __init__(self):
        self.pulses = 0

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(False, inhibition[1], idempotency_key)
        self.pulses += 1
        return RelayResult(True, "activated", idempotency_key)


class DelayingStore(LocalStore):
    def __init__(self, path, after_attempt):
        super().__init__(path)
        self._after_attempt = after_attempt

    def mark_actuation_attempt(self, claim, attempted_at, **kwargs):
        result = super().mark_actuation_attempt(claim, attempted_at, **kwargs)
        self._after_attempt()
        return result


class DirectCommandExecutorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.relay = FakeRelay()
        self.store = LocalStore(Path(self.directory.name) / "gate.db")
        self.executor = DirectCommandExecutor(
            "primary", ActuationCoordinator(self.store, self.relay, clock=lambda: self.now),
            self.store,
            clock=lambda: self.now,
        )
        self.valid_payload = {
            "controller_id": "primary",
            "command": "open_gate",
            "idempotency_key": "request-1",
            "expires_at": "2026-08-17T10:00:10Z",
        }

    def test_expired_direct_command_never_pulses_relay(self):
        response = self.executor.execute({
            **self.valid_payload,
            "expires_at": "2026-08-17T10:00:00Z",
        }, now=datetime(2026, 8, 17, 10, 0, 1, tzinfo=timezone.utc))

        self.assertEqual(response["status"], "expired")
        self.assertEqual(self.relay.pulses, 0)

    def test_direct_command_expiring_during_coordination_never_pulses_relay(self):
        now = [self.now]
        relay = FakeRelay()
        store = DelayingStore(
            Path(self.directory.name) / "delayed-gate.db",
            after_attempt=lambda: now.__setitem__(0, self.now + timedelta(seconds=11)),
        )
        executor = DirectCommandExecutor(
            "primary", ActuationCoordinator(store, relay, clock=lambda: now[0]),
            store,
            clock=lambda: now[0],
        )

        response = executor.execute(self.valid_payload)

        self.assertEqual(response, {"status": "expired", "detail": "expired_before_activation"})
        self.assertEqual(relay.pulses, 0)

    def test_repeated_direct_command_idempotency_does_not_repulse(self):
        first = self.executor.execute(self.valid_payload)
        second = self.executor.execute(self.valid_payload)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.relay.pulses, 1)

    def test_direct_command_lifetime_over_ten_seconds_is_expired(self):
        response = self.executor.execute({
            **self.valid_payload,
            "expires_at": "2026-08-17T10:00:11Z",
        })

        self.assertEqual(response, {"status": "expired", "detail": "invalid_expiry_window"})
        self.assertEqual(self.relay.pulses, 0)

    def test_direct_command_for_another_controller_never_pulses_relay(self):
        response = self.executor.execute({**self.valid_payload, "controller_id": "secondary"})

        self.assertEqual(response, {"status": "failed", "detail": "wrong_controller"})
        self.assertEqual(self.relay.pulses, 0)

    def test_repeated_prompt_command_plays_once(self):
        played = []
        executor = DirectCommandExecutor(
            "primary", ActuationCoordinator(self.store, self.relay, clock=lambda: self.now),
            self.store,
            prompt_player=type("PromptPlayer", (), {
                "play": lambda self, key: played.append(key) or True,
            })(),
            clock=lambda: self.now,
        )
        payload = {
            **self.valid_payload,
            "command": "play_prompt",
            "prompt_key": "arrival",
        }

        first = executor.execute(payload)
        second = executor.execute(payload)

        self.assertEqual(first, {"status": "completed"})
        self.assertEqual(second, {"status": "completed"})
        self.assertEqual(played, ["arrival"])


class CommandServerTests(unittest.TestCase):
    def test_daemon_main_runs_loopback_server_with_runtime_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            calls = []

            def record_runner(host, port, executor, stop_event):
                calls.append((host, port, executor, stop_event))

            stop_event = threading.Event()
            command_server.main(
                environment={"GATE_CONTROLLER_ID": "primary"},
                relay=FakeRelay(),
                store=store,
                stop_event=stop_event,
                server_runner=record_runner,
            )

        self.assertEqual(calls[0][0:2], ("127.0.0.1", 8765))
        self.assertIsInstance(calls[0][2], DirectCommandExecutor)
        self.assertIs(calls[0][3], stop_event)
        self.assertEqual(
            calls[0][2].execute({
                "controller_id": "other-controller",
                "command": "open_gate",
                "idempotency_key": "request-1",
                "expires_at": "2026-08-17T10:00:10Z",
            }),
            {"status": "failed", "detail": "wrong_controller"},
        )

    def test_command_server_rejects_non_loopback_bind(self):
        for host in ("0.0.0.0", "127.0.0.2"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "loopback"):
                build_command_server(host, 8765, executor=object())

    def test_post_commands_returns_executor_response_as_json(self):
        executor = type("Executor", (), {
            "execute": lambda self, payload: {"status": "completed"}
        })()
        server = build_command_server("127.0.0.1", 0, executor)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=2))
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST", "/commands", body=json.dumps({"command": "open_gate"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "completed"})

    def test_post_commands_rejects_bodies_larger_than_4096_bytes(self):
        server = build_command_server("127.0.0.1", 0, executor=object())
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=2))
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST", "/commands", body=b"x" * 4097,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 413)
        self.assertEqual(payload, {"status": "failed", "detail": "request_too_large"})


if __name__ == "__main__":
    unittest.main()
