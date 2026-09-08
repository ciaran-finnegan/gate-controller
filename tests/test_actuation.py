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

    def test_persisted_cooldown_records_the_grant_rather_than_a_denial(self):
        """The store's cooldown: another event pulsed the relay recently.

        The decision on this frame was still a grant -- the plate matched, at
        the confidence the reader gave it -- so that is what the event says.
        Only the actuation was skipped, and ``actuation_outcome`` says so.
        """
        now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            coordinator = ActuationCoordinator(store, RecordingRelay(), clock=lambda: now)
            coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=now, decision_at=now, authorised_plate="10CE1990",
                observed_plate="10CE1990", ocr_confidence=0.999,
            ))

            coalesced = coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
                received_at=now, decision_at=now, authorised_plate="10CE1990",
                observed_plate="10CE1990", ocr_confidence=0.999,
            ), outbox_payload={})
            payload = store.event_payload(coalesced.event_id)

        self.assertEqual(coalesced.reason, "cooldown")
        self.assertFalse(coalesced.opened, "no second pulse was attempted")
        self.assertTrue(payload["opened"])
        self.assertEqual(payload["reason"], "exact_match")
        self.assertEqual(payload["observed_plate"], "10CE1990")
        self.assertEqual(payload["ocr_confidence"], 0.999)
        self.assertIsNone(payload["relay_activated_at"])

    def test_in_process_cooldown_records_the_grant_rather_than_a_denial(self):
        """The same, for the coordinator's own last-attempt guard.

        This is the branch that fires when the claim is granted but the relay
        was pulsed within the cooldown by this same process.
        """
        wall = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        monotonic_now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            relay = RecordingRelay()
            coordinator = ActuationCoordinator(
                store, relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
            )
            coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
                received_at=wall[0], decision_at=wall[0], authorised_plate="11WH2571",
                observed_plate="11WH2571", ocr_confidence=0.998,
            ))
            # The wall clock jumps an hour, so the persisted evidence is out of
            # the window and the claim succeeds; only the monotonic guard is
            # left to refuse the second pulse.
            wall[0] += timedelta(hours=1)
            monotonic_now[0] += 1
            coalesced = coordinator.actuate(GateEvent(
                source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
                received_at=wall[0], decision_at=wall[0], authorised_plate="11WH2571",
                observed_plate="11WH2571", ocr_confidence=0.998,
            ), command_ack=("command-1", wall[0]))
            payload = store.event_payload(coalesced.event_id)
            acks = store.pending_command_acks()

        self.assertEqual(coalesced.reason, "cooldown")
        self.assertEqual(relay.calls, [("ocr", "ocr-1")])
        self.assertTrue(payload["opened"])
        self.assertEqual(payload["reason"], "exact_match")
        self.assertEqual(payload["ocr_confidence"], 0.998)
        self.assertEqual(
            acks, [("command-1", "failed", "cooldown")],
            "the caller is still told the relay did not fire",
        )

    def _cooldown_pair(self, store, *, second_inhibition, in_process):
        """Grant once, then present a second matched frame inside the cooldown.

        ``in_process`` picks which of the two cooldown branches answers: the
        persisted one that ``claim_actuation`` refuses, or the coordinator's own
        last-attempt guard, reached by jumping the wall clock past the persisted
        window so only the monotonic evidence is left.
        """
        wall = [datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)]
        monotonic_now = [100.0]
        relay = RecordingRelay()
        coordinator = ActuationCoordinator(
            store, relay, cooldown=timedelta(seconds=20),
            clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
            boot_id="boot-1",
        )
        coordinator.actuate(GateEvent(
            source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
            received_at=wall[0], decision_at=wall[0], authorised_plate="10CE1990",
            observed_plate="10CE1990", ocr_confidence=0.999,
        ))
        if in_process:
            wall[0] += timedelta(hours=1)
            monotonic_now[0] += 1
        else:
            wall[0] += timedelta(seconds=2)
            monotonic_now[0] += 2
            # Leave only the persisted evidence: a fresh coordinator over the
            # same store has no in-process last attempt to remember.
            coordinator = ActuationCoordinator(
                store, relay, cooldown=timedelta(seconds=20),
                clock=lambda: wall[0], monotonic_clock=lambda: monotonic_now[0],
                boot_id="boot-1",
            )
        coalesced = coordinator.actuate(GateEvent(
            source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-2",
            received_at=wall[0], decision_at=wall[0], authorised_plate="10CE1990",
            observed_plate="10CE1990", ocr_confidence=0.999,
        ), outbox_payload={}, pre_activation_inhibit=lambda: second_inhibition)
        return relay, coalesced

    def test_persisted_cooldown_records_a_denial_when_authorisation_was_revoked(self):
        """The plate came off the list between the match and the actuation.

        A pulse would have been held off by ``pre_activation_inhibit`` under the
        relay's lock. The cooldown branch never reaches that lock, so the check
        has to run before the short-circuit -- otherwise a withdrawn
        authorisation is the one way a revoked plate still reads as a grant.
        """
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            relay, coalesced = self._cooldown_pair(
                store, second_inhibition=("failed", "authorisation_revoked"),
                in_process=False,
            )
            payload = store.event_payload(coalesced.event_id)

        self.assertEqual(coalesced.reason, "authorisation_revoked")
        self.assertEqual(coalesced.terminal_status, "failed")
        self.assertEqual(coalesced.terminal_detail, "authorisation_revoked")
        self.assertFalse(coalesced.opened)
        self.assertEqual(relay.calls, [("ocr", "ocr-1")], "no second pulse")
        self.assertFalse(payload["opened"], "a revoked plate is not a grant")
        self.assertEqual(payload["reason"], "authorisation_revoked")
        self.assertIsNone(payload["relay_activated_at"])

    def test_in_process_cooldown_records_a_denial_when_the_deadline_expired(self):
        """The same ordering, on the coordinator's own last-attempt guard."""
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / "gate.db")
            relay, coalesced = self._cooldown_pair(
                store, second_inhibition=("failed", "decision_timeout"),
                in_process=True,
            )
            payload = store.event_payload(coalesced.event_id)

        self.assertEqual(coalesced.reason, "decision_timeout")
        self.assertEqual(coalesced.terminal_status, "failed")
        self.assertEqual(coalesced.terminal_detail, "decision_timeout")
        self.assertFalse(coalesced.opened)
        self.assertEqual(relay.calls, [("ocr", "ocr-1")], "no second pulse")
        self.assertFalse(payload["opened"])
        self.assertEqual(payload["reason"], "decision_timeout")

    def test_an_uninhibited_match_in_cooldown_is_still_recorded_as_a_grant(self):
        """The check gates the grant; it does not replace it.

        Both branches, so neither ordering can quietly start denying the frames
        this change exists to record honestly.
        """
        for in_process in (False, True):
            with self.subTest(in_process=in_process), \
                    tempfile.TemporaryDirectory() as directory:
                store = LocalStore(Path(directory) / "gate.db")
                _relay, coalesced = self._cooldown_pair(
                    store, second_inhibition=None, in_process=in_process
                )
                payload = store.event_payload(coalesced.event_id)

                self.assertEqual(coalesced.reason, "cooldown")
                self.assertTrue(payload["opened"])
                self.assertEqual(payload["reason"], "exact_match")
                self.assertEqual(payload["ocr_confidence"], 0.999)
                self.assertIsNone(payload["relay_activated_at"])

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


