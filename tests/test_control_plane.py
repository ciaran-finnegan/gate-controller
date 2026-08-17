import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gate_controller.audio import PromptPlayer
from gate_controller.actuation import ActuationCoordinator
from gate_controller import __main__ as gate_main
from gate_controller.control_plane import (
    CommandWorker, ControlPlaneError, GateCommand, HeartbeatWorker, SupabaseControlPlane,
)
from gate_controller.cloudflare_client import CloudflareStatusReporter
from gate_controller.models import GateEvent, RelayResult
from gate_controller.store import LocalStore


HEARTBEAT_RPC_ARGUMENTS = (
    "p_controller_id", "p_camera_timestamp", "p_queue_depth", "p_capabilities",
)

HEALTHY_MEDIA = {
    "video": {"configured": True, "ready": True, "verified": True, "reason": "ready"},
    "listen": {
        "configured": True, "ready": True, "verified": False,
        "reason": "hardware_unverified",
    },
    "talkback": {
        "configured": False, "ready": False, "verified": False,
        "reason": "hardware_unverified",
    },
}

UNAVAILABLE_MEDIA = {
    "video": {
        "configured": False, "ready": False, "verified": False,
        "reason": "gateway_unhealthy",
    },
    "listen": {
        "configured": False, "ready": False, "verified": False,
        "reason": "gateway_unhealthy",
    },
    "talkback": {
        "configured": False, "ready": False, "verified": False,
        "reason": "hardware_unverified",
    },
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {"Date": "Thu, 13 Aug 2026 10:00:00 GMT"}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *, json_response=None):
        self.json_response = json_response
        self.requests = []

    def post_json(self, path, payload, *, headers=None):
        self.requests.append(type("Request", (), {
            "path": path, "payload": payload, "headers": headers or {},
        })())
        return self.json_response


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeRelay:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or RelayResult(True, "activated")

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return RelayResult(False, inhibition[1], idempotency_key)
        self.calls.append((source, idempotency_key))
        return self.result


class FakeControlPlane:
    def __init__(self, commands):
        self.commands = list(commands)
        self.completed = []

    def claim_command(self):
        return self.commands.pop(0) if self.commands else None

    def complete_command(self, command, status, detail=None):
        self.completed.append((command.id, status, detail))


class FailingReplayControlPlane(FakeControlPlane):
    def __init__(self, commands):
        super().__init__(commands)
        self.claim_calls = 0

    def claim_command(self):
        self.claim_calls += 1
        return super().claim_command()

    def complete_command(self, command, status, detail=None):
        raise TimeoutError("ack endpoint offline")


class FailingAckStore(LocalStore):
    def queue_command_ack(self, *args, **kwargs):
        raise RuntimeError("database unavailable while queuing ack")


class FailingAckCleanupStore(LocalStore):
    def complete_command_ack(self, command_id):
        raise RuntimeError("database unavailable while completing ack")


class FlakyAckControlPlane(FakeControlPlane):
    def __init__(self, commands):
        super().__init__(commands)
        self.fail_first_ack = True

    def complete_command(self, command, status, detail=None):
        super().complete_command(command, status, detail)
        if self.fail_first_ack:
            self.fail_first_ack = False
            raise TimeoutError("network lost after relay")


class FailingFinalizeStore(LocalStore):
    def finalize_actuation(self, claim, event):
        raise RuntimeError("finalization interrupted")


class DelayingActuationStore(LocalStore):
    def __init__(self, path, after_attempt):
        super().__init__(path)
        self._after_attempt = after_attempt

    def mark_actuation_attempt(self, claim, attempted_at, **kwargs):
        result = super().mark_actuation_attempt(claim, attempted_at, **kwargs)
        self._after_attempt()
        return result


