import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .models import ActuationClaim, GateEvent, TerminalOutcome
from .telemetry import EventTelemetry, MAX_DELIVERY_ATTEMPT, MAX_DURATION_MS


_TELEMETRY_FRAME_KEYS = (
    "sequence", "digest", "width", "height", "sharpness", "brightness",
    "darkness", "highlight_clipping", "status",
)
_TELEMETRY_OCR_KEYS = (
    "frame_sequence", "duration_ms", "status", "plate", "confidence", "make", "colour",
)
_MAX_TELEMETRY_PAGE_SIZE = 100
_SNAPSHOT_PAGES_PER_STEP = 16
_SNAPSHOT_BUSY_TIMEOUT_SECONDS = 0.25
_SNAPSHOT_BUSY_SLICE_MS = 10
_OUTBOX_READY = "ready"
_OUTBOX_AWAITING_TELEMETRY = "awaiting_telemetry"
_OUTBOX_TELEMETRY_READY = "telemetry_ready"
_OUTBOX_LOCAL_ONLY = "local_only"
_LOGGER = logging.getLogger(__name__)


class _MalformedTelemetry(ValueError):
    pass


class LocalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def was_opened_since(self, cutoff: datetime) -> bool:
        with closing(self._connect()) as connection:
            return self._was_opened_since(connection, cutoff)

    def claim_actuation(self, idempotency_key: str, claimed_at: datetime,
                        cooldown_cutoff: datetime | None = None, *,
                        monotonic_cutoff: float | None = None,
                        boot_id: str | None = None, event: GateEvent | None = None,
                        outbox_payload: dict | None = None,
                        command_ack: tuple[str, datetime] | None = None) -> ActuationClaim:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, state, claimed_at FROM actuation_claims WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                connection.commit()
                status = "indeterminate_claim" if existing[1] == "claimed" else "duplicate_event"
                return ActuationClaim(idempotency_key, status, _parse_timestamp(existing[2]), existing[0])
            if cooldown_cutoff is not None and self._was_opened_since(
                connection, cooldown_cutoff, monotonic_cutoff, boot_id
            ):
                connection.commit()
                return ActuationClaim(idempotency_key, "cooldown")
            cursor = connection.execute(
                """
                INSERT INTO actuation_claims (
                    idempotency_key, claimed_at, state, activation_attempt_at,
                    pending_event, pending_outbox_payload, pending_command_id,
                    pending_command_created_at
                ) VALUES (?, ?, 'claimed', NULL, ?, ?, ?, ?)
                """,
                (
                    idempotency_key, _timestamp(claimed_at),
                    _encode_pending_event(event) if event is not None else None,
                    _encode_optional_json(outbox_payload),
                    command_ack[0] if command_ack is not None else None,
                    _timestamp(command_ack[1]) if command_ack is not None else None,
                ),
            )
            connection.commit()
            return ActuationClaim(idempotency_key, "claimed", _as_utc(claimed_at), cursor.lastrowid)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_actuation_attempt(self, claim: ActuationClaim, attempted_at: datetime, *,
                               event: GateEvent, outbox_payload: dict | None = None,
                               command_ack: tuple[str, datetime] | None = None,
                               attempted_monotonic: float | None = None,
                               boot_id: str | None = None) -> None:
        """Persist recovery data and the global inhibit before GPIO can be energized."""
        if claim.status != "claimed" or claim.claim_id is None:
            raise ValueError("actuation attempt requires an active claim")
        pending_command_id = command_ack[0] if command_ack is not None else None
        pending_command_created_at = (
            _timestamp(command_ack[1]) if command_ack is not None else None
        )
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE actuation_claims
                SET activation_attempt_at = ?, pending_event = ?,
                    pending_outbox_payload = ?, pending_command_id = ?,
                    pending_command_created_at = ?, activation_monotonic = ?, boot_id = ?
                WHERE id = ? AND state = 'claimed' AND activation_attempt_at IS NULL
                """,
                (
                    _timestamp(attempted_at), _encode_pending_event(event),
                    _encode_optional_json(outbox_payload), pending_command_id,
                    pending_command_created_at, attempted_monotonic, boot_id,
                    claim.claim_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("actuation claim is no longer safe to execute")

    def finalize_actuation(self, claim: ActuationClaim, event: GateEvent, *,
                           terminal_status: str | None = None,
                           terminal_detail: str | None = None,
                           outbox_payload: dict | None = None,
                           command_ack: tuple[str, datetime] | None = None,
                           retain_activation_attempt: bool = True) -> int:
        if claim.status != "claimed" or event.idempotency_key != claim.idempotency_key:
            raise ValueError("event does not match active actuation claim")
        status = terminal_status or ("completed" if event.opened else "failed")
        keep_attempt = retain_activation_attempt and status != "expired"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM actuation_claims WHERE id = ?", (claim.claim_id,)
            ).fetchone()
            if row is None or row[0] != "claimed":
                raise RuntimeError("actuation claim is no longer active")
            event_id = self._insert_event(connection, event)
            self._ensure_outbox(connection, event_id, outbox_payload)
            self._ensure_command_ack(connection, command_ack, status, terminal_detail)
            connection.execute(
                """
                UPDATE actuation_claims
                SET state = 'finalized', event_id = ?, terminal_status = ?, terminal_detail = ?,
                    activation_attempt_at = CASE
                        WHEN ? THEN activation_attempt_at ELSE NULL
                    END, activation_monotonic = CASE
                        WHEN ? THEN activation_monotonic ELSE NULL
                    END, boot_id = CASE WHEN ? THEN boot_id ELSE NULL END,
                    pending_event = NULL, pending_outbox_payload = NULL,
                    pending_command_id = NULL, pending_command_created_at = NULL
                WHERE id = ?
                """,
                (
                    event_id, status, terminal_detail, int(keep_attempt), int(keep_attempt),
                    int(keep_attempt), claim.claim_id,
                ),
            )
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_terminal_outcome(self, event: GateEvent, *, status: str, detail: str | None,
                                outbox_payload: dict | None = None,
                                command_ack: tuple[str, datetime] | None = None) -> int:
        """Record a no-pulse terminal decision, including command cooldown failures."""
        if not event.idempotency_key:
            return self.record_event_with_outbox(event, outbox_payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id FROM actuation_claims WHERE idempotency_key = ?",
                (event.idempotency_key,),
            ).fetchone()
            if existing:
                if existing[0] is not None:
                    self._ensure_outbox(connection, existing[0], outbox_payload)
                self._ensure_command_ack(connection, command_ack, status, detail)
                connection.commit()
                return existing[0]
            event_id = self._insert_event(connection, event)
            self._ensure_outbox(connection, event_id, outbox_payload)
            self._ensure_command_ack(connection, command_ack, status, detail)
            connection.execute(
                """
                INSERT INTO actuation_claims (
                    idempotency_key, claimed_at, state, event_id, terminal_status, terminal_detail
                ) VALUES (?, ?, 'finalized', ?, ?, ?)
                """,
                (event.idempotency_key, _timestamp(event.decision_at or event.received_at),
                 event_id, status, detail),
            )
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_event_with_outbox(self, event: GateEvent,
                                 outbox_payload: dict | None = None) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event(connection, event)
            except sqlite3.IntegrityError:
                if not event.idempotency_key:
                    raise
                row = connection.execute(
                    "SELECT id FROM events WHERE idempotency_key = ?", (event.idempotency_key,)
                ).fetchone()
                if row is None:
                    raise
                event_id = row[0]
            self._ensure_outbox(connection, event_id, outbox_payload)
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_event(self, event: GateEvent) -> int:
        return self.record_event_with_outbox(event)

    def recover_interrupted_actuations(self) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            recovered = self._recover_interrupted_actuations(connection)
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def terminal_outcome(self, idempotency_key: str) -> TerminalOutcome | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT terminal_status, terminal_detail, event_id, state
                FROM actuation_claims WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None or row[3] == "claimed":
            return None
        return TerminalOutcome(row[0] or "failed", row[1], row[2])

    def event_exists(self, idempotency_key: str) -> bool:
        return self.actuation_claim_status(idempotency_key) is not None or self._event_id(idempotency_key) is not None

    def event_id(self, idempotency_key: str) -> int | None:
        return self._event_id(idempotency_key)

    def actuation_claim_status(self, idempotency_key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM actuation_claims WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            return None
        return "indeterminate_claim" if row[0] == "claimed" else "duplicate_event"

    def ensure_outbox(self, event_id: int, payload: dict) -> int | None:
        with closing(self._connect()) as connection, connection:
            return self._ensure_outbox(connection, event_id, payload)

    def queue_outbox(self, event_id: int, payload: dict) -> int:
        return self.ensure_outbox(event_id, payload) or self._outbox_id(event_id)

    def pending_outbox_count(self) -> int:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT COUNT(*) FROM outbox
                WHERE completed_at IS NULL AND send_state != ?
                """,
                (_OUTBOX_LOCAL_ONLY,),
            ).fetchone()[0]

    def pending_outbox_items(
        self, limit: int = 20, *, after_id: int | None = None,
    ) -> list[tuple[int, dict]]:
        with closing(self._connect()) as connection:
            if after_id is None:
                rows = connection.execute(
                    """
                    SELECT id, payload FROM outbox
                    WHERE completed_at IS NULL AND send_state IN (?, ?)
                    ORDER BY id LIMIT ?
                    """,
                    (_OUTBOX_READY, _OUTBOX_TELEMETRY_READY, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, payload FROM outbox
                    WHERE completed_at IS NULL AND send_state IN (?, ?)
                    ORDER BY CASE WHEN id > ? THEN 0 ELSE 1 END, id
                    LIMIT ?
                    """,
                    (
                        _OUTBOX_READY, _OUTBOX_TELEMETRY_READY, after_id, limit,
                    ),
                ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    def release_outbox_without_telemetry(self, event_id: int) -> None:
        """Make a processor outbox sendable when no trace could be attached."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE outbox SET send_state = ?
                WHERE event_id = ? AND completed_at IS NULL AND send_state = ?
                """,
                (_OUTBOX_READY, event_id, _OUTBOX_AWAITING_TELEMETRY),
            )

    def attach_event_telemetry(self, event_id: int, telemetry: EventTelemetry) -> bool:
        """Attach one trace after the terminal event transaction has committed."""
        payload = _telemetry_payload(telemetry)
        created_at = _timestamp(datetime.now(timezone.utc))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM events WHERE id = ?", (event_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown event {event_id}")
            _enrich_outbox_delivery(connection, event_id, payload)
            encoded = _encode_json(payload)
            existing = connection.execute(
                "SELECT trace_id, payload FROM event_telemetry WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            inserted = existing is None
            if existing is not None:
                existing_payload = json.loads(existing[1])
                if existing[0] != payload["trace_id"] or not _same_trace(
                    existing_payload, payload
                ):
                    raise ValueError("event already has conflicting telemetry")
                payload = existing_payload
                encoded = _encode_json(payload)
            else:
                trace_owner = connection.execute(
                    "SELECT event_id FROM event_telemetry WHERE trace_id = ?",
                    (payload["trace_id"],),
                ).fetchone()
                if trace_owner is not None:
                    raise ValueError("telemetry trace is already attached to another event")
                connection.execute(
                    """
                    INSERT INTO event_telemetry (event_id, trace_id, payload, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event_id, payload["trace_id"], encoded, created_at),
                )
            self._promote_outbox(connection, event_id, payload)
            connection.commit()
            cloud_enqueued_at = payload.get("stage_timestamps", {}).get(
                "cloud_enqueued_at"
            )
            if cloud_enqueued_at is not None:
                _LOGGER.info(
                    "gate_pipeline stage=cloud_enqueued trace_id=%s event_id=%d "
                    "observed_at=%s",
                    payload["trace_id"], event_id, cloud_enqueued_at,
                )
            return inserted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def event_telemetry(self, event_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM event_telemetry WHERE event_id = ?", (event_id,)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def event_telemetry_rows(self, since: datetime) -> list[dict]:
        exported = []
        after = None
        while True:
            page = self.event_telemetry_page(since, after=after)
            if not page:
                return exported
            exported.extend(page)
            after = (page[-1]["received_at"], page[-1]["event_id"])

    def event_telemetry_page(
        self,
        since: datetime,
        *,
        after: tuple[str, int] | None = None,
        limit: int = _MAX_TELEMETRY_PAGE_SIZE,
    ) -> list[dict]:
        try:
            bounded_limit = min(max(int(limit), 1), _MAX_TELEMETRY_PAGE_SIZE)
        except (TypeError, ValueError):
            bounded_limit = _MAX_TELEMETRY_PAGE_SIZE
        parameters: tuple[object, ...]
        if after is None:
            keyset = ""
            parameters = (_timestamp(since), bounded_limit)
        else:
            received_at, event_id = after
            keyset = """
              AND (e.received_at > ? OR (e.received_at = ? AND e.id > ?))
            """
            parameters = (
                _timestamp(since), received_at, received_at, int(event_id), bounded_limit,
            )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT e.id, e.received_at, e.decision_at, e.relay_activated_at,
                       e.source, e.reason, e.opened, e.authorised_plate,
                       e.observed_plate, e.ocr_confidence, t.created_at, t.payload
                FROM event_telemetry AS t
                JOIN events AS e ON e.id = t.event_id
                WHERE e.received_at >= ?
                {keyset}
                ORDER BY e.received_at, e.id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_event_telemetry_export_row(row) for row in rows]

    def create_telemetry_snapshot(self, destination: Path) -> None:
        """Copy the live database in short backup steps with bounded busy waits."""
        deadline = time.monotonic() + _SNAPSHOT_BUSY_TIMEOUT_SECONDS
        destination = Path(destination)

        def apply_remaining_budget(connection: sqlite3.Connection) -> None:
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1_000)
            if remaining_ms <= 0:
                raise TimeoutError("telemetry snapshot database remained busy")
            busy_timeout_ms = min(remaining_ms, _SNAPSHOT_BUSY_SLICE_MS)
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")

        def check_deadline(status: int, _remaining: int, _total: int) -> None:
            if status == sqlite3.SQLITE_DONE:
                return
            apply_remaining_budget(source)

        source_uri = f"{self.path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(
            source_uri, uri=True, timeout=0
        )) as source, closing(sqlite3.connect(destination, timeout=0)) as snapshot:
            apply_remaining_budget(source)
            source.backup(
                snapshot,
                pages=_SNAPSHOT_PAGES_PER_STEP,
                progress=check_deadline,
                sleep=0,
            )

    def prepare_outbox_attempt(self, item_id: int,
                               attempted_at: datetime | None = None) -> dict | None:
        attempted_at = attempted_at or datetime.now(timezone.utc)
        connection = self._connect()
        fallback = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, payload, created_at FROM outbox
                WHERE id = ? AND completed_at IS NULL
                  AND send_state IN (?, ?)
                """,
                (item_id, _OUTBOX_READY, _OUTBOX_TELEMETRY_READY),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            event_id, payload_text, created_at = row
            payload = json.loads(payload_text)
            fallback = _v2_outbox_payload(payload)
            telemetry_row = connection.execute(
                "SELECT payload FROM event_telemetry WHERE event_id = ?", (event_id,)
            ).fetchone()
            if telemetry_row is None:
                connection.commit()
                return fallback
            try:
                telemetry = _decode_telemetry_payload(telemetry_row[0])
                attempt = min(
                    _telemetry_attempt(telemetry) + 1, MAX_DELIVERY_ATTEMPT
                )
                telemetry["delivery"] = {"outbox_attempt": attempt, "state": "sending"}
                queued_at = _parse_timestamp(created_at)
                stage_timestamps = dict(telemetry.get("stage_timestamps", {}))
                stage_timestamps.setdefault("cloud_enqueued_at", _timestamp(queued_at))
                stage_timestamps["cloud_send_started_at"] = _timestamp(attempted_at)
                stage_timestamps.pop("cloud_acknowledged_at", None)
                telemetry["stage_timestamps"] = stage_timestamps
                lag_ms = max(
                    0,
                    int((_as_utc(attempted_at) - queued_at).total_seconds() * 1000),
                )
                telemetry["stage_durations"]["delivery_lag_ms"] = min(
                    lag_ms, MAX_DURATION_MS
                )
                self._write_telemetry_payload(connection, event_id, telemetry)
                payload = dict(fallback)
                payload["schema_version"] = 3
                payload["telemetry"] = telemetry
                connection.execute(
                    "UPDATE outbox SET payload = ? WHERE id = ? AND completed_at IS NULL",
                    (_encode_json(payload), item_id),
                )
            except _MalformedTelemetry:
                connection.rollback()
                _log_telemetry_fallback("malformed")
                return fallback
            except Exception:
                connection.rollback()
                _log_telemetry_fallback("rewrite_failed")
                return fallback
            connection.commit()
            return payload
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            if fallback is not None:
                _log_telemetry_fallback("rewrite_failed")
                return fallback
            raise
        finally:
            connection.close()

    def mark_outbox_retry(self, item_id: int) -> None:
        self._set_outbox_delivery_state(item_id, "retry_pending")

    def purge_delivered_telemetry(self, cutoff: datetime) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT t.event_id, o.id, o.payload
                FROM event_telemetry AS t
                JOIN outbox AS o ON o.event_id = t.event_id
                WHERE t.created_at < ? AND o.completed_at IS NOT NULL
                """,
                (_timestamp(cutoff),),
            ).fetchall()
            removed = 0
            for event_id, outbox_id, payload_text in rows:
                try:
                    payload = _v2_outbox_payload(json.loads(payload_text))
                except (TypeError, ValueError):
                    continue
                connection.execute(
                    "UPDATE outbox SET payload = ? WHERE id = ? AND completed_at IS NOT NULL",
                    (_encode_json(payload), outbox_id),
                )
                removed += connection.execute(
                    "DELETE FROM event_telemetry WHERE event_id = ?", (event_id,)
                ).rowcount
            connection.commit()
            return removed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pending_evidence_digests(self) -> set[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM outbox WHERE completed_at IS NULL"
            ).fetchall()
            rows.extend(connection.execute(
                """
                SELECT pending_outbox_payload FROM actuation_claims
                WHERE state = 'claimed' AND pending_outbox_payload IS NOT NULL
                """
            ).fetchall())
        digests = set()
        for (payload_text,) in rows:
            try:
                digest = json.loads(payload_text).get("image_sha256")
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(digest, str):
                digests.add(digest)
        return digests

    def bind_pending_outbox_controller(self, controller_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, payload FROM outbox WHERE completed_at IS NULL"
            ).fetchall()
            for outbox_id, payload_text in rows:
                try:
                    payload = json.loads(payload_text)
                except (TypeError, ValueError):
                    payload = {}
                changed = False
                legacy_path = payload.pop("_local_image_path", None)
                if legacy_path is not None:
                    changed = True
                    if "image_sha256" not in payload:
                        payload["image_status"] = "legacy_evidence_unavailable"
                if not payload.get("controller_id"):
                    payload["controller_id"] = controller_id
                    changed = True
                if not changed:
                    continue
                connection.execute(
                    "UPDATE outbox SET payload = ? WHERE id = ?",
                    (json.dumps(payload, sort_keys=True), outbox_id),
                )

    def event_payload(self, event_id: int) -> dict:
        with closing(self._connect()) as connection:
            payload = self._event_payload(connection, event_id)
        if payload is None:
            raise KeyError(f"unknown event {event_id}")
        return payload

    def queue_command_ack(self, command_id: str, status: str, detail: str | None,
                          created_at: datetime) -> None:
        with closing(self._connect()) as connection, connection:
            self._ensure_command_ack(connection, (command_id, created_at), status, detail)

    def pending_command_acks(self, limit: int = 20) -> list[tuple[str, str, str | None]]:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT command_id, status, detail FROM command_ack_outbox
                ORDER BY created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def complete_command_ack(self, command_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM command_ack_outbox WHERE command_id = ?", (command_id,))

    def complete_outbox_item(
        self, item_id: int, completed_at: datetime | None = None, *,
        prepared_payload: dict | None = None,
    ) -> None:
        self._set_outbox_delivery_state(
            item_id,
            "delivered",
            completed_at or datetime.now(timezone.utc),
            prepared_payload=prepared_payload,
        )

    def _set_outbox_delivery_state(self, item_id: int, state: str,
                                   completed_at: datetime | None = None, *,
                                   prepared_payload: dict | None = None) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_id, payload FROM outbox WHERE id = ? AND completed_at IS NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return
            event_id, payload_text = row
            payload = (
                json.loads(payload_text)
                if prepared_payload is None else dict(prepared_payload)
            )
            fallback = _v2_outbox_payload(payload)
            if prepared_payload is None:
                payload = fallback
            telemetry_row = connection.execute(
                "SELECT payload FROM event_telemetry WHERE event_id = ?", (event_id,)
            ).fetchone()
            prepared_telemetry = payload.get("telemetry")
            if telemetry_row is not None and (
                prepared_payload is None or prepared_telemetry is not None
            ):
                connection.execute("SAVEPOINT telemetry_delivery_state")
                try:
                    telemetry_source = (
                        _encode_json(prepared_telemetry)
                        if prepared_telemetry is not None else telemetry_row[0]
                    )
                    telemetry = _decode_telemetry_payload(telemetry_source)
                    telemetry["delivery"] = {
                        "outbox_attempt": _telemetry_attempt(telemetry),
                        "state": state,
                    }
                    if state == "delivered" and completed_at is not None:
                        stage_timestamps = dict(
                            telemetry.get("stage_timestamps", {})
                        )
                        acknowledged_at = _timestamp(completed_at)
                        stage_timestamps["cloud_acknowledged_at"] = acknowledged_at
                        telemetry["stage_timestamps"] = stage_timestamps
                        send_started_at = stage_timestamps.get(
                            "cloud_send_started_at"
                        )
                        send_to_ack_ms = _bounded_interval_ms(
                            send_started_at, acknowledged_at
                        )
                        if send_to_ack_ms is not None:
                            telemetry["stage_durations"][
                                "cloud_send_to_ack_ms"
                            ] = send_to_ack_ms
                    self._write_telemetry_payload(connection, event_id, telemetry)
                    if prepared_payload is None:
                        payload = dict(fallback)
                        payload["schema_version"] = 3
                        payload["telemetry"] = telemetry
                except _MalformedTelemetry:
                    connection.execute("ROLLBACK TO telemetry_delivery_state")
                    _log_telemetry_fallback("malformed")
                except Exception:
                    connection.execute("ROLLBACK TO telemetry_delivery_state")
                    _log_telemetry_fallback("rewrite_failed")
                finally:
                    connection.execute("RELEASE telemetry_delivery_state")
            connection.execute(
                """
                UPDATE outbox SET payload = ?, completed_at = ?
                WHERE id = ? AND completed_at IS NULL
                """,
                (
                    _encode_json(payload),
                    _timestamp(completed_at) if completed_at is not None else None,
                    item_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _write_telemetry_payload(connection: sqlite3.Connection, event_id: int,
                                 telemetry: dict) -> None:
        connection.execute(
            "UPDATE event_telemetry SET payload = ? WHERE event_id = ?",
            (_encode_json(telemetry), event_id),
        )

    @staticmethod
    def _promote_outbox(connection: sqlite3.Connection, event_id: int,
                        telemetry: dict) -> None:
        row = connection.execute(
            "SELECT id, payload, completed_at FROM outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return
        outbox_id, payload_text, completed_at = row
        payload = json.loads(payload_text)
        if completed_at is not None:
            return
        payload["schema_version"] = 3
        payload["telemetry"] = telemetry
        connection.execute(
            "UPDATE outbox SET payload = ?, send_state = ? WHERE id = ?",
            (_encode_json(payload), _OUTBOX_TELEMETRY_READY, outbox_id),
        )

    def _event_id(self, idempotency_key: str) -> int | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT id FROM events WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return row[0] if row else None

    def _outbox_id(self, event_id: int) -> int:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT id FROM outbox WHERE event_id = ?", (event_id,)).fetchone()[0]

    @staticmethod
    def _ensure_command_ack(connection: sqlite3.Connection,
                            command_ack: tuple[str, datetime] | None,
                            status: str, detail: str | None) -> None:
        if command_ack is None:
            return
        command_id, created_at = command_ack
        connection.execute(
            """
            INSERT INTO command_ack_outbox (command_id, status, detail, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                status = excluded.status, detail = excluded.detail
            """,
            (command_id, status, detail, _timestamp(created_at)),
        )

    @staticmethod
    def _ensure_outbox(connection: sqlite3.Connection, event_id: int,
                       payload: dict | None) -> int | None:
        if payload is None:
            return None
        payload = dict(payload)
        send_state = (
            _OUTBOX_AWAITING_TELEMETRY
            if payload.pop("_awaiting_telemetry", False) is True
            else _OUTBOX_READY
        )
        encoded = LocalStore._event_payload(connection, event_id) or {}
        encoded.update(payload)
        if encoded.get("event_id") is None:
            encoded["event_id"] = event_id
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO outbox (event_id, payload, created_at, send_state)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_id, json.dumps(encoded, sort_keys=True),
                _timestamp(datetime.now(timezone.utc)), send_state,
            ),
        )
        return cursor.lastrowid or None

    @staticmethod
    def _was_opened_since(connection: sqlite3.Connection, cutoff: datetime,
                          monotonic_cutoff: float | None = None,
                          boot_id: str | None = None) -> bool:
        if monotonic_cutoff is not None and boot_id is not None:
            monotonic_attempt = connection.execute(
                """
                SELECT 1 FROM actuation_claims
                WHERE boot_id = ? AND activation_monotonic >= ? LIMIT 1
                """,
                (boot_id, monotonic_cutoff),
            ).fetchone()
            if monotonic_attempt is not None:
                return True
            previous_boot = connection.execute(
                """
                SELECT boot_id FROM actuation_claims
                WHERE activation_attempt_at IS NOT NULL AND boot_id IS NOT NULL
                ORDER BY activation_attempt_at DESC LIMIT 1
                """
            ).fetchone()
            if (previous_boot is not None and previous_boot[0] != boot_id
                    and monotonic_cutoff < 0):
                return True
        cutoff_text = _timestamp(cutoff)
        event = connection.execute(
            """
            SELECT 1 FROM events WHERE (
                relay_activated_at >= ? OR
                (opened = 1 AND relay_activated_at IS NULL AND received_at >= ?)
            ) LIMIT 1
            """, (cutoff_text, cutoff_text),
        ).fetchone()
        if event is not None:
            return True
        return connection.execute(
            """
            SELECT 1 FROM actuation_claims
            WHERE activation_attempt_at >= ? LIMIT 1
            """,
            (cutoff_text,),
        ).fetchone() is not None

    @staticmethod
    def _event_payload(connection: sqlite3.Connection, event_id: int) -> dict | None:
        row = connection.execute(
            """
            SELECT id, received_at, decision_at, relay_activated_at, source, reason,
                   opened, idempotency_key, authorised_plate, observed_plate,
                   ocr_confidence
            FROM events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "event_id", "received_at", "decision_at", "relay_activated_at", "source",
            "reason", "opened", "idempotency_key", "authorised_plate",
            "observed_plate", "ocr_confidence",
        )
        payload = dict(zip(keys, row))
        payload["schema_version"] = 2
        payload["opened"] = bool(payload["opened"])
        return payload

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: GateEvent) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events (
                received_at, decision_at, relay_activated_at, source, reason, opened,
                idempotency_key, authorised_plate, observed_plate, ocr_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_timestamp(event.received_at), _optional_timestamp(event.decision_at),
             _optional_timestamp(event.relay_activated_at), event.source, event.reason,
             int(event.opened), event.idempotency_key, event.authorised_plate,
             event.observed_plate, event.ocr_confidence),
        )
        return cursor.lastrowid

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _migrate(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, decision_at TEXT,
                    relay_activated_at TEXT, source TEXT NOT NULL, reason TEXT NOT NULL,
                    opened INTEGER NOT NULL, idempotency_key TEXT UNIQUE, authorised_plate TEXT,
                    observed_plate TEXT, ocr_confidence REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS actuation_claims (
                    id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    claimed_at TEXT NOT NULL, state TEXT NOT NULL,
                    event_id INTEGER REFERENCES events(id), detail TEXT,
                    terminal_status TEXT, terminal_detail TEXT,
                    activation_attempt_at TEXT, activation_monotonic REAL, boot_id TEXT,
                    pending_event TEXT,
                    pending_outbox_payload TEXT, pending_command_id TEXT,
                    pending_command_created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id),
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
                    send_state TEXT NOT NULL DEFAULT 'ready'
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS command_ack_outbox (
                    command_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    detail TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_telemetry (
                    event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                    trace_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_relay_cooldown ON events (opened, relay_activated_at);
                CREATE INDEX IF NOT EXISTS events_received_cooldown ON events (opened, received_at);
                CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_per_event ON outbox (event_id);
                CREATE INDEX IF NOT EXISTS event_telemetry_created_at
                    ON event_telemetry (created_at);
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(actuation_claims)")}
            for name in (
                "terminal_status", "terminal_detail", "activation_attempt_at",
                "activation_monotonic", "boot_id",
                "pending_event", "pending_outbox_payload", "pending_command_id",
                "pending_command_created_at",
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE actuation_claims ADD COLUMN {name} TEXT")
            outbox_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(outbox)")
            }
            if "send_state" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE outbox ADD COLUMN send_state TEXT NOT NULL DEFAULT 'ready'"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS outbox_pending_send
                ON outbox (send_state, completed_at, id)
                """
            )
            applied = connection.execute("SELECT 1 FROM schema_migrations WHERE name = 'legacy_log_import'").fetchone()
            if not applied and self._legacy_log_exists(connection):
                self._import_legacy_log(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migrations (name) VALUES ('legacy_log_import')")
            enriched = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'outbox_payload_v1'"
            ).fetchone()
            if not enriched:
                for outbox_id, event_id, payload_text, completed_at in connection.execute(
                    "SELECT id, event_id, payload, completed_at FROM outbox"
                ).fetchall():
                    if completed_at is not None:
                        continue
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, ValueError):
                        payload = {}
                    event_payload = self._event_payload(connection, event_id) or {}
                    event_payload.update(payload)
                    connection.execute(
                        "UPDATE outbox SET payload = ? WHERE id = ?",
                        (json.dumps(event_payload, sort_keys=True), outbox_id),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES ('outbox_payload_v1')"
                )
            evidence_v2 = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'outbox_evidence_v2'"
            ).fetchone()
            if not evidence_v2:
                for outbox_id, event_id, payload_text in connection.execute(
                    "SELECT id, event_id, payload FROM outbox"
                ).fetchall():
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, ValueError):
                        payload = {}
                    legacy_path = payload.pop("_local_image_path", None)
                    if legacy_path is not None and "image_sha256" not in payload:
                        payload["image_status"] = "legacy_evidence_unavailable"
                    event_payload = self._event_payload(connection, event_id) or {}
                    event_payload.update(payload)
                    connection.execute(
                        "UPDATE outbox SET payload = ? WHERE id = ?",
                        (json.dumps(event_payload, sort_keys=True), outbox_id),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES ('outbox_evidence_v2')"
                )
            incompatible_followup = connection.execute(
                """
                SELECT 1 FROM schema_migrations
                WHERE name = 'outbox_incompatible_followup_v2'
                """
            ).fetchone()
            if not incompatible_followup:
                for outbox_id, payload_text, send_state in connection.execute(
                    """
                    SELECT id, payload, send_state FROM outbox
                    WHERE completed_at IS NULL
                      AND send_state IN (?, ?)
                    """,
                    (_OUTBOX_READY, _OUTBOX_TELEMETRY_READY),
                ).fetchall():
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, ValueError):
                        continue
                    if not _is_incompatible_v3_followup(payload, send_state):
                        continue
                    connection.execute(
                        "UPDATE outbox SET send_state = ? WHERE id = ?",
                        (_OUTBOX_LOCAL_ONLY, outbox_id),
                    )
                connection.execute(
                    """
                    INSERT INTO schema_migrations (name)
                    VALUES ('outbox_incompatible_followup_v2')
                    """
                )
    def _recover_interrupted_actuations(self, connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            """
            SELECT id, pending_event, pending_outbox_payload, pending_command_id,
                   pending_command_created_at, activation_attempt_at
            FROM actuation_claims
            WHERE state = 'claimed' AND pending_event IS NOT NULL
            """
        ).fetchall()
        recovered_count = 0
        for (claim_id, event_text, outbox_text, command_id, command_created_at,
             activation_attempt_at) in rows:
            try:
                event = _decode_pending_event(event_text)
                outbox_payload = _decode_optional_json(outbox_text)
                command_ack = (
                    (command_id, _parse_timestamp(command_created_at))
                    if command_id is not None and command_created_at is not None else None
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            recovery_reason = (
                "indeterminate_claim" if activation_attempt_at is not None
                else "interrupted_before_activation"
            )
            recovered = GateEvent(
                source=event.source, reason=recovery_reason, opened=False,
                idempotency_key=event.idempotency_key, received_at=event.received_at,
                decision_at=event.decision_at, relay_activated_at=None,
                authorised_plate=event.authorised_plate,
                observed_plate=event.observed_plate,
                ocr_confidence=event.ocr_confidence,
            )
            try:
                event_id = self._insert_event(connection, recovered)
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id FROM events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if row is None:
                    raise
                event_id = row[0]
            self._ensure_outbox(connection, event_id, outbox_payload)
            self._ensure_command_ack(
                connection, command_ack, "failed", recovery_reason
            )
            connection.execute(
                """
                UPDATE actuation_claims
                SET state = 'finalized', event_id = ?, terminal_status = 'failed',
                    terminal_detail = ?, pending_event = NULL,
                    pending_outbox_payload = NULL, pending_command_id = NULL,
                    pending_command_created_at = NULL
                WHERE id = ? AND state = 'claimed'
                """,
                (event_id, recovery_reason, claim_id),
            )
            recovered_count += 1
        connection.execute(
            """
            UPDATE outbox SET send_state = ?
            WHERE completed_at IS NULL AND send_state = ?
            """,
            (_OUTBOX_READY, _OUTBOX_AWAITING_TELEMETRY),
        )
        return recovered_count

    @staticmethod
    def _legacy_log_exists(connection: sqlite3.Connection) -> bool:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(log)")}
        return {"timestamp", "gate_opened"}.issubset(columns)

    @staticmethod
    def _import_legacy_log(connection: sqlite3.Connection) -> None:
        for timestamp, opened in connection.execute("SELECT timestamp, gate_opened FROM log"):
            connection.execute(
                "INSERT INTO events (received_at, source, reason, opened) VALUES (?, 'legacy', 'legacy_log_import', ?)",
                (_timestamp(_parse_timestamp(timestamp)), int(_legacy_true(opened))),
            )


def _legacy_true(value) -> bool:
    return value if isinstance(value, bool) else (value != 0 if isinstance(value, (int, float)) else isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y", "on"})


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _encode_pending_event(event: GateEvent) -> str:
    return json.dumps({
        "source": event.source,
        "reason": event.reason,
        "idempotency_key": event.idempotency_key,
        "received_at": _timestamp(event.received_at),
        "decision_at": _optional_timestamp(event.decision_at),
        "authorised_plate": event.authorised_plate,
        "observed_plate": event.observed_plate,
        "ocr_confidence": event.ocr_confidence,
    }, sort_keys=True)


def _decode_pending_event(encoded: str) -> GateEvent:
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("pending event is not an object")
    return GateEvent(
        source=payload["source"], reason=payload["reason"], opened=False,
        idempotency_key=payload["idempotency_key"],
        received_at=_parse_timestamp(payload["received_at"]),
        decision_at=(
            _parse_timestamp(payload["decision_at"])
            if payload.get("decision_at") is not None else None
        ),
        authorised_plate=payload.get("authorised_plate"),
        observed_plate=payload.get("observed_plate"),
        ocr_confidence=float(payload.get("ocr_confidence", 0.0)),
    )


def _encode_optional_json(payload: dict | None) -> str | None:
    return json.dumps(payload, sort_keys=True) if payload is not None else None


def _decode_optional_json(encoded: str | None) -> dict | None:
    if encoded is None:
        return None
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("pending payload is not an object")
    return payload


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _encode_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _telemetry_payload(telemetry: EventTelemetry) -> dict:
    raw = telemetry.to_wire()
    if raw.get("schema_version") != 3:
        raise ValueError("telemetry must use schema version 3")
    payload = {
        "trace_id": raw["trace_id"],
        "taxonomy_version": raw["taxonomy_version"],
        "stage_durations": dict(raw["stage_durations"]),
        "frames": [
            {key: frame[key] for key in _TELEMETRY_FRAME_KEYS if key in frame}
            for frame in raw["frames"]
        ],
        "ocr_attempts": [
            {key: attempt[key] for key in _TELEMETRY_OCR_KEYS if key in attempt}
            for attempt in raw["ocr_attempts"]
        ],
        "decision": dict(raw["decision"]),
        "actuation": dict(raw["actuation"]),
        "delivery": dict(raw["delivery"]),
    }
    if "stage_timestamps" in raw:
        payload["stage_timestamps"] = dict(raw["stage_timestamps"])
    return payload


def _same_trace(left: dict, right: dict) -> bool:
    left_immutable = dict(left)
    right_immutable = dict(right)
    left_immutable.pop("delivery", None)
    right_immutable.pop("delivery", None)
    left_durations = dict(left_immutable.get("stage_durations", {}))
    right_durations = dict(right_immutable.get("stage_durations", {}))
    left_durations.pop("delivery_lag_ms", None)
    right_durations.pop("delivery_lag_ms", None)
    left_durations.pop("cloud_send_to_ack_ms", None)
    right_durations.pop("cloud_send_to_ack_ms", None)
    left_immutable["stage_durations"] = left_durations
    right_immutable["stage_durations"] = right_durations
    for candidate in (left_immutable, right_immutable):
        stage_timestamps = dict(candidate.get("stage_timestamps", {}))
        for key in tuple(stage_timestamps):
            if key.startswith("cloud_"):
                stage_timestamps.pop(key)
        if stage_timestamps:
            candidate["stage_timestamps"] = stage_timestamps
        else:
            candidate.pop("stage_timestamps", None)
    return left_immutable == right_immutable


def _event_telemetry_export_row(row: tuple) -> dict:
    keys = (
        "event_id", "received_at", "decision_at", "relay_activated_at", "source",
        "reason", "opened", "authorised_plate", "observed_plate", "ocr_confidence",
        "telemetry_created_at", "telemetry",
    )
    item = dict(zip(keys, row))
    item["opened"] = bool(item["opened"])
    try:
        telemetry = json.loads(item["telemetry"])
    except (TypeError, ValueError):
        telemetry = {}
    item["telemetry"] = telemetry if isinstance(telemetry, dict) else {}
    return item


def _v2_outbox_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("outbox payload must be an object")
    fallback = dict(payload)
    fallback.pop("telemetry", None)
    fallback["schema_version"] = 2
    return fallback


def _is_incompatible_v3_followup(payload: object, send_state: str) -> bool:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 3
        or not isinstance(payload.get("telemetry"), dict)
    ):
        return False
    if payload.get("image_status") == "delivered_before_telemetry":
        return True
    return (
        send_state == _OUTBOX_READY
        and "image_sha256" not in payload
        and "image_status" not in payload
    )


def _decode_telemetry_payload(encoded: object) -> dict:
    try:
        telemetry = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise _MalformedTelemetry("telemetry is not valid JSON") from error
    if not isinstance(telemetry, dict):
        raise _MalformedTelemetry("telemetry is not an object")
    expected = {
        "trace_id": str,
        "taxonomy_version": int,
        "stage_durations": dict,
        "frames": list,
        "ocr_attempts": list,
        "decision": dict,
        "actuation": dict,
        "delivery": dict,
    }
    if any(not isinstance(telemetry.get(key), value_type)
           for key, value_type in expected.items()):
        raise _MalformedTelemetry("telemetry has an invalid bounded contract")
    stage_timestamps = telemetry.get("stage_timestamps")
    if stage_timestamps is not None and (
        not isinstance(stage_timestamps, dict)
        or any(not isinstance(value, str) for value in stage_timestamps.values())
    ):
        raise _MalformedTelemetry("telemetry stage timestamps are invalid")
    return telemetry


def _enrich_outbox_delivery(
    connection: sqlite3.Connection, event_id: int, telemetry: dict
) -> None:
    row = connection.execute(
        "SELECT created_at FROM outbox WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return
    stage_timestamps = dict(telemetry.get("stage_timestamps", {}))
    stage_timestamps.setdefault("cloud_enqueued_at", row[0])
    telemetry["stage_timestamps"] = stage_timestamps


def _bounded_interval_ms(start: object, end: object) -> int | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if parsed_start.tzinfo is None or parsed_end.tzinfo is None:
            return None
        interval = (
            parsed_end.astimezone(timezone.utc)
            - parsed_start.astimezone(timezone.utc)
        ).total_seconds()
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    if interval < 0:
        return None
    return min(round(interval * 1_000), MAX_DURATION_MS)


def _telemetry_attempt(telemetry: dict) -> int:
    try:
        attempt = int(telemetry["delivery"].get("outbox_attempt", 0))
    except (AttributeError, TypeError, ValueError) as error:
        raise _MalformedTelemetry("telemetry delivery attempt is invalid") from error
    return min(max(attempt, 0), MAX_DELIVERY_ATTEMPT)


def _log_telemetry_fallback(reason: str) -> None:
    _LOGGER.warning(
        "outbox_telemetry_enrichment status=fallback_v2 reason=%s", reason
    )


def _optional_timestamp(value: datetime | None) -> str | None:
    return _timestamp(value) if value is not None else None


def _parse_timestamp(value) -> datetime:
    try:
        return _as_utc(value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (AttributeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