class NotifyingRelay:
    """A relay that takes an activation callback, as the real one does."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or RelayResult(True, "activated")

    def trigger(self, source, idempotency_key=None, *, on_activation=None, **kwargs):
        self.calls.append((source, idempotency_key))
        if on_activation is not None:
            on_activation()
        return self.result


class RecordingObserver:
    def __init__(self, raises=False):
        self.actuations = []
        self.outcomes = []
        self._raises = raises

    def note_actuation(self, **fields):
        if self._raises:
            raise RuntimeError("the observer is broken")
        self.actuations.append(fields)

    def note_actuation_outcome(self, **fields):
        if self._raises:
            raise RuntimeError("the observer is broken")
        self.outcomes.append(fields)


class ActivationObserverTests(unittest.TestCase):
    """The audio corpus labels itself from here: the coordinator is the sole
    owner of the relay, so an observer on it sees every actuation."""

    def actuate(self, relay, observer, database):
        now = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        return ActuationCoordinator(
            LocalStore(database), relay, clock=lambda: now,
            activation_observer=observer,
        ).actuate(GateEvent(
            source="ocr", reason="exact_match", opened=False, idempotency_key="ocr-1",
            received_at=now, decision_at=now, observed_plate="11wh2571",
            authorised_plate="11WH2571",
        ))

    def test_the_observer_is_told_when_the_relay_energises_and_how_it_finished(self):
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as directory:
            result = self.actuate(
                NotifyingRelay(), observer, Path(directory) / "gate.db",
            )

        self.assertTrue(result.opened)
        self.assertEqual(len(observer.actuations), 1)
        self.assertEqual(observer.actuations[0]["idempotency_key"], "ocr-1")
        self.assertEqual(observer.actuations[0]["source"], "ocr")
        self.assertEqual(observer.actuations[0]["authorised_plate"], "11WH2571")
        self.assertEqual(len(observer.outcomes), 1)
        self.assertEqual(observer.outcomes[0]["status"], "completed")
        self.assertEqual(observer.outcomes[0]["idempotency_key"], "ocr-1")

    def test_a_relay_without_an_activation_callback_still_labels_the_clip(self):
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as directory:
            result = self.actuate(
                RecordingRelay(), observer, Path(directory) / "gate.db",
            )

        self.assertTrue(result.opened)
        self.assertEqual(len(observer.actuations), 1)
        self.assertEqual(observer.actuations[0]["idempotency_key"], "ocr-1")

    def test_a_relay_that_never_activated_is_never_reported_as_one_that_did(self):
        observer = RecordingObserver()
        with tempfile.TemporaryDirectory() as directory:
            result = self.actuate(
                RecordingRelay(RelayResult(False, "relay_latched", latched=True)),
                observer, Path(directory) / "gate.db",
            )

        self.assertFalse(result.opened)
        self.assertEqual(observer.actuations, [])
        self.assertEqual(observer.outcomes, [])

    def test_a_broken_observer_can_never_stop_the_gate_opening(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.actuate(
                NotifyingRelay(), RecordingObserver(raises=True),
                Path(directory) / "gate.db",
            )

        self.assertTrue(result.opened)
        self.assertEqual(result.reason, "exact_match")

    def test_no_observer_leaves_the_actuation_path_exactly_as_it_was(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.actuate(NotifyingRelay(), None, Path(directory) / "gate.db")

        self.assertTrue(result.opened)
