import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_controller.actuation import ActuationCoordinator
from gate_controller.models import GateEvent, RelayResult
from gate_controller.store import LocalStore


class RecordingRelay:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or RelayResult(True, "activated")

    def trigger(self, source, idempotency_key=None):
        self.calls.append((source, idempotency_key))
        return self.result


class FailingFinalizeStore(LocalStore):
    def finalize_actuation(self, claim, event, **kwargs):
        raise RuntimeError("disk failed after the relay pulse")


class FailingClaimStore(LocalStore):
    def claim_actuation(self, *args, **kwargs):
        raise RuntimeError("disk unavailable before the relay pulse")


class FailingMarkStore(LocalStore):
    def mark_actuation_attempt(self, *args, **kwargs):
        raise RuntimeError("process stopped after the claim was committed")


class ActuationCoordinatorTests(unittest.TestCase):
    def test_relay_latch_does_not_persist_a_nonexistent_activation_attempt(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            first = ActuationCoordinator(
                LocalStore(database),
                RecordingRelay(RelayResult(False, "relay_latched", latched=True)),
                clock=lambda: now,
                monotonic_clock=lambda: 100.0,
                boot_id="boot-1",
            ).actuate(GateEvent(
                source="remote_command", reason="remote_command", opened=False,
                idempotency_key="command:shutdown", received_at=now, decision_at=now,
            ))
            healthy_relay = RecordingRelay()
            second = ActuationCoordinator(
                LocalStore(database), healthy_relay,
                clock=lambda: now + timedelta(seconds=1),
                monotonic_clock=lambda: 101.0,
                boot_id="boot-1",
            ).actuate(GateEvent(
                source="remote_command", reason="remote_command", opened=False,
                idempotency_key="command:after-restart",
                received_at=now + timedelta(seconds=1),
                decision_at=now + timedelta(seconds=1),
            ))

        self.assertEqual(first.reason, "relay_latched")
        self.assertTrue(second.opened)
        self.assertEqual(
            healthy_relay.calls,
            [("remote_command", "command:after-restart")],
        )

    def test_forwards_activation_hook_without_delaying_finalization(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        calls = []

        class CallbackRelay:
            def trigger(self, source, idempotency_key=None, *, on_activation=None):
                calls.append("relay")
                on_activation()
                return RelayResult(True, "activated", idempotency_key, now)

        class FinalizationStore(LocalStore):
            def finalize_actuation(self, *args, **kwargs):
                calls.append("finalize")
                return super().finalize_actuation(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            result = ActuationCoordinator(
                FinalizationStore(Path(directory) / "gate.db"),
                CallbackRelay(),
                clock=lambda: now,
            ).actuate(
                GateEvent(
                    source="ocr",
                    reason="exact_match",
                    opened=False,
                    idempotency_key="ocr-1",
                    received_at=now,
                    decision_at=now,
                ),
                on_activation=lambda: calls.append("activation"),
            )

        self.assertTrue(result.opened)
        self.assertEqual(calls, ["relay", "activation", "finalize"])

    def test_forwards_relay_completion_hook_before_finalization(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        calls = []

        class CallbackRelay:
            def trigger(self, source, idempotency_key=None, *, on_activation=None,
                        on_deactivation=None):
                calls.append("relay")
                on_activation()
                on_deactivation()
                return RelayResult(True, "activated", idempotency_key, now)

        class FinalizationStore(LocalStore):
            def finalize_actuation(self, *args, **kwargs):
                calls.append("finalize")
                return super().finalize_actuation(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            coordinator = ActuationCoordinator(
                FinalizationStore(Path(directory) / "gate.db"),
                CallbackRelay(),
                clock=lambda: now,
            )
            try:
                result = coordinator.actuate(
                    GateEvent(
                        source="ocr", reason="exact_match", opened=False,
                        idempotency_key="ocr-relay-complete", received_at=now,
                        decision_at=now,
                    ),
                    on_activation=lambda: calls.append("activation"),
                    on_deactivation=lambda: calls.append("deactivation"),
                )
            except TypeError as error:
                self.fail(f"coordinator relay completion hook is unavailable: {error}")

        self.assertTrue(result.opened)
        self.assertEqual(
            calls,
            ["relay", "activation", "deactivation", "finalize"],
        )


    def test_successful_finalization_durably_queues_the_command_ack_before_restart(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            result = ActuationCoordinator(
                LocalStore(database), RecordingRelay(), clock=lambda: now
            ).actuate(GateEvent(
                source="remote_command", reason="remote_command", opened=False,
                idempotency_key="command:command-1", received_at=now, decision_at=now,
            ), command_ack=("command-1", now))

            pending_after_restart = LocalStore(database).pending_command_acks()

        self.assertTrue(result.opened)
        self.assertEqual(pending_after_restart, [("command-1", "completed", None)])

    def test_cooldown_outcome_durably_queues_a_failed_command_ack(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            relay = RecordingRelay()
            coordinator = ActuationCoordinator(store, relay, clock=lambda: now)
            coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=now, decision_at=now,
            ))

            result = coordinator.actuate(GateEvent(
                source="remote_command", reason="remote_command", opened=False,
                idempotency_key="command:command-1", received_at=now, decision_at=now,
            ), command_ack=("command-1", now))

            pending = store.pending_command_acks()

        self.assertEqual(result.reason, "cooldown")
        self.assertEqual(pending, [("command-1", "failed", "cooldown")])

    def test_relay_failure_finalization_durably_queues_a_failed_command_ack(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            result = ActuationCoordinator(
                store, RecordingRelay(RelayResult(False, "relay_error")), clock=lambda: now
            ).actuate(GateEvent(
                source="remote_command", reason="remote_command", opened=False,
                idempotency_key="command:command-1", received_at=now, decision_at=now,
            ), command_ack=("command-1", now))

            pending = store.pending_command_acks()

        self.assertEqual(result.reason, "relay_error")
        self.assertEqual(pending, [("command-1", "failed", "relay_error")])

    def test_shared_coordinator_applies_one_persisted_cooldown_to_all_sources(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            relay = RecordingRelay()
            coordinator = ActuationCoordinator(
                LocalStore(Path(directory) / "gate.db"), relay, clock=lambda: now
            )
            automatic = coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=now, decision_at=now,
            ))
            remote = coordinator.actuate(GateEvent(
                source="remote_command", reason="remote_command", opened=False,
                idempotency_key="command-1", received_at=now, decision_at=now,
            ))

        self.assertTrue(automatic.opened)
        self.assertEqual(remote.reason, "cooldown")
        self.assertEqual(relay.calls, [("ocr", "ocr-1")])

    def test_forward_wall_clock_jump_does_not_bypass_real_time_cooldown(self):
        wall = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        monotonic_now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            relay = RecordingRelay()
            coordinator = ActuationCoordinator(
                LocalStore(Path(directory) / "gate.db"), relay,
                cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
            )
            first = coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=wall[0], decision_at=wall[0],
            ))
            wall[0] += timedelta(hours=1)
            monotonic_now[0] += 1
            second = coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
                received_at=wall[0], decision_at=wall[0],
            ))

        self.assertTrue(first.opened)
        self.assertEqual(second.reason, "cooldown")
        self.assertEqual(relay.calls, [("ocr", "ocr-1")])

    def test_same_boot_restart_keeps_monotonic_cooldown_after_wall_clock_jump(self):
        wall = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        monotonic_now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            relay = RecordingRelay()
            first = ActuationCoordinator(
                LocalStore(database), relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
                boot_id="boot-1",
            )
            self.assertTrue(first.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=wall[0], decision_at=wall[0],
            )).opened)

            wall[0] += timedelta(hours=1)
            monotonic_now[0] += 1
            second = ActuationCoordinator(
                LocalStore(database), relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
                boot_id="boot-1",
            ).actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
                received_at=wall[0], decision_at=wall[0],
            ))

        self.assertEqual(second.reason, "cooldown")
        self.assertEqual(relay.calls, [("ocr", "ocr-1")])

    def test_reboot_keeps_cooldown_until_new_boot_uptime_reaches_the_interval(self):
        wall = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        monotonic_now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            relay = RecordingRelay()
            first = ActuationCoordinator(
                LocalStore(database), relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
                boot_id="boot-1",
            )
            self.assertTrue(first.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=wall[0], decision_at=wall[0],
            )).opened)

            wall[0] += timedelta(hours=1)
            monotonic_now[0] = 1.0
            inhibited = ActuationCoordinator(
                LocalStore(database), relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
                boot_id="boot-2",
            ).actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
                received_at=wall[0], decision_at=wall[0],
            ))

            monotonic_now[0] = 21.0
            allowed = ActuationCoordinator(
                LocalStore(database), relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
                boot_id="boot-2",
            ).actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-3",
                received_at=wall[0], decision_at=wall[0],
            ))

        self.assertEqual(inhibited.reason, "cooldown")
        self.assertTrue(allowed.opened)
        self.assertEqual(relay.calls, [("ocr", "ocr-1"), ("ocr", "ocr-3")])

    def test_persisted_attempt_blocks_a_different_key_after_finalization_failure(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            relay = RecordingRelay()
            first = ActuationCoordinator(FailingFinalizeStore(database), relay, clock=lambda: now)
            second = ActuationCoordinator(
                LocalStore(database), relay,
                clock=lambda: now.replace(second=now.second + 1),
            )

            failed = first.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=now, decision_at=now,
            ))
            inhibited = second.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
                received_at=now, decision_at=now,
            ))

        self.assertEqual(failed.reason, "indeterminate_claim")
        self.assertEqual(inhibited.reason, "cooldown")
        self.assertEqual(relay.calls, [("ocr", "ocr-1")])

    def test_interrupted_finalization_recovers_event_outbox_and_evidence_on_restart(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            result = ActuationCoordinator(
                FailingFinalizeStore(database), RecordingRelay(), clock=lambda: now
            ).actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False,
                idempotency_key="ocr-1", received_at=now, decision_at=now,
                authorised_plate="12D3456", observed_plate="12D3456",
                ocr_confidence=0.98,
            ), outbox_payload={"event_id": None, "image_sha256": digest})

            pending_store = FailingFinalizeStore(database)
            self.assertIn(digest, pending_store.pending_evidence_digests())

            recovered = LocalStore(database)
            self.assertEqual(recovered.recover_interrupted_actuations(), 1)
            terminal = recovered.terminal_outcome("ocr-1")
            outbox = recovered.pending_outbox_items()

        self.assertEqual(result.reason, "indeterminate_claim")
        self.assertEqual((terminal.status, terminal.detail), (
            "failed", "indeterminate_claim",
        ))
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0][1]["image_sha256"], digest)
        self.assertEqual(outbox[0][1]["reason"], "indeterminate_claim")
        self.assertFalse(outbox[0][1]["opened"])

    def test_interrupted_claim_before_activation_recovers_an_auditable_failure(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gate.db"
            relay = RecordingRelay()
            result = ActuationCoordinator(
                FailingMarkStore(database), relay, clock=lambda: now
            ).actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False,
                idempotency_key="ocr-1", received_at=now, decision_at=now,
                authorised_plate="12D3456", observed_plate="12D3456",
                ocr_confidence=0.98,
            ), outbox_payload={"event_id": None})

            recovered = LocalStore(database)
            self.assertEqual(recovered.recover_interrupted_actuations(), 1)
            terminal = recovered.terminal_outcome("ocr-1")
            outbox = recovered.pending_outbox_items()

        self.assertEqual(result.reason, "actuation_inhibit_error")
        self.assertEqual(relay.calls, [])
        self.assertEqual((terminal.status, terminal.detail), (
            "failed", "interrupted_before_activation",
        ))
        self.assertEqual(outbox[0][1]["reason"], "interrupted_before_activation")
        self.assertFalse(outbox[0][1]["opened"])

    def test_claim_failure_fails_closed_without_touching_the_relay(self):
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            relay = RecordingRelay()
            result = ActuationCoordinator(
                FailingClaimStore(Path(directory) / "gate.db"), relay, clock=lambda: now
            ).actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=now, decision_at=now,
            ))

        self.assertEqual(result.reason, "actuation_inhibit_error")
        self.assertEqual(relay.calls, [])