class ControlPlaneTests(unittest.TestCase):
    def test_cloudflare_status_reporter_forwards_existing_heartbeat_contract(self):
        reporter = CloudflareStatusReporter(FakeClient(), "primary")
        status = {
            "queue_depth": 2,
            "media": {"video": {
                "configured": False, "ready": False, "verified": False,
                "reason": "not_reported",
            }},
        }

        reporter.heartbeat(status)

        self.assertEqual(reporter.client.requests[0].path, "/api/controller/status")
        self.assertEqual(reporter.client.requests[0].payload, {
            "controller_id": "primary", **status,
        })

    def test_control_plane_rejects_http_before_creating_a_credentialed_session(self):
        for url in ("http://project.supabase.co", "http://127.0.0.1:54321"):
            with self.subTest(url=url), patch(
                "gate_controller.control_plane.requests.Session"
            ) as create_session, self.assertRaisesRegex(ValueError, "HTTPS"):
                SupabaseControlPlane(url, "service-key", "pi-front-gate")
            create_session.assert_not_called()

    def test_complete_command_accepts_an_exact_controller_reported_confirmation(self):
        session = FakeSession([FakeResponse(payload=[{
            "id": "command-1", "controller_reported_status": "completed",
        }])])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate", session=session
        )
        command = GateCommand(
            "command-1", "open_gate",
            datetime(2026, 8, 14, 10, 1, tzinfo=timezone.utc),
        )

        control_plane.complete_command(command, "completed")

        self.assertEqual(session.calls[0][1]["json"], {
            "p_command_id": "command-1",
            "p_status": "completed",
            "p_controller_id": "pi-front-gate",
        })

    def test_completed_ack_remains_queued_when_server_reports_a_different_outcome(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        session = FakeSession([FakeResponse(payload=[{
            "id": "command-1", "controller_reported_status": "expired",
        }])])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate", session=session
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            store.queue_command_ack("command-1", "completed", None, now)

            result = CommandWorker(
                control_plane, FakeRelay(), store, clock=lambda: now
            ).run_once()

            pending = store.pending_command_acks()

        self.assertFalse(result)
        self.assertEqual(pending, [("command-1", "completed", None)])

    def test_completed_ack_remains_queued_when_server_returns_no_confirmation_row(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate",
            session=FakeSession([FakeResponse(payload=[])]),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            store.queue_command_ack("command-1", "completed", None, now)

            result = CommandWorker(
                control_plane, FakeRelay(), store, clock=lambda: now
            ).run_once()

            pending = store.pending_command_acks()

        self.assertFalse(result)
        self.assertEqual(pending, [("command-1", "completed", None)])

    def test_claim_posts_to_the_bound_rpc_with_bounded_timeout(self):
        session = FakeSession([
            FakeResponse(payload=[{
                "id": "command-1", "command": "open_gate",
                "expires_at": "2026-08-13T10:01:00Z",
            }])
        ])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate",
            session=session,
            clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        )

        command = control_plane.claim_command()

        self.assertEqual(command.id, "command-1")
        self.assertEqual(command.command, "open_gate")
        url, request = session.calls[0]
        self.assertEqual(url, "https://project.supabase.co/rest/v1/rpc/claim_gate_command")
        self.assertEqual(request["json"], {"p_controller_id": "pi-front-gate"})
        self.assertEqual(request["timeout"], (2, 4))
        self.assertEqual(request["headers"]["apikey"], "service-key")
        self.assertEqual(
            command.server_time,
            datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        )

    def test_server_time_rejects_an_expired_command_when_the_pi_clock_is_slow(self):
        pi_now = datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc)
        session = FakeSession([
            FakeResponse(payload=[{
                "id": "command-1", "command": "open_gate",
                "expires_at": "2026-08-13T09:59:59Z",
            }]),
            FakeResponse(payload=[{
                "id": "command-1", "controller_reported_status": "expired",
            }]),
        ])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate",
            session=session, monotonic_clock=lambda: 100.0,
        )
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            CommandWorker(
                control_plane, relay, LocalStore(Path(directory) / "gate.db"),
                clock=lambda: pi_now, monotonic_clock=lambda: 100.0,
            ).run_once()

        self.assertEqual(relay.calls, [])
        self.assertEqual(session.calls[1][1]["json"]["p_status"], "expired")
        self.assertEqual(
            session.calls[1][1]["json"]["p_detail"], "expired_before_execution"
        )

    def test_expired_claim_is_returned_for_the_worker_to_acknowledge_durably(self):
        session = FakeSession([
            FakeResponse(payload=[{
                "id": "command-1", "command": "open_gate",
                "expires_at": "2026-08-13T09:59:59Z",
            }]),
        ])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co/", "service-key", "pi-front-gate",
            session=session,
            clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        )

        command = control_plane.claim_command()

        self.assertEqual(command.id, "command-1")
        self.assertEqual(len(session.calls), 1)

    def test_claim_rejects_a_non_string_prompt_key(self):
        session = FakeSession([FakeResponse(payload=[{
            "id": "command-1", "command": "play_prompt",
            "expires_at": "2026-08-13T10:01:00Z", "prompt_key": {"path": "arrival"},
        }])])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate", session=session
        )

        with self.assertRaisesRegex(ControlPlaneError, "invalid command"):
            control_plane.claim_command()

    def test_heartbeat_payload_matches_the_deployed_rpc_contract(self):
        session = FakeSession([FakeResponse(payload={})])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate", session=session
        )

        control_plane.heartbeat({
            "last_camera_upload_at": "2026-08-14T10:00:00+00:00",
            "queue_depth": 2,
            "audio_available": True,
            "camera_configured": True,
            "camera_upload_ready": True,
            "camera_upload_recent": False,
            "camera_connection_probed": False,
            "camera_connected": None,
            "relay": {
                "ready": True,
                "last_outcome": "activated",
                "last_outcome_at": "2026-08-14T09:58:00+00:00",
            },
            "authorisation": {
                "available": True,
                "stale": False,
                "refreshed_at": "2026-08-14T09:59:00+00:00",
                "last_error": None,
            },
            "media": HEALTHY_MEDIA,
        })

        url, request = session.calls[0]
        self.assertEqual(url, "https://project.supabase.co/rest/v1/rpc/update_controller_status")
        self.assertEqual(
            request["json"],
            {
                "p_controller_id": "pi-front-gate",
                "p_camera_timestamp": "2026-08-14T10:00:00+00:00",
                "p_queue_depth": 2,
                "p_capabilities": {
                    "audio_available": True,
                    "audio_prompts": True,
                    "camera": {
                        "configured": True,
                        "upload_ready": True,
                        "last_upload_at": "2026-08-14T10:00:00+00:00",
                        "upload_recent": False,
                        "connection_probed": False,
                        "connected": None,
                    },
                    "relay": {
                        "ready": True,
                        "last_outcome": "activated",
                        "last_outcome_at": "2026-08-14T09:58:00+00:00",
                    },
                    "authorisation": {
                        "available": True,
                        "stale": False,
                        "refreshed_at": "2026-08-14T09:59:00+00:00",
                        "last_error": None,
                    },
                    "media": HEALTHY_MEDIA,
                },
            },
        )
        self.assertEqual(tuple(request["json"]), HEARTBEAT_RPC_ARGUMENTS)

    def test_controller_status_callback_forwards_validated_media_to_heartbeat_rpc(self):
        now = datetime.now(timezone.utc)
        uploaded_at = (now - timedelta(seconds=5)).isoformat()
        session = FakeSession([FakeResponse(payload={})])
        control_plane = SupabaseControlPlane(
            "https://project.supabase.co", "service-key", "pi-front-gate", session=session
        )

        class StatusSource:
            available = True

            @staticmethod
            def status():
                return {
                    "available": True,
                    "stale": False,
                    "refreshed_at": now.isoformat(),
                    "last_error": None,
                }

        class Relay:
            @staticmethod
            def status():
                return {
                    "ready": True,
                    "last_outcome": "activated",
                    "last_outcome_at": uploaded_at,
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capabilities_path = root / "capabilities.json"
            capabilities_path.write_text(json.dumps({
                "observed_at": int(time.time()),
                "media": HEALTHY_MEDIA,
            }), encoding="utf-8")
            store = LocalStore(root / "gate.db")
            status = lambda: gate_main._controller_status(
                store,
                StatusSource(),
                {"path": "/camera/latest.jpg", "received_at": uploaded_at},
                StatusSource(),
                relay=Relay(),
                camera_directory=root,
                media_capabilities_path=capabilities_path,
                clock=lambda: now,
            )

            self.assertTrue(HeartbeatWorker(control_plane, status).run_once())

        url, request = session.calls[0]
        self.assertEqual(url, "https://project.supabase.co/rest/v1/rpc/update_controller_status")
        self.assertEqual(request["json"], {
            "p_controller_id": "pi-front-gate",
            "p_camera_timestamp": uploaded_at,
            "p_queue_depth": 0,
            "p_capabilities": {
                "audio_available": True,
                "audio_prompts": True,
                "camera": {
                    "configured": True,
                    "upload_ready": True,
                    "last_upload_at": uploaded_at,
                    "upload_recent": True,
                    "connection_probed": False,
                    "connected": None,
                },
                "relay": {
                    "ready": True,
                    "last_outcome": "activated",
                    "last_outcome_at": uploaded_at,
                },
                "authorisation": {
                    "available": True,
                    "stale": False,
                    "refreshed_at": now.isoformat(),
                    "last_error": None,
                },
                "media": HEALTHY_MEDIA,
            },
        })

    def test_heartbeat_media_fails_closed_for_malformed_or_unexpected_values(self):
        malformed_values = (
            None,
            {"video": HEALTHY_MEDIA["video"]},
            {**HEALTHY_MEDIA, "unexpected": "must-not-leak"},
            {**HEALTHY_MEDIA, "video": {**HEALTHY_MEDIA["video"], "secret": "no"}},
        )
        for media in malformed_values:
            with self.subTest(media=media):
                session = FakeSession([FakeResponse(payload={})])
                control_plane = SupabaseControlPlane(
                    "https://project.supabase.co", "service-key", "pi-front-gate",
                    session=session,
                )

                control_plane.heartbeat({"media": media})

                forwarded = session.calls[0][1]["json"]["p_capabilities"]["media"]
                self.assertEqual(UNAVAILABLE_MEDIA, forwarded)
                self.assertNotIn("must-not-leak", json.dumps(forwarded))
                self.assertNotIn("secret", json.dumps(forwarded))

    def test_duplicate_command_id_does_not_reactivate_the_relay_after_ack_failure(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FakeControlPlane([command, command])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, relay, LocalStore(Path(directory) / "gate.db"), clock=lambda: now
            )

            worker.run_once()
            worker.run_once()

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(control_plane.completed[0][1], "completed")
        self.assertEqual(control_plane.completed[1][1], "completed")

    def test_failed_relay_is_acknowledged_as_failed(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FakeControlPlane([command])
        relay = FakeRelay(RelayResult(False, "relay_error"))
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, relay, LocalStore(Path(directory) / "gate.db"), clock=lambda: now
            )

            worker.run_once()

        self.assertEqual(control_plane.completed, [("command-1", "failed", "relay_error")])

    def test_failed_relay_replays_the_same_terminal_acknowledgement(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FakeControlPlane([command, command])
        relay = FakeRelay(RelayResult(False, "relay_error"))
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, relay, LocalStore(Path(directory) / "gate.db"), clock=lambda: now
            )
            worker.run_once()
            worker.run_once()

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(control_plane.completed, [
            ("command-1", "failed", "relay_error"),
            ("command-1", "failed", "relay_error"),
        ])

    def test_lost_failed_acknowledgement_replays_persisted_failure(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FlakyAckControlPlane([command, command])
        relay = FakeRelay(RelayResult(False, "relay_error"))
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, relay, LocalStore(Path(directory) / "gate.db"), clock=lambda: now
            )
            self.assertFalse(worker.run_once())
            self.assertTrue(worker.run_once())

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(control_plane.completed, [
            ("command-1", "failed", "relay_error"),
            ("command-1", "failed", "relay_error"),
        ])

    def test_lost_acknowledgement_replays_after_worker_restart_without_reclaim(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        first_control_plane = FlakyAckControlPlane([command])
        second_control_plane = FakeControlPlane([])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            self.assertFalse(CommandWorker(
                first_control_plane, relay, LocalStore(database), clock=lambda: now
            ).run_once())
            self.assertTrue(CommandWorker(
                second_control_plane, relay, LocalStore(database), clock=lambda: now
            ).run_once())

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(second_control_plane.completed, [
            ("command-1", "completed", None),
        ])

    def test_open_command_ack_survives_restart_without_a_separate_queue_write(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            first_store = FailingAckStore(database)
            first = CommandWorker(
                FailingReplayControlPlane([command]), relay, first_store, clock=lambda: now
            )

            self.assertFalse(first.run_once())
            self.assertEqual(
                LocalStore(database).pending_command_acks(),
                [("command-1", "completed", None)],
            )

            replay_control_plane = FakeControlPlane([])
            self.assertTrue(CommandWorker(
                replay_control_plane, relay, LocalStore(database), clock=lambda: now
            ).run_once())

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(replay_control_plane.completed, [("command-1", "completed", None)])

    def test_failed_ack_replay_does_not_claim_another_command(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            store.queue_command_ack("old-command", "completed", None, now)
            control_plane = FailingReplayControlPlane([
                GateCommand("new-command", "open_gate", now + timedelta(minutes=1))
            ])

            result = CommandWorker(
                control_plane, FakeRelay(), store, clock=lambda: now
            ).run_once()

        self.assertFalse(result)
        self.assertEqual(control_plane.claim_calls, 0)

    def test_ack_replay_cleanup_failure_is_retained_for_another_retry(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = FailingAckCleanupStore(Path(directory) / "gate.db")
            store.queue_command_ack("old-command", "completed", None, now)
            control_plane = FakeControlPlane([])

            result = CommandWorker(
                control_plane, FakeRelay(), store, clock=lambda: now
            ).run_once()
            pending = store.pending_command_acks()

        self.assertFalse(result)
        self.assertEqual(control_plane.completed, [("old-command", "completed", None)])
        self.assertEqual(pending, [
            ("old-command", "completed", None),
        ])

    def test_ack_queue_failure_returns_false_without_terminating_the_worker(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand(
            "command-1", "play_prompt", now + timedelta(minutes=1), "arrival"
        )
        with tempfile.TemporaryDirectory() as directory:
            control_plane = FakeControlPlane([command])
            result = CommandWorker(
                control_plane, FakeRelay(RelayResult(False, "relay_error")),
                FailingAckStore(Path(directory) / "gate.db"), clock=lambda: now,
                prompt_player=PromptPlayer(
                    {"arrival": Path("/opt/gate-controller/prompts/arrival.wav")},
                    runner=lambda _: True,
                ),
            ).run_once()

        self.assertFalse(result)
        self.assertEqual(control_plane.completed, [])

    def test_remote_command_within_shared_cooldown_fails_without_pulse(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FakeControlPlane([command])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            store.record_event(GateEvent(
                source="ocr", reason="exact_match", opened=True, idempotency_key="ocr-1",
                received_at=now, relay_activated_at=now,
            ))
            worker = CommandWorker(control_plane, relay, store, clock=lambda: now + timedelta(seconds=1))
            worker.run_once()

        self.assertEqual(relay.calls, [])
        self.assertEqual(control_plane.completed, [("command-1", "failed", "cooldown")])

    def test_implausibly_future_command_fails_closed_after_clock_rollback(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(hours=1))
        control_plane = FakeControlPlane([command])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, relay, LocalStore(Path(directory) / "gate.db"), clock=lambda: now
            )

            worker.run_once()

        self.assertEqual(relay.calls, [])
        self.assertEqual(control_plane.completed, [
            ("command-1", "expired", "invalid_expiry_window"),
        ])

    def test_open_command_expiring_during_store_coordination_never_pulses(self):
        claimed_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        now = [claimed_at]
        command = GateCommand("command-1", "open_gate", claimed_at + timedelta(seconds=1))
        control_plane = FakeControlPlane([command])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            store = DelayingActuationStore(
                Path(directory) / "gate.db",
                after_attempt=lambda: now.__setitem__(0, claimed_at + timedelta(seconds=2)),
            )

            result = CommandWorker(
                control_plane, relay, store, clock=lambda: now[0]
            ).run_once()
            terminal = store.terminal_outcome("command:command-1")

        self.assertTrue(result)
        self.assertEqual(relay.calls, [])
        self.assertEqual(control_plane.completed, [
            ("command-1", "expired", "expired_before_activation"),
        ])
        self.assertEqual((terminal.status, terminal.detail), (
            "expired", "expired_before_activation",
        ))

    def test_expired_remote_command_does_not_inhibit_local_ocr_opening(self):
        claimed_at = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        now = [claimed_at]
        command = GateCommand("command-1", "open_gate", claimed_at + timedelta(seconds=1))
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            store = DelayingActuationStore(
                Path(directory) / "gate.db",
                after_attempt=lambda: now.__setitem__(0, claimed_at + timedelta(seconds=2)),
            )
            coordinator = ActuationCoordinator(store, relay, clock=lambda: now[0])
            CommandWorker(
                FakeControlPlane([command]), relay, store, clock=lambda: now[0],
                coordinator=coordinator,
            ).run_once()

            local = coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False,
                idempotency_key="ocr-1", received_at=now[0], decision_at=now[0],
            ))

        self.assertTrue(local.opened)
        self.assertEqual(relay.calls, [("ocr", "ocr-1")])

    def test_remote_command_claim_survives_finalization_failure_without_repulsing(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FakeControlPlane([command, command])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            CommandWorker(
                control_plane, relay, FailingFinalizeStore(database), clock=lambda: now
            ).run_once()
            CommandWorker(
                control_plane, relay, LocalStore(database), clock=lambda: now
            ).run_once()

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(control_plane.completed, [
            ("command-1", "failed", "indeterminate_claim"),
            ("command-1", "failed", "indeterminate_claim"),
        ])

    def test_same_worker_reports_an_indeterminate_claim_after_ack_retry(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "open_gate", now + timedelta(minutes=1))
        control_plane = FakeControlPlane([command, command])
        relay = FakeRelay()
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, relay, FailingFinalizeStore(Path(directory) / "gate.db"),
                clock=lambda: now,
            )
            worker.run_once()
            worker.run_once()

        self.assertEqual(relay.calls, [("remote_command", "command:command-1")])
        self.assertEqual(control_plane.completed, [
            ("command-1", "failed", "indeterminate_claim"),
            ("command-1", "failed", "indeterminate_claim"),
        ])

    def test_prompt_player_only_runs_fixed_configured_prompt_keys(self):
        played = []
        player = PromptPlayer({"arrival": Path("/opt/gate-controller/prompts/arrival.wav")},
                              runner=lambda path: played.append(path) or True)

        self.assertTrue(player.play("arrival"))
        self.assertFalse(player.play("../../etc/passwd"))

        self.assertEqual(played, [Path("/opt/gate-controller/prompts/arrival.wav")])

    def test_play_prompt_command_rejects_an_unknown_prompt_key(self):
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        command = GateCommand("command-1", "play_prompt", now + timedelta(minutes=1), "missing")
        control_plane = FakeControlPlane([command])
        with tempfile.TemporaryDirectory() as directory:
            worker = CommandWorker(
                control_plane, FakeRelay(), LocalStore(Path(directory) / "gate.db"),
                prompt_player=PromptPlayer({}), clock=lambda: now,
            )

            worker.run_once()

        self.assertEqual(control_plane.completed, [("command-1", "failed", "invalid_prompt")])


if __name__ == "__main__":
    unittest.main()
